"""Durable source evidence, owner isolation and idempotent draft handoff."""

import asyncio
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.context import ToolContext
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import Approval, Invite, Message, ProcurementAssessment, Rfq, RfqLine
from bido_back.onboarding import service as onboarding
from bido_back.purchasing.service import Conflict, InvalidRequest, NotFound
from bido_back.purchasing.sources.contracts import MappingProposal, MappingTask, SourceAssessment
from bido_back.purchasing.sources.service import analyze_uploads
from bido_back.purchasing.sources.snapshots import (
    AssessmentRfqRequest,
    draft_from_snapshot,
    get_snapshot,
    list_snapshots,
    save_snapshot,
)
from evals.source_fixtures import SCOPE
from tests.approvals.conftest import People
from tests.copilot.test_source_tools import SourceMapper, TrackingStorage
from tests.purchasing.test_workbook import XLSX, workbook
from tests.rfqs.test_service import buyer

pytestmark = pytest.mark.db


def _workbook(quantity: str | None = "20", unit: str = "pcs") -> bytes:
    return workbook(
        {
            "Stock": [
                ["Material", "Description", "Unit", "Available", "On order", "Safety stock"],
                ["000010", "Bolts", unit, "0", "0", "0"],
            ],
            "Requirements": [
                ["Material", "Unit", "Quantity", "Need by", "Demand basis"],
                ["000010", unit, quantity, "2026-11-01", "Gross"],
            ],
        }
    )


async def upload_source(
    engine: AsyncEngine,
    who: Principal,
    storage: TrackingStorage,
    *,
    quantity: str | None = "20",
    unit: str = "pcs",
) -> UUID:
    upload = await onboarding.store_upload(
        engine,
        storage,
        who,
        filename="procurement.xlsx",
        content_type=XLSX,
        data=_workbook(quantity, unit),
    )
    return upload.id


async def _assess(
    engine: AsyncEngine,
    who: Principal,
    storage: TrackingStorage,
    mapper: SourceMapper,
    upload_id: UUID,
) -> SourceAssessment:
    return await analyze_uploads(
        engine,
        storage,
        who,
        [upload_id],
        "Assess motor requirements",
        ToolContext(tenant_id=who.tenant_id, user_id=who.user_id, run_id=uuid4()),
        mapper,
        persist=True,
    )


