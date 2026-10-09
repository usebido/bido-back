"""The reply-by date of each email type (the Bido email standard, §2.4).

Business days skip weekends and the supplier country's public holidays
(`bido_back.followups.calendar`). No reply-by is ever after the negotiation deadline.

| type          | reply by                                                              |
|---------------|-----------------------------------------------------------------------|
| reminder      | the RFQ's due date; +2 business days once it has passed               |
| clarification | +2 business days, no later than the RFQ's due date when that is ahead  |
| revised_offer | +3 business days                                                      |
| delivery_date | +2 business days                                                      |
| bafo          | the negotiation deadline, with its time: one cut-off for every supplier |
| not_selected  | none                                                                  |
"""

from datetime import date, datetime

from bido_back.followups.calendar import add_business_days
from bido_back.supplier_email.model import EmailType, ReplyBy


def _capped(day: date, deadline: datetime | None, today: date) -> date:
    if deadline is not None and deadline.date() < day:
        return max(deadline.date(), today)
    return day


def reply_by_for(
    kind: EmailType,
    *,
    now: datetime,
    country: str | None,
    response_due: date | None = None,
    deadline: datetime | None = None,
) -> ReplyBy | None:
    today = now.date()

    def business(days: int) -> date:
        return add_business_days(now, days, country).date()

    match kind:
        case "not_selected":
            return None
        case "reminder":
            if response_due is not None and response_due >= today:
                return ReplyBy(_capped(response_due, deadline, today))
            return ReplyBy(_capped(business(2), deadline, today))
        case "clarification":
            day = business(2)
            if response_due is not None and today <= response_due < day:
                day = response_due
            return ReplyBy(_capped(day, deadline, today))
        case "revised_offer":
            return ReplyBy(_capped(business(3), deadline, today))
        case "delivery_date":
            return ReplyBy(_capped(business(2), deadline, today))
        case "bafo":
            if deadline is not None:
                return ReplyBy(deadline.date(), cutoff=deadline)
            return ReplyBy(business(2))
