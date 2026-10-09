"""Tell the supplier a sent purchase order is cancelled: `send_po_cancellation`.

Cancelling is the buyer's decision and takes effect in Bido at once
(`lifecycle.cancel`). A supplier who already received the order must be told,
or they ship it: when the cancelled order had been sent, the cancel proposes one
email in the RFQ thread, from the mailbox of the buyer who sent the RFQ, written
by code in the supplier's language and quoting the PO number. Like every email,
it goes out only after approval (and the recipient guard).

Send once: the Message-ID is derived from the PO (a retry reuses it, and the
mailbox looks it up in Sent before sending again), and a
`purchase_order.cancellation_sent` event makes a later run a no-op.
"""

import logging
from typing import Final, cast
from uuid import UUID, uuid5

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.approvals import ApprovalProposal
from bido_back.agents.context import ToolContext
from bido_back.agents.events import args_hash
from bido_back.agents.tools import Tool
from bido_back.approvals.errors import ApprovalConflict
from bido_back.approvals.executor import ActToolRegistry
from bido_back.approvals.sink import PostgresApprovalSink
from bido_back.auth import Principal
from bido_back.awards.schemas import (
    ApprovalRequested,
    SendThreadEmailArgs,
    ThreadEmailResult,
)
from bido_back.awards.thread import Outgoing, deliver, thread_of
from bido_back.db import tenant_session
from bido_back.db.models import Approval, AuditEvent, IssuedPurchaseOrder, Tenant, User
from bido_back.rfqs.audit import rfq_event
from bido_back.rfqs.errors import InvalidState, NotFound
from bido_back.rfqs.language import supplier_email_language
from bido_back.rfqs.mailbox import MailboxSenderFactory
from bido_back.rfqs.send import RfqActDeps
from bido_back.rfqs.service import buyer_display_name

logger = logging.getLogger(__name__)

type Db = AsyncEngine | async_sessionmaker[AsyncSession]

SEND_PO_CANCELLATION: Final = "send_po_cancellation"
PO_CANCELLATION_SENT: Final = "purchase_order.cancellation_sent"
_NAMESPACE: Final = UUID("9d6c2b1a-3e4f-4a5b-8c7d-6e5f4a3b2c1d")
_SUBJECT_MAX: Final = 300

_TEXT: Final[dict[str, tuple[str, str, str, str]]] = {
    # greeting, the cancellation, the ask, sign-off
    "en": (
        "Dear {supplier},",
        "Please cancel our purchase order {po_ref} (our request {rfq_ref}). Do not ship or "
        "invoice it.",
        "Please confirm the cancellation by replying to this email.",
        "Kind regards,",
    ),
    "pt": (
        "Prezados {supplier},",
        "Por favor, cancelem nosso pedido de compra {po_ref} (nossa solicitação {rfq_ref}). Não "
        "o enviem nem o faturem.",
        "Por favor, confirmem o cancelamento respondendo a este e-mail.",
        "Atenciosamente,",
    ),
    "de": (
        "Sehr geehrte Damen und Herren von {supplier},",
        "bitte stornieren Sie unsere Bestellung {po_ref} (unsere Anfrage {rfq_ref}). Bitte "
        "weder liefern noch berechnen.",
        "Bitte bestätigen Sie die Stornierung per Antwort auf diese E-Mail.",
        "Mit freundlichen Grüßen",
    ),
    "fr": (
        "Madame, Monsieur,",
        "Merci d'annuler notre bon de commande {po_ref} (notre demande {rfq_ref}). Merci de ne "
        "pas l'expédier ni le facturer.",
        "Merci de confirmer l'annulation en répondant à cet e-mail.",
        "Cordialement,",
    ),
    "it": (
        "Gentili {supplier},",
        "vi preghiamo di annullare il nostro ordine di acquisto {po_ref} (nostra richiesta "
        "{rfq_ref}). Non speditelo e non fatturatelo.",
        "Vi preghiamo di confermare l'annullamento rispondendo a questa e-mail.",
        "Cordiali saluti,",
    ),
    "es": (
        "Estimados {supplier}:",
        "Les rogamos cancelen nuestra orden de compra {po_ref} (nuestra solicitud {rfq_ref}). "
        "No la envíen ni la facturen.",
        "Les rogamos confirmen la cancelación respondiendo a este correo.",
        "Saludos cordiales,",
    ),
}


