"""A supplier's bank details, sealed for Bido's own database (`supplier_payout_accounts`).

The payout provider (Tazapay, a next step) is who pays the supplier's account in its
currency, so the details end up there, as a beneficiary; until that integration exists
they wait here, encrypted. Only the payout step opens them. Bido keeps, in clear, only
a masked view and the account's fingerprint.

Two schemes, told apart by the first byte (the same layouts as the mailbox tokens,
`bido_back.mailbox.crypto`, with their own keys):

* `0x01` Fernet (local and test): `PAYOUT_DETAILS_ENCRYPTION_KEY`. Fernet has no
  associated data, so the context is sealed inside and compared on open.
* `0x02` KMS envelope (AWS): a fresh AES-256 data key per seal from KMS
  `GenerateDataKey` (`PAYOUT_DETAILS_KMS_KEY_ID`), the details sealed with AES-GCM:

      0x02 | len(encrypted data key) (2 bytes, big endian) | encrypted data key
           | nonce (12 bytes) | AES-GCM ciphertext + tag

  The context `{tenant_id, supplier_id, purpose}` is the KMS encryption context (so
  CloudTrail records whose details were opened) and the AES-GCM associated data.

Either way a sealed value copied onto another tenant's or supplier's row does not open.
With neither key set, payments are off (`payments.config`); in `prd` only KMS is
accepted. Nothing here logs the details, keys or ciphertext.
"""

import asyncio
import base64
import json
import os
from collections.abc import Mapping
from dataclasses import asdict
from typing import Final, Protocol, cast
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, ValidationError

from bido_back.payments.errors import PaymentError, PaymentsNotConfigured
from bido_back.payments.ports import BankAccountDetails, BankDetailsCipher
from bido_back.settings import Settings

FERNET_SCHEME: Final = b"\x01"
KMS_SCHEME: Final = b"\x02"
PURPOSE: Final = "supplier_bank_details"
_NONCE_BYTES: Final = 12


class BankDetailsUnreadable(PaymentError):
    """The sealed details are corrupt, from another row, or for a key this server lacks."""


def _context(tenant_id: UUID, supplier_id: UUID) -> dict[str, str]:
    return {"tenant_id": str(tenant_id), "supplier_id": str(supplier_id), "purpose": PURPOSE}


def _aad(context: Mapping[str, str]) -> bytes:
    return json.dumps(dict(context), sort_keys=True, separators=(",", ":")).encode()


class _Details(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    beneficiary_name: str
    bank_name: str
    account_number: str
    country: str
    currency: str
    swift_bic: str | None
    bank_code: str | None
    bank_address: str | None
    beneficiary_address: str | None


def _to_bytes(details: BankAccountDetails) -> bytes:
    return json.dumps(asdict(details), sort_keys=True, separators=(",", ":")).encode()


def _from_bytes(plaintext: bytes) -> BankAccountDetails:
    try:
        parsed = _Details.model_validate_json(plaintext)
    except ValidationError:
        raise BankDetailsUnreadable("the sealed bank details are malformed") from None
    return BankAccountDetails(**parsed.model_dump())


class FernetBankDetailsCipher:
    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode())
        except (ValueError, TypeError):
            raise PaymentsNotConfigured(
                "PAYOUT_DETAILS_ENCRYPTION_KEY is not a valid Fernet key"
            ) from None

    async def seal(
        self, details: BankAccountDetails, *, tenant_id: UUID, supplier_id: UUID
    ) -> bytes:
        sealed = json.dumps(
            {
                "c": _context(tenant_id, supplier_id),
                "d": base64.b64encode(_to_bytes(details)).decode(),
            },
            separators=(",", ":"),
        ).encode()
        return FERNET_SCHEME + self._fernet.encrypt(sealed)

    async def open(
        self, sealed: bytes, *, tenant_id: UUID, supplier_id: UUID
    ) -> BankAccountDetails:
        if not sealed.startswith(FERNET_SCHEME):
            raise BankDetailsUnreadable("not Fernet-sealed bank details")
        try:
            inner: object = json.loads(self._fernet.decrypt(sealed[1:]))
        except (InvalidToken, ValueError):
            raise BankDetailsUnreadable("the bank details do not open") from None
        if not isinstance(inner, dict):
            raise BankDetailsUnreadable("the sealed bank details have an unexpected shape")
        obj = cast(dict[str, object], inner)
        data = obj.get("d")
        if obj.get("c") != _context(tenant_id, supplier_id) or not isinstance(data, str):
            raise BankDetailsUnreadable("the bank details belong to another supplier")
        return _from_bytes(base64.b64decode(data))


class KmsApi(Protocol):
    """The slice of the boto3 KMS client used here (boto3 ships no type stubs)."""

    def generate_data_key(self, **kwargs: object) -> Mapping[str, object]: ...

    def decrypt(self, **kwargs: object) -> Mapping[str, object]: ...


