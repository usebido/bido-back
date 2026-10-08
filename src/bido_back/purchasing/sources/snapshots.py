"""Immutable buyer-owned evidence snapshots and validated, idempotent draft handoff."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import ProcurementAssessment, Rfq
from bido_back.purchasing.service import Conflict, InvalidRequest, NotFound
from bido_back.purchasing.sources.contracts import PlanningScope, ScopeRequest, SourceAssessment
from bido_back.rfqs.schemas import CurrencyCode
from bido_back.rfqs.service import NewRfqLine, create_draft_rfq, preliminary_title

Db = AsyncEngine | async_sessionmaker[AsyncSession]


class AssessmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_ids: list[UUID] = Field(min_length=1, max_length=10)
    objective: str = Field(min_length=1, max_length=2000)
    # The buyer's boundaries, resolved against the evidence. A complete PlanningScope stays
    # an accepted shape (any valid one parses as a ScopeRequest first): the published
    # contract only gains an alternative.
    scope: ScopeRequest | PlanningScope | None = None


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
        title = (
            preliminary_title(request.title, request.pending_validation)[:300]
            if request.pending_validation is not None
            else request.title
        )
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
        if not lines:
            raise InvalidRequest("the assessment has no validated positive purchase requirements")
        if row.rfq_id is not None:
            rfq = await session.get(Rfq, row.rfq_id)
            if rfq is None:
                raise NotFound("linked RFQ not found")
            if (rfq.title, rfq.base_currency, rfq.response_due) != (
                title,
                request.base_currency,
                request.response_due,
            ):
                raise Conflict("this assessment already has a draft RFQ with different settings")
            return AssessmentRfqResult(
                assessment_id=row.id,
                rfq_id=rfq.id,
                ref=rfq.ref,
                lines=len(lines),
                reused=True,
                title=rfq.title,
                preliminary=preliminary,
                skipped=skipped,
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
        rfq, rfq_lines = await create_draft_rfq(
            session,
            principal,
            title=title,
            lines=new_lines,
            source_text=f"Procurement assessment {row.id}",
            response_due=request.response_due,
            base_currency=request.base_currency,
        )
        for assessed, rfq_line in zip(lines, rfq_lines, strict=True):
            # JSON decimal strings round-trip exactly; the common RFQ constructor
            # uses floats for legacy callers and can lose source precision.
            rfq_line.quantities = [str(assessed.calculated_to_buy)]
            rfq_line.delivery_location = assessed.location
            rfq_line.source_evidence = {
                "assessment_id": str(row.id),
                "line": assessed.model_dump(mode="json"),
            }
        row.rfq_id = rfq.id
        return AssessmentRfqResult(
            assessment_id=row.id,
            rfq_id=rfq.id,
            ref=rfq.ref,
            lines=len(lines),
            reused=False,
            title=rfq.title,
            preliminary=preliminary,
            skipped=skipped,
        )
