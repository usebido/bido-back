"""Immutable buyer-owned evidence snapshots and validated, idempotent draft handoff."""

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import ProcurementAssessment, Rfq
from bido_back.purchasing.service import Conflict, InvalidRequest, NotFound
from bido_back.purchasing.sources.contracts import AssessmentLine, PlanningScope, SourceAssessment
from bido_back.purchasing.sources.handoff import (
    AssessmentInvitedSupplier,
    AssessmentSkippedSupplier,
    LimitsBasis,
    PlannedInvite,
    handoff,
    invited_suppliers,
    limits_basis,
    rfq_line_limits,
    wanted_suppliers,
)
from bido_back.rfqs.schemas import CurrencyCode
from bido_back.rfqs.service import NewRfqLine, create_draft_rfq, preliminary_title

logger = logging.getLogger(__name__)

Db = AsyncEngine | async_sessionmaker[AsyncSession]

OTHER_SETTINGS: Final = "this assessment already has a draft RFQ with different settings"


class AssessmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_ids: list[UUID] = Field(min_length=1, max_length=10)
    objective: str = Field(min_length=1, max_length=2000)
    scope: PlanningScope | None = None


class AssessmentRfqRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    base_currency: CurrencyCode
    response_due: date | None = None
    pending_validation: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=(
            "Create a PRELIMINARY draft although evidence issues remain, naming what it waits "
            "for (e.g. 'engineering validation of the BOM'). Lines without a quantity or unit "
            "are left out and listed in `skipped`. A preliminary RFQ cannot be prepared for "
            "sending until the mark is removed."
        ),
    )
    max_suppliers_per_line: int = Field(
        default=3,
        ge=1,
        le=3,
        description=(
            "How many of each line's assessed suppliers (the suggested, best-ranked first) "
            "to invite as DRAFT invites. Nothing is sent."
        ),
    )


class AssessmentRfqResult(BaseModel):
    assessment_id: UUID
    rfq_id: UUID
    ref: str
    lines: int
    reused: bool
    title: str = ""
    preliminary: bool = False
    # Preliminary only: assessed lines left out (no quantity or unit yet), by part number.
    skipped: list[str] = Field(default_factory=list[str])
    # The suppliers on the draft as DRAFT invites (nothing is sent), and the assessed
    # suppliers that could not be invited, with why.
    invited: list[AssessmentInvitedSupplier] = Field(
        default_factory=list[AssessmentInvitedSupplier]
    )
    skipped_suppliers: list[AssessmentSkippedSupplier] = Field(
        default_factory=list[AssessmentSkippedSupplier]
    )
    # Line positions without a default target/maximum unit price: no internal estimate,
    # or no exchange rate into the RFQ's currency. The others' defaults come from the
    # internal estimate; nothing is approved until the buyer approves the mandate.
    lines_without_limits: list[int] = Field(default_factory=list[int])


class LineLimits(BaseModel):
    """A line's default negotiation limits, in the RFQ's currency; None: it has none."""

    position: int
    target_unit_price: Decimal | None = None
    max_unit_price: Decimal | None = None
    # Why, for limits a new draft takes from the internal estimate.
    basis: LimitsBasis | None = None


class AssessmentRfqPlan(BaseModel):
    """What `draft_from_snapshot` would do with a request now, by the same rules and data,
    writing nothing. It checks everything again when it runs."""

    # The assessment's existing draft, which it would return as it is.
    rfq_id: UUID | None = None
    ref: str | None = None
    # One per line `rfq_lines` gives, in order.
    limits: list[LineLimits]
    invited: list[PlannedInvite]
    skipped_suppliers: list[AssessmentSkippedSupplier]
    lines_without_limits: list[int]
    # What would stop it besides the evidence checks.
    issues: list[str] = Field(default_factory=list[str])


class AssessmentSnapshotSummary(BaseModel):
    id: UUID
    objective: str
    created_at: datetime
    status: Literal["assessed", "needs_review"]
    rfq_id: UUID | None
    line_count: int


