"""Negotiation hooks quote extraction calls inside its own transaction (no agents).

`cancel_on_new_offer`: the supplier sent a new offer, so a counter-offer or
best-and-final request decided or still awaiting approval answers an older
offer. It is canceled (it never counts as a round) and its pending approval
expired; the re-plan (or the next sweep) plans the new offer.

`request_replan`: a reply was stored for an RFQ whose negotiation is open, so
its plan should move now rather than at the next 15-minute sweep. It marks the
RFQ (`negotiation_replan_requested_at`) in the caller's transaction and says
whether to enqueue a `negotiation.replan` job (`replan_envelope`) after the
commit: only when no re-plan is pending yet, so a burst of replies enqueues one.
"""

from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.approvals.audit import approval_audit
from bido_back.db.models import Approval, NegotiationMessage, Rfq
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import NEGOTIATION_REPLAN

# The negotiation statuses the sweep plans (`bido_back.negotiation.service._ACTIVE`).
_PLANNED: Final = ("open", "ready_to_close")
# A request still pending this long after the last reply most likely lost its job (an
# enqueue that failed after the commit): the next reply enqueues one again. The sweep
# heals it anyway.
REPLAN_LOST_AFTER: Final = timedelta(minutes=5)


async def cancel_on_new_offer(
    session: AsyncSession, invite_id: UUID, *, reason: str = "newer_offer"
) -> int:
    rows = list(
        await session.scalars(
            select(NegotiationMessage)
            .where(
                NegotiationMessage.invite_id == invite_id,
                NegotiationMessage.status.in_(("decided", "proposed")),
            )
            .with_for_update()
        )
    )
    approval_ids = [r.approval_id for r in rows if r.approval_id is not None]
    for row in rows:
        row.status = "canceled"
        row.canceled_reason = reason
    if approval_ids:
        expired = list(
            await session.scalars(
                update(Approval)
                .where(Approval.id.in_(approval_ids), Approval.status == "pending")
                .values(status="expired")
                .returning(Approval)
            )
        )
        for approval in expired:
            session.add(approval_audit(approval, "approval.expired", None))
    await session.flush()
    return len(rows)


def request_replan(rfq: Rfq, now: datetime) -> bool:
    """Ask for the RFQ's negotiation to be planned again now; True: enqueue the job.

    The caller holds the RFQ's row lock and enqueues `replan_envelope` only after
    its transaction commits. Nothing is asked unless the RFQ is open and its
    negotiation is being planned. The request is bumped to `now` on every reply, so a
    re-plan already running sees it and runs once more."""
    if rfq.status != "open" or rfq.negotiation_status not in _PLANNED:
        return False
    previous = rfq.negotiation_replan_requested_at
    rfq.negotiation_replan_requested_at = now
    return previous is None or now - previous >= REPLAN_LOST_AFTER


def replan_envelope(tenant_id: UUID, rfq_id: UUID, requested_at: datetime) -> Envelope:
    """The `negotiation.replan` job for one RFQ; keyed by the request it answers."""
    return Envelope(
        type=NEGOTIATION_REPLAN,
        tenant_id=tenant_id,
        idempotency_key=f"{NEGOTIATION_REPLAN}:{rfq_id}:{requested_at.isoformat()}",
        payload={"rfq_id": str(rfq_id)},
    )
