"""Correcting (or confirming) values the quote extraction stored.

A correction does three things in one tenant transaction:

1. appends a `field_corrections` row with the old value, the new value and
   the extraction's provenance of the old value (the eval set for quote
   extraction);
2. writes the new value to the proposal / proposal line, which is what the
   comparison reads;
3. marks the field's provenance `corrected` (confidence 1, no issues) and
   resolves the review issues the new value answers, then recomputes
   `needs_review`. A corrected field never counts as low confidence again.

Sending the value a field already has records a confirmation: nothing
changes but the field leaves review. Any member of the tenant may correct;
every row says who did.

Review issues a correction resolves (the issue strings come from
`bido_back.quotes.validation`):
- `missing:<field>` and `currency_mismatch:<field>` when `<field>` is corrected;
- `unmatched_rfq_line` / `unknown_rfq_line` when `rfq_line_id` is corrected;
- on every line of the proposal, `currency_unverified` and
  `currency_mismatch:*` when the proposal's currency is corrected;
- `validity_expired` when `valid_until` is corrected to today or later.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final, cast
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.auth import Principal
from bido_back.corrections.schemas import (
    FieldCorrectionView,
    HeaderCorrectionRequest,
    LineCorrectionRequest,
    ProposalLineView,
    ProposalView,
)
from bido_back.db import tenant_session
from bido_back.db.models import FieldCorrection, Invite, Proposal, ProposalLine, RfqLine
from bido_back.rfqs.errors import InvalidState, NotFound

Db = AsyncEngine | async_sessionmaker[AsyncSession]

_REVIEW: Final = "_review"
# Columns that cannot be cleared.
_REQUIRED: Final = frozenset({"is_alternative"})
_PRICED: Final = ("unit_price", "tooling_cost")


# --- Reading -------------------------------------------------------------------------


async def get_proposal(db: Db, principal: Principal, proposal_id: UUID) -> ProposalView:
    async with tenant_session(db, principal.tenant_id) as session:
        proposal, rfq_id = await _proposal(session, proposal_id)
        lines = await _lines(session, proposal_id)
        return _proposal_view(proposal, rfq_id, lines)


async def list_corrections(
    db: Db, principal: Principal, proposal_id: UUID
) -> list[FieldCorrectionView]:
    async with tenant_session(db, principal.tenant_id) as session:
        await _proposal(session, proposal_id)
        rows = await session.scalars(
            select(FieldCorrection)
            .where(FieldCorrection.proposal_id == proposal_id)
            .order_by(FieldCorrection.created_at, FieldCorrection.id)
        )
        return [_correction_view(row) for row in rows]


# --- Correcting ----------------------------------------------------------------------


async def correct_line(
    db: Db,
    principal: Principal,
    proposal_id: UUID,
    line_id: UUID,
    request: LineCorrectionRequest,
) -> ProposalLineView:
    changes = request.changes()
    for name in _REQUIRED & changes.keys():
        if changes[name] is None:
            raise InvalidState(f"{name} cannot be cleared")

    async with tenant_session(db, principal.tenant_id) as session:
        _, rfq_id = await _proposal(session, proposal_id, lock=True)
        line = await session.scalar(
            select(ProposalLine)
            .where(ProposalLine.id == line_id, ProposalLine.proposal_id == proposal_id)
            .with_for_update()
        )
        if line is None:
            raise NotFound(f"proposal line {line_id} not found")
        new_rfq_line = changes.get("rfq_line_id")
        if new_rfq_line is not None:
            belongs = await session.scalar(
                select(RfqLine.id).where(RfqLine.id == new_rfq_line, RfqLine.rfq_id == rfq_id)
            )
            if belongs is None:
                raise NotFound(f"rfq line {new_rfq_line} is not a line of this RFQ")

        fields = _copy(line.fields)
        resolved: set[str] = set()
        for name, new in changes.items():
            await _record(
                session, principal, proposal_id, line.id, fields, name, line, new, request
            )
            resolved |= {f"missing:{name}", f"currency_mismatch:{name}"}
            if name == "rfq_line_id":
                resolved |= {"unmatched_rfq_line", "unknown_rfq_line"}
        line.needs_review = _resolve(fields, resolved)
        line.fields = fields
        await session.flush()
        return _line_view(line)


async def correct_header(
    db: Db, principal: Principal, proposal_id: UUID, request: HeaderCorrectionRequest
) -> ProposalView:
    changes = request.changes()
    async with tenant_session(db, principal.tenant_id) as session:
        proposal, rfq_id = await _proposal(session, proposal_id, lock=True)
        lines = await _lines(session, proposal_id, lock=True)

        fields = _copy(proposal.fields)
        resolved: set[str] = set()
        for name, new in changes.items():
            await _record(
                session, principal, proposal_id, None, fields, name, proposal, new, request
            )
            resolved.add(f"missing:{name}")
            if name == "valid_until" and isinstance(new, date) and new >= datetime.now(UTC).date():
                resolved.add("validity_expired")
        _resolve(fields, resolved)
        proposal.fields = fields

        if "currency" in changes and changes["currency"] is not None:
            # A trusted currency answers every line's currency doubts.
            line_resolved = {"currency_unverified", *(f"currency_mismatch:{p}" for p in _PRICED)}
            for line in lines:
                line_fields = _copy(line.fields)
                line.needs_review = _resolve(line_fields, line_resolved)
                line.fields = line_fields
        await session.flush()
        return _proposal_view(proposal, rfq_id, lines)


async def _record(
    session: AsyncSession,
    principal: Principal,
    proposal_id: UUID,
    line_id: UUID | None,
    fields: dict[str, Any],
    name: str,
    row: Proposal | ProposalLine,
    new: object,
    request: LineCorrectionRequest | HeaderCorrectionRequest,
) -> None:
    """Append the correction, write the value, mark the field corrected."""
    old: object = getattr(row, name)
    entry = fields.get(name)
    old_provenance = cast(dict[str, Any], entry) if isinstance(entry, dict) else {}
    correction = FieldCorrection(
        tenant_id=principal.tenant_id,
        proposal_id=proposal_id,
        proposal_line_id=line_id,
        field=name,
        action="confirmed" if _same(old, new) else "corrected",
        old_value=_json(old),
        new_value=_json(new),
        old_provenance=old_provenance,
        reason=request.reason,
        corrected_by=principal.user_id,
    )
    session.add(correction)
    await session.flush()
    setattr(row, name, new)
    fields[name] = {
        **old_provenance,
        "value": _json(new),
        "parsed": _json(new),
        "confidence": 1.0,
        "needs_review": False,
        "issues": [],
        "corrected": True,
        "correction_id": str(correction.id),
    }


def _resolve(fields: dict[str, Any], resolved: set[str]) -> bool:
    """Mark `resolved` issues answered and recompute the row's needs_review."""
    review = _copy(fields.get(_REVIEW))
    answered = set(cast(list[str], review.get("resolved_issues", []))) | resolved
    issues = cast(list[str], review.get("issues", []))
    review["resolved_issues"] = sorted(answered & set(issues))
    needs_review = bool(_review_fields(fields)) or any(i not in answered for i in issues)
    review["needs_review"] = needs_review
    fields[_REVIEW] = review
    return needs_review