def _view(row: ProcurementAssessment) -> SourceAssessment:
    return SourceAssessment.model_validate(row.assessment).model_copy(
        update={"assessment_id": row.id, "rfq_id": row.rfq_id, "persisted": True}
    )


async def find_snapshot(db: Db, principal: Principal, source_key: str) -> SourceAssessment | None:
    async with tenant_session(db, principal.tenant_id) as session:
        row = await session.scalar(
            select(ProcurementAssessment).where(
                ProcurementAssessment.user_id == principal.user_id,
                ProcurementAssessment.source_key == source_key,
            )
        )
        return _view(row) if row is not None else None


async def save_snapshot(
    db: Db,
    principal: Principal,
    *,
    source_key: str,
    upload_ids: list[UUID],
    objective: str,
    assessment: SourceAssessment,
) -> SourceAssessment:
    """Called only with the server's validated assessment, never a client-supplied one."""
    async with tenant_session(db, principal.tenant_id) as session:
        await session.execute(
            insert(ProcurementAssessment)
            .values(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                source_key=source_key,
                upload_ids=upload_ids,
                objective=objective,
                assessment=assessment.model_dump(mode="json"),
            )
            .on_conflict_do_nothing(constraint="procurement_assessments_source_key")
        )
        row = await session.scalar(
            select(ProcurementAssessment).where(
                ProcurementAssessment.user_id == principal.user_id,
                ProcurementAssessment.source_key == source_key,
            )
        )
        if row is None:
            raise RuntimeError("assessment was not persisted")
        return _view(row)


async def get_snapshot(db: Db, principal: Principal, assessment_id: UUID) -> SourceAssessment:
    async with tenant_session(db, principal.tenant_id) as session:
        row = await session.scalar(
            select(ProcurementAssessment).where(
                ProcurementAssessment.id == assessment_id,
                ProcurementAssessment.user_id == principal.user_id,
            )
        )
        if row is None:
            raise NotFound("procurement assessment not found")
        return _view(row)


async def list_snapshots(
    db: Db,
    principal: Principal,
    *,
    limit: int = 20,
    search: str | None = None,
) -> list[AssessmentSnapshotSummary]:
    """Rediscover the buyer's saved evidence without returning source content."""
    if not 1 <= limit <= 50:
        raise InvalidRequest("limit must be between 1 and 50")
    query = select(ProcurementAssessment).where(ProcurementAssessment.user_id == principal.user_id)
    if search is not None and search.strip():
        query = query.where(
            ProcurementAssessment.objective.icontains(search.strip(), autoescape=True)
        )
    query = query.order_by(
        ProcurementAssessment.created_at.desc(), ProcurementAssessment.id.desc()
    ).limit(limit)
    async with tenant_session(db, principal.tenant_id) as session:
        summaries: list[AssessmentSnapshotSummary] = []
        for row in await session.scalars(query):
            assessment = _view(row)
            summaries.append(
                AssessmentSnapshotSummary(
                    id=row.id,
                    objective=row.objective,
                    created_at=row.created_at,
                    status=assessment.status,
                    rfq_id=row.rfq_id,
                    line_count=len(assessment.lines),
                )
            )
        return summaries


def rfq_lines(assessment: SourceAssessment) -> tuple[list[AssessmentLine], list[str]]:
    """The assessed lines a draft gets (a positive quantity and a unit), and the part
    numbers of those it leaves out (no quantity or unit yet)."""
    lines = [
        line
        for line in assessment.lines
        if line.calculated_to_buy is not None and line.calculated_to_buy > 0 and line.unit
    ]
    skipped = [
        line.part_number
        for line in assessment.lines
        if line.calculated_to_buy is None or not line.unit
    ]
    return lines, skipped


def _title(request: AssessmentRfqRequest) -> str:
    if request.pending_validation is None:
        return request.title
    return preliminary_title(request.title, request.pending_validation)[:300]


def _same_settings(rfq: Rfq, request: AssessmentRfqRequest) -> bool:
    return (rfq.title, rfq.base_currency, rfq.response_due) == (
        _title(request),
        request.base_currency,
        request.response_due,
    )


