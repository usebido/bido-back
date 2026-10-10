"""The card amount of a purchase order: ECB reference rates to USD, rounding, the minimum.

Pure functions: no database. The quote's database read (`quote_charge`) runs in the
service tests.
"""

from datetime import date
from decimal import Decimal

import pytest

from bido_back.comparison.compute import Rate
from bido_back.payments.errors import PaymentRefused
from bido_back.payments.quote import U64_MAX, ChargeQuote, charge_quote, token_base_units

DAY = date(2026, 10, 8)
EARLIER = date(2026, 10, 7)


def rate(currency: str, per_eur: str, day: date = DAY) -> Rate:
    return Rate(currency=currency, per_eur=Decimal(per_eur), rate_date=day)


def test_a_usd_order_is_charged_its_total_without_a_rate() -> None:
    assert charge_quote(po_currency="USD", po_total=Decimal("1650.00"), rates={}) == ChargeQuote(
        amount=Decimal("1650.00"), fx_rate=None, fx_rate_date=None
    )


def test_a_eur_order_is_charged_at_the_usd_reference_rate() -> None:
    quote = charge_quote(
        po_currency="EUR", po_total=Decimal("1650.00"), rates={"USD": rate("USD", "1.10")}
    )
    assert (quote.currency, quote.amount, quote.fx_rate, quote.fx_rate_date) == (
        "USD",
        Decimal("1815.00"),
        Decimal("1.10000000"),
        DAY,
    )


def test_a_cross_rate_is_rounded_as_shown_and_the_charge_follows_the_shown_rate() -> None:
    # CNY -> USD through the EUR rates: 1.1 / 7.8 = 0.1410256410..., shown as 0.14102564.
    rates = {"CNY": rate("CNY", "7.8", EARLIER), "USD": rate("USD", "1.1")}
    quote = charge_quote(po_currency="CNY", po_total=Decimal("99999999.99"), rates=rates)
    assert quote.fx_rate == Decimal("0.14102564")
    # The total times the shown rate (the unrounded rate would give 14102564.10).
    assert quote.amount == Decimal("14102564.00")
    # The older of the two publications the cross rate rests on.
    assert quote.fx_rate_date == EARLIER


def test_the_charge_rounds_half_up_to_the_cent() -> None:
    quote = charge_quote(
        po_currency="EUR", po_total=Decimal("10.00"), rates={"USD": rate("USD", "1.0005")}
    )
    assert quote.amount == Decimal("10.01")  # 10.005, not banker's 10.00


def test_without_a_rate_to_usd_nothing_is_quoted() -> None:
    with pytest.raises(PaymentRefused, match="no exchange rate from CNY to USD"):
        charge_quote(
            po_currency="CNY", po_total=Decimal("100.00"), rates={"USD": rate("USD", "1.1")}
        )
    with pytest.raises(PaymentRefused, match="no exchange rate from EUR to USD"):
        charge_quote(po_currency="EUR", po_total=Decimal("100.00"), rates={})


def test_a_charge_below_the_card_minimum_is_refused_and_the_minimum_itself_is_fine() -> None:
    with pytest.raises(PaymentRefused, match="at least USD 0.50"):
        charge_quote(po_currency="USD", po_total=Decimal("0.49"), rates={})
    # 0.45 EUR x 1.10 = 0.495 -> 0.50: exactly the minimum.
    quote = charge_quote(
        po_currency="EUR", po_total=Decimal("0.45"), rates={"USD": rate("USD", "1.10")}
    )
    assert quote.amount == Decimal("0.50")


def test_token_amounts_are_the_charge_in_base_units_exactly() -> None:
    assert token_base_units(Decimal("1815.00"), 6) == 1_815_000_000
    assert token_base_units(Decimal("0.50"), 6) == 500_000
    assert token_base_units(Decimal("18446744073709.551615"), 6) == U64_MAX


@pytest.mark.parametrize(
    ("amount", "decimals"),
    [
        (Decimal("1.25"), 1),  # more decimals than the token
        (Decimal("0"), 6),
        (Decimal("-1.00"), 6),
        (Decimal("18446744073709.551616"), 6),  # u64 + 1
    ],
)
def test_token_amounts_that_cannot_be_minted_are_refused(amount: Decimal, decimals: int) -> None:
    with pytest.raises(ValueError):
        token_base_units(amount, decimals)
