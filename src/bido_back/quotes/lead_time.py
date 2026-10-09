"""Lead times and validity periods as data, and the dates they lead to.

Suppliers write "approximately 10 business days after order confirmation",
"Lieferzeit ca. 3 Wochen", "consegna entro il 30/10" or "valid for 15 days".
The extraction keeps what was written: an amount and a unit (calendar days,
business days, weeks, months, or a date), what the period runs from (`basis`),
whether it is approximate, and the supplier's own words when it may slip.
Code turns that into a date from a reference date (the offer date, or a
hypothetical order date such as today) with the supplier's holiday calendar
(`bido_back.followups.calendar`). Business days are never counted as calendar days.

Everything here is pure: no database, no clock, no model.
"""

import calendar
import math
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Final, Literal

import holidays
from pydantic import BaseModel, ConfigDict, ValidationError

from bido_back.agents.catalog.contracts import LeadTimeBasis, LeadTimeUnit, ValidityUnit
from bido_back.followups.calendar import add_business_days

PeriodUnit = Literal["calendar_days", "business_days", "weeks", "months"]
LeadTimeScope = Literal["line", "offer"]
# Why an expected date is not exact.
InexactReason = Literal[
    "approximate",
    "holidays_unknown",
    "starts_after_payment",
    "starts_after_approval",
    "excludes_transport",
    "fractional_months",
]

# A single-country currency names the supplier's holiday calendar when the
# supplier record has no country. EUR, USD and other shared currencies do not.
_CURRENCY_COUNTRY: Final[dict[str, str]] = {
    "BRL": "BR", "MXN": "MX", "CNY": "CN", "JPY": "JP", "KRW": "KR", "INR": "IN",
    "GBP": "GB", "CHF": "CH", "PLN": "PL", "CZK": "CZ", "HUF": "HU", "RON": "RO",
    "BGN": "BG", "TRY": "TR", "SEK": "SE", "NOK": "NO", "DKK": "DK", "CAD": "CA",
    "AUD": "AU", "TWD": "TW", "HKD": "HK", "SGD": "SG", "THB": "TH", "MYR": "MY",
    "VND": "VN", "ILS": "IL", "AED": "AE", "ZAR": "ZA",
}  # fmt: skip
_DAYS_PER_MONTH: Final = 30


class LeadTime(BaseModel):
    """A lead time as the supplier stated it, with the calendar to count it in.

    `amount` is the number of `unit`s; for unit `date`, `delivery_date` is the
    date itself. `scope` says whether the line stated it or it is the offer-level
    lead time applied to a line without its own. `holiday_calendar` is the ISO
    country whose public holidays business days skip (None: weekends only).
    """

    model_config = ConfigDict(frozen=True)

    unit: LeadTimeUnit
    amount: Decimal | None = None
    delivery_date: date | None = None
    basis: LeadTimeBasis = "unspecified"
    approximate: bool = False
    risk_note: str | None = None
    scope: LeadTimeScope = "line"
    holiday_calendar: str | None = None


class ExpectedDelivery(BaseModel):
    """The date a lead time leads to from a reference date.

    `exact` is False when the supplier hedged ("approximately", a range), the
    period starts after an event whose date is unknown (payment, approval), it
    ends ex works (transport not included), or business days were counted
    without the supplier's holiday calendar; `reasons` says which.
    """

    model_config = ConfigDict(frozen=True)

    delivery_date: date
    exact: bool
    reasons: tuple[InexactReason, ...] = ()
    # Days from the reference date to `delivery_date`.
    calendar_days: int


class ValidityPeriod(BaseModel):
    """An offer validity given as a duration, anchored and converted to an end date.

    `anchor` is the date the period runs from: the supplier message's date, or
    the date of the supplier's first offer in this thread when the text says so
    ("15 days from the date of our original offer").
    """

    model_config = ConfigDict(frozen=True)

    amount: Decimal
    unit: ValidityUnit
    anchor: date
    anchor_kind: Literal["message", "original_offer"]
    holiday_calendar: str | None = None
    valid_until: date
    exact: bool


# --- Calendars ---------------------------------------------------------------------------


def country_for_currency(currency: str | None) -> str | None:
    """The one country that uses `currency`, or None for shared or unknown currencies."""
    if not currency:
        return None
    return _CURRENCY_COUNTRY.get(currency.strip().upper())


@lru_cache(maxsize=64)
def has_holiday_calendar(country: str | None) -> bool:
    """Whether the public holidays of `country` (ISO 3166-1 alpha-2) are known."""
    if not country:
        return False
    try:
        holidays.country_holidays(country.upper())
    except NotImplementedError:
        return False
    return True


