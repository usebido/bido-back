"""What a supplier's payout account must carry to be paid: CNAPS and Chinese names in China."""

import pytest
from pydantic import ValidationError

from bido_back.payments.schemas import CreatePayoutAccountRequest

CHINA: dict[str, str] = {
    "beneficiary_name": "Shenzhen Precision Parts Co., Ltd.",
    "bank_name": "Bank of China",
    "account_number": "6222021234567890",
    "country": "CN",
    "currency": "CNY",
    "bank_code": "104584000003",
    "beneficiary_name_local": "深圳精密零件有限公司",
    "bank_name_local": "中国银行深圳分行",
}


def test_a_cny_account_in_china_with_cnaps_and_chinese_names_is_accepted() -> None:
    account = CreatePayoutAccountRequest.model_validate(CHINA)
    assert (account.beneficiary_type, account.swift_bic, account.national_id) == (
        "business",
        None,
        None,
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"bank_code": None, "swift_bic": "BKCHCNBJ45A"}, "paid through CNAPS"),
        ({"bank_code": "1045"}, "12 digits"),
        ({"beneficiary_name_local": None}, "beneficiary_name_local in Chinese"),
        ({"beneficiary_name_local": "Shenzhen Precision"}, "beneficiary_name_local in Chinese"),
        ({"bank_name_local": None}, "bank_name_local in Chinese"),
        ({"bank_name_local": "Bank of China"}, "bank_name_local in Chinese"),
        ({"beneficiary_type": "individual"}, "national identification number"),
    ],
)
def test_a_cny_account_in_china_missing_what_the_payout_needs_is_refused(
    change: dict[str, str | None], message: str
) -> None:
    body = {k: v for k, v in {**CHINA, **change}.items() if v is not None}
    with pytest.raises(ValidationError, match=message):
        CreatePayoutAccountRequest.model_validate(body)


def test_an_individual_gives_their_national_id() -> None:
    account = CreatePayoutAccountRequest.model_validate(
        {**CHINA, "beneficiary_type": "individual", "national_id": "11010519491231002X"}
    )
    assert account.national_id == "11010519491231002X"


def test_outside_china_or_in_another_currency_the_local_names_are_optional() -> None:
    usd_in_china = {
        k: v
        for k, v in CHINA.items()
        if k not in {"bank_code", "beneficiary_name_local", "bank_name_local"}
    }
    CreatePayoutAccountRequest.model_validate(
        {**usd_in_china, "currency": "USD", "swift_bic": "BKCHCNBJ45A"}
    )
    CreatePayoutAccountRequest.model_validate(
        {**usd_in_china, "country": "DE", "currency": "EUR", "swift_bic": "DEUTDEFF"}
    )
