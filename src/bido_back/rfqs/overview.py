"""The RFQ list as the list screen needs it: stage, next step, suppliers, best total.

Everything is loaded in a fixed number of batched queries per page, never one
query per RFQ, except the best total: it comes from the full comparison
(`load_comparison`, FX and rules included), so it is computed only for the
RFQs on the page that have at least one proposal.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final, cast
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.auth import Principal
from bido_back.comparison.compute import best_first_tier_total
from bido_back.comparison.loader import load_comparison
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    Award,
    Contact,
    Invite,
    IssuedPurchaseOrder,
    Proposal,
    Rfq,
    RfqLine,
    Supplier,
    User,
)
from bido_back.rfqs.schemas import RfqNextStep, RfqStage, RfqStatus, RfqSummary

type Db = AsyncEngine | async_sessionmaker[AsyncSession]

DEFAULT_LIMIT: Final = 100
MAX_LIMIT: Final = 500

# Invites that still owe an answer once sent.
_WAITING: Final = frozenset({"sent"})
_ANSWERED: Final = frozenset({"responded", "declined"})


@dataclass(slots=True)
class RfqFacts:
    suppliers: list[str] = field(default_factory=list[str])
    invite_statuses: list[str] = field(default_factory=list[str])
    pending_approvals: int = 0
    awarded: bool = False
    pos_total: int = 0
    pos_sent: int = 0


@dataclass(frozen=True, slots=True)
class RfqPage:
    items: list[RfqSummary]
    total: int


def derive_stage(rfq_status: str, negotiation_status: str, facts: RfqFacts) -> RfqStage:
    """Where the RFQ is in the journey."""
    if rfq_status == "cancelled":
        return "cancelled"
    if rfq_status == "draft":
        return "draft"
    if facts.awarded:
        if facts.pos_total and facts.pos_sent == facts.pos_total:
            return "completed"
        return "awarded"
    if rfq_status == "closed":
        return "completed"
    statuses = facts.invite_statuses
    all_answered = bool(statuses) and all(s in _ANSWERED for s in statuses)
    if negotiation_status in ("ready_to_close", "closed") or all_answered:
        return "review"
    return "bidding"


def derive_next_step(
    rfq_status: str, negotiation_status: str, facts: RfqFacts
) -> RfqNextStep | None:
    """What the buyer should do next, or None when nothing is on them."""
    if rfq_status == "cancelled":
        return None
    if facts.pending_approvals:
        return "approve"
    if rfq_status == "draft":
        return "send RFQ"
    if facts.awarded:
        return "send PO" if facts.pos_sent < facts.pos_total else None
    if rfq_status != "open":
        return None
    if negotiation_status == "ready_to_close":
        return "close negotiation"
    if negotiation_status == "closed":
        return "award"
    if any(s in _WAITING for s in facts.invite_statuses):
        return "waiting for suppliers"
    if any(s == "responded" for s in facts.invite_statuses):
        return "review quotes"
    return None


async def list_rfq_overviews(
    db: Db,
    principal: Principal,
    *,
    today: date,
    status: RfqStatus | None = None,
    stage: RfqStage | None = None,
    q: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> RfqPage:
    """A page of the RFQ list, newest first, with the total that matched."""
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    async with tenant_session(db, principal.tenant_id) as session:
        query = select(Rfq)
        if status is not None:
            query = query.where(Rfq.status == status)
        if q:
            pattern = f"%{_escape_like(q.strip())}%"
            query = query.where(
                or_(Rfq.ref.ilike(pattern, escape="\\"), Rfq.title.ilike(pattern, escape="\\"))
            )
        query = query.order_by(Rfq.created_at.desc(), Rfq.id)
        if stage is None:
            total = await session.scalar(select(func.count()).select_from(query.subquery()))
            rfqs = list(await session.scalars(query.limit(limit).offset(offset)))
            facts = await _facts(session, [r.id for r in rfqs])
        else:
            # The stage is derived, so filter after deriving it (bounded by MAX_RFQS).
            candidates = list(await session.scalars(query.limit(_MAX_STAGE_SCAN)))
            facts = await _facts(session, [r.id for r in candidates])
            matching = [
                r
                for r in candidates
                if derive_stage(r.status, r.negotiation_status, facts[r.id]) == stage
            ]
            total = len(matching)
            rfqs = matching[offset : offset + limit]
        need_by = await _need_by(session, [r.id for r in rfqs])
        owners = await owner_names(session, {r.created_by for r in rfqs})
        quoted = await _quoted(session, [r.id for r in rfqs])

    best: dict[UUID, Decimal | None] = {}
    for rfq in rfqs:
        if rfq.id in quoted:
            best[rfq.id] = best_first_tier_total(
                await load_comparison(db, principal, rfq.id, today)
            )

    items = [
        RfqSummary(
            id=rfq.id,
            ref=rfq.ref,
            title=rfq.title,
            status=cast(RfqStatus, rfq.status),
            response_due=rfq.response_due,
            created_at=rfq.created_at,
            stage=derive_stage(rfq.status, rfq.negotiation_status, facts[rfq.id]),
            next_step=derive_next_step(rfq.status, rfq.negotiation_status, facts[rfq.id]),
            supplier_names=facts[rfq.id].suppliers,
            invited_count=len(facts[rfq.id].invite_statuses),
            responses_count=sum(s == "responded" for s in facts[rfq.id].invite_statuses),
            best_total_base=best.get(rfq.id),
            base_currency=rfq.base_currency,
            need_by=need_by.get(rfq.id),
            owner=owners.get(rfq.created_by),
        )
        for rfq in rfqs
    ]
    return RfqPage(items=items, total=total or 0)


_MAX_STAGE_SCAN: Final = 2000


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _facts(session: AsyncSession, rfq_ids: list[UUID]) -> dict[UUID, RfqFacts]:
    facts = {rfq_id: RfqFacts() for rfq_id in rfq_ids}
    if not rfq_ids:
        return facts
    invite_rfq: dict[UUID, UUID] = {}
    for invite_id, rfq_id, name, status in await session.execute(
        select(Invite.id, Invite.rfq_id, Supplier.name, Invite.status)
        .join(Supplier, Supplier.id == Invite.supplier_id)
        .where(Invite.rfq_id.in_(rfq_ids))
        .order_by(Invite.created_at, Invite.id)
    ):
        invite_rfq[invite_id] = rfq_id
        facts[rfq_id].suppliers.append(name)
        facts[rfq_id].invite_statuses.append(status)

    rfq_keys = [str(i) for i in rfq_ids]
    invite_keys = [str(i) for i in invite_rfq]
    rfq_arg = Approval.args["rfq_id"].astext
    invite_arg = Approval.args["invite_id"].astext
    conditions = [rfq_arg.in_(rfq_keys)]
    if invite_keys:
        conditions.append(invite_arg.in_(invite_keys))
    for rfq_key, invite_key in await session.execute(
        select(rfq_arg, invite_arg).where(Approval.status == "pending", or_(*conditions))
    ):
        rfq_id = _rfq_for(rfq_key, invite_key, invite_rfq, facts)
        if rfq_id is not None:
            facts[rfq_id].pending_approvals += 1

    for (rfq_id,) in await session.execute(select(Award.rfq_id).where(Award.rfq_id.in_(rfq_ids))):
        facts[rfq_id].awarded = True
    for rfq_id, sent_at in await session.execute(
        select(IssuedPurchaseOrder.rfq_id, IssuedPurchaseOrder.sent_at).where(
            IssuedPurchaseOrder.rfq_id.in_(rfq_ids)
        )
    ):
        facts[rfq_id].pos_total += 1
        facts[rfq_id].pos_sent += sent_at is not None
    return facts


def _rfq_for(
    rfq_key: str | None,
    invite_key: str | None,
    invite_rfq: dict[UUID, UUID],
    facts: dict[UUID, RfqFacts],
) -> UUID | None:
    for key in (rfq_key, invite_key):
        if key is None:
            continue
        try:
            value = UUID(key)
        except ValueError:
            continue
        if value in facts:
            return value
        if value in invite_rfq:
            return invite_rfq[value]
    return None


async def _need_by(session: AsyncSession, rfq_ids: list[UUID]) -> dict[UUID, date | None]:
    if not rfq_ids:
        return {}
    return {
        rfq_id: earliest
        for rfq_id, earliest in await session.execute(
            select(RfqLine.rfq_id, func.min(RfqLine.need_by))
            .where(RfqLine.rfq_id.in_(rfq_ids))
            .group_by(RfqLine.rfq_id)
        )
    }


async def _quoted(session: AsyncSession, rfq_ids: list[UUID]) -> set[UUID]:
    if not rfq_ids:
        return set()
    return set(
        await session.scalars(
            select(Invite.rfq_id)
            .join(Proposal, Proposal.invite_id == Invite.id)
            .where(Invite.rfq_id.in_(rfq_ids))
            .distinct()
        )
    )


async def owner_names(session: AsyncSession, user_ids: Iterable[UUID]) -> dict[UUID, str]:
    """A user's name from their `buyer` contact (matched by email), else their email."""
    ids = set(user_ids)
    if not ids:
        return {}
    users: dict[UUID, str] = {
        user_id: email
        for user_id, email in await session.execute(
            select(User.id, User.email).where(User.id.in_(ids))
        )
    }
    names: dict[str, str] = {}
    for email, name in await session.execute(
        select(Contact.email, Contact.name)
        .where(
            Contact.kind == "buyer",
            Contact.email.in_(set(users.values())),
            Contact.pseudonymized_at.is_(None),
            Contact.name.is_not(None),
        )
        .order_by(Contact.created_at.desc())
    ):
        if email is not None and name is not None:
            names[email] = name
    return {user_id: names.get(email, email) for user_id, email in users.items()}


__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "RfqPage", "list_rfq_overviews", "owner_names"]