def holiday_calendar_for(country: str | None, currency: str | None = None) -> str | None:
    """The supplier's holiday calendar: its own country, else its currency's country."""
    for candidate in (country, country_for_currency(currency)):
        if candidate and has_holiday_calendar(candidate):
            return candidate.strip().upper()
    return None


# --- Periods -----------------------------------------------------------------------------


def add_period(
    start: date, amount: Decimal, unit: PeriodUnit, country: str | None = None
) -> tuple[date, bool]:
    """`start` moved forward by `amount` `unit`s, and whether that is exact.

    Business days skip weekends and `country`'s public holidays: 12 business days
    after Friday 9 October 2026 in Brazil (12 October is a holiday) is 28 October.
    Weeks are 7 days (part weeks round up to whole days). Whole months follow the
    calendar (31 January + 1 month = 28 February); part months count 30 days a
    month, rounded up, and are not exact.
    """
    if amount < 0:
        raise ValueError("amount must be >= 0")
    match unit:
        case "calendar_days":
            return start + timedelta(days=math.ceil(amount)), True
        case "business_days":
            moved = add_business_days(
                datetime.combine(start, time(), tzinfo=UTC), math.ceil(amount), country
            )
            return moved.date(), True
        case "weeks":
            return start + timedelta(days=math.ceil(amount * 7)), True
        case "months":
            if amount == amount.to_integral_value():
                return _add_months(start, int(amount)), True
            return start + timedelta(days=math.ceil(amount * _DAYS_PER_MONTH)), False


def _add_months(start: date, months: int) -> date:
    index = start.month - 1 + months
    year, month = start.year + index // 12, index % 12 + 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def expected_delivery(lead_time: LeadTime, reference: date) -> ExpectedDelivery:
    """The date `lead_time` leads to when it starts on `reference` (the order date).

    Pure: the supplier's holiday calendar is `lead_time.holiday_calendar`. Use the
    offer date for what the supplier meant when it wrote, or today (a hypothetical
    order placed today) for what the buyer would get now.
    """
    reasons: list[InexactReason] = []
    if lead_time.unit == "date":
        if lead_time.delivery_date is None:
            raise ValueError("a lead time given as a date needs delivery_date")
        expected = lead_time.delivery_date
    else:
        if lead_time.amount is None:
            raise ValueError("a lead time needs an amount")
        expected, exact = add_period(
            reference, lead_time.amount, lead_time.unit, lead_time.holiday_calendar
        )
        if not exact:
            reasons.append("fractional_months")
        if lead_time.unit == "business_days" and not has_holiday_calendar(
            lead_time.holiday_calendar
        ):
            reasons.append("holidays_unknown")
    if lead_time.approximate:
        reasons.append("approximate")
    match lead_time.basis:
        case "payment":
            reasons.append("starts_after_payment")
        case "approval":
            reasons.append("starts_after_approval")
        case "ex_works":
            reasons.append("excludes_transport")
        case "order" | "unspecified":
            pass
    return ExpectedDelivery(
        delivery_date=expected,
        exact=not reasons,
        reasons=tuple(reasons),
        calendar_days=(expected - reference).days,
    )


def validity_end(
    amount: Decimal, unit: ValidityUnit, anchor: date, country: str | None = None
) -> tuple[date, bool]:
    """The last day of an offer valid for `amount` `unit`s from `anchor`, and if exact.

    "Valid for 15 days" written on 9 October is valid until 24 October.
    """
    end, exact = add_period(anchor, amount, unit, country)
    if unit == "business_days" and not has_holiday_calendar(country):
        exact = False
    return end, exact


# --- Persistence ---------------------------------------------------------------------------
#
# The extraction stores a validated lead time (and validity) as JSON in the
# `parsed` value of its provenance field; these read it back.


class StoredLeadTime(LeadTime):
    """A `LeadTime` as stored per proposal line, with its conversion at the offer date."""

    reference_date: date
    expected: ExpectedDelivery


def encode_lead_time(lead_time: LeadTime, reference: date) -> str:
    stored = StoredLeadTime(
        **lead_time.model_dump(),
        reference_date=reference,
        expected=expected_delivery(lead_time, reference),
    )
    return stored.model_dump_json()


def decode_lead_time(raw: object) -> StoredLeadTime | None:
    if not isinstance(raw, str):
        return None
    try:
        return StoredLeadTime.model_validate_json(raw)
    except ValidationError:
        return None


def decode_validity(raw: object) -> ValidityPeriod | None:
    if not isinstance(raw, str):
        return None
    try:
        return ValidityPeriod.model_validate_json(raw)
    except ValidationError:
        return None
