"""Why an email a negotiation mandate covers has not gone out yet, and until when.

Reads only (no decisions): the mandate service uses it to decide, and the
negotiation view uses the same answers to tell the buyer what each supplier's
pending email is waiting for. Kept apart from `bido_back.mandates.service` so
the negotiation package can import it without a cycle.

The daily cap counts the automatic follow-up and negotiation emails approved
for one supplier in the last 24 hours. The initial RFQ email does not count:
the cap is about how often Bido writes to a supplier during the negotiation,
and the invitation is what starts it.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.db.models import Approval, MailboxConnection, MandateSend, NegotiationMandate
from bido_back.followups.send import SEND_FOLLOWUP_EMAIL
from bido_back.mandates.schemas import ApproveMandateArgs
from bido_back.negotiation.send import SEND_NEGOTIATION_EMAIL
from bido_back.rfqs.send import SEND_RFQ_EMAIL

# The emails a mandate may approve. Awards and purchase orders are never among them.
AUTO_TOOLS: Final = frozenset({SEND_RFQ_EMAIL, SEND_FOLLOWUP_EMAIL, SEND_NEGOTIATION_EMAIL})
# The emails the daily cap counts.
CAPPED_TOOLS: Final = frozenset({SEND_FOLLOWUP_EMAIL, SEND_NEGOTIATION_EMAIL})
DAY: Final = timedelta(hours=24)

MandateWaitReason = Literal["held", "paused", "mailbox", "daily_cap", "in_bounds"]


@dataclass(frozen=True, slots=True)
class MandateWait:
    reason: MandateWaitReason
    until: datetime | None = None
    # The mandate's hold reason, for `held`.
    detail: str | None = None


async def mailbox_connected(session: AsyncSession, tenant_id: UUID, user_id: UUID) -> bool:
    """Whether `user_id` has an active mailbox connection (sends go out from it)."""
    found = await session.scalar(
        select(MailboxConnection.id)
        .where(
            MailboxConnection.tenant_id == tenant_id,
            MailboxConnection.user_id == user_id,
            MailboxConnection.status == "active",
        )
        .limit(1)
    )
    return found is not None


async def daily_cap_until(
    session: AsyncSession,
    mandate_id: UUID,
    invite_id: UUID | None,
    cap: int,
    now: datetime,
) -> datetime | None:
    """When the next automatic email to this supplier may be approved; None: now.

    The counted sends are the follow-up and negotiation emails this mandate
    approved for the invite in the 24 hours before `now`.
    """
    if invite_id is None:
        return None
    sent = list(
        await session.scalars(
            select(MandateSend.created_at)
            .where(
                MandateSend.mandate_id == mandate_id,
                MandateSend.invite_id == invite_id,
                MandateSend.outcome == "auto_approved",
                MandateSend.tool.in_(CAPPED_TOOLS),
                MandateSend.created_at > now - DAY,
            )
            .order_by(MandateSend.created_at)
        )
    )
    if len(sent) < cap:
        return None
    # A slot frees when enough of the counted sends leave the window.
    return sent[len(sent) - cap] + DAY


def approval_invite(approval: Approval) -> UUID | None:
    raw = approval.args.get("invite_id")
    try:
        return UUID(str(raw)) if raw is not None else None
    except ValueError:
        return None


async def live_mandate(session: AsyncSession, rfq_id: UUID) -> NegotiationMandate | None:
    return await session.scalar(
        select(NegotiationMandate)
        .where(NegotiationMandate.rfq_id == rfq_id, NegotiationMandate.status != "ended")
        .order_by(NegotiationMandate.created_at.desc())
        .limit(1)
    )


async def approval_waits(
    session: AsyncSession, rfq_id: UUID, approvals: Sequence[Approval], now: datetime
) -> dict[UUID, MandateWait]:
    """For pending approvals of an RFQ under a live mandate: what each one waits for.

    `held`: the mandate held it for a person (with the reason); `paused`: the
    mandate is paused; `mailbox`: in bounds, the buyer's mailbox is not
    connected; `daily_cap`: in bounds, waiting for the cap (`until`);
    `in_bounds`: the next automatic pass approves it. Approvals without a live
    mandate, or of tools a mandate never covers, are absent.
    """
    pending = [a for a in approvals if a.status == "pending" and a.tool in AUTO_TOOLS]
    if not pending:
        return {}
    mandate = await live_mandate(session, rfq_id)
    if mandate is None:
        return {}
    try:
        terms = ApproveMandateArgs.model_validate(mandate.terms)
    except ValidationError:
        return {}
    held = {
        approval_id: reason
        for approval_id, reason in (
            await session.execute(
                select(MandateSend.approval_id, MandateSend.reason).where(
                    MandateSend.mandate_id == mandate.id, MandateSend.outcome == "held"
                )
            )
        ).all()
    }
    connected = await mailbox_connected(session, mandate.tenant_id, terms.sender_user_id)
    waits: dict[UUID, MandateWait] = {}
    for approval in pending:
        if approval.id in held:
            waits[approval.id] = MandateWait("held", detail=held[approval.id])
        elif mandate.status != "active" or now >= mandate.deadline:
            waits[approval.id] = MandateWait("paused")
        elif not connected:
            waits[approval.id] = MandateWait("mailbox")
        elif approval.tool == SEND_RFQ_EMAIL:
            waits[approval.id] = MandateWait("in_bounds")
        else:
            until = await daily_cap_until(
                session,
                mandate.id,
                approval_invite(approval),
                terms.max_messages_per_supplier_per_day,
                now,
            )
            waits[approval.id] = (
                MandateWait("daily_cap", until) if until is not None else MandateWait("in_bounds")
            )
    return waits
