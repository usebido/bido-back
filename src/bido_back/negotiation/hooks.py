"""Negotiation hook quote extraction calls inside its own transaction (no agents).

`cancel_on_new_offer`: the supplier sent a new offer, so a counter-offer or
best-and-final request decided or still awaiting approval answers an older
offer. It is canceled (it never counts as a round) and its pending approval
expired; the next sweep plans the new offer.
"""

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.approvals.audit import approval_audit
from bido_back.db.models import Approval, NegotiationMessage


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
