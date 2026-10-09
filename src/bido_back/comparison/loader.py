"""Load an RFQ's comparison inputs in the caller's tenant and compute it.

Reads, in one `tenant_session`: the RFQ and its lines, its invites with the
supplier names, the LATEST proposal version per invite and its lines, and
the ECB rates in effect today (per currency, the latest `rate_date` on or
before today; `fx_rates` is global reference data, not tenant-owned). Supplier
history per line is the imported purchase history, or, without an imported
observation in the policy window, what the line's source assessment measured
(`bido_back.comparison.assessment_history`). Then applies the sector rules and
recommendations (`bido_back.comparison.rules`).
"""

from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import cast
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.auth import Principal
from bido_back.comparison.assessment_history import with_assessment_history
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
from bido_back.comparison.policy import (
    ComparisonPolicy,
    PurchaseObservation,
    SupplierPerformance,
    summarize_performance,
)
from bido_back.comparison.rules import apply_rules
from bido_back.db import tenant_session
from bido_back.db.models import (
    FieldCorrection,
    FxRate,
    Invite,
    Item,
    Message,
    PoLine,
    Proposal,
    ProposalLine,
    PurchaseOrder,
    Rfq,
    RfqLine,
    Supplier,
)
from bido_back.rfqs.errors import NotFound

Db = AsyncEngine | async_sessionmaker[AsyncSession]


def parsed_text(raw: Mapping[str, object], name: str) -> str | None:
    """A grounded text value from the provenance jsonb, or None when unusable."""
    entry = raw.get(name)
    if not isinstance(entry, dict):
        return None
    values = cast(dict[str, object], entry)
    parsed = values.get("parsed")
    if not isinstance(parsed, str) or values.get("needs_review") is True:
        return None
    return parsed


def parsed_decimal(raw: Mapping[str, object], name: str) -> Decimal | None:
    value = parsed_text(raw, name)
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None


def negotiation_holds(raw: Mapping[str, object]) -> tuple[str, ...]:
    metadata = raw.get("_offer_state")
    if not isinstance(metadata, dict):
        return ()
    state = cast(dict[str, object], metadata)
    reasons: list[str] = []
    if (
        state.get("reply_kind") == "clarification"
        or state.get("requires_clarification") is True
        or state.get("technical_questions")
    ):
        reasons.append("supplier_clarification_requires_buyer")
    if state.get("reply_kind") == "withdrawal" and state.get("fully_withdrawn", True) is not False:
        reasons.append("offer_withdrawn")
    if state.get("issues"):
        reasons.append("offer_state_requires_review")
    return tuple(reasons)


def evidence_messages(raw: Mapping[str, object]) -> tuple[UUID, ...]:
    metadata = raw.get("_offer_state")
    if not isinstance(metadata, dict):
        return ()
    ids = cast(dict[str, object], metadata).get("message_ids")
    if not isinstance(ids, list):
        return ()
    result: list[UUID] = []
    for value in cast(list[object], ids):
        if not isinstance(value, str):
            continue
        try:
            result.append(UUID(value))
        except ValueError:
            continue
    return tuple(result)


async def load_performance(
    session: AsyncSession,
    lines: Sequence[RequestedLine],
    supplier_ids: Sequence[UUID],
    *,
    today: date,
    policy: ComparisonPolicy,
) -> dict[UUID, dict[UUID, SupplierPerformance]]:
    """Match exact item identifiers; no fuzzy join across different components."""
    mpns = {line.mpn.strip().upper() for line in lines if line.mpn}
    parts = {line.part_number.strip().upper() for line in lines if line.part_number}
    if not supplier_ids or not (mpns or parts):
        return {}
    rows = (
        await session.execute(
            select(PurchaseOrder, PoLine, Item)
            .join(PoLine, PoLine.purchase_order_id == PurchaseOrder.id)
            .join(Item, Item.id == PoLine.item_id)
            .where(
                PurchaseOrder.supplier_id.in_(supplier_ids),
                PurchaseOrder.ordered_at <= today,
                or_(
                    func.upper(func.trim(Item.mpn)).in_(mpns),
                    func.upper(func.trim(Item.part_number)).in_(parts),
                ),
            )
            .order_by(PurchaseOrder.ordered_at, PoLine.id)
        )
    ).all()
    result: dict[UUID, dict[UUID, SupplierPerformance]] = {}
    for supplier_id in supplier_ids:
        by_line: dict[UUID, SupplierPerformance] = {}
        for line in lines:
            observations: list[PurchaseObservation] = []
            for order, row, item in rows:
                exact = (
                    line.mpn is not None
                    and item.mpn is not None
                    and line.mpn.strip().upper() == item.mpn.strip().upper()
                ) or (
                    line.part_number is not None
                    and line.part_number.strip().upper() == item.part_number.strip().upper()
                )
                conflict = (
                    line.mpn is not None
                    and item.mpn is not None
                    and line.mpn.strip().upper() != item.mpn.strip().upper()
                ) or (
                    line.part_number is not None
                    and line.part_number.strip().upper() != item.part_number.strip().upper()
                )
                if order.supplier_id != supplier_id or not exact or conflict:
                    continue
                observations.append(
                    PurchaseObservation(
                        po_line_id=row.id,
                        ordered_at=order.ordered_at,
                        quantity=row.quantity,
                        promised_at=row.promised_at,
                        received_at=row.received_at,
                        received_qty=row.received_qty,
                        rejected_qty=row.rejected_qty,
                    )
                )
            by_line[line.id] = summarize_performance(
                observations,
                today=today,
                window_days=policy.history_window_days,
            )
        result[supplier_id] = by_line
    return result


