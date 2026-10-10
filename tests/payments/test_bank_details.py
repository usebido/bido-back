"""Sealing a supplier's bank details: Fernet locally, a KMS envelope in AWS, bound to the row."""

# ruff: noqa: S105, S106 - fake keys, never real credentials

import base64
import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import cast
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet

from bido_back.payments.bank_details import (
    FERNET_SCHEME,
    KMS_SCHEME,
    PURPOSE,
    BankDetailsUnreadable,
    FernetBankDetailsCipher,
    KmsBankDetailsCipher,
    build_bank_details_cipher,
)
from bido_back.payments.config import missing_payment_settings
from bido_back.payments.errors import PaymentsNotConfigured
from bido_back.payments.ports import BankAccountDetails
from bido_back.settings import Settings

TENANT, SUPPLIER = uuid4(), uuid4()
DETAILS = BankAccountDetails(
    beneficiary_name="Ningbo Precision Tooling Co., Ltd.",
    bank_name="Bank of China",
    account_number="6217850000001234567",
    country="CN",
    currency="CNY",
    swift_bic="BKCHCNBJ92A",
    bank_code="104332000017",
    bank_address=None,
    beneficiary_address="Ningbo, Zhejiang",
    beneficiary_type="business",
    beneficiary_name_local="宁波精密工具有限公司",
    bank_name_local="中国银行宁波分行",
    national_id=None,
)
CLEAR = (
    b"6217850000001234567",
    b"Ningbo Precision",
    b"104332000017",
    "宁波精密".encode(),
    b"\\u5b81",  # the same, as JSON escapes it
)


def settings(**values: object) -> Settings:
    return Settings.model_validate(
        {"environment": "test", "database_url": "postgresql+asyncpg://app@localhost/x", **values}
    )


@dataclass
class FakeKms:
    """Wraps data keys in memory; refuses another encryption context, like KMS does."""

    wrapped: dict[bytes, tuple[bytes, dict[str, str]]] = field(
        default_factory=dict[bytes, tuple[bytes, dict[str, str]]]
    )

    def generate_data_key(self, **kwargs: object) -> Mapping[str, object]:
        assert kwargs["KeySpec"] == "AES_256"
        context = cast(dict[str, str], kwargs["EncryptionContext"])
        data_key, blob = os.urandom(32), b"wrapped:" + uuid4().bytes
        self.wrapped[blob] = (data_key, dict(context))
        return {"Plaintext": data_key, "CiphertextBlob": blob}

    def decrypt(self, **kwargs: object) -> Mapping[str, object]:
        blob = kwargs["CiphertextBlob"]
        assert isinstance(blob, bytes)
        data_key, context = self.wrapped[blob]
        if kwargs["EncryptionContext"] != context:
            raise RuntimeError("InvalidCiphertextException")
        return {"Plaintext": data_key}


async def roundtrip_and_binding(sealer: FernetBankDetailsCipher | KmsBankDetailsCipher) -> bytes:
    sealed = await sealer.seal(DETAILS, tenant_id=TENANT, supplier_id=SUPPLIER)
    assert not any(clear in sealed for clear in CLEAR)
    assert await sealer.open(sealed, tenant_id=TENANT, supplier_id=SUPPLIER) == DETAILS
    for tenant, supplier in ((TENANT, uuid4()), (uuid4(), SUPPLIER)):
        with pytest.raises(BankDetailsUnreadable):
            await sealer.open(sealed, tenant_id=tenant, supplier_id=supplier)
    return sealed


async def test_fernet_seals_and_opens_only_for_the_same_row() -> None:
    sealed = await roundtrip_and_binding(FernetBankDetailsCipher(Fernet.generate_key().decode()))
    assert sealed.startswith(FERNET_SCHEME)


async def test_kms_seals_under_a_fresh_data_key_bound_to_the_row() -> None:
    kms = FakeKms()
    sealed = await roundtrip_and_binding(KmsBankDetailsCipher("alias/payout-details", kms))
    assert sealed.startswith(KMS_SCHEME)
    [(_, context)] = kms.wrapped.values()
    assert context == {
        "tenant_id": str(TENANT),
        "supplier_id": str(SUPPLIER),
        "purpose": "supplier_bank_details",
    }


