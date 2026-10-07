"""Encryption of stored OAuth tokens (`mailbox_connections.token_ciphertext`).

Two schemes, told apart by the first byte of the ciphertext:

* `0x01` Fernet (local and test): `TOKEN_ENCRYPTION_KEY` is a Fernet key.
  Fernet has no associated data, so the encryption context is sealed inside
  the plaintext and compared on decrypt.
* `0x02` KMS envelope encryption (AWS): a fresh AES-256 data key per write
  from KMS `GenerateDataKey` (key `OAUTH_TOKEN_KMS_KEY_ID`), the tokens sealed with
  AES-GCM under it, and the KMS-encrypted data key stored next to them:

      0x02 | len(encrypted data key) (2 bytes, big endian) | encrypted data key
           | nonce (12 bytes) | AES-GCM ciphertext + tag

  The encryption context `{tenant_id, user_id, provider}` is passed to KMS
  (so CloudTrail records whose tokens were decrypted, and a key policy can
  condition on it) and is the AES-GCM associated data.

Either way a ciphertext copied onto another tenant's, user's or provider's
row fails to decrypt. With neither `OAUTH_TOKEN_KMS_KEY_ID` nor `TOKEN_ENCRYPTION_KEY`
set, tokens are refused, never stored in clear; in `prd` the Fernet path is
refused too (KMS only). When both are set, new writes use KMS and Fernet
rows stay readable.

Nothing here logs plaintext, keys or ciphertext.
"""

import asyncio
import base64
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from bido_back.mailbox.base import MailboxNotConfigured, Provider, TokenSet
from bido_back.settings import Settings

FERNET_SCHEME = b"\x01"
KMS_SCHEME = b"\x02"
_NONCE_BYTES = 12


class TokenDecryptionError(Exception):
    """The ciphertext is corrupt, from another row, or for a key we do not have."""


@dataclass(frozen=True, slots=True)
class TokenContext:
    tenant_id: UUID
    user_id: UUID
    provider: Provider

    def as_dict(self) -> dict[str, str]:
        return {
            "tenant_id": str(self.tenant_id),
            "user_id": str(self.user_id),
            "provider": self.provider,
        }

    def aad(self) -> bytes:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":")).encode()


class TokenCipher(Protocol):
    async def encrypt(self, plaintext: bytes, context: TokenContext) -> bytes: ...

    async def decrypt(self, ciphertext: bytes, context: TokenContext) -> bytes: ...


