"""Whether an RFQ's draft invites may be prepared for sending: the reply-by rule.

Suppliers need a date to answer by, and follow-ups and the negotiation anchor
on it, so no RFQ email is prepared from an RFQ without a reply-by date
(`rfqs.response_due`) or with one already in the past. `prepare_send` refuses
with `ReplyByRequired`, and so does a negotiation mandate, whose initial emails
are prepared through it. The RFQ views carry the same blockers
(`send_blockers`) so a screen can disable sending before the click.

"Today" is the UTC date, as everywhere else in the API.
"""

from datetime import UTC, date, datetime

from bido_back.rfqs.schemas import SendBlocker

# Only these statuses can still have emails prepared.
_SENDABLE = frozenset({"draft", "open"})


def utc_today() -> date:
    return datetime.now(UTC).date()


def send_blockers(status: str, response_due: date | None, today: date) -> list[SendBlocker]:
    """Why a send of this RFQ would be refused today; empty when nothing blocks it."""
    if status not in _SENDABLE:
        return []
    if response_due is None:
        return ["reply_by_missing"]
    if response_due < today:
        return ["reply_by_past"]
    return []


def reply_by_refusal(ref: str, status: str, response_due: date | None, today: date) -> str | None:
    """The refusal for a send blocked by the reply-by rule, or None when it may go.

    Ids and dates only: it is shown to people (HTTP 409) and to the copilot."""
    blockers = send_blockers(status, response_due, today)
    if not blockers:
        return None
    if response_due is None:
        reason = f"{ref} has no reply-by date"
    else:
        reason = f"{ref}'s reply-by date {response_due.isoformat()} is in the past"
    return (
        f"{reason}: set the date suppliers must answer by ({today.isoformat()} or later) "
        "before sending"
    )