async def _as_it_is(
    session: AsyncSession,
    principal: Principal,
    rfq: Rfq,
    lines: list[AssessmentLine],
    request: AssessmentRfqRequest,
) -> tuple[
    list[AssessmentInvitedSupplier],
    list[AssessmentSkippedSupplier],
    dict[int, tuple[Decimal, Decimal] | None],
]:
    """An assessment's existing draft, read-only: its invites, the assessed suppliers it
    lacks with why, and its limits by line position."""
    wanted = wanted_suppliers(lines, request.max_suppliers_per_line)
    invited, skipped = await invited_suppliers(session, principal, rfq, wanted)
    return invited, skipped, await rfq_line_limits(session, rfq.id)


async def plan_from_snapshot(
    db: Db, principal: Principal, assessment_id: UUID, request: AssessmentRfqRequest
) -> tuple[SourceAssessment, AssessmentRfqPlan]:
    """The buyer's saved assessment and what `draft_from_snapshot` would do with `request`:
    the same lines, default limits and supplier resolution, writing nothing (for the
    preview the buyer confirms). The evidence checks stay with the caller's preview and
    run again in `draft_from_snapshot`."""
    async with tenant_session(db, principal.tenant_id) as session:
        row = await session.scalar(
            select(ProcurementAssessment).where(
                ProcurementAssessment.id == assessment_id,
                ProcurementAssessment.user_id == principal.user_id,
            )
        )
        if row is None:
            raise NotFound("procurement assessment not found")
        assessment = _view(row)
        lines, _ = rfq_lines(assessment)
        rfq = await session.get(Rfq, row.rfq_id) if row.rfq_id is not None else None
        if rfq is not None:
            invited, skipped, limits = await _as_it_is(session, principal, rfq, lines, request)
            return assessment, AssessmentRfqPlan(
                rfq_id=rfq.id,
                ref=rfq.ref,
                limits=[_line_limits(position, found) for position, found in limits.items()],
                invited=[PlannedInvite.model_validate(s.model_dump()) for s in invited],
                skipped_suppliers=skipped,
                lines_without_limits=[p for p, found in limits.items() if found is None],
                issues=[] if _same_settings(rfq, request) else [OTHER_SETTINGS],
            )
        planned = await handoff(
            session,
            principal,
            lines,
            currency=request.base_currency,
            per_line=request.max_suppliers_per_line,
            rfq=None,
        )
    return assessment, AssessmentRfqPlan(
        limits=[
            _line_limits(
                position,
                found,
                limits_basis(line.price) if found is not None and line.price else None,
            )
            for position, (line, found) in enumerate(zip(lines, planned.limits, strict=True), 1)
        ],
        invited=planned.invited,
        skipped_suppliers=planned.skipped_suppliers,
        lines_without_limits=planned.lines_without_limits,
    )


def _line_limits(
    position: int, limits: tuple[Decimal, Decimal] | None, basis: LimitsBasis | None = None
) -> LineLimits:
    if limits is None:
        return LineLimits(position=position)
    return LineLimits(
        position=position, target_unit_price=limits[0], max_unit_price=limits[1], basis=basis
    )