class SendPoCancellationArgs(SendThreadEmailArgs):
    purchase_order_id: UUID
    purchase_order_ref: str


def cancellation_proposal_ids(po_id: UUID) -> tuple[UUID, str]:
    call_id = f"{SEND_PO_CANCELLATION}:{po_id}"
    return uuid5(_NAMESPACE, call_id), call_id


def cancellation_body(
    language: str, *, supplier: str, po_ref: str, rfq_ref: str, buyer_name: str, company: str
) -> str:
    """The cancellation email, in the supplier's language (else English)."""
    greeting, cancel, ask, sign_off = _TEXT.get(language.lower()[:2], _TEXT["en"])
    values = {"supplier": supplier, "po_ref": po_ref, "rfq_ref": rfq_ref}
    signature = buyer_name if buyer_name == company else f"{buyer_name}\n{company}"
    return (
        f"{greeting.format(**values)}\n\n{cancel.format(**values)}\n\n{ask}\n\n"
        f"{sign_off}\n{signature}"
    )


async def request_cancellation_notice(
    db: Db, principal: Principal, po_id: UUID, mailboxes: MailboxSenderFactory
) -> ApprovalRequested | None:
    """Propose telling the supplier about a cancelled order; None if it was never sent."""
    async with tenant_session(db, principal.tenant_id) as session:
        po = await session.get(IssuedPurchaseOrder, po_id)
        if po is None:
            raise NotFound(f"purchase order {po_id} not found")
        if po.fulfilment_state != "cancelled":
            raise InvalidState(f"purchase order {po_id} is not cancelled")
        if po.sent_at is None:
            return None
        call_id = cancellation_proposal_ids(po.id)[1]
        existing = await session.scalar(
            select(Approval.id).where(
                Approval.tool == SEND_PO_CANCELLATION, Approval.tool_call_id == call_id
            )
        )
        if existing is not None:
            return ApprovalRequested(approval_id=existing)
        thread = await thread_of(session, po.invite_id)
        sender = await session.get(User, thread.sender_user_id) if thread.sender_user_id else None
        if sender is None or not thread.contact_email or thread.invite.thread_message_id is None:
            raise InvalidState(f"purchase order {po_id}: the RFQ thread has no sender")
        await mailboxes(session, principal.tenant_id, sender.id)
        tenant = await session.get(Tenant, principal.tenant_id)
        if tenant is None:
            raise NotFound("tenant not found")
        body = cancellation_body(
            supplier_email_language(thread.supplier),
            supplier=thread.supplier.name,
            po_ref=po.ref,
            rfq_ref=thread.rfq.ref,
            buyer_name=await buyer_display_name(session, sender, tenant.name),
            company=tenant.name,
        )
        args = SendPoCancellationArgs(
            purchase_order_id=po.id,
            purchase_order_ref=po.ref,
            invite_id=po.invite_id,
            sender_user_id=sender.id,
            to=[thread.contact_email],
            subject=f"{thread.subject} – {po.ref}"[:_SUBJECT_MAX],
            body=body,
        )
    payload = cast(dict[str, JsonValue], args.model_dump(mode="json"))
    run_id, call_id = cancellation_proposal_ids(po_id)
    try:
        await PostgresApprovalSink(db).request_approval(
            ApprovalProposal(
                tenant_id=principal.tenant_id,
                run_id=run_id,
                session_id=f"rfq:{args.invite_id}",
                tool_call_id=call_id,
                tool=SEND_PO_CANCELLATION,
                args=payload,
                args_hash=args_hash(payload),
                requested_by=principal.user_id,
            )
        )
    except ApprovalConflict:
        raise InvalidState(f"{call_id}: another request is already waiting") from None
    async with tenant_session(db, principal.tenant_id) as session:
        approval_id = await session.scalar(
            select(Approval.id).where(
                Approval.run_id == str(run_id), Approval.tool_call_id == call_id
            )
        )
    if approval_id is None:
        raise InvalidState(f"{call_id}: the approval was not recorded")
    return ApprovalRequested(approval_id=approval_id)