async def test_scope_is_persisted_and_part_of_the_snapshot_identity(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who = buyer(purchasing_rows)
    storage, mapper = TrackingStorage(), SourceMapper()
    upload_id = await upload_source(app_engine, who, storage)
    context = ToolContext(tenant_id=who.tenant_id, user_id=who.user_id, run_id=uuid4())
    first = await analyze_uploads(
        app_engine,
        storage,
        who,
        [upload_id],
        "Plan",
        context,
        mapper,
        persist=True,
        scope=SCOPE,
    )
    repeated = await analyze_uploads(
        app_engine,
        storage,
        who,
        [upload_id],
        "Plan",
        context,
        mapper,
        persist=True,
        scope=SCOPE,
    )
    changed = await analyze_uploads(
        app_engine,
        storage,
        who,
        [upload_id],
        "Plan",
        context,
        mapper,
        persist=True,
        scope=SCOPE.model_copy(update={"safety_stock": Decimal(10)}),
    )
    assert first.scope == repeated.scope == SCOPE
    assert first.assessment_id == repeated.assessment_id != changed.assessment_id
    assert changed.scope is not None and changed.scope.safety_stock == 10
    assert first.assessment_id is not None
    assert (await get_snapshot(app_engine, who, first.assessment_id)).scope == SCOPE


def _draft_request() -> AssessmentRfqRequest:
    return AssessmentRfqRequest(title="Motor component enquiry", base_currency="EUR")


async def test_same_source_and_objective_reuses_snapshot_without_reinterpreting(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who = buyer(purchasing_rows)
    storage, mapper = TrackingStorage(), SourceMapper()
    upload_id = await upload_source(app_engine, who, storage)
    first = await _assess(app_engine, who, storage, mapper, upload_id)
    assert first.persisted and first.assessment_id is not None
    calls = len(mapper.contexts)
    again = await _assess(app_engine, who, storage, mapper, upload_id)
    assert again == first
    assert len(mapper.contexts) == calls
    assert await get_snapshot(app_engine, who, first.assessment_id) == first
    async with tenant_session(app_engine, who.tenant_id) as session:
        assert await session.scalar(select(func.count()).select_from(ProcurementAssessment)) == 1


async def test_changed_source_bytes_create_new_evidence_revision(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who = buyer(purchasing_rows)
    storage, mapper = TrackingStorage(), SourceMapper()
    upload_id = await upload_source(app_engine, who, storage)
    first = await _assess(app_engine, who, storage, mapper, upload_id)
    storage.blobs[next(iter(storage.blobs))] = _workbook("25")
    changed = await _assess(app_engine, who, storage, mapper, upload_id)
    assert changed.assessment_id != first.assessment_id
    assert first.lines[0].calculated_to_buy == 20
    assert changed.lines[0].calculated_to_buy == 25


async def test_application_cannot_mutate_saved_evidence(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who = buyer(purchasing_rows)
    storage = TrackingStorage()
    upload_id = await upload_source(app_engine, who, storage)
    assessment = await _assess(app_engine, who, storage, SourceMapper(), upload_id)
    assert assessment.assessment_id is not None
    with pytest.raises(DBAPIError):
        async with tenant_session(app_engine, who.tenant_id) as session:
            await session.execute(
                update(ProcurementAssessment)
                .where(
                    ProcurementAssessment.id == assessment.assessment_id,
                )
                .values(assessment={"status": "assessed", "lines": []})
            )
    restored = await get_snapshot(app_engine, who, assessment.assessment_id)
    assert restored.lines == assessment.lines


@pytest.mark.parametrize("other_tenant", [False, True])
async def test_assessment_cannot_be_read_or_drafted_by_another_buyer(
    app_engine: AsyncEngine,
    purchasing_rows: People,
    other_tenant: bool,
) -> None:
    who = buyer(purchasing_rows)
    storage = TrackingStorage()
    upload_id = await upload_source(app_engine, who, storage)
    assessment = await _assess(app_engine, who, storage, SourceMapper(), upload_id)
    assert assessment.assessment_id is not None
    foreign = Principal(
        tenant_id=purchasing_rows.other_tenant if other_tenant else purchasing_rows.tenant,
        user_id=purchasing_rows.other_approver if other_tenant else purchasing_rows.approver,
        role="approver",
    )
    with pytest.raises(NotFound):
        await get_snapshot(app_engine, foreign, assessment.assessment_id)
    with pytest.raises(NotFound):
        await draft_from_snapshot(app_engine, foreign, assessment.assessment_id, _draft_request())
    if other_tenant:
        async with tenant_session(app_engine, foreign.tenant_id) as session:
            assert await session.get(ProcurementAssessment, assessment.assessment_id) is None


async def test_concurrent_saves_and_draft_requests_create_one_of_each(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who = buyer(purchasing_rows)
    storage = TrackingStorage()
    upload_id = await upload_source(app_engine, who, storage)
    source = await _assess(app_engine, who, storage, SourceMapper(), upload_id)
    # Exercise the unique-key conflict path, independently of the read cache.
    first, second = await asyncio.gather(
        *[
            save_snapshot(
                app_engine,
                who,
                source_key="concurrent-source",
                upload_ids=[upload_id],
                objective="Concurrent assessment",
                assessment=source,
            )
            for _ in range(2)
        ]
    )
    assert first.assessment_id is not None and first.assessment_id == second.assessment_id
    drafts = await asyncio.gather(
        *[
            draft_from_snapshot(app_engine, who, first.assessment_id, _draft_request())
            for _ in range(2)
        ]
    )
    assert drafts[0].rfq_id == drafts[1].rfq_id
    assert sorted(d.reused for d in drafts) == [False, True]
    async with tenant_session(app_engine, who.tenant_id) as session:
        assert await session.scalar(select(func.count()).select_from(Rfq)) == 1
        assert await session.scalar(select(func.count()).select_from(RfqLine)) == 1


async def test_unknown_quantity_blocks_draft_without_creating_business_rows(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who = buyer(purchasing_rows)
    storage = TrackingStorage()
    upload_id = await upload_source(app_engine, who, storage, quantity=None)
    source = await _assess(app_engine, who, storage, SourceMapper(), upload_id)
    assert source.assessment_id is not None and source.status == "needs_review"
    with pytest.raises(InvalidRequest):
        await draft_from_snapshot(app_engine, who, source.assessment_id, _draft_request())
    async with tenant_session(app_engine, who.tenant_id) as session:
        assert await session.scalar(select(func.count()).select_from(Rfq)) == 0


async def test_draft_preserves_decimal_units_evidence_and_does_not_send(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who = buyer(purchasing_rows)
    storage = TrackingStorage()
    exact = Decimal("123456789.123456789")
    upload_id = await upload_source(app_engine, who, storage, quantity=str(exact), unit="kg")
    source = await _assess(app_engine, who, storage, SourceMapper(), upload_id)
    assert source.assessment_id is not None and source.status == "assessed"
    result = await draft_from_snapshot(app_engine, who, source.assessment_id, _draft_request())
    async with tenant_session(app_engine, who.tenant_id) as session:
        rfq = await session.get(Rfq, result.rfq_id)
        line = await session.scalar(select(RfqLine).where(RfqLine.rfq_id == result.rfq_id))
        assert rfq is not None and rfq.status == "draft"
        assert line is not None and line.unit == "kg" and line.part_number == "000010"
        assert Decimal(str(line.quantities[0])) == exact
        assert line.source_evidence == {
            "assessment_id": str(source.assessment_id),
            "line": source.lines[0].model_dump(mode="json"),
            "supplier_links": [],
        }
        for model in (Approval, Message, Invite):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    restored = await get_snapshot(app_engine, who, source.assessment_id)
    assert restored.rfq_id == result.rfq_id
    with pytest.raises(Conflict):
        await draft_from_snapshot(
            app_engine,
            who,
            source.assessment_id,
            AssessmentRfqRequest(title="Other title", base_currency="EUR"),
        )


async def test_snapshot_search_escapes_wildcards_and_excludes_other_buyers(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who = buyer(purchasing_rows)
    storage = TrackingStorage()
    upload_id = await upload_source(app_engine, who, storage)
    source = await _assess(app_engine, who, storage, SourceMapper(), upload_id)
    for suffix, objective in (
        ("exact", "Motor 100% requirement"),
        ("other", "Motor 100x requirement"),
    ):
        await save_snapshot(
            app_engine,
            who,
            source_key=suffix,
            upload_ids=[upload_id],
            objective=objective,
            assessment=source,
        )
    matching = await list_snapshots(app_engine, who, search="MOTOR 100%")
    assert len(matching) == 1 and matching[0].objective == "Motor 100% requirement"
    assert matching[0].line_count == 1 and matching[0].status == "assessed"
    assert len(await list_snapshots(app_engine, who, limit=1)) == 1
    for foreign in (
        Principal(tenant_id=who.tenant_id, user_id=purchasing_rows.approver, role="approver"),
        Principal(
            tenant_id=purchasing_rows.other_tenant,
            user_id=purchasing_rows.other_approver,
            role="approver",
        ),
    ):
        assert await list_snapshots(app_engine, foreign) == []
    with pytest.raises(InvalidRequest, match="limit"):
        await list_snapshots(app_engine, who, limit=51)


class TwoRowMapper(SourceMapper):
    """SourceMapper, reading two data rows per table instead of one."""

    async def __call__(self, task: MappingTask, context: ToolContext) -> MappingProposal:
        proposal = await super().__call__(task, context)
        return proposal.model_copy(
            update={"sections": [s.model_copy(update={"last_row": 3}) for s in proposal.sections]}
        )


async def test_a_preliminary_draft_keeps_the_assessed_units_and_skips_unready_lines(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who = buyer(purchasing_rows)
    storage = TrackingStorage()
    upload = await onboarding.store_upload(
        app_engine,
        storage,
        who,
        filename="procurement.xlsx",
        content_type=XLSX,
        data=workbook(
            {
                "Stock": [
                    ["Material", "Description", "Unit", "Available", "On order", "Safety stock"],
                    ["FI-CU-180", "Copper wire", "kg", "0", "0", "0"],
                    ["IM-N48H-40", "Magnet", None, "0", "0", "0"],
                ],
                "Requirements": [
                    ["Material", "Unit", "Quantity", "Need by", "Demand basis"],
                    ["FI-CU-180", "kg", "150", "2026-11-01", "Gross"],
                    ["IM-N48H-40", None, "400", "2026-11-01", "Gross"],
                ],
            }
        ),
    )
    source = await _assess(app_engine, who, storage, TwoRowMapper(), upload.id)
    assert source.assessment_id is not None and source.status == "needs_review"

    # Open issues still block a normal draft...
    with pytest.raises(InvalidRequest):
        await draft_from_snapshot(app_engine, who, source.assessment_id, _draft_request())
    # ...and a preliminary one carries them, with the units the assessment found.
    result = await draft_from_snapshot(
        app_engine,
        who,
        source.assessment_id,
        AssessmentRfqRequest(
            title="Motor component enquiry",
            base_currency="EUR",
            pending_validation="engineering validation of the BOM",
        ),
    )

    assert result.preliminary and result.skipped == ["IM-N48H-40"]
    assert result.title == (
        "PRELIMINARY (pending engineering validation of the BOM): Motor component enquiry"
    )
    async with tenant_session(app_engine, who.tenant_id) as session:
        lines = list(await session.scalars(select(RfqLine).where(RfqLine.rfq_id == result.rfq_id)))
        for model in (Approval, Message, Invite):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
    assert [(line.part_number, line.unit, line.quantities) for line in lines] == [
        ("FI-CU-180", "kg", ["150"])
    ]