async def draft_from_snapshot(
    db: Db, principal: Principal, assessment_id: UUID, request: AssessmentRfqRequest
) -> AssessmentRfqResult:
    async with tenant_session(db, principal.tenant_id) as session:
        row = await session.scalar(
            select(ProcurementAssessment)
            .where(
                ProcurementAssessment.id == assessment_id,
                ProcurementAssessment.user_id == principal.user_id,
            )
            .with_for_update()
        )
        if row is None:
            raise NotFound("procurement assessment not found")
        assessment = _view(row)
        preliminary = request.pending_validation is not None
        title = _title(request)
        # A complete assessment, or a PRELIMINARY draft that carries its open issues: the
        # mark keeps it from being prepared for sending, and the quantities and units still
        # come from the assessment, never from a rewritten text.
        if not preliminary and (
            assessment.status != "assessed"
            or any(issue.severity == "blocker" for issue in assessment.issues)
            or not assessment.coverage
            or not all(part.inspected for part in assessment.coverage)
        ):
            raise InvalidRequest("resolve the assessment's evidence issues before creating an RFQ")
        if not preliminary and any(
            not line.ready
            or line.calculated_to_buy is None
            or not line.unit
            or not line.evidence
            or any(issue.severity == "blocker" for issue in line.issues)
            for line in assessment.lines
        ):
            raise InvalidRequest(
                "every assessed line needs validated quantities, units and evidence"
            )
        lines, skipped = rfq_lines(assessment)
        if not lines:
            raise InvalidRequest("the assessment has no validated positive purchase requirements")
        if row.rfq_id is not None:
            rfq = await session.get(Rfq, row.rfq_id)
            if rfq is None:
                raise NotFound("linked RFQ not found")
            if not _same_settings(rfq, request):
                raise Conflict(OTHER_SETTINGS)
            # The same answer, read from the draft as it is now: nothing is written again.
            invited, skipped_suppliers, limits = await _as_it_is(
                session, principal, rfq, lines, request
            )
            return AssessmentRfqResult(
                assessment_id=row.id,
                rfq_id=rfq.id,
                ref=rfq.ref,
                lines=len(lines),
                reused=True,
                title=rfq.title,
                preliminary=preliminary,
                skipped=skipped,
                invited=invited,
                skipped_suppliers=skipped_suppliers,
                lines_without_limits=[p for p, found in limits.items() if found is None],
            )
        new_lines = [
            NewRfqLine(
                description=line.description or line.part_number,
                part_number=line.part_number,
                quantity=line.calculated_to_buy,
                unit=line.unit,
                need_by=line.need_by,
            )
            for line in lines
            if line.calculated_to_buy is not None and line.unit is not None
        ]
        rfq, rfq_rows = await create_draft_rfq(
            session,
            principal,
            title=title,
            lines=new_lines,
            source_text=f"Procurement assessment {row.id}",
            response_due=request.response_due,
            base_currency=request.base_currency,
        )
        done = await handoff(
            session,
            principal,
            lines,
            currency=request.base_currency,
            per_line=request.max_suppliers_per_line,
            rfq=rfq,
        )
        for assessed, rfq_line, limits, links in zip(
            lines, rfq_rows, done.limits, done.supplier_links, strict=True
        ):
            # JSON decimal strings round-trip exactly; the common RFQ constructor
            # uses floats for legacy callers and can lose source precision.
            rfq_line.quantities = [str(assessed.calculated_to_buy)]
            rfq_line.delivery_location = assessed.location
            rfq_line.source_evidence = {
                "assessment_id": str(row.id),
                "line": assessed.model_dump(mode="json"),
                # Which invited supplier each assessment supplier became, for the
                # comparison's assessment history (`comparison.assessment_history`).
                "supplier_links": list(links),
            }
            # The internal estimate becomes the negotiation's default limits, in the
            # RFQ's currency; the buyer approves (or changes) them in the mandate.
            if limits is not None:
                rfq_line.target_unit_price, rfq_line.max_unit_price = limits
        row.rfq_id = rfq.id
    invited = done.done()
    logger.info(
        "rfq from assessment tenant=%s assessment=%s rfq=%s invites=%d created_suppliers=%d "
        "skipped_suppliers=%d lines_without_limits=%d",
        principal.tenant_id,
        assessment_id,
        rfq.id,
        len(invited),
        sum(1 for supplier in invited if supplier.supplier_created),
        len(done.skipped_suppliers),
        len(done.lines_without_limits),
    )
    return AssessmentRfqResult(
        assessment_id=assessment_id,
        rfq_id=rfq.id,
        ref=rfq.ref,
        lines=len(lines),
        reused=False,
        title=rfq.title,
        preliminary=preliminary,
        skipped=skipped,
        invited=invited,
        skipped_suppliers=done.skipped_suppliers,
        lines_without_limits=done.lines_without_limits,
    )
