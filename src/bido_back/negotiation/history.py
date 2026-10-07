"""Price history for lines without a buyer target, behind a small seam.

`PriceHistory(session, rfq, mpn)` returns a reference unit price for a part in
the RFQ's base currency, or None. The default reads QUOTED prices from past
proposals of other RFQs in the same tenant (same MPN, same currency as the
RFQ's base currency, last 365 days) and returns the lowest. When purchase
history exists (import of past purchase orders), a reader over those actual
purchase prices can be passed instead; the strategy does not care where the
number comes from.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.db.models import Invite, Proposal, ProposalLine, Rfq, RfqLine

PriceHistory = Callable[[AsyncSession, Rfq, str], Awaitable[Decimal | None]]

_WINDOW: Final = timedelta(days=365)


async def quoted_price_history(session: AsyncSession, rfq: Rfq, mpn: str) -> Decimal | None:
    since = datetime.now(UTC) - _WINDOW
    return await session.scalar(
        select(func.min(ProposalLine.unit_price))
        .join(Proposal, Proposal.id == ProposalLine.proposal_id)
        .join(Invite, Invite.id == Proposal.invite_id)
        .join(RfqLine, RfqLine.id == ProposalLine.rfq_line_id)
        .where(
            Invite.rfq_id != rfq.id,
            func.upper(RfqLine.mpn) == mpn.upper(),
            Proposal.currency == rfq.base_currency,
            Proposal.created_at >= since,
            ProposalLine.unit_price.is_not(None),
            ProposalLine.unit_price > 0,
        )
    )
