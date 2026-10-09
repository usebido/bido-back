"""Follow-up hooks intake and quote extraction call inside their own transaction (no agents).

- `cancel_on_reply`: the supplier wrote back, so a proposed chase or reminder is
  moot: canceled and its pending approval expired. A proposed clarification
  stays: a reply without an offer ("we will answer tomorrow") changes nothing it
  asks; a reply with a newer offer makes the sweep cancel it (`newer_offer`).
- `pause_on_question`: the supplier asked the buyer something, so every
  proposed automatic follow-up (chase, clarification, reminder) is canceled
  until the buyer answers. The buyer's own answer email is never touched.
- `pause_on_auto_reply`: an out-of-office arrived, so chases wait until the
  stated return date (`bido_back.followups.autoreply`).

Canceling a clarification withdraws its asks: nothing was asked, so the items
may be asked again later (`bido_back.followups.ledger`).
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.approvals.audit import approval_audit
from bido_back.db.models import Approval, FollowupMessage, Invite, Supplier
from bido_back.followups.autoreply import paused_until
from bido_back.followups.ledger import withdraw_asks

# Follow-ups Bido proposes on its own (the buyer's answer to a question is not one).
AUTOMATIC: Final = ("chase", "clarification", "reminder")


async def cancel_followups(
    session: AsyncSession, rows: Sequence[FollowupMessage], reason: str
) -> int:
    approval_ids = [r.approval_id for r in rows if r.approval_id is not None]
    for row in rows:
        row.status = "canceled"
        row.canceled_reason = reason
    await withdraw_asks(session, [r.id for r in rows if r.kind == "clarification"])
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


async def _proposed(
    session: AsyncSession, invite_id: UUID, kinds: Sequence[str]
) -> list[FollowupMessage]:
    return list(
        await session.scalars(
            select(FollowupMessage)
            .where(
                FollowupMessage.invite_id == invite_id,
                FollowupMessage.status == "proposed",
                FollowupMessage.kind.in_(kinds),
            )
            .with_for_update()
        )
    )


async def cancel_on_reply(session: AsyncSession, invite_id: UUID) -> int:
    """The supplier answered: a proposed chase or reminder for the invite is moot."""
    rows = await _proposed(session, invite_id, ("chase", "reminder"))
    return await cancel_followups(session, rows, "supplier_replied")


async def pause_on_question(session: AsyncSession, invite_id: UUID) -> int:
    """The supplier asked the buyer something: no automatic follow-up until answered."""
    rows = await _proposed(session, invite_id, AUTOMATIC)
    return await cancel_followups(session, rows, "supplier_question")


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
