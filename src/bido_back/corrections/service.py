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

"Use the current offer" (`use_current_offer`) is the buyer's decision on a
held offer: the supplier's latest reply could not be applied as it was (no
"other terms unchanged", a change nobody asked about, conflicting statements,
an unclear chronology) or an asked item stayed unusable. The offer as shown
becomes the offer: its holds are released (on the proposal and its lines, and
for every later rebuild of the offer, `quotes.state`), the unusable asks are
resolved, and the negotiation evaluates it again. Never automatic. It follows
the approval rules: the RFQ's buyer may decide when the company allows
self-approval, otherwise an approver or owner who is not that buyer; an
approver or owner always may. Audited with ids and reason codes only.

Review issues a correction resolves (the issue strings come from
`bido_back.quotes.validation`):
- `missing:<field>` and `currency_mismatch:<field>` when `<field>` is corrected;
- `unmatched_rfq_line` / `unknown_rfq_line` and `quantity_mismatch` when
  `rfq_line_id` is corrected; `quantity_mismatch` when `min_quantity` is;
- on every line of the proposal, `currency_unverified` and
  `currency_mismatch:*` when the proposal's currency is corrected, and the
  proposal's own `currency_mismatch` (a currency other than the RFQ's);
- `incoterm_mismatch` when `incoterm` is corrected;
- `validity_expired` when `valid_until` is corrected to today or later.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final, cast
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.approvals.guard import APPROVER_ROLES
from bido_back.auth import Principal
from bido_back.corrections.schemas import (
    FieldCorrectionView,
    HeaderCorrectionRequest,
    LineCorrectionRequest,
    ProposalLineView,
    ProposalView,
    UseCurrentOfferRequest,
)
from bido_back.db import tenant_session
from bido_back.db.models import (
    AuditEvent,
    FieldCorrection,
    Invite,
    Membership,
    Proposal,
    ProposalLine,
    Rfq,
    RfqLine,
    Tenant,
)
from bido_back.followups.ledger import resolve_asks
from bido_back.negotiation.hooks import cancel_on_new_offer
from bido_back.rfqs.errors import Forbidden, InvalidState, NotFound

Db = AsyncEngine | async_sessionmaker[AsyncSession]

_REVIEW: Final = "_review"
_OFFER_STATE: Final = "_offer_state"
_PROVENANCE_ONLY: Final = frozenset(
    {
        "qualification",
        "price_basis_quantity",
        "freight_cost",
        "tax_rate_percent",
        "conditional_terms",
    }
)
# Columns that cannot be cleared.
_REQUIRED: Final = frozenset({"is_alternative"})
_PRICED: Final = ("unit_price", "tooling_cost", "freight_cost")


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
        proposal, rfq_id = await _proposal(session, proposal_id, lock=True)
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
                resolved |= {"unmatched_rfq_line", "unknown_rfq_line", "quantity_mismatch"}
            if name == "min_quantity":
                resolved.add("quantity_mismatch")
            if name == "tax_rate_percent":
                resolved.add("invalid_tax_rate")
            if name == "conditional_terms" and new is None:
                resolved.add("conditional_terms_require_review")
        line.needs_review = _resolve(fields, resolved)
        line.fields = fields
        await _invalidate_negotiation(session, proposal.invite_id, rfq_id)
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
            if name in ("currency", "incoterm"):
                # The buyer looked at the term that differs from the RFQ's.
                resolved.add(f"{name}_mismatch")
        _resolve(fields, resolved)
        proposal.fields = fields
        await _invalidate_negotiation(session, proposal.invite_id, rfq_id)

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
    entry = fields.get(name)
    old_provenance = cast(dict[str, Any], entry) if isinstance(entry, dict) else {}
    # Most fields are columns; a few (qualification) live only in the provenance.
    is_column = name not in _PROVENANCE_ONLY
    old: object = getattr(row, name) if is_column else old_provenance.get("parsed")
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
    if is_column:
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


async def use_current_offer(
    db: Db, principal: Principal, proposal_id: UUID, request: UseCurrentOfferRequest
) -> ProposalView:
    """The buyer takes the supplier's latest offer as shown: its holds are released."""
    now = datetime.now(UTC)
    async with tenant_session(db, principal.tenant_id) as session:
        proposal, rfq_id = await _proposal(session, proposal_id, lock=True)
        rfq = await session.get(Rfq, rfq_id)
        if rfq is None:
            raise NotFound(f"rfq {rfq_id} not found")
        await _may_decide(session, principal, rfq)
        latest = await session.scalar(
            select(func.max(Proposal.version)).where(Proposal.invite_id == proposal.invite_id)
        )
        if proposal.version != latest:
            raise InvalidState(f"proposal {proposal_id} is not the supplier's latest offer")
        raw = proposal.fields.get(_OFFER_STATE)
        offer_state = dict(cast(Mapping[str, Any], raw)) if isinstance(raw, Mapping) else {}
        holds = [str(issue) for issue in cast(list[object], offer_state.get("issues") or [])]
        if offer_state.get("reply_kind") == "clarification":
            holds.append("supplier_clarification_required")
        holds = list(dict.fromkeys(holds))
        resolved_asks = await resolve_asks(
            session, proposal.invite_id, by=principal.user_id, at=now
        )
        if not holds and not resolved_asks:
            raise InvalidState(f"proposal {proposal_id} is not held")
        questions = offer_state.get("technical_questions")
        offer_state.update(
            {
                "issues": [],
                "held_message_ids": [],
                "held_changes": [],
                "reply_kind": "amendment"
                if offer_state.get("reply_kind") == "clarification"
                else offer_state.get("reply_kind"),
                "requires_clarification": bool(questions),
                "released": {
                    "message_id": offer_state.get("active_message_id"),
                    "by": str(principal.user_id),
                    "at": now.isoformat(),
                    "issues": holds,
                    "reason": request.reason,
                },
            }
        )
        fields = _copy(proposal.fields)
        fields[_OFFER_STATE] = offer_state
        _resolve(fields, set(holds))
        proposal.fields = fields
        lines = await _lines(session, proposal_id, lock=True)
        for line in lines:
            line_fields = _copy(line.fields)
            line.needs_review = _resolve(line_fields, set(holds))
            line.fields = line_fields
        await _invalidate_negotiation(session, proposal.invite_id, rfq_id)
        session.add(
            AuditEvent(
                tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id,
                action="proposal.hold_released",
                payload={
                    "rfq_id": str(rfq_id),
                    "invite_id": str(proposal.invite_id),
                    "proposal_id": str(proposal_id),
                    "issues": holds,
                    "resolved_asks": resolved_asks,
                },
            )
        )
        await session.flush()
        return _proposal_view(proposal, rfq_id, lines)


