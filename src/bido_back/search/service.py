"""Search the caller's tenant: RFQs, requested lines, suppliers and quoted parts.

A case-insensitive substring match (ILIKE), with `%`, `_` and `\\` in the
query matched literally. Postgres has no trigram index here (pg_trgm is not in
the image), so this scans; fine at prototype volumes. RLS limits every table
to the caller's tenant, and `tenant_session` sets it.

Results are ordered by kind (RFQs, then lines, suppliers, quoted parts), then
newest first, and paged with limit / offset. Each hit says which field matched.
"""

from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import ColumnElement, Integer, String, Subquery, case, cast, func, literal, select
from sqlalchemy import null as sql_null
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import InstrumentedAttribute

from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import Invite, Proposal, ProposalLine, Rfq, RfqLine, Supplier

Db = AsyncEngine | async_sessionmaker[AsyncSession]

SearchKind = Literal["rfq", "rfq_line", "supplier", "quoted_part"]

MIN_QUERY: Final = 2
MAX_QUERY: Final = 100
MAX_LIMIT: Final = 50


class SearchHit(BaseModel):
    kind: SearchKind
    id: UUID
    rfq_id: UUID | None
    rfq_ref: str | None
    title: str
    detail: str | None
    matched: str
    created_at: datetime


class SearchResults(BaseModel):
    query: str
    total: int
    limit: int
    offset: int
    items: list[SearchHit]


def like_pattern(query: str) -> str:
    """`%query%` with LIKE's wildcards and escape character taken literally."""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _ilike(
    column: InstrumentedAttribute[str] | InstrumentedAttribute[str | None], pattern: str
) -> ColumnElement[bool]:
    return column.ilike(pattern, escape="\\")


def _hits(pattern: str) -> Subquery:
    rfqs = select(
        literal("rfq").label("kind"),
        literal(0, Integer).label("rank"),
        Rfq.id.label("id"),
        Rfq.id.label("rfq_id"),
        Rfq.ref.label("rfq_ref"),
        Rfq.title.label("title"),
        Rfq.status.label("detail"),
        case((_ilike(Rfq.ref, pattern), "ref"), else_="title").label("matched"),
        Rfq.created_at.label("created_at"),
    ).where(_ilike(Rfq.ref, pattern) | _ilike(Rfq.title, pattern))

    lines = (
        select(
            literal("rfq_line"),
            literal(1, Integer),
            RfqLine.id,
            Rfq.id,
            Rfq.ref,
            RfqLine.description,
            RfqLine.mpn,
            case((_ilike(RfqLine.description, pattern), "description"), else_="mpn"),
            Rfq.created_at,
        )
        .join(Rfq, Rfq.id == RfqLine.rfq_id)
        .where(_ilike(RfqLine.description, pattern) | _ilike(RfqLine.mpn, pattern))
    )

    suppliers = select(
        literal("supplier"),
        literal(2, Integer),
        Supplier.id,
        cast(sql_null(), Rfq.id.type),
        cast(sql_null(), String),
        Supplier.name,
        Supplier.country,
        literal("name"),
        Supplier.created_at,
    ).where(_ilike(Supplier.name, pattern))

    parts = (
        select(
            literal("quoted_part"),
            literal(3, Integer),
            ProposalLine.id,
            Rfq.id,
            Rfq.ref,
            func.coalesce(ProposalLine.offered_mpn, ""),
            Supplier.name,
            literal("offered_mpn"),
            ProposalLine.created_at,
        )
        .join(Proposal, Proposal.id == ProposalLine.proposal_id)
        .join(Invite, Invite.id == Proposal.invite_id)
        .join(Rfq, Rfq.id == Invite.rfq_id)
        .join(Supplier, Supplier.id == Invite.supplier_id)
        .where(_ilike(ProposalLine.offered_mpn, pattern))
    )
    return rfqs.union_all(lines, suppliers, parts).subquery("hits")


async def search(
    db: Db, principal: Principal, query: str, *, limit: int = 20, offset: int = 0
) -> SearchResults:
    query = query.strip()
    if not MIN_QUERY <= len(query) <= MAX_QUERY:
        raise ValueError(f"query must be {MIN_QUERY} to {MAX_QUERY} characters")
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    hits = _hits(like_pattern(query))

    async with tenant_session(db, principal.tenant_id) as session:
        total = await session.scalar(select(func.count()).select_from(hits)) or 0
        rows = await session.execute(
            select(hits)
            .order_by(hits.c.rank, hits.c.created_at.desc(), hits.c.id)
            .limit(limit)
            .offset(offset)
        )
        items = [
            SearchHit(
                kind=row.kind,
                id=row.id,
                rfq_id=row.rfq_id,
                rfq_ref=row.rfq_ref,
                title=row.title,
                detail=row.detail,
                matched=row.matched,
                created_at=row.created_at,
            )
            for row in rows
        ]
    return SearchResults(query=query, total=total, limit=limit, offset=offset, items=items)