class FernetTokenCipher:
    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode())
        except (ValueError, TypeError):
            raise MailboxNotConfigured("TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from None

    async def encrypt(self, plaintext: bytes, context: TokenContext) -> bytes:
        sealed = json.dumps(
            {"c": context.as_dict(), "d": base64.b64encode(plaintext).decode()},
            separators=(",", ":"),
        ).encode()
        return FERNET_SCHEME + self._fernet.encrypt(sealed)

    async def decrypt(self, ciphertext: bytes, context: TokenContext) -> bytes:
        if not ciphertext.startswith(FERNET_SCHEME):
            raise TokenDecryptionError("not a Fernet token ciphertext")
        try:
            sealed: object = json.loads(self._fernet.decrypt(ciphertext[1:]))
        except (InvalidToken, ValueError):
            raise TokenDecryptionError("token ciphertext does not decrypt") from None
        if not isinstance(sealed, dict):
            raise TokenDecryptionError("token ciphertext has an unexpected shape")
        obj = cast(dict[str, object], sealed)
        data = obj.get("d")
        if obj.get("c") != context.as_dict() or not isinstance(data, str):
            raise TokenDecryptionError("token ciphertext belongs to another connection")
        return base64.b64decode(data)


class KmsApi(Protocol):
    """The slice of the boto3 KMS client used here (boto3 ships no type stubs)."""

    def generate_data_key(self, **kwargs: object) -> Mapping[str, object]: ...

    def decrypt(self, **kwargs: object) -> Mapping[str, object]: ...


class KmsTokenCipher:
    def __init__(self, key_id: str, kms: KmsApi) -> None:
        self._key_id = key_id
        self._kms = kms

    async def encrypt(self, plaintext: bytes, context: TokenContext) -> bytes:
        response = await asyncio.to_thread(
            self._kms.generate_data_key,
            KeyId=self._key_id,
            KeySpec="AES_256",
            EncryptionContext=context.as_dict(),
        )
        data_key = response.get("Plaintext")
        encrypted_key = response.get("CiphertextBlob")
        if not isinstance(data_key, bytes) or not isinstance(encrypted_key, bytes):
            raise MailboxNotConfigured("KMS GenerateDataKey returned no data key")
        if len(encrypted_key) > 0xFFFF:
            raise MailboxNotConfigured("KMS data key is unexpectedly large")
        nonce = os.urandom(_NONCE_BYTES)
        sealed = AESGCM(data_key).encrypt(nonce, plaintext, context.aad())
        return KMS_SCHEME + len(encrypted_key).to_bytes(2, "big") + encrypted_key + nonce + sealed

    async def decrypt(self, ciphertext: bytes, context: TokenContext) -> bytes:
        if not ciphertext.startswith(KMS_SCHEME) or len(ciphertext) < 3:
            raise TokenDecryptionError("not a KMS token ciphertext")
        key_len = int.from_bytes(ciphertext[1:3], "big")
        encrypted_key = ciphertext[3 : 3 + key_len]
        rest = ciphertext[3 + key_len :]
        if len(encrypted_key) != key_len or len(rest) <= _NONCE_BYTES:
            raise TokenDecryptionError("token ciphertext is truncated")
        try:
            response = await asyncio.to_thread(
                self._kms.decrypt,
                KeyId=self._key_id,
                CiphertextBlob=encrypted_key,
                EncryptionContext=context.as_dict(),
            )
        except Exception:  # noqa: BLE001 - botocore raises dynamic ClientError types
            raise TokenDecryptionError("KMS refused to decrypt the data key") from None
        data_key = response.get("Plaintext")
        if not isinstance(data_key, bytes):
            raise TokenDecryptionError("KMS returned no data key")
        try:
            return AESGCM(data_key).decrypt(rest[:_NONCE_BYTES], rest[_NONCE_BYTES:], context.aad())
        except Exception:  # noqa: BLE001 - cryptography's InvalidTag
            raise TokenDecryptionError("token ciphertext does not decrypt") from None


class SchemeDispatchCipher:
    """Encrypts with the primary cipher; decrypts with whichever matches the scheme byte."""

    def __init__(self, primary: TokenCipher, by_scheme: Mapping[bytes, TokenCipher]) -> None:
        self._primary = primary
        self._by_scheme = dict(by_scheme)

    async def encrypt(self, plaintext: bytes, context: TokenContext) -> bytes:
        return await self._primary.encrypt(plaintext, context)

    async def decrypt(self, ciphertext: bytes, context: TokenContext) -> bytes:
        cipher = self._by_scheme.get(ciphertext[:1])
        if cipher is None:
            raise TokenDecryptionError("no key configured for this token ciphertext")
        return await cipher.decrypt(ciphertext, context)


def kms_key_id(settings: Settings) -> str | None:
    """The KMS key for token encryption: the ONE place that names the setting."""
    return settings.oauth_token_kms_key_id


def build_token_cipher(settings: Settings, *, kms: KmsApi | None = None) -> TokenCipher:
    """The configured cipher. Raises MailboxNotConfigured when tokens cannot be protected."""
    fernet = (
        FernetTokenCipher(settings.token_encryption_key) if settings.token_encryption_key else None
    )
    key_id = kms_key_id(settings)
    if key_id:
        kms_cipher = KmsTokenCipher(key_id, kms or _boto3_kms(settings.aws_region))
        by_scheme: dict[bytes, TokenCipher] = {KMS_SCHEME: kms_cipher}
        if fernet is not None:
            by_scheme[FERNET_SCHEME] = fernet
        return SchemeDispatchCipher(kms_cipher, by_scheme)
    if fernet is None:
        raise MailboxNotConfigured(
            "token encryption is not configured "
            "(set OAUTH_TOKEN_KMS_KEY_ID or TOKEN_ENCRYPTION_KEY)"
        )
    if settings.environment == "prd":
        raise MailboxNotConfigured(
            "production stores mailbox tokens only under KMS (OAUTH_TOKEN_KMS_KEY_ID)"
        )
    return SchemeDispatchCipher(fernet, {FERNET_SCHEME: fernet})


async def encrypt_tokens(cipher: TokenCipher, tokens: TokenSet, context: TokenContext) -> bytes:
    return await cipher.encrypt(tokens.to_json(), context)


async def decrypt_tokens(cipher: TokenCipher, ciphertext: bytes, context: TokenContext) -> TokenSet:
    plaintext = await cipher.decrypt(ciphertext, context)
    try:
        return TokenSet.from_json(plaintext)
    except ValueError:
        raise TokenDecryptionError("decrypted token set is malformed") from None


def _boto3_kms(region: str) -> KmsApi:
    import boto3  # pyright: ignore[reportMissingTypeStubs]

    return cast(KmsApi, boto3.client("kms", region_name=region))  # pyright: ignore[reportUnknownMemberType]