async def _may_decide(session: AsyncSession, principal: Principal, rfq: Rfq) -> None:
    """Who may take an offer as shown: like an approval of what Bido prepared for the
    RFQ's buyer (`approvals.guard`). Roles are read now, never from the token."""
    role = await session.scalar(
        select(Membership.role)
        .where(
            Membership.tenant_id == principal.tenant_id,
            Membership.user_id == principal.user_id,
        )
        .with_for_update(read=True)
    )
    if role is None:
        raise Forbidden("only a member of the company can decide")
    own = rfq.created_by == principal.user_id
    if own:
        allowed = await session.scalar(
            select(Tenant.allow_self_approval).where(Tenant.id == principal.tenant_id)
        )
        if allowed:
            return
        raise Forbidden("this company requires a second person: the RFQ's buyer cannot decide")
    if role not in APPROVER_ROLES:
        raise Forbidden("the approver or owner role is required")


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
        # Match negotiation settings/proposal writes: RFQ before proposal before
        # pending messages. Corrections cannot race a freshly prepared draft.
        rfq_id = await session.scalar(
            select(Invite.rfq_id)
            .join(Proposal, Proposal.invite_id == Invite.id)
            .where(Proposal.id == proposal_id)
        )
        if rfq_id is not None:
            await session.scalar(select(Rfq.id).where(Rfq.id == rfq_id).with_for_update())
        query = query.with_for_update(of=Proposal)
    row = (await session.execute(query)).first()
    if row is None:
        raise NotFound(f"proposal {proposal_id} not found")
    proposal, rfq_id = row
    return proposal, rfq_id


async def _invalidate_negotiation(session: AsyncSession, invite_id: UUID, rfq_id: UUID) -> None:
    await cancel_on_new_offer(session, invite_id, reason="offer_corrected")
    rfq = await session.get(Rfq, rfq_id)
    if rfq is None or rfq.negotiation_status not in {"open", "ready_to_close"}:
        return
    invite = await session.get(Invite, invite_id)
    if invite is not None:
        invite.negotiation_evaluated_proposal_id = None
        invite.negotiation_status = "open"
    rfq.negotiation_status = "open"
    rfq.negotiation_ready_at = None


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
    def value(name: str) -> str | None:
        entry: object = line.fields.get(name)
        if not isinstance(entry, dict):
            return None
        parsed: object = cast(dict[str, object], entry).get("parsed")
        return parsed if isinstance(parsed, str) else None

    def number(name: str) -> Decimal | None:
        parsed = value(name)
        return Decimal(parsed) if parsed is not None else None

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
        price_basis_quantity=number("price_basis_quantity"),
        freight_cost=number("freight_cost"),
        tax_rate_percent=number("tax_rate_percent"),
        conditional_terms=value("conditional_terms"),
    )


def _holds(fields: Mapping[str, Any]) -> tuple[list[str], datetime | None]:
    """Why the offer is held, and when a person released it (`_offer_state`)."""
    raw = fields.get(_OFFER_STATE)
    if not isinstance(raw, Mapping):
        return [], None
    state = cast(Mapping[str, Any], raw)
    holds = [str(i) for i in cast(list[object], state.get("issues") or [])]
    if state.get("reply_kind") == "clarification" and not holds:
        holds.append("supplier_clarification_required")
    released = state.get("released")
    at = cast(Mapping[str, Any], released).get("at") if isinstance(released, Mapping) else None
    try:
        released_at = datetime.fromisoformat(at) if isinstance(at, str) else None
    except ValueError:
        released_at = None
    return holds, released_at


def _proposal_view(proposal: Proposal, rfq_id: UUID, lines: Sequence[ProposalLine]) -> ProposalView:
    review = proposal.fields.get(_REVIEW)
    needs_review = (
        cast(Mapping[str, Any], review).get("needs_review") is True
        if isinstance(review, Mapping)
        else bool(_review_fields(proposal.fields))
    )
    holds, released_at = _holds(proposal.fields)
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
        holds=holds,
        released_at=released_at,
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