# --- Helpers ---------------------------------------------------------------------------


async def _proposal(
    session: AsyncSession, proposal_id: UUID, *, lock: bool = False
) -> tuple[Proposal, UUID]:
    query = (
        select(Proposal, Invite.rfq_id)
        .join(Invite, Invite.id == Proposal.invite_id)
        .where(Proposal.id == proposal_id)
    )
    if lock:
        query = query.with_for_update(of=Proposal)
    row = (await session.execute(query)).first()
    if row is None:
        raise NotFound(f"proposal {proposal_id} not found")
    proposal, rfq_id = row
    return proposal, rfq_id


async def _lines(
    session: AsyncSession, proposal_id: UUID, *, lock: bool = False
) -> list[ProposalLine]:
    query = (
        select(ProposalLine)
        .where(ProposalLine.proposal_id == proposal_id)
        .order_by(ProposalLine.created_at, ProposalLine.id)
    )
    if lock:
        query = query.with_for_update()
    return list(await session.scalars(query))


def _copy(value: object) -> dict[str, Any]:
    # A new dict, so SQLAlchemy sees the JSONB column change.
    return dict(cast(Mapping[str, Any], value)) if isinstance(value, Mapping) else {}


def _entries(fields: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    return [
        (name, cast(Mapping[str, Any], entry))
        for name, entry in fields.items()
        if not name.startswith("_") and isinstance(entry, Mapping)
    ]


def _review_fields(fields: Mapping[str, Any]) -> list[str]:
    return sorted(name for name, entry in _entries(fields) if entry.get("needs_review") is True)


def corrected_fields(fields: Mapping[str, Any]) -> list[str]:
    return sorted(name for name, entry in _entries(fields) if entry.get("corrected") is True)


def _same(old: object, new: object) -> bool:
    if isinstance(old, Decimal) and isinstance(new, Decimal):
        return old.compare(new) == 0
    return old == new


def _json(value: object) -> JsonValue:
    match value:
        case None | bool() | int() | str():
            return value
        case Decimal():
            # Plain notation without trailing zeros: 10000.0000 -> "10000", 0.0420 -> "0.042".
            return format(value.normalize(), "f")
        case date():
            return value.isoformat()
        case UUID():
            return str(value)
        case _:
            raise TypeError(f"unexpected value type {type(value).__name__}")


def _line_view(line: ProposalLine) -> ProposalLineView:
    return ProposalLineView(
        id=line.id,
        proposal_id=line.proposal_id,
        rfq_line_id=line.rfq_line_id,
        offered_mpn=line.offered_mpn,
        is_alternative=line.is_alternative,
        min_quantity=line.min_quantity,
        unit_price=line.unit_price,
        unit=line.unit,
        tooling_cost=line.tooling_cost,
        moq=line.moq,
        lead_time_days=line.lead_time_days,
        origin_country=line.origin_country,
        needs_review=line.needs_review,
        review_fields=_review_fields(line.fields),
        corrected_fields=corrected_fields(line.fields),
    )


def _proposal_view(proposal: Proposal, rfq_id: UUID, lines: Sequence[ProposalLine]) -> ProposalView:
    review = proposal.fields.get(_REVIEW)
    needs_review = (
        cast(Mapping[str, Any], review).get("needs_review") is True
        if isinstance(review, Mapping)
        else bool(_review_fields(proposal.fields))
    )
    return ProposalView(
        id=proposal.id,
        invite_id=proposal.invite_id,
        rfq_id=rfq_id,
        version=proposal.version,
        currency=proposal.currency,
        valid_until=proposal.valid_until,
        incoterm=proposal.incoterm,
        payment_terms=proposal.payment_terms,
        needs_review=needs_review,
        review_fields=_review_fields(proposal.fields),
        corrected_fields=corrected_fields(proposal.fields),
        lines=[_line_view(line) for line in lines],
    )


def _correction_view(row: FieldCorrection) -> FieldCorrectionView:
    return FieldCorrectionView(
        id=row.id,
        proposal_id=row.proposal_id,
        proposal_line_id=row.proposal_line_id,
        field=row.field,
        action="confirmed" if row.action == "confirmed" else "corrected",
        old_value=cast(JsonValue, row.old_value),
        new_value=cast(JsonValue, row.new_value),
        old_provenance=cast(dict[str, JsonValue], row.old_provenance),
        reason=row.reason,
        corrected_by=row.corrected_by,
        created_at=row.created_at,
    )
