"""Lead times and validity periods to dates, with the supplier's holiday calendar."""

from collections.abc import Mapping
from datetime import date
from decimal import Decimal

import pytest

from bido_back.quotes.lead_time import (
    LeadTime,
    PeriodUnit,
    add_period,
    country_for_currency,
    decode_lead_time,
    decode_validity,
    encode_lead_time,
    expected_delivery,
    has_holiday_calendar,
    holiday_calendar_for,
    validity_end,
)

# Friday. Monday 12 October 2026 is a public holiday in Brazil (Nossa Senhora Aparecida).
FRIDAY = date(2026, 10, 9)


def business(
    days: int, calendar: str | None = "BR", extra: Mapping[str, object] | None = None
) -> LeadTime:
    return LeadTime.model_validate(
        {"unit": "business_days", "amount": days, "holiday_calendar": calendar, **(extra or {})}
    )


@pytest.mark.parametrize(
    ("days", "expected", "calendar_days"),
    [
        (7, date(2026, 10, 21), 12),
        (8, date(2026, 10, 22), 13),
        (10, date(2026, 10, 26), 17),
        (12, date(2026, 10, 28), 19),
    ],
)
def test_business_days_skip_weekends_and_brazilian_holidays(
    days: int, expected: date, calendar_days: int
) -> None:
    # RFQ-6: 7/12/8 business days from the offer date are 12/19/13 calendar days.
    delivery = expected_delivery(business(days), FRIDAY)

    assert delivery.delivery_date == expected
    assert delivery.calendar_days == calendar_days
    assert delivery.exact
    assert delivery.reasons == ()


def test_business_days_are_never_calendar_days() -> None:
    as_business = expected_delivery(business(12), FRIDAY)
    as_calendar = expected_delivery(LeadTime(unit="calendar_days", amount=Decimal(12)), FRIDAY)

    assert as_calendar.delivery_date == date(2026, 10, 21)
    assert as_business.delivery_date == date(2026, 10, 28)


def test_business_days_without_a_known_calendar_count_weekends_only_and_are_not_exact() -> None:
    delivery = expected_delivery(business(12, calendar=None), FRIDAY)

    # The 12 October holiday is not skipped: one day earlier than in Brazil.
    assert delivery.delivery_date == date(2026, 10, 27)
    assert not delivery.exact
    assert delivery.reasons == ("holidays_unknown",)


@pytest.mark.parametrize(
    ("amount", "unit", "expected", "exact"),
    [
        (Decimal(15), "calendar_days", date(2026, 10, 24), True),
        (Decimal(3), "weeks", date(2026, 10, 30), True),
        (Decimal("2.5"), "weeks", date(2026, 10, 27), True),
        (Decimal(2), "months", date(2026, 12, 9), True),
        (Decimal("1.5"), "months", date(2026, 11, 23), False),
        (Decimal(0), "calendar_days", FRIDAY, True),
    ],
)
def test_periods(amount: Decimal, unit: PeriodUnit, expected: date, exact: bool) -> None:
    assert add_period(FRIDAY, amount, unit, "BR") == (expected, exact)


def test_whole_months_follow_the_calendar() -> None:
    assert add_period(date(2026, 1, 31), Decimal(1), "months") == (date(2026, 2, 28), True)
    assert add_period(date(2026, 11, 30), Decimal(3), "months") == (date(2027, 2, 28), True)


def test_a_delivery_date_is_the_date() -> None:
    lead = LeadTime(unit="date", delivery_date=date(2026, 10, 30))

    delivery = expected_delivery(lead, FRIDAY)

    assert (delivery.delivery_date, delivery.calendar_days, delivery.exact) == (
        date(2026, 10, 30),
        21,
        True,
    )


@pytest.mark.parametrize(
    ("extra", "reasons"),
    [
        ({"approximate": True}, ("approximate",)),
        ({"basis": "payment"}, ("starts_after_payment",)),
        ({"basis": "approval"}, ("starts_after_approval",)),
        ({"basis": "ex_works"}, ("excludes_transport",)),
        ({"basis": "order"}, ()),
        ({"approximate": True, "basis": "ex_works"}, ("approximate", "excludes_transport")),
    ],
)
def test_what_makes_an_expected_date_inexact(
    extra: dict[str, object], reasons: tuple[str, ...]
) -> None:
    delivery = expected_delivery(business(10, extra=extra), FRIDAY)

    assert delivery.delivery_date == date(2026, 10, 26)
    assert delivery.reasons == reasons
    assert delivery.exact is (not reasons)


def test_a_lead_time_needs_its_amount_or_date() -> None:
    with pytest.raises(ValueError):
        expected_delivery(LeadTime(unit="weeks"), FRIDAY)
    with pytest.raises(ValueError):
        expected_delivery(LeadTime(unit="date"), FRIDAY)
    with pytest.raises(ValueError):
        add_period(FRIDAY, Decimal(-1), "calendar_days")


def test_validity_end() -> None:
    assert validity_end(Decimal(15), "calendar_days", FRIDAY, "BR") == (date(2026, 10, 24), True)
    assert validity_end(Decimal(10), "business_days", FRIDAY, "BR") == (date(2026, 10, 26), True)
    # Without the supplier's calendar a business-day validity is not exact.
    assert validity_end(Decimal(10), "business_days", FRIDAY, None) == (
        date(2026, 10, 23),
        False,
    )


def test_the_holiday_calendar_is_the_suppliers_country_else_its_currencys() -> None:
    assert country_for_currency("BRL") == "BR"
    assert country_for_currency("brl") == "BR"
    assert country_for_currency("EUR") is None  # shared currency: no country
    assert country_for_currency(None) is None
    assert has_holiday_calendar("BR")
    assert not has_holiday_calendar("XX")
    assert not has_holiday_calendar(None)
    assert holiday_calendar_for("de", "BRL") == "DE"
    assert holiday_calendar_for(None, "BRL") == "BR"
    assert holiday_calendar_for("XX", "BRL") == "BR"
    assert holiday_calendar_for(None, "EUR") is None
    assert holiday_calendar_for(None, None) is None


def test_stored_lead_time_round_trips_with_its_conversion() -> None:
    lead = business(
        10,
        extra={
            "approximate": True,
            "basis": "order",
            "risk_note": "a few more days",
            "scope": "offer",
        },
    )

    stored = decode_lead_time(encode_lead_time(lead, FRIDAY))

    assert stored is not None
    assert LeadTime.model_validate(stored.model_dump(include=set(LeadTime.model_fields))) == lead
    assert stored.reference_date == FRIDAY
    assert stored.expected.delivery_date == date(2026, 10, 26)
    assert stored.expected.reasons == ("approximate",)
    assert decode_lead_time("not json") is None
    assert decode_lead_time(None) is None
    assert decode_validity(42) is None
