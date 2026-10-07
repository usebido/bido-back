"""Load an RFQ's comparison inputs in the caller's tenant and compute it.

Reads, in one `tenant_session`: the RFQ and its lines, its invites with the
supplier names, the LATEST proposal version per invite and its lines, and
the ECB rates in effect today (per currency, the latest `rate_date` on or
before today; `fx_rates` is global reference data, not tenant-owned).
"""

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.auth import Principal
from bido_back.comparison.compute import (
    AwaitingSupplier,
    Comparison,
    FieldInfo,
    QuotedLine,
    Rate,
    RequestedLine,
    SupplierQuote,
    compute_comparison,
)
from bido_back.db import tenant_session
from bido_back.db.models import FxRate, Invite, Proposal, ProposalLine, Rfq, RfqLine, Supplier
from bido_back.rfqs.errors import NotFound

Db = AsyncEngine | async_sessionmaker[AsyncSession]


def field_infos(raw: Mapping[str, object]) -> dict[str, FieldInfo]:
    """Per-field provenance (`{"unit_price": {"confidence": 0.9, "needs_review": false}}`).

    `corrected` is set by `bido_back.corrections` when a person fixed the value.
    """
    infos: dict[str, FieldInfo] = {}
    for name, value in raw.items():
        if name.startswith("_") or not isinstance(value, dict):
            continue
        entry = cast(dict[str, object], value)
        confidence = entry.get("confidence")
        infos[name] = FieldInfo(
            confidence=float(confidence) if isinstance(confidence, int | float) else None,
            needs_review=entry.get("needs_review") is True,
            corrected=entry.get("corrected") is True,
        )
    return infos


async def load_rates(session: AsyncSession, today: date) -> dict[str, Rate]:
    rows = await session.execute(
        select(FxRate.currency, FxRate.per_eur, FxRate.rate_date)
        .where(FxRate.rate_date <= today)
        .ext(distinct_on(FxRate.currency))
        .order_by(FxRate.currency, FxRate.rate_date.desc())
    )
    return {
        currency: Rate(currency=currency, per_eur=Decimal(per_eur), rate_date=rate_date)
        for currency, per_eur, rate_date in rows
    }


async def load_comparison(db: Db, principal: Principal, rfq_id: UUID, today: date) -> Comparison:
    async with tenant_session(db, principal.tenant_id) as session:
        rfq = await session.scalar(select(Rfq).where(Rfq.id == rfq_id))
        if rfq is None:
            raise NotFound(f"rfq {rfq_id} not found")
        lines = [
            RequestedLine(
                id=line.id,
                position=line.position,
                description=line.description,
                mpn=line.mpn,
                unit=line.unit,
                quantities=tuple(Decimal(str(q)) for q in line.quantities),
                incoterm=line.incoterm,
            )
            for line in await session.scalars(
                select(RfqLine).where(RfqLine.rfq_id == rfq_id).order_by(RfqLine.position)
            )
        ]
        invites = list(
            (
                await session.execute(
                    select(Invite, Supplier.name)
                    .join(Supplier, Supplier.id == Invite.supplier_id)
                    .where(Invite.rfq_id == rfq_id)
                    .order_by(Invite.created_at, Invite.id)
                )
            ).all()
        )
        latest = (
            select(Proposal.invite_id, func.max(Proposal.version).label("version"))
            .join(Invite, Invite.id == Proposal.invite_id)
            .where(Invite.rfq_id == rfq_id)
            .group_by(Proposal.invite_id)
            .subquery()
        )
        proposals = {
            p.invite_id: p
            for p in await session.scalars(
                select(Proposal).join(
                    latest,
                    (latest.c.invite_id == Proposal.invite_id)
                    & (latest.c.version == Proposal.version),
                )
            )
        }
        proposal_lines: dict[UUID, list[ProposalLine]] = {p.id: [] for p in proposals.values()}
        if proposal_lines:
            for pl in await session.scalars(
                select(ProposalLine)
                .where(ProposalLine.proposal_id.in_(proposal_lines.keys()))
                .order_by(ProposalLine.created_at, ProposalLine.id)
            ):
                proposal_lines[pl.proposal_id].append(pl)
        rates = await load_rates(session, today)

    quotes: list[SupplierQuote] = []
    awaiting: list[AwaitingSupplier] = []
    for invite, supplier_name in invites:
        proposal = proposals.get(invite.id)
        if proposal is None:
            awaiting.append(
                AwaitingSupplier(
                    invite_id=invite.id,
                    supplier_id=invite.supplier_id,
                    supplier_name=supplier_name,
                    status=invite.status,
                )
            )
            continue
        quotes.append(
            SupplierQuote(
                invite_id=invite.id,
                supplier_id=invite.supplier_id,
                supplier_name=supplier_name,
                proposal_id=proposal.id,
                version=proposal.version,
                currency=proposal.currency,
                valid_until=proposal.valid_until,
                incoterm=proposal.incoterm,
                fields=field_infos(proposal.fields),
                lines=tuple(
                    QuotedLine(
                        id=pl.id,
                        rfq_line_id=pl.rfq_line_id,
                        offered_mpn=pl.offered_mpn,
                        is_alternative=pl.is_alternative,
                        min_quantity=pl.min_quantity,
                        unit_price=pl.unit_price,
                        unit=pl.unit,
                        tooling_cost=pl.tooling_cost,
                        moq=pl.moq,
                        lead_time_days=pl.lead_time_days,
                        needs_review=pl.needs_review,
                        fields=field_infos(pl.fields),
                    )
                    for pl in proposal_lines[proposal.id]
                ),
            )
        )
    return compute_comparison(
        rfq_id=rfq.id,
        base_currency=rfq.base_currency,
        lines=lines,
        quotes=quotes,
        rates=rates,
        today=today,
        awaiting=awaiting,
    )
