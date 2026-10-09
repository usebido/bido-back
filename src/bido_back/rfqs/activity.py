"""An RFQ's activity feed: audit events plus received replies, newest first.

Only ids, names and summaries written here in code reach the response; never
an email body, subject or attachment name.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final, cast
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    Attachment,
    AuditEvent,
    Invite,
    Message,
    NegotiationMandate,
    Rfq,
    Supplier,
)
from bido_back.negotiation.labels import reason_text
from bido_back.rfqs.errors import NotFound
from bido_back.rfqs.overview import owner_names
from bido_back.rfqs.schemas import ActivityKind, RfqActivityItem

type Db = AsyncEngine | async_sessionmaker[AsyncSession]

DEFAULT_LIMIT: Final = 50
MAX_LIMIT: Final = 200
BIDO: Final = "Bido"

_TOOLS: Final[Mapping[str, str]] = {
    "send_rfq_email": "RFQ email",
    "send_followup_email": "follow-up email",
    "send_negotiation_email": "negotiation email",
    "award_rfq": "award",
    "send_purchase_order": "purchase order",
    "send_award_notice": "not-selected notice",
    "approve_negotiation_mandate": "negotiation mandate",
}
_NEGOTIATION_ACTIONS: Final[Mapping[str, str]] = {
    "counter": "Counter-offer",
    "counteroffer": "Counter-offer",
    "bafo": "Best-and-final request",
    "accept": "Acceptance",
}
# What a negotiation email decided by Bido asks for.
_EMAIL_KINDS: Final[Mapping[str, str]] = {
    "revised_offer": "a request for a revised offer",
    "delivery_date": "a delivery-date request",
    "bafo": "the best-and-final request",
}
_ORIGINS: Final[Mapping[str, str]] = {
    "copilot": " from the Copilot",
    "analysis": " from a purchase analysis",
    "api": "",
}
# Lifecycle changes named in the summary. Fields whose values are short codes,
# numbers or dates show old -> new; longer text only says that it changed.
_FIELD_LABELS: Final[Mapping[str, str]] = {
    "quantities": "quantity",
    "unit": "unit",
    "part_number": "part number",
    "mpn": "MPN",
    "manufacturer": "manufacturer",
    "category": "category",
    "incoterm": "Incoterm",
    "delivery_location": "delivery location",
    "need_by": "need-by date",
    "description": "description",
    "qualification": "qualification",
    "title": "title",
    "response_due": "response deadline",
    "currency": "currency",
    "payment_terms": "payment terms",
    "quote_validity_days": "quote validity (days)",
    "logo_asset_id": "logo",
    "footer_override": "footer",
    "logo_placement": "logo placement",
}
_FIELD_ORDER: Final[Mapping[str, int]] = {f: i for i, f in enumerate(_FIELD_LABELS)}
_VALUED_FIELDS: Final = frozenset(
    {
        "quantities",
        "unit",
        "part_number",
        "mpn",
        "manufacturer",
        "category",
        "incoterm",
        "delivery_location",
        "need_by",
        "response_due",
        "currency",
        "payment_terms",
        "quote_validity_days",
    }
)


async def rfq_activity(
    db: Db, principal: Principal, rfq_id: UUID, *, limit: int = DEFAULT_LIMIT
) -> list[RfqActivityItem]:
    limit = max(1, min(limit, MAX_LIMIT))
    async with tenant_session(db, principal.tenant_id) as session:
        if await session.get(Rfq, rfq_id) is None:
            raise NotFound(f"rfq {rfq_id} not found")
        suppliers: dict[UUID, str] = {
            invite_id: name
            for invite_id, name in await session.execute(
                select(Invite.id, Supplier.name)
                .join(Supplier, Supplier.id == Invite.supplier_id)
                .where(Invite.rfq_id == rfq_id)
            )
        }
        invite_keys = [str(i) for i in suppliers]
        mandate_keys = [
            str(i)
            for i in await session.scalars(
                select(NegotiationMandate.id).where(NegotiationMandate.rfq_id == rfq_id)
            )
        ]
        approval_filter = [Approval.args["rfq_id"].astext == str(rfq_id)]
        if invite_keys:
            approval_filter.append(Approval.args["invite_id"].astext.in_(invite_keys))
        approval_keys = [
            str(i) for i in await session.scalars(select(Approval.id).where(or_(*approval_filter)))
        ]

        payload = AuditEvent.payload
        event_filter = [payload["rfq_id"].astext == str(rfq_id)]
        for key, values in (
            ("invite_id", invite_keys),
            ("mandate_id", mandate_keys),
            ("approval_id", approval_keys),
        ):
            if values:
                event_filter.append(payload[key].astext.in_(values))
        events = list(
            await session.scalars(
                select(AuditEvent)
                .where(or_(*event_filter))
                .order_by(AuditEvent.created_at.desc(), AuditEvent.id)
                .limit(limit)
            )
        )

        replies: list[tuple[UUID, datetime, int]] = []
        if suppliers:
            attachments = (
                select(func.count())
                .select_from(Attachment)
                .where(Attachment.message_id == Message.id)
                .scalar_subquery()
            )
            replies = [
                (invite_id, occurred_at, count)
                for invite_id, occurred_at, count in await session.execute(
                    select(Message.invite_id, Message.occurred_at, attachments)
                    .where(Message.invite_id.in_(list(suppliers)), Message.direction == "in")
                    .order_by(Message.occurred_at.desc())
                    .limit(limit)
                )
                if invite_id is not None
            ]

        people = await owner_names(
            session, {e.actor_user_id for e in events if e.actor_user_id is not None}
        )

    items = [_event_item(e, suppliers, people) for e in events]
    items += [
        RfqActivityItem(
            at=occurred_at,
            kind="email",
            action="reply",
            actor=suppliers[invite_id],
            supplier_name=suppliers[invite_id],
            summary=f"Reply received from {suppliers[invite_id]}"
            + (f" with {count} attachment{'s' if count != 1 else ''}" if count else ""),
        )
        for invite_id, occurred_at, count in replies
    ]
    items.sort(key=lambda item: item.at, reverse=True)
    return items[:limit]


def _event_item(
    event: AuditEvent, suppliers: Mapping[UUID, str], people: Mapping[UUID, str]
) -> RfqActivityItem:
    data: Mapping[str, Any] = event.payload or {}
    person = people.get(event.actor_user_id) if event.actor_user_id else None
    supplier = _supplier(data.get("invite_id"), suppliers)
    to = f" to {supplier}" if supplier else ""
    kind: ActivityKind
    actor: str | None = person
    match event.action:
        case "rfq.email_sent":
            kind, summary = "email", f"RFQ sent{to}"
        case "rfq.followup_sent":
            what = "Clarification question" if data.get("kind") == "clarification" else "Reminder"
            kind, summary = "email", f"{what} sent{to}"
        case "rfq.negotiation_sent":
            action = _NEGOTIATION_ACTIONS.get(str(data.get("action")), "Negotiation email")
            kind, summary = "email", f"{action} (round {data.get('round', '?')}) sent{to}"
            actor = actor or BIDO
        case "rfq.negotiation_draft_held":
            action = _NEGOTIATION_ACTIONS.get(str(data.get("action")), "Negotiation email")
            kind, actor = "agent", BIDO
            summary = (
                f"{action} (round {data.get('round', '?')}){to} not sent: the draft failed "
                "Bido's checks. Needs your decision"
            )
        case "rfq.followup_draft_held":
            what = "Clarification question" if data.get("kind") == "clarification" else "Reminder"
            kind, actor = "agent", BIDO
            summary = f"{what}{to} not sent: the draft failed Bido's checks. Needs your decision"
        case "purchase_order.sent":
            kind, summary = "email", f"Purchase order sent{to}"
        case "award.notice_sent":
            kind, summary = "email", f"Not-selected notice sent{to}"
        case "rfq.awarded":
            count = len(data.get("purchase_order_ids") or [])
            kind = "user"
            summary = f"Awarded: {count} purchase order{'s' if count != 1 else ''} created"
        case "rfq.created":
            count = data.get("lines")
            origin = _ORIGINS.get(str(data.get("origin")), "")
            kind, summary = "user", f"RFQ created{origin}{_count(count, 'line', ' with ')}"
        case "rfq.line_added":
            kind, summary = "user", f"Line {data.get('position', '?')} added{_what(data)}"
        case "rfq.line_removed":
            kind, summary = "user", f"Line {data.get('position', '?')} removed{_what(data)}"
        case "rfq.line_updated":
            changed = _changes(data.get("changes"))
            kind, summary = "user", f"Line {data.get('position', '?')} updated: {changed}"
        case "rfq.terms_set":
            parts = [_changes(data.get("changes"))] if data.get("changes") else []
            line_fields = data.get("line_fields")
            if isinstance(line_fields, Mapping):
                lines = _count(data.get("lines"), "line", " on ")
                for field, value in cast(Mapping[str, object], line_fields).items():
                    parts.append(f"{_change(field, {'to': value})}{lines}")
            kind, summary = "user", "Terms updated: " + ", ".join(parts)
        case "rfq.invite_added":
            supplier = supplier or _name(data)
            kind, summary = "user", f"Supplier added: {supplier}"
        case "rfq.invite_removed":
            supplier = supplier or _name(data)
            kind, summary = "user", f"Supplier removed: {supplier}"
        case "rfq.status_changed":
            kind = "system"
            summary = f"RFQ {data.get('to_status', '?')} (was {data.get('from_status', '?')})"
            actor = actor or BIDO
        case "rfq.negotiation_settings":
            kind, summary = "user", f"Negotiation limits set for {data.get('lines', 0)} lines"
        case "rfq.negotiation_ready":
            kind, summary, actor = "agent", "Negotiation ready to close", BIDO
        case "rfq.negotiation_closed":
            kind = "user" if person else "agent"
            summary = f"Negotiation closed ({data.get('reason', 'closed')})"
            actor = actor or BIDO
        case "mandate.started":
            # The payload stores the number of suppliers (a list in older events).
            raw = data.get("suppliers")
            count = raw if isinstance(raw, int) else len(cast(list[object], raw or []))
            kind = "user"
            plural = "s" if count != 1 else ""
            summary = f"Negotiation mandate approved for {count} supplier{plural}"
        case "mandate.paused" | "mandate.resumed":
            kind = "user"
            paused = event.action.endswith("paused")
            summary = "Negotiation paused" if paused else "Negotiation resumed"
        case "mandate.held":
            tool = _TOOLS.get(str(data.get("tool")), "email")
            why = _words(data.get("reason"))
            summary = f"{tool.capitalize()}{to} held for your approval" + (
                f": {why}" if why else ""
            )
            kind, actor = "agent", BIDO
        case "mandate.waiting":
            tool = _TOOLS.get(str(data.get("tool")), "email")
            until = _moment(data.get("until"))
            if data.get("reason") == "daily_cap":
                summary = f"{tool.capitalize()}{to} waits for the daily email limit" + (
                    f" until {until}" if until else ""
                )
            else:
                summary = f"{tool.capitalize()}{to} waits for your mailbox to be connected"
            kind, actor = "agent", BIDO
        case "rfq.negotiation_decided":
            what = _EMAIL_KINDS.get(str(data.get("kind")), "a negotiation email")
            summary = f"Bido prepared {what} (round {data.get('round', '?')}){_for(supplier)}"
            kind, actor = "agent", BIDO
        case "rfq.negotiation_accepted":
            summary = f"Accepted internally – nothing to ask{_for(supplier)}"
            kind, actor = "agent", BIDO
        case "rfq.negotiation_final":
            summary = reason_text(str(data.get("reason", ""))) + _for(supplier)
            kind, actor = "agent", BIDO
        case "rfq.negotiation_needs_decision" | "rfq.negotiation_needs_answer":
            summary = reason_text(str(data.get("reason", ""))) + _for(supplier)
            kind, actor = "agent", BIDO
        case "rfq.negotiation_hold_released":
            kind = "user"
            summary = f"Hold released: Bido negotiates the current offer as it is{_for(supplier)}"
        case "rfq.negotiation_held":
            why = _words(data.get("reason"))
            summary = f"Needs your decision: the email{to} could not be prepared" + (
                f" ({why})" if why else ""
            )
            kind, actor = "agent", BIDO
        case action if action.startswith("approval."):
            tool = _TOOLS.get(str(data.get("tool")), "action")
            verb = action.removeprefix("approval.").replace("_", " ")
            kind = "user" if verb in ("approved", "rejected") else "system"
            summary = f"Approval {verb}: {tool}"
        case _:
            kind, summary = "system", event.action
    return RfqActivityItem(
        at=event.created_at,
        kind=kind,
        action=event.action,
        actor=actor,
        supplier_name=supplier,
        summary=summary,
    )


def _for(supplier: str | None) -> str:
    return f" ({supplier})" if supplier else ""


def _words(value: object) -> str:
    """A reason code as words (codes only; never content)."""
    return value.replace("_", " ") if isinstance(value, str) and value else ""


def _moment(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return ""


def _count(value: object, noun: str, prefix: str) -> str:
    if not isinstance(value, int):
        return ""
    return f"{prefix}{value} {noun}{'s' if value != 1 else ''}"


def _what(data: Mapping[str, Any]) -> str:
    description = data.get("description")
    return f": {description}" if isinstance(description, str) and description else ""


def _name(data: Mapping[str, Any]) -> str:
    name = data.get("supplier_name")
    return name if isinstance(name, str) and name else "a supplier"


def _changes(value: object) -> str:
    if not isinstance(value, Mapping) or not value:
        return "no change"
    # JSONB does not keep key order: name the fields in a fixed one.
    fields = cast(Mapping[str, object], value)
    ordered = sorted(fields.items(), key=lambda item: _FIELD_ORDER.get(item[0], len(_FIELD_ORDER)))
    return ", ".join(_change(field, change) for field, change in ordered)


def _change(field: str, change: object) -> str:
    label = _FIELD_LABELS.get(field, field.replace("_", " "))
    if not isinstance(change, Mapping):
        return f"{label} changed"
    values = cast(Mapping[str, object], change)
    old, new = values.get("from"), values.get("to")
    if field not in _VALUED_FIELDS:
        return f"{label} {'cleared' if new is None else 'set' if old is None else 'changed'}"
    if new is None:
        return f"{label} cleared"
    if old is None:
        return f"{label} set to {_value(new)}"
    return f"{label} {_value(old)} -> {_value(new)}"


def _value(value: object) -> str:
    if isinstance(value, list):
        return "/".join(_value(item) for item in cast(list[object], value))
    return str(value)


def _supplier(value: object, suppliers: Mapping[UUID, str]) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return suppliers.get(UUID(value))
    except ValueError:
        return None
