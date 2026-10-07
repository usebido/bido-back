"""How long to hold chases after an auto-reply (out of office).

Intake already recognises auto-replies (`Auto-Submitted`, `X-Autoreply`,
`Precedence`, out-of-office subjects) and quarantines them. Here we only read
the stated return date from the text: the latest date written in it that lies
in the next `MAX_PAUSE_DAYS` days ("away from 6 Oct until 14 Oct" -> 14 Oct).
Chases resume the next business day after it. Without a usable date, chases
pause for `DEFAULT_PAUSE_BUSINESS_DAYS` business days.
"""

from datetime import UTC, date, datetime, time
from typing import Final

from bido_back.followups.calendar import add_business_days
from bido_back.quotes.parsing import dates_in

MAX_PAUSE_DAYS: Final = 60
DEFAULT_PAUSE_BUSINESS_DAYS: Final = 3


def stated_return_date(text: str, received_at: datetime) -> date | None:
    today = received_at.date()
    upcoming = [day for day in dates_in(text) if 0 < (day - today).days <= MAX_PAUSE_DAYS]
    return max(upcoming) if upcoming else None


def paused_until(text: str, received_at: datetime, country: str | None) -> datetime:
    """When chasing may resume after an auto-reply received at `received_at`."""
    returning = stated_return_date(text, received_at)
    if returning is None:
        return add_business_days(received_at, DEFAULT_PAUSE_BUSINESS_DAYS, country)
    # The day after the stated return date, at the time the auto-reply came in.
    back = datetime.combine(returning, time(0, 0), tzinfo=UTC)
    return add_business_days(
        back.replace(hour=received_at.hour, minute=received_at.minute), 1, country
    )