async def cancellation_sent(session: AsyncSession, po_id: UUID) -> bool:
    """The supplier was told the order is cancelled."""
    found = await session.scalar(
        select(AuditEvent.id)
        .where(
            AuditEvent.action == PO_CANCELLATION_SENT,
            AuditEvent.payload["purchase_order_id"].astext == str(po_id),
        )
        .limit(1)
    )
    return found is not None


class _SendPoCancellation:
    def __init__(self, deps: RfqActDeps) -> None:
        self._deps = deps

    async def __call__(self, ctx: ToolContext, args: SendPoCancellationArgs) -> ThreadEmailResult:
        message_id = f"<po-cancel.{uuid5(_NAMESPACE, str(args.purchase_order_id))}@" + (
            f"{self._deps.email_domain}>"
        )
        async with tenant_session(self._deps.db, ctx.tenant_id) as session:
            po = await session.scalar(
                select(IssuedPurchaseOrder)
                .where(IssuedPurchaseOrder.id == args.purchase_order_id)
                .with_for_update()
            )
            if po is None or po.invite_id != args.invite_id:
                raise NotFound(f"purchase order {args.purchase_order_id} not found")
            if await cancellation_sent(session, po.id):
                return ThreadEmailResult(outcome="already_sent", message_id=message_id)
            if po.fulfilment_state != "cancelled":
                return ThreadEmailResult(outcome="skipped")
            thread = await thread_of(session, po.invite_id)
            if thread.invite.thread_message_id is None:
                return ThreadEmailResult(outcome="skipped")
            sent_id, message_row = await deliver(
                session,
                self._deps,
                ctx,
                Outgoing(
                    invite_id=args.invite_id,
                    sender_user_id=args.sender_user_id,
                    to=tuple(args.to),
                    cc=tuple(args.cc),
                    subject=args.subject,
                    body=args.body,
                    message_id=message_id,
                    thread_message_id=thread.invite.thread_message_id,
                    # The Message-ID is fixed per PO: any earlier attempt may have sent it.
                    possibly_sent=True,
                ),
            )
            session.add(
                rfq_event(
                    ctx.tenant_id,
                    ctx.user_id,
                    PO_CANCELLATION_SENT,
                    po.rfq_id,
                    purchase_order_id=po.id,
                    purchase_order_ref=po.ref,
                    invite_id=po.invite_id,
                    message_row_id=message_row,
                    run_id=ctx.run_id,
                )
            )
            await session.flush()
        logger.info("sent po cancellation %s tenant=%s", args.purchase_order_id, ctx.tenant_id)
        return ThreadEmailResult(outcome="sent", message_id=sent_id)


def cancellation_tool(deps: RfqActDeps) -> Tool[SendPoCancellationArgs, ThreadEmailResult]:
    return Tool(
        name=SEND_PO_CANCELLATION,
        description="Tell the supplier, in the RFQ thread, that a sent order is cancelled.",
        input_model=SendPoCancellationArgs,
        output_model=ThreadEmailResult,
        kind="act",
        handler=_SendPoCancellation(deps),
    )


def register_cancellation_act_tool(registry: ActToolRegistry, deps: RfqActDeps) -> None:
    registry.register(cancellation_tool(deps))
