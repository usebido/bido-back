"""Business days: weekends and the supplier country's public holidays are skipped.

Holidays come from the `holidays` package, by ISO 3166-1 alpha-2 country code
(the supplier's `country`). An unknown or missing country counts weekends
only. Times keep their time of day: two business days after Friday 15:00 is
Tuesday 15:00.
"""

from datetime import date, datetime, timedelta
from functools import lru_cache

import holidays


@lru_cache(maxsize=64)
def _calendar(country: str | None, year: int) -> frozenset[date]:
    if not country:
        return frozenset()
    try:
        found = holidays.country_holidays(country.upper(), years=year)
    except NotImplementedError:
        return frozenset()
    return frozenset(found.keys())


def is_business_day(day: date, country: str | None = None) -> bool:
    return day.weekday() < 5 and day not in _calendar(country, day.year)


def add_business_days(start: datetime, days: int, country: str | None = None) -> datetime:
    """`start` moved forward by `days` business days (0 = `start` itself)."""
    if days < 0:
        raise ValueError("days must be >= 0")
    current = start
    remaining = days
    while remaining > 0:
        current += timedelta(days=1)
        if is_business_day(current.date(), country):
            remaining -= 1
    return current
