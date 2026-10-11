"""An RFQ's activity feed: audit events plus received replies, newest first.

Only ids, names and summaries written here in code reach the response; never
an email body, subject or attachment name, nor a milestone's note, file names or
the reason changes were asked for.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
    IssuedPurchaseOrder,
    Message,
    NegotiationMandate,
    PoMilestoneSubmission,
    PoPayment,
    PoPaymentMilestone,
    Rfq,
    Supplier,
)
from bido_back.negotiation.labels import reason_text
from bido_back.payments.milestones.catalog import EVIDENCE, TOOLING_PLAN
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
    "send_supplier_answer": "answer to a supplier's question",
    "send_negotiation_email": "negotiation email",
    "award_rfq": "award",
    "send_purchase_order": "purchase order",
    "send_award_notice": "not-selected notice",
    "send_po_cancellation": "purchase order cancellation",
    "approve_negotiation_mandate": "negotiation mandate",
    "pay_purchase_order": "purchase order payment",
    "release_po_milestone": "milestone release",
}
_FOLLOWUP_KINDS: Final[Mapping[str, str]] = {
    "chase": "Reminder",
    "clarification": "Clarification question",
    "reminder": "Reminder of our unanswered email",
    "answer": "Answer to the supplier's question",
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
    "reminder": "a reminder of our unanswered request",
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
# A PO payment's milestones (`bido_back.payments.milestones`): the factory's link, the
# documents, the approval, the release. Their events carry ids, the milestone's index and
# codes; its name and share come from the payment's milestones, the supplier from its PO.
_MILESTONE_EVENTS: Final = ("purchase_order.milestone_", "purchase_order.factory_")
_CHANGES_REQUESTED: Final = "purchase_order.milestone_changes_requested"
# The catalog's document types (codes are unique across the plan).
_DOCUMENT_LABELS: Final[Mapping[str, str]] = {
    document.code: document.label for milestone in TOOLING_PLAN for document in milestone.documents
} | {EVIDENCE.code: EVIDENCE.label}
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
                    # A quote the buyer uploaded is also stored as an inbound message;
                    # its `quote.registered` event is the feed's line for it.
                    .where(
                        Message.invite_id.in_(list(suppliers)),
                        Message.direction == "in",
                        Message.origin == "email",
                    )
                    .order_by(Message.occurred_at.desc())
                    .limit(limit)
                )
                if invite_id is not None
            ]

        people = await owner_names(
            session, {e.actor_user_id for e in events if e.actor_user_id is not None}
        )
        facts = await _milestone_facts(session, events)

    items = [_event_item(e, suppliers, people, facts) for e in events]
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


type _MilestoneKey = tuple[str, int]  # (payment id, milestone index)


@dataclass(frozen=True, slots=True)
class _MilestoneFacts:
    """What milestone events name by id only: the milestones (by payment id and index), the
    supplier each payment pays (by payment id), and why a submission was sent back (by
    submission id: the code a person chose, never their text)."""

    milestones: Mapping[_MilestoneKey, PoPaymentMilestone]
    suppliers: Mapping[str, str]
    rejections: Mapping[str, str]


def _uuids(events: Sequence[AuditEvent], key: str) -> set[UUID]:
    """The ids the events' payloads hold under `key`."""
    found: set[UUID] = set()
    for event in events:
        raw = (event.payload or {}).get(key)
        if not isinstance(raw, str):
            continue
        try:
            found.add(UUID(raw))
        except ValueError:
            continue
    return found


async def _milestone_facts(session: AsyncSession, events: Sequence[AuditEvent]) -> _MilestoneFacts:
    ours = [e for e in events if e.action.startswith(_MILESTONE_EVENTS)]
    payment_ids = _uuids(ours, "payment_id")
    if not payment_ids:
        return _MilestoneFacts({}, {}, {})
    milestones = await session.scalars(
        select(PoPaymentMilestone).where(PoPaymentMilestone.payment_id.in_(payment_ids))
    )
    suppliers = await session.execute(
        select(PoPayment.id, Supplier.name)
        .join(IssuedPurchaseOrder, IssuedPurchaseOrder.id == PoPayment.purchase_order_id)
        .join(Supplier, Supplier.id == IssuedPurchaseOrder.supplier_id)
        .where(PoPayment.id.in_(payment_ids))
    )
    rejections: dict[str, str] = {}
    sent_back = _uuids([e for e in ours if e.action == _CHANGES_REQUESTED], "submission_id")
    if sent_back:
        rows = await session.execute(
            select(PoMilestoneSubmission.id, PoMilestoneSubmission.rejection_code).where(
                PoMilestoneSubmission.id.in_(sent_back)
            )
        )
        rejections = {str(sid): code for sid, code in rows if code is not None}
    return _MilestoneFacts(
        {(str(row.payment_id), row.milestone_index): row for row in milestones},
        {str(payment_id): name for payment_id, name in suppliers},
        rejections,
    )