class KmsBankDetailsCipher:
    def __init__(self, key_id: str, kms: KmsApi) -> None:
        self._key_id = key_id
        self._kms = kms

    async def seal(
        self, details: BankAccountDetails, *, tenant_id: UUID, supplier_id: UUID
    ) -> bytes:
        context = _context(tenant_id, supplier_id)
        response = await asyncio.to_thread(
            self._kms.generate_data_key,
            KeyId=self._key_id,
            KeySpec="AES_256",
            EncryptionContext=context,
        )
        data_key = response.get("Plaintext")
        encrypted_key = response.get("CiphertextBlob")
        if not isinstance(data_key, bytes) or not isinstance(encrypted_key, bytes):
            raise PaymentsNotConfigured("KMS GenerateDataKey returned no data key")
        if len(encrypted_key) > 0xFFFF:
            raise PaymentsNotConfigured("KMS data key is unexpectedly large")
        nonce = os.urandom(_NONCE_BYTES)
        sealed = AESGCM(data_key).encrypt(nonce, _to_bytes(details), _aad(context))
        return KMS_SCHEME + len(encrypted_key).to_bytes(2, "big") + encrypted_key + nonce + sealed

    async def open(
        self, sealed: bytes, *, tenant_id: UUID, supplier_id: UUID
    ) -> BankAccountDetails:
        if not sealed.startswith(KMS_SCHEME) or len(sealed) < 3:
            raise BankDetailsUnreadable("not KMS-sealed bank details")
        context = _context(tenant_id, supplier_id)
        key_len = int.from_bytes(sealed[1:3], "big")
        encrypted_key = sealed[3 : 3 + key_len]
        rest = sealed[3 + key_len :]
        if len(encrypted_key) != key_len or len(rest) <= _NONCE_BYTES:
            raise BankDetailsUnreadable("the sealed bank details are truncated")
        try:
            response = await asyncio.to_thread(
                self._kms.decrypt,
                KeyId=self._key_id,
                CiphertextBlob=encrypted_key,
                EncryptionContext=context,
            )
        except Exception:  # noqa: BLE001 - botocore raises dynamic ClientError types
            raise BankDetailsUnreadable("KMS refused to open the data key") from None
        data_key = response.get("Plaintext")
        if not isinstance(data_key, bytes):
            raise BankDetailsUnreadable("KMS returned no data key")
        try:
            plaintext = AESGCM(data_key).decrypt(
                rest[:_NONCE_BYTES], rest[_NONCE_BYTES:], _aad(context)
            )
        except Exception:  # noqa: BLE001 - cryptography's InvalidTag
            raise BankDetailsUnreadable("the bank details do not open") from None
        return _from_bytes(plaintext)


class _SchemeDispatch:
    """Seals with the primary cipher; opens with whichever matches the scheme byte."""

    def __init__(
        self, primary: BankDetailsCipher, by_scheme: Mapping[bytes, BankDetailsCipher]
    ) -> None:
        self._primary = primary
        self._by_scheme = dict(by_scheme)

    async def seal(
        self, details: BankAccountDetails, *, tenant_id: UUID, supplier_id: UUID
    ) -> bytes:
        return await self._primary.seal(details, tenant_id=tenant_id, supplier_id=supplier_id)

    async def open(
        self, sealed: bytes, *, tenant_id: UUID, supplier_id: UUID
    ) -> BankAccountDetails:
        cipher = self._by_scheme.get(sealed[:1])
        if cipher is None:
            raise BankDetailsUnreadable("no key configured for these sealed bank details")
        return await cipher.open(sealed, tenant_id=tenant_id, supplier_id=supplier_id)


def bank_details_configured(settings: Settings) -> bool:
    return bool(settings.payout_details_kms_key_id or settings.payout_details_encryption_key)


def build_bank_details_cipher(
    settings: Settings, *, kms: KmsApi | None = None
) -> BankDetailsCipher:
    """The configured cipher; `PaymentsNotConfigured` when the details cannot be protected."""
    fernet = (
        FernetBankDetailsCipher(settings.payout_details_encryption_key)
        if settings.payout_details_encryption_key
        else None
    )
    key_id = settings.payout_details_kms_key_id
    if key_id:
        kms_cipher = KmsBankDetailsCipher(key_id, kms or _boto3_kms(settings.aws_region))
        by_scheme: dict[bytes, BankDetailsCipher] = {KMS_SCHEME: kms_cipher}
        if fernet is not None:
            by_scheme[FERNET_SCHEME] = fernet
        return _SchemeDispatch(kms_cipher, by_scheme)
    if fernet is None:
        raise PaymentsNotConfigured(
            "bank details encryption is not configured "
            "(set PAYOUT_DETAILS_KMS_KEY_ID or PAYOUT_DETAILS_ENCRYPTION_KEY)"
        )
    if settings.environment == "prd":
        raise PaymentsNotConfigured(
            "production seals bank details only under KMS (PAYOUT_DETAILS_KMS_KEY_ID)"
        )
    return _SchemeDispatch(fernet, {FERNET_SCHEME: fernet})


def _boto3_kms(region: str) -> KmsApi:
    import boto3  # pyright: ignore[reportMissingTypeStubs]

    return cast(KmsApi, boto3.client("kms", region_name=region))  # pyright: ignore[reportUnknownMemberType]
