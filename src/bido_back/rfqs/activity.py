"""An RFQ's activity feed: audit events plus received replies, newest first.

Only ids, names and summaries written here in code reach the response; never
an email body, subject or attachment name.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final
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
        case "purchase_order.sent":
            kind, summary = "email", f"Purchase order sent{to}"
        case "award.notice_sent":
            kind, summary = "email", f"Not-selected notice sent{to}"
        case "rfq.awarded":
            count = len(data.get("purchase_order_ids") or [])
            kind = "user"
            summary = f"Awarded: {count} purchase order{'s' if count != 1 else ''} created"
        case "rfq.negotiation_settings":
            kind, summary = "user", f"Negotiation limits set for {data.get('lines', 0)} lines"
        case "rfq.negotiation_ready":
            kind, summary, actor = "agent", "Negotiation ready to close", BIDO
        case "rfq.negotiation_closed":
            kind = "user" if person else "agent"
            summary = f"Negotiation closed ({data.get('reason', 'closed')})"
            actor = actor or BIDO
        case "mandate.started":
            count = len(data.get("suppliers") or [])
            kind = "user"
            plural = "s" if count != 1 else ""
            summary = f"Negotiation mandate approved for {count} supplier{plural}"
        case "mandate.paused" | "mandate.resumed":
            kind = "user"
            paused = event.action.endswith("paused")
            summary = "Negotiation paused" if paused else "Negotiation resumed"
        case "mandate.held":
            kind, summary, actor = "agent", "An email was held for review", BIDO
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


def _supplier(value: object, suppliers: Mapping[UUID, str]) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return suppliers.get(UUID(value))
    except ValueError:
        return None
