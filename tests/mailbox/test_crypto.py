"""Token encryption: Fernet locally, KMS envelope encryption in AWS."""

# ruff: noqa: S105, S106 - fake OAuth tokens and keys, never real credentials

import os
from collections.abc import Mapping
from uuid import uuid4

import pytest

from bido_back.mailbox.base import MailboxNotConfigured, TokenSet
from bido_back.mailbox.crypto import (
    FERNET_SCHEME,
    KMS_SCHEME,
    FernetTokenCipher,
    KmsTokenCipher,
    TokenContext,
    TokenDecryptionError,
    build_token_cipher,
    decrypt_tokens,
    encrypt_tokens,
)

from .support import FERNET_KEY, make_settings

TOKENS = TokenSet(
    access_token="ya29.secret-access",
    refresh_token="1//secret-refresh",
    expires_at=1_900_000_000,
    scopes=("https://www.googleapis.com/auth/gmail.send",),
)


def _context() -> TokenContext:
    return TokenContext(tenant_id=uuid4(), user_id=uuid4(), provider="google")


class FakeKms:
    """Wraps data keys with a fixed XOR 'master key' and checks the encryption context."""

    def __init__(self) -> None:
        self.contexts: list[object] = []
        self.refuse = False

    def generate_data_key(self, **kwargs: object) -> Mapping[str, object]:
        self.contexts.append(kwargs["EncryptionContext"])
        assert kwargs["KeySpec"] == "AES_256"
        plaintext = os.urandom(32)
        return {"Plaintext": plaintext, "CiphertextBlob": self._wrap(plaintext, kwargs)}

    def decrypt(self, **kwargs: object) -> Mapping[str, object]:
        if self.refuse:
            raise RuntimeError("AccessDenied")
        blob = kwargs["CiphertextBlob"]
        assert isinstance(blob, bytes)
        context, _, wrapped = blob.partition(b"|")
        if context != repr(kwargs["EncryptionContext"]).encode():
            raise RuntimeError("InvalidCiphertextException")
        return {"Plaintext": bytes(b ^ 0x5A for b in wrapped)}

    def _wrap(self, plaintext: bytes, kwargs: Mapping[str, object]) -> bytes:
        return (
            repr(kwargs["EncryptionContext"]).encode() + b"|" + bytes(b ^ 0x5A for b in plaintext)
        )


async def test_fernet_round_trip_and_no_plaintext() -> None:
    cipher = FernetTokenCipher(FERNET_KEY)
    context = _context()
    ciphertext = await encrypt_tokens(cipher, TOKENS, context)

    assert ciphertext.startswith(FERNET_SCHEME)
    assert b"secret" not in ciphertext
    assert await decrypt_tokens(cipher, ciphertext, context) == TOKENS


async def test_fernet_refuses_another_rows_context() -> None:
    cipher = FernetTokenCipher(FERNET_KEY)
    context = _context()
    ciphertext = await encrypt_tokens(cipher, TOKENS, context)
    other = TokenContext(tenant_id=uuid4(), user_id=context.user_id, provider="google")

    with pytest.raises(TokenDecryptionError):
        await decrypt_tokens(cipher, ciphertext, other)


async def test_fernet_refuses_tampering() -> None:
    cipher = FernetTokenCipher(FERNET_KEY)
    context = _context()
    ciphertext = bytearray(await encrypt_tokens(cipher, TOKENS, context))
    ciphertext[20] ^= 1

    with pytest.raises(TokenDecryptionError):
        await decrypt_tokens(cipher, bytes(ciphertext), context)


async def test_kms_envelope_round_trip_with_context() -> None:
    kms = FakeKms()
    cipher = KmsTokenCipher("alias/bido-tokens", kms)
    context = _context()
    ciphertext = await encrypt_tokens(cipher, TOKENS, context)

    assert ciphertext.startswith(KMS_SCHEME)
    assert b"secret" not in ciphertext
    assert kms.contexts == [context.as_dict()]
    assert await decrypt_tokens(cipher, ciphertext, context) == TOKENS


async def test_kms_refuses_another_rows_context() -> None:
    cipher = KmsTokenCipher("alias/bido-tokens", FakeKms())
    context = _context()
    ciphertext = await encrypt_tokens(cipher, TOKENS, context)
    other = TokenContext(tenant_id=context.tenant_id, user_id=context.user_id, provider="microsoft")

    with pytest.raises(TokenDecryptionError):
        await decrypt_tokens(cipher, ciphertext, other)


async def test_kms_refuses_truncated_ciphertext() -> None:
    cipher = KmsTokenCipher("alias/bido-tokens", FakeKms())
    context = _context()
    ciphertext = await encrypt_tokens(cipher, TOKENS, context)

    with pytest.raises(TokenDecryptionError):
        await decrypt_tokens(cipher, ciphertext[:10], context)


def test_refuses_to_store_tokens_without_a_key() -> None:
    with pytest.raises(MailboxNotConfigured):
        build_token_cipher(make_settings(token_encryption_key=None, kms_key_id=None))


def test_production_requires_kms() -> None:
    with pytest.raises(MailboxNotConfigured):
        build_token_cipher(make_settings(environment="prd", kms_key_id=None))


def test_invalid_fernet_key_is_a_configuration_error() -> None:
    with pytest.raises(MailboxNotConfigured):
        build_token_cipher(make_settings(token_encryption_key="not-a-key"))


async def test_kms_is_primary_and_fernet_rows_stay_readable() -> None:
    context = _context()
    legacy = await encrypt_tokens(FernetTokenCipher(FERNET_KEY), TOKENS, context)
    cipher = build_token_cipher(make_settings(kms_key_id="alias/bido-tokens"), kms=FakeKms())

    fresh = await encrypt_tokens(cipher, TOKENS, context)

    assert fresh.startswith(KMS_SCHEME)
    assert await decrypt_tokens(cipher, fresh, context) == TOKENS
    assert await decrypt_tokens(cipher, legacy, context) == TOKENS


def test_token_repr_hides_secrets() -> None:
    assert "secret" not in repr(TOKENS)