def review_issues(raw: Mapping[str, object]) -> tuple[str, ...] | None:
    """Line-level validation issues from the provenance (`_review.issues`), if recorded."""
    review = raw.get("_review")
    if not isinstance(review, dict):
        return None
    issues = cast(dict[str, object], review).get("issues")
    if not isinstance(issues, list):
        return None
    return tuple(str(i) for i in cast(list[object], issues))


def field_infos(raw: Mapping[str, object]) -> dict[str, FieldInfo]:
    """Per-field provenance (`{"unit_price": {"confidence": 0.9, "needs_review": false}}`).

    `corrected` is set by `bido_back.corrections` when a person fixed the value.
    """
    infos: dict[str, FieldInfo] = {}
    for name, value in raw.items():
        if name.startswith("_") or not isinstance(value, dict):
            continue
        entry = cast(dict[str, object], value)
        # An absent value (the supplier did not state it) carries no confidence: the
        # extraction records 0 for it, which must not make the offer doubtful.
        absent = "value" in entry and entry["value"] is None
        confidence = None if absent else entry.get("confidence")
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
        policy = ComparisonPolicy.model_validate(rfq.comparison_policy or {})
        lines = [
            RequestedLine(
                id=line.id,
                position=line.position,
                description=line.description,
                mpn=line.mpn,
                unit=line.unit,
                quantities=tuple(Decimal(str(q)) for q in line.quantities),
                incoterm=line.incoterm,
                need_by=line.need_by,
                qualification=line.qualification,
                part_number=line.part_number,
                max_unit_price=line.max_unit_price,
                max_lead_time_days=line.max_lead_time_days,
                source_evidence=line.source_evidence or {},
            )
            for line in await session.scalars(
                select(RfqLine).where(RfqLine.rfq_id == rfq_id).order_by(RfqLine.position)
            )
        ]
        invites = list(
            (
                await session.execute(
                    select(Invite, Supplier.name, Supplier.type)
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
                select(Proposal)
                .join(
                    latest,
                    (latest.c.invite_id == Proposal.invite_id)
                    & (latest.c.version == Proposal.version),
                )
                .with_for_update(read=True, of=Proposal)
            )
        }
        revisions = dict(
            (
                await session.execute(
                    select(FieldCorrection.proposal_id, func.count(FieldCorrection.id))
                    .where(FieldCorrection.proposal_id.in_([p.id for p in proposals.values()]))
                    .group_by(FieldCorrection.proposal_id)
                )
            ).all()
        )
        origins: dict[UUID, str] = {}
        if proposals:
            origins = {
                proposal_id: origin
                for proposal_id, origin in await session.execute(
                    select(Proposal.id, Message.origin)
                    .join(Message, Message.id == Proposal.message_id)
                    .where(Proposal.id.in_([p.id for p in proposals.values()]))
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
        supplier_ids = [invite.supplier_id for invite, _, _ in invites]
        performance = with_assessment_history(
            await load_performance(session, lines, supplier_ids, today=today, policy=policy),
            lines,
            supplier_ids,
            today=today,
            policy=policy,
        )

    quotes: list[SupplierQuote] = []
    awaiting: list[AwaitingSupplier] = []
    for invite, supplier_name, supplier_type in invites:
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
                supplier_type=supplier_type,
                payment_terms=proposal.payment_terms,
                performance=performance.get(invite.supplier_id, {}),
                negotiation_holds=negotiation_holds(proposal.fields),
                correction_revision=revisions.get(proposal.id, 0),
                origin=origins.get(proposal.id, "email"),
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
                        review_issues=review_issues(pl.fields),
                        qualification=parsed_text(pl.fields, "qualification"),
                        price_basis_quantity=parsed_decimal(pl.fields, "price_basis_quantity"),
                        freight_cost=parsed_decimal(pl.fields, "freight_cost"),
                        tax_rate_percent=parsed_decimal(pl.fields, "tax_rate_percent"),
                        conditional_terms=parsed_text(pl.fields, "conditional_terms"),
                        source_message_ids=evidence_messages(proposal.fields),
                    )
                    for pl in proposal_lines[proposal.id]
                ),
            )
        )
    comparison = compute_comparison(
        rfq_id=rfq.id,
        base_currency=rfq.base_currency,
        lines=lines,
        quotes=quotes,
        rates=rates,
        today=today,
        awaiting=awaiting,
    )
    return apply_rules(comparison, lines, today, policy)