def _event_item(
    event: AuditEvent,
    suppliers: Mapping[UUID, str],
    people: Mapping[UUID, str],
    facts: _MilestoneFacts,
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
            what = _FOLLOWUP_KINDS.get(str(data.get("kind")), "Reminder")
            kind, summary = "email", f"{what} sent{to}"
        case "rfq.supplier_question":
            kind, actor = "agent", BIDO
            summary = f"Question from the supplier{_from(supplier)}: needs your answer"
        case "rfq.supplier_question_answered":
            kind, summary = "user", f"Supplier's question answered{to} (email awaits approval)"
        case "rfq.supplier_question_dismissed":
            kind, summary = "user", f"Supplier's question closed without an email{_from(supplier)}"
        case "rfq.supplier_question_superseded":
            # Bido closed it on its own (never answered it): the buyer may reopen it.
            kind, actor = "agent", BIDO
            offer_date = data.get("offer_date")
            offer = f"the supplier's offer of {offer_date}" if offer_date else "a later offer"
            summary = (
                f"Supplier's question{_from(supplier)} considered superseded by {offer} "
                "— reopen if needed"
            )
        case "rfq.supplier_question_reopened":
            kind, summary = (
                "user",
                f"Supplier's question reopened{_from(supplier)}: needs your answer",
            )
        case "proposal.hold_released":
            kind, summary = "user", f"Offer{_from(supplier)} taken as shown: hold released"
        case "rfq.negotiation_sent":
            action = _NEGOTIATION_ACTIONS.get(str(data.get("action")), "Negotiation email")
            if data.get("reminder") is True:
                action = f"Reminder of the {action.lower()}"
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
        case "purchase_order.acknowledged":
            kind = "user" if data.get("source") != "email" else "email"
            by = f" by {supplier}" if supplier else ""
            theirs = data.get("supplier_order_ref")
            ref = f" (their ref {theirs})" if isinstance(theirs, str) and theirs else ""
            summary = f"Purchase order {_po_ref(data)} acknowledged{by}{ref}"
        case "purchase_order.received":
            kind = "user"
            what = "fully received" if data.get("state") == "received" else "partially received"
            summary = f"Purchase order {_po_ref(data)} {what}: {_received_lines(data)}"
            invoice = data.get("invoice_number")
            if isinstance(invoice, str) and invoice:
                summary += f" (invoice {invoice})"
        case "purchase_order.closed":
            kind = "user"
            if data.get("short"):
                short = data.get("shortfall")
                count = len(cast(list[object], short)) if isinstance(short, list) else 0
                summary = (
                    f"Purchase order {_po_ref(data)} closed short{_count(count, 'line', ': ')}"
                )
                summary += " outstanding" if count else ""
            else:
                summary = f"Purchase order {_po_ref(data)} closed"
        case "purchase_order.cancelled":
            kind, summary = "user", f"Purchase order {_po_ref(data)} cancelled"
        case "purchase_order.supplier_response":
            kind, actor = "agent", BIDO
            summary = _supplier_response(data, supplier)
        case "purchase_order.response_reviewed":
            kind = "user"
            verdict = "accepted" if data.get("accepted") else "not accepted"
            summary = f"Supplier reply on purchase order {_po_ref(data)} reviewed: {verdict}"
        case "purchase_order.cancellation_sent":
            kind, summary = "email", f"Cancellation of purchase order {_po_ref(data)} sent{to}"
        case action if action.startswith(("purchase_order.payment_", "purchase_order.escrow_")):
            kind, summary = _payment_event(action, data)
            if kind == "system":
                # The worker moves the money once a person approved it.
                actor = actor or BIDO
        case action if action.startswith(_MILESTONE_EVENTS):
            supplier = supplier or facts.suppliers.get(str(data.get("payment_id")))
            kind, summary = _milestone_event(action, data, facts)
            if kind == "system":
                actor = actor or BIDO
            elif event.actor_user_id is None:
                # The factory's page has no login: the supplier acted, through its link.
                actor = supplier or "The factory"
        case "rfq.completed":
            kind = "system"
            closed, cancelled = data.get("closed"), data.get("cancelled")
            summary = f"RFQ completed{_count(closed, 'purchase order', ': ')} closed"
            if isinstance(cancelled, int) and cancelled:
                summary += f", {cancelled} cancelled"
            actor = actor or BIDO
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
        case "quote.registered":
            supplier = supplier or _name(data)
            how = " from an uploaded document" if data.get("origin") == "copilot_upload" else ""
            version = data.get("version")
            kind = "user"
            summary = f"Quote registered{how} for {supplier}" + (
                f" (version {version})" if isinstance(version, int) else ""
            )
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
            questions = data.get("questions")
            # One email per supplier answer: it also asks what the offer lacks.
            also = (
                " that also asks for what the offer lacks"
                if isinstance(questions, int) and questions > 0
                else ""
            )
            summary = f"Bido prepared {what}{also} (round {data.get('round', '?')}){_for(supplier)}"
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


_RESPONSE_KINDS: Final[Mapping[str, str]] = {
    "confirmation": "confirmed",
    "confirmation_with_changes": "confirmed with changes",
    "rejection": "refused",
    "question": "asked a question about",
    "delivery_update": "sent a delivery update on",
    "other": "replied about",
    "unread": "replied (not read automatically) about",
}
_FIELDS: Final[Mapping[str, str]] = {
    "delivery_date": "delivery date",
    "unit_price": "unit price",
    "quantity": "quantity",
}


def _supplier_response(data: Mapping[str, Any], supplier: str | None) -> str:
    """`Atlantic confirmed with changes PO-3: line 2 delivery date 2026-11-20 -> 2026-12-04`."""
    who = supplier or "The supplier"
    what = _RESPONSE_KINDS.get(str(data.get("kind")), "replied about")
    summary = f"{who} {what} purchase order {_po_ref(data)}"
    changes = data.get("changes")
    parts: list[str] = []
    for change in cast(list[Any], changes) if isinstance(changes, list) else []:
        if not isinstance(change, dict):
            continue
        item = cast(dict[str, Any], change)
        field = _FIELDS.get(str(item.get("field")), str(item.get("field")))
        parts.append(
            f"line {item.get('position', '?')} {field} {item.get('ordered') or '—'} -> "
            f"{item.get('stated') or '—'}"
        )
    if parts:
        summary += ": " + "; ".join(parts)
    if data.get("needs_review"):
        summary += " (needs your review)"
    return summary


def _po_ref(data: Mapping[str, Any]) -> str:
    ref = data.get("purchase_order_ref")
    return ref if isinstance(ref, str) and ref else "?"


def _payment_event(action: str, data: Mapping[str, Any]) -> tuple[ActivityKind, str]:
    """One step of a PO's card payment into its escrow (`bido_back.payments`).

    Reads `purchase_order_ref`, `charge_currency` and `charge_amount`, `simulated`,
    `card_rail`, `failure_code` and `step` from the event; all optional. A failure at a chain `step`
    came after the card was charged. `completed`: the escrow released its last milestone.
    """
    ref = data.get("purchase_order_ref")
    order = f"purchase order {ref}" if isinstance(ref, str) and ref else "the purchase order"
    currency, amount = data.get("charge_currency"), data.get("charge_amount")
    charge = f" ({currency} {amount})" if isinstance(currency, str) and amount else ""
    why = _words(data.get("failure_code"))
    reason = f" ({why})" if why else ""
    match action:
        case "purchase_order.payment_requested":
            return "user", f"Payment of {order} requested{charge}: waits for approval"
        case "purchase_order.payment_card_verified":
            return "user", f"Card approved with the Visa passkey for {order}"
        case "purchase_order.payment_charged":
            simulated = " (simulated: no money moved)" if data.get("simulated") is True else ""
            if data.get("card_rail") == "vic":
                return (
                    "system",
                    f"Single-use Visa card issued to the agent{charge} for {order}{simulated}",
                )
            return "system", f"Card charged{charge} for {order}{simulated}"
        case "purchase_order.payment_declined":
            return "system", f"Card payment for {order} declined{reason}"
        case "purchase_order.payment_failed":
            step = data.get("step")
            charged = ": the card was charged, a person must act" if step else ""
            return "system", f"Payment of {order} failed{reason}{charged}"
        case "purchase_order.payment_minted":
            return "system", f"Stablecoin for {order} minted into the company's wallet"
        case "purchase_order.escrow_created":
            return "system", f"Escrow for {order} created on Solana"
        case "purchase_order.escrow_funded":
            return "system", f"Escrow for {order} funded: the payment waits for its milestones"
        case "purchase_order.payment_completed":
            return "system", f"Payment of {order} completed: the escrow released every milestone"
        case _:
            return "system", action


def _milestone_event(
    action: str, data: Mapping[str, Any], facts: _MilestoneFacts
) -> tuple[ActivityKind, str]:
    """One step of a PO payment's milestone release (`bido_back.payments.milestones`).

    Reads `purchase_order_ref`, `payment_id` and `milestone_index` (the milestone's name and
    share come from the payment's milestones), `doc_type`, `attempt`, `submission_id` (why
    it was sent back: the code stored with the decision), `failure_code` and
    `previous_failure_code`; all optional. Never a note, a file name or the reason's text,
    even when given.
    """
    ref = data.get("purchase_order_ref")
    order = f"purchase order {ref}" if isinstance(ref, str) and ref else "the purchase order"
    raw_index, payment = data.get("milestone_index"), data.get("payment_id")
    index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else None
    row = (
        facts.milestones.get((payment, index))
        if isinstance(payment, str) and index is not None
        else None
    )
    if row is not None:
        milestone = f"milestone {row.name}"
    elif index is not None:
        milestone = f"milestone {index + 1}"
    else:
        milestone = "a milestone"
    of = f"{milestone} of {order}"
    match action:
        case "purchase_order.factory_link_issued":
            return (
                "user",
                f"Factory link created for {order}: the factory sends its documents there",
            )
        case "purchase_order.factory_link_revoked":
            return "user", f"Factory link for {order} revoked"
        case "purchase_order.factory_order_accepted":
            return "user", _sentence(f"{order} and its milestones confirmed by the factory")
        case "purchase_order.milestone_document_added":
            label = _DOCUMENT_LABELS.get(str(data.get("doc_type")))
            return "user", f"Document added to {of}" + (f": {label}" if label else "")
        case "purchase_order.milestone_document_removed":
            label = _DOCUMENT_LABELS.get(str(data.get("doc_type")))
            return "user", f"Document removed from {of}" + (f": {label}" if label else "")
        case "purchase_order.milestone_submitted":
            attempt = data.get("attempt")
            again = " again" if isinstance(attempt, int) and attempt > 1 else ""
            return "user", _sentence(f"{of} sent for approval{again}")
        case "purchase_order.milestone_changes_requested":
            code = facts.rejections.get(str(data.get("submission_id")))
            why = _words(data.get("reason_code") or code)
            reason = f" ({why})" if why else ""
            return "user", f"Changes requested on {of}{reason}: the factory sends it again"
        case "purchase_order.milestone_releasing":
            return "system", _sentence(f"{of} approved: releasing it from the escrow")
        case "purchase_order.milestone_released":
            share = f" ({row.bps / 100:g}% of the total)" if row is not None else ""
            return "system", _sentence(f"{of} released from the escrow{share}")
        case "purchase_order.milestone_release_failed":
            # Refused when the approval ran (back to the factory) or failed on chain (for a
            # person): either way nothing left the escrow.
            why = _words(data.get("failure_code"))
            reason = f" ({why})" if why else ""
            return "system", _sentence(f"{of} not released{reason}: the money stays in the escrow")
        case "purchase_order.milestone_release_retried":
            why = _words(data.get("previous_failure_code"))
            after = f" after it failed ({why})" if why else ""
            return "user", f"Release of {of} retried{after}"
        case _:
            return "system", action


def _sentence(text: str) -> str:
    """`text` with a capital first letter (`str.capitalize` would lower the rest: `T1`)."""
    return text[:1].upper() + text[1:]


def _received_lines(data: Mapping[str, Any]) -> str:
    """`line 1: 300 of 500, line 2: 10 of 10` from the receipt event's lines."""
    lines = data.get("lines")
    if not isinstance(lines, list):
        return "no lines"
    parts: list[str] = []
    for item in cast(list[object], lines):
        if not isinstance(item, Mapping):
            continue
        line = cast(Mapping[str, object], item)
        parts.append(
            f"line {line.get('position', '?')}: {_value(line.get('received'))} of "
            f"{_value(line.get('ordered'))}"
        )
    return ", ".join(parts) or "no lines"


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


def _from(supplier: str | None) -> str:
    return f" from {supplier}" if supplier else ""


def _supplier(value: object, suppliers: Mapping[UUID, str]) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return suppliers.get(UUID(value))
    except ValueError:
        return None
