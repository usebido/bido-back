"""Audit events for the RFQ lifecycle: created, lines edited, terms set, suppliers
invited or removed, status changed, a quote registered by the buyer.

The services write them in the same transaction as the change, from the REST
and the Copilot paths alike, and the activity feed (`bido_back.rfqs.activity`)
reads them back. Every payload carries `rfq_id` (the feed's key) and the ids of
what changed. The values that appear are the RFQ's own fields (quantities,
codes, dates, terms), which the RFQ stores anyway; never a secret, an email, or
a person's details. Text is cut to a short prefix.
"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Final, Literal, cast
from uuid import UUID

from bido_back.db.models import AuditEvent

# How the RFQ came to be: the REST API, the Copilot's `create_rfq`, or code
# (a purchase analysis or procurement assessment).
RfqOrigin = Literal["api", "copilot", "analysis"]
# How a supplier's quote reached the RFQ: the supplier's email reply, or a document
# the buyer attached in the Copilot and registered (`messages.origin`).
QuoteOrigin = Literal["email", "copilot_upload"]

RFQ_CREATED: Final = "rfq.created"
RFQ_LINE_ADDED: Final = "rfq.line_added"
RFQ_LINE_UPDATED: Final = "rfq.line_updated"
RFQ_LINE_REMOVED: Final = "rfq.line_removed"
RFQ_TERMS_SET: Final = "rfq.terms_set"
RFQ_INVITE_ADDED: Final = "rfq.invite_added"
RFQ_INVITE_REMOVED: Final = "rfq.invite_removed"
RFQ_STATUS_CHANGED: Final = "rfq.status_changed"
# A supplier's quote stored on an invite from somewhere other than its email
# reply (the buyer registered an uploaded document). Email replies are visible
# in the feed as received messages and need no event.
QUOTE_REGISTERED: Final = "quote.registered"

TEXT_LIMIT: Final = 120

type Json = str | int | float | bool | None | list[Json] | dict[str, Json]
type Change = dict[str, Json]


def json_value(value: object) -> Json:
    """A payload-safe copy of a field value: JSON scalars, ISO dates, short text."""
    match value:
        case None | bool() | int() | float():
            return value
        case str():
            return value[:TEXT_LIMIT]
        case Decimal():
            return int(value) if value == value.to_integral_value() else float(value)
        case datetime() | date():
            return value.isoformat()
        case UUID():
            return str(value)
        case Mapping():
            mapping = cast(Mapping[object, object], value)
            return {str(key): json_value(item) for key, item in mapping.items()}
        case Sequence():
            return [json_value(item) for item in cast(Sequence[object], value)]
        case _:
            return str(value)[:TEXT_LIMIT]


def changes(before: Mapping[str, object], after: Mapping[str, object]) -> dict[str, Change]:
    """`{field: {"from": old, "to": new}}` for every field whose value changed."""
    diff: dict[str, Change] = {}
    for field, old in before.items():
        old_json, new_json = json_value(old), json_value(after.get(field))
        if old_json != new_json:
            diff[field] = {"from": old_json, "to": new_json}
    return diff


def rfq_event(
    tenant_id: UUID,
    actor_user_id: UUID | None,
    action: str,
    rfq_id: UUID,
    **payload: object,
) -> AuditEvent:
    """One lifecycle event for `rfq_id`, keyed by it so the feed finds it."""
    return AuditEvent(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action=action,
        payload={"rfq_id": str(rfq_id), **{key: json_value(v) for key, v in payload.items()}},
    )