async def test_tampered_or_foreign_ciphertext_does_not_open() -> None:
    fernet = FernetBankDetailsCipher(Fernet.generate_key().decode())
    sealed = await fernet.seal(DETAILS, tenant_id=TENANT, supplier_id=SUPPLIER)
    # A byte mid-token: the last base64 characters may carry only padding bits.
    middle = len(sealed) // 2
    tampered = sealed[:middle] + bytes([sealed[middle] ^ 1]) + sealed[middle + 1 :]
    with pytest.raises(BankDetailsUnreadable):
        await fernet.open(tampered, tenant_id=TENANT, supplier_id=SUPPLIER)
    other_key = FernetBankDetailsCipher(Fernet.generate_key().decode())
    with pytest.raises(BankDetailsUnreadable):
        await other_key.open(sealed, tenant_id=TENANT, supplier_id=SUPPLIER)
    kms = KmsBankDetailsCipher("alias/payout-details", FakeKms())
    with pytest.raises(BankDetailsUnreadable):
        await kms.open(sealed, tenant_id=TENANT, supplier_id=SUPPLIER)


async def test_details_sealed_before_the_local_names_open_as_a_company() -> None:
    key = Fernet.generate_key().decode()
    earlier = {
        name: value
        for name, value in asdict(DETAILS).items()
        if name
        not in {"beneficiary_type", "beneficiary_name_local", "bank_name_local", "national_id"}
    }
    inner = {
        "c": {"tenant_id": str(TENANT), "supplier_id": str(SUPPLIER), "purpose": PURPOSE},
        "d": base64.b64encode(json.dumps(earlier).encode()).decode(),
    }
    sealed = FERNET_SCHEME + Fernet(key.encode()).encrypt(json.dumps(inner).encode())
    opened = await FernetBankDetailsCipher(key).open(sealed, tenant_id=TENANT, supplier_id=SUPPLIER)
    assert opened == replace(
        DETAILS, beneficiary_type="business", beneficiary_name_local=None, bank_name_local=None
    )


async def test_the_builder_prefers_kms_and_still_opens_fernet_rows() -> None:
    key = Fernet.generate_key().decode()
    local = build_bank_details_cipher(settings(payout_details_encryption_key=key))
    old = await local.seal(DETAILS, tenant_id=TENANT, supplier_id=SUPPLIER)
    both = build_bank_details_cipher(
        settings(payout_details_encryption_key=key, payout_details_kms_key_id="alias/x"),
        kms=FakeKms(),
    )
    assert (await both.seal(DETAILS, tenant_id=TENANT, supplier_id=SUPPLIER)).startswith(KMS_SCHEME)
    assert await both.open(old, tenant_id=TENANT, supplier_id=SUPPLIER) == DETAILS


def test_the_builder_refuses_to_store_details_unprotected() -> None:
    with pytest.raises(PaymentsNotConfigured, match="PAYOUT_DETAILS_KMS_KEY_ID"):
        build_bank_details_cipher(settings())
    with pytest.raises(PaymentsNotConfigured, match="Fernet"):
        build_bank_details_cipher(settings(payout_details_encryption_key="not-a-key"))
    fernet_only_prd = settings(
        environment="prd",
        payout_details_encryption_key=Fernet.generate_key().decode(),
        cors_allowed_origins=["https://app.usebido.com"],
    )
    with pytest.raises(PaymentsNotConfigured, match="only under KMS"):
        build_bank_details_cipher(fernet_only_prd)


def test_payments_need_a_key_for_bank_details() -> None:
    need = "PAYOUT_DETAILS_KMS_KEY_ID or PAYOUT_DETAILS_ENCRYPTION_KEY"
    assert need in missing_payment_settings(settings())
    key = Fernet.generate_key().decode()
    assert need not in missing_payment_settings(settings(payout_details_encryption_key=key))
    assert need not in missing_payment_settings(settings(payout_details_kms_key_id="alias/x"))


def test_settings_never_show_the_key() -> None:
    key = Fernet.generate_key().decode()
    assert key not in repr(settings(payout_details_encryption_key=key))
