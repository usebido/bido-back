"""Comparable quoted-price history, kept separate from paid-price evidence.

The default fallback is the lowest trusted, applicable price from the latest
snapshot of each older RFQ invite in the same tenant. Currency, part, unit and
quantity basis must agree. Unknown conversions, conditional bundles, alternatives
and review holds cannot create a negotiating target. This is an internal
historical reference, not a market benchmark or a supplier commitment.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.comparison.compute import LOW_CONFIDENCE, same_unit
from bido_back.comparison.loader import field_infos, negotiation_holds, parsed_decimal, parsed_text
from bido_back.db.models import Invite, Message, Proposal, ProposalLine, Rfq, RfqLine

PriceHistory = Callable[[AsyncSession, Rfq, str], Awaitable[Decimal | None]]

_WINDOW: Final = timedelta(days=365)


async def quoted_price_history(session: AsyncSession, rfq: Rfq, mpn: str) -> Decimal | None:
    now = datetime.now(UTC)
    requested = list(
        await session.scalars(
            select(RfqLine).where(
                RfqLine.rfq_id == rfq.id,
                func.upper(RfqLine.mpn) == mpn.upper(),
            )
        )
    )
    if not requested:
        return None
    quantities = [Decimal(str(q)) for line in requested for q in line.quantities]
    if not quantities or any(not same_unit(requested[0].unit, line.unit) for line in requested):
        return None
    quantity = min(quantities)
    latest = (
        select(Proposal.invite_id, func.max(Proposal.version).label("version"))
        .group_by(Proposal.invite_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Proposal, ProposalLine)
            .join(ProposalLine, ProposalLine.proposal_id == Proposal.id)
            .join(
                latest,
                (latest.c.invite_id == Proposal.invite_id) & (latest.c.version == Proposal.version),
            )
            .join(Invite, Invite.id == Proposal.invite_id)
            .join(Message, Message.id == Proposal.message_id)
            .join(RfqLine, RfqLine.id == ProposalLine.rfq_line_id)
            .where(
                Invite.rfq_id != rfq.id,
                func.upper(RfqLine.mpn) == mpn.upper(),
                Proposal.currency == rfq.base_currency,
                Message.occurred_at >= now - _WINDOW,
                Message.occurred_at <= now,
                ProposalLine.unit_price.is_not(None),
                ProposalLine.unit_price > 0,
                ProposalLine.is_alternative.is_(False),
                ProposalLine.needs_review.is_(False),
            )
        )
    ).all()
    prices: list[Decimal] = []
    for proposal, line in rows:
        if negotiation_holds(proposal.fields) or not same_unit(line.unit, requested[0].unit):
            continue
        if line.min_quantity is not None and line.min_quantity > quantity:
            continue
        if line.moq is not None and line.moq > quantity:
            continue
        if parsed_text(line.fields, "conditional_terms"):
            continue
        infos = {**field_infos(proposal.fields), **field_infos(line.fields)}
        if any(
            info.needs_review or (info.confidence is not None and info.confidence < LOW_CONFIDENCE)
            for info in infos.values()
            if not info.corrected
        ):
            continue
        basis = parsed_decimal(line.fields, "price_basis_quantity") or Decimal(1)
        if basis <= 0 or line.unit_price is None:
            continue
        # Targets share the comparison's goods-plus-tooling basis for this quantity.
        tooling = line.tooling_cost or Decimal(0)
        prices.append(line.unit_price / basis + tooling / quantity)
    return min(prices) if prices else None
