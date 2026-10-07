"""Follow-up hooks intake calls inside its own transaction (kept light: no agents).

- `cancel_on_reply`: the supplier answered, so every proposed follow-up for
  the invite is canceled and its pending approval expired.
- `pause_on_auto_reply`: an out-of-office arrived, so chases wait until the
  stated return date (`bido_back.followups.autoreply`).
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.approvals.audit import approval_audit
from bido_back.db.models import Approval, FollowupMessage, Invite, Supplier
from bido_back.followups.autoreply import paused_until


async def cancel_followups(
    session: AsyncSession, rows: Sequence[FollowupMessage], reason: str
) -> int:
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


async def cancel_on_reply(session: AsyncSession, invite_id: UUID) -> int:
    """The supplier answered: every proposed follow-up for the invite is moot."""
    rows = list(
        await session.scalars(
            select(FollowupMessage)
            .where(FollowupMessage.invite_id == invite_id, FollowupMessage.status == "proposed")
            .with_for_update()
        )
    )
    return await cancel_followups(session, rows, "supplier_replied")


async def pause_on_auto_reply(
    session: AsyncSession, invite_id: UUID, text: str, received_at: datetime
) -> datetime | None:
    """An auto-reply: hold chases until the stated return (or a few business days)."""
    row = (
        await session.execute(
            select(Invite, Supplier.country)
            .join(Supplier, Supplier.id == Invite.supplier_id)
            .where(Invite.id == invite_id)
            .with_for_update(of=Invite)
        )
    ).one_or_none()
    if row is None:
        return None
    invite, country = row
    until = paused_until(text, received_at, country)
    current = invite.followups_paused_until
    if current is None or until > current:
        invite.followups_paused_until = until
        await session.flush()
    return invite.followups_paused_until
