"""Source assessment stays within the conversation, user and tenant and writes nothing."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bido_back.agents.context import ToolContext
from bido_back.agents.tools import Tool, ToolError, ToolValidationError
from bido_back.auth import Principal
from bido_back.copilot.journey_tools import (
    AnalyzeSourcesIn,
    AssessmentRfqOut,
    ListAssessmentsOut,
)
from bido_back.copilot.tools import CopilotToolDeps, copilot_tools
from bido_back.db import tenant_session
from bido_back.db.models import (
    Analysis,
    Approval,
    Base,
    BomLine,
    Contact,
    InventoryRow,
    Item,
    PoLine,
    ProductionPlanRow,
    PurchaseOrder,
    Rfq,
    Supplier,
)
from bido_back.onboarding import service as onboarding
from bido_back.purchasing.sources.contracts import (
    ColumnMapping,
    MappingProposal,
    MappingTask,
    SourceAssessment,
    TableProposal,
)
from evals.source_fixtures import EXPECTED, SCOPE
from tests.approvals.conftest import People
from tests.purchasing.conftest import FakeStorage
from tests.purchasing.motor_sources import motor_workbook
from tests.purchasing.test_workbook import XLSX, workbook
from tests.rfqs.conftest import FakeMailboxes, fake_agents

from .conftest import buyer_of, context_of


class TrackingStorage(FakeStorage):
    def __init__(self) -> None:
        super().__init__()
        self.reads: list[str] = []

    async def get(self, key: str) -> bytes:
        self.reads.append(key)
        return await super().get(key)


class SourceMapper:
    def __init__(self) -> None:
        self.contexts: list[ToolContext] = []

    async def __call__(self, task: MappingTask, context: ToolContext) -> MappingProposal:
        self.contexts.append(context)
        if task.table == "Stock":
            section = TableProposal(
                kind="inventory",
                header_row=1,
                first_row=2,
                last_row=2,
                columns=[
                    ColumnMapping(field=field, column=column)
                    for field, column in {
                        "part_number": 1,
                        "description": 2,
                        "unit": 3,
                        "available": 4,
                        "on_order": 5,
                        "safety_stock": 6,
                    }.items()
                ],
            )
        else:
            assert task.table == "Requirements"
            section = TableProposal(
                kind="material_requirement",
                header_row=1,
                first_row=2,
                last_row=2,
                columns=[
                    ColumnMapping(field=field, column=column)
                    for field, column in {
                        "part_number": 1,
                        "unit": 2,
                        "quantity": 3,
                        "need_by": 4,
                        "demand_basis": 5,
                    }.items()
                ],
            )
        return MappingProposal(sections=[section])


def source_tool(
    engine: AsyncEngine,
    storage: TrackingStorage,
    mapper: SourceMapper,
    mailboxes: FakeMailboxes,
    *,
    current: tuple[UUID, ...] = (),
    earlier: tuple[UUID, ...] = (),
) -> Tool[AnalyzeSourcesIn, SourceAssessment]:
    tools = copilot_tools(
        CopilotToolDeps(
            db=engine,
            agents=fake_agents(),
            mailboxes=mailboxes,
            storage=storage,
            attachment_ids=current,
            earlier_attachment_ids=earlier,
            source_mapper=mapper,
        )
    )
    found = next(tool for tool in tools if tool.name == "analyze_procurement_sources")
    assert found.input_model is AnalyzeSourcesIn
    assert found.output_model is SourceAssessment
    return cast(Tool[AnalyzeSourcesIn, SourceAssessment], found)


def test_source_tool_is_read_only_and_has_provider_compatible_inputs() -> None:
    engine = create_async_engine("postgresql+asyncpg://app@localhost:5432/unused")
    tool = source_tool(engine, TrackingStorage(), SourceMapper(), FakeMailboxes())

    assert tool.kind == "read"
    schema = tool.input_schema()
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert set(properties) == {"upload_ids", "objective", "scope", "recompute"}
    definitions = schema["$defs"]
    assert isinstance(definitions, dict)
    scope_schema = definitions["ScopeRequestInput"]
    assert isinstance(scope_schema, dict)
    scope_properties = scope_schema["properties"]
    assert isinstance(scope_properties, dict)
    # The buyer's words, all optional: code settles the rest against the evidence.
    assert set(scope_properties) == {
        "product_code",
        "product",
        "area",
        "location",
        "as_of",
        "horizon_end",
        "horizon_days",
        "safety_stock",
        "production_lead_days",
        "plants",
        "mrp_element",
    }
    assert "required" not in scope_schema
    assert tool.parse_input({"upload_ids": [str(uuid4())], "objective": "What should I buy?"})
    words = tool.parse_input(
        {
            "upload_ids": [str(uuid4())],
            "objective": "O que comprar para o Motor MTR-100 na Planta A?",
            "scope": {
                "product": "Motor MTR-100",
                "location": "Planta A",
                "horizon_days": 14,
                "safety_stock": 0,
            },
        }
    ).scope
    assert words is not None and (words.product_code, words.as_of, words.horizon_days) == (
        None,
        None,
        14,
    )
    invalid_inputs: tuple[dict[str, object], ...] = (
        {"upload_ids": [], "objective": "What should I buy?"},
        {"upload_ids": [str(uuid4())] * 11, "objective": "What should I buy?"},
        {"upload_ids": [str(uuid4())], "objective": ""},
        {
            "upload_ids": [str(uuid4())],
            "objective": "Plan",
            "scope": {"as_of": "2026-10-22", "horizon_end": "2026-10-08"},
        },
    )
    for args in invalid_inputs:
        with pytest.raises(ValidationError):
            tool.parse_input(args)


async def test_source_tool_does_not_substitute_an_unattached_identifier() -> None:
    engine = create_async_engine("postgresql+asyncpg://app@localhost:5432/unused")
    storage, mapper = TrackingStorage(), SourceMapper()
    tool = source_tool(engine, storage, mapper, FakeMailboxes(), current=(uuid4(),))
    context = ToolContext(tenant_id=uuid4(), user_id=uuid4(), run_id=uuid4())

    with pytest.raises(ToolError, match="attached in this conversation"):
        await tool.invoke(context, AnalyzeSourcesIn(upload_ids=[uuid4()], objective="Analyze"))

    assert storage.reads == []
    assert mapper.contexts == []
    await engine.dispose()


@pytest.mark.db
@pytest.mark.parametrize("other_tenant", [False, True])
async def test_all_sources_require_user_and_tenant_access_before_any_content_is_read(
    app_engine: AsyncEngine,
    copilot_rows: People,
    other_tenant: bool,
) -> None:
    principal = buyer_of(copilot_rows)
    foreign = Principal(
        tenant_id=copilot_rows.other_tenant if other_tenant else copilot_rows.tenant,
        user_id=copilot_rows.other_approver if other_tenant else copilot_rows.member,
    )
    storage, mapper = TrackingStorage(), SourceMapper()
    own = await onboarding.store_upload(
        app_engine,
        storage,
        principal,
        filename="own.csv",
        content_type="text/csv",
        data=b"Material,Quantity\nA-1,1\n",
    )
    unavailable = await onboarding.store_upload(
        app_engine,
        storage,
        foreign,
        filename="foreign.csv",
        content_type="text/csv",
        data=b"Material,Quantity\nB-1,2\n",
    )
    tool = source_tool(
        app_engine,
        storage,
        mapper,
        FakeMailboxes(),
        current=(own.id, unavailable.id),
    )

    with pytest.raises(ToolError, match="not available to this buyer"):
        await tool.invoke(
            context_of(principal),
            AnalyzeSourcesIn(
                upload_ids=[own.id, unavailable.id],
                objective="Analyze both sources",
            ),
        )

    assert storage.reads == []
    assert mapper.contexts == []


async def business_counts(engine: AsyncEngine, tenant_id: UUID) -> dict[str, int]:
    models: tuple[type[Base], ...] = (
        Item,
        InventoryRow,
        BomLine,
        ProductionPlanRow,
        PurchaseOrder,
        PoLine,
        Supplier,
        Contact,
        Analysis,
        Approval,
        Rfq,
    )
    async with tenant_session(engine, tenant_id) as session:
        return {
            model.__tablename__: int(
                await session.scalar(select(func.count()).select_from(model)) or 0
            )
            for model in models
        }


@pytest.mark.db
@pytest.mark.parametrize("earlier", [False, True])
async def test_source_assessment_reads_current_and_earlier_files_without_business_writes(
    app_engine: AsyncEngine,
    copilot_rows: People,
    earlier: bool,
) -> None:
    principal = buyer_of(copilot_rows)
    storage, mapper, mailboxes = TrackingStorage(), SourceMapper(), FakeMailboxes()
    upload = await onboarding.store_upload(
        app_engine,
        storage,
        principal,
        filename="procurement.xlsx",
        content_type=XLSX,
        data=workbook(
            {
                "Stock": [
                    ["Material", "Description", "Unit", "Available", "On order", "Safety stock"],
                    ["000010", "Bolts", "pcs", 4, 0, 2],
                ],
                "Requirements": [
                    ["Material", "Unit", "Quantity", "Need by", "Demand basis"],
                    ["000010", "pcs", 20, "2026-11-01", "Gross"],
                ],
            }
        ),
    )
    tool = source_tool(
        app_engine,
        storage,
        mapper,
        mailboxes,
        current=() if earlier else (upload.id,),
        earlier=(upload.id,) if earlier else (),
    )
    before = await business_counts(app_engine, principal.tenant_id)
    args = tool.parse_input({"upload_ids": [str(upload.id)], "objective": "What should I buy?"})

    first = await tool.invoke(context_of(principal), args)
    again = await tool.invoke(context_of(principal), args)

    assert first.status == again.status == "assessed"
    assert first.persisted is again.persisted is False
    assert [(line.part_number, line.calculated_to_buy, line.ready) for line in first.lines] == [
        ("000010", Decimal(18), True),
    ]
    assert len(first.coverage) == 2
    assert all(row.inspected for row in first.coverage)
    assert all(
        context.tenant_id == principal.tenant_id and context.user_id == principal.user_id
        for context in mapper.contexts
    )
    assert await business_counts(app_engine, principal.tenant_id) == before
    assert mailboxes.calls == []
    assert mailboxes.sender.sent == []


@pytest.mark.db
async def test_the_buyers_scope_is_resolved_and_saved_with_the_same_request(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    principal = buyer_of(copilot_rows)
    ctx = context_of(principal)
    storage = TrackingStorage()
    data, mapper = motor_workbook()
    upload = await onboarding.store_upload(
        app_engine, storage, principal, filename="erp.xlsx", content_type=XLSX, data=data
    )
    tools = {
        tool.name: tool
        for tool in copilot_tools(
            CopilotToolDeps(
                db=app_engine,
                agents=fake_agents(),
                mailboxes=FakeMailboxes(),
                storage=storage,
                source_mapper=mapper,
                attachment_ids=(upload.id,),
            )
        )
    }
    scope: dict[str, object] = {
        "product": "Motor MTR-100",
        "location": "PLANT A",
        "horizon_days": 14,
        "safety_stock": 0,
    }
    args: dict[str, object] = {
        "upload_ids": [str(upload.id)],
        "objective": "What do we need to buy for the motor in Plant A?",
        "scope": scope,
    }
    analyze, save = tools["analyze_procurement_sources"], tools["save_procurement_assessment"]

    analysis = SourceAssessment.model_validate(await analyze.invoke(ctx, analyze.parse_input(args)))
    saved = SourceAssessment.model_validate(await save.invoke(ctx, save.parse_input(args)))
    again = SourceAssessment.model_validate(await save.invoke(ctx, save.parse_input(args)))
    later = {**args, "scope": {**scope, "horizon_days": 24}}
    changed = SourceAssessment.model_validate(await save.invoke(ctx, save.parse_input(later)))

    assert analysis.scope == saved.scope == SCOPE
    assert not analysis.persisted and saved.persisted
    assert saved.scope_resolution == analysis.scope_resolution
    assert saved.scope_resolution is not None
    assert saved.scope_resolution.request.location == "PLANT A"
    assert {line.part_number: line.calculated_to_buy for line in saved.lines} == EXPECTED
    assert again.assessment_id == saved.assessment_id != changed.assessment_id
    assert changed.scope is not None and changed.scope.horizon_end == date(2026, 11, 1)


@pytest.mark.db
async def test_saved_evidence_can_be_found_and_drafted_in_a_later_conversation(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    principal = buyer_of(copilot_rows)
    ctx = context_of(principal)
    storage, mapper, mailboxes = TrackingStorage(), SourceMapper(), FakeMailboxes()
    upload = await onboarding.store_upload(
        app_engine,
        storage,
        principal,
        filename="motor.xlsx",
        content_type=XLSX,
        data=workbook(
            {
                "Stock": [
                    ["Material", "Description", "Unit", "Available", "On order", "Safety stock"],
                    ["000010", "Bolts", "pcs", 4, 0, 2],
                ],
                "Requirements": [
                    ["Material", "Unit", "Quantity", "Need by", "Demand basis"],
                    ["000010", "pcs", 20, "2026-11-01", "Gross"],
                ],
            }
        ),
    )
    first = {
        tool.name: tool
        for tool in copilot_tools(
            CopilotToolDeps(
                db=app_engine,
                agents=fake_agents(),
                mailboxes=mailboxes,
                storage=storage,
                source_mapper=mapper,
                attachment_ids=(upload.id,),
            )
        )
    }
    save = first["save_procurement_assessment"]
    saved = SourceAssessment.model_validate(
        await save.invoke(
            ctx,
            save.parse_input(
                {
                    "upload_ids": [str(upload.id)],
                    "objective": "Analyze motor demand",
                }
            ),
        )
    )
    assert saved.persisted and saved.assessment_id is not None
    later = {
        tool.name: tool
        for tool in copilot_tools(
            CopilotToolDeps(
                db=app_engine,
                agents=fake_agents(),
                mailboxes=mailboxes,
                conversation_id=uuid4(),
            )
        )
    }
    find = later["list_procurement_assessments"]
    found = ListAssessmentsOut.model_validate(
        await find.invoke(
            ctx,
            find.parse_input(
                {
                    "search": "motor",
                }
            ),
        )
    )
    assert [row.id for row in found.assessments] == [saved.assessment_id]
    get = later["get_procurement_assessment"]
    restored = SourceAssessment.model_validate(
        await get.invoke(
            ctx,
            get.parse_input(
                {
                    "assessment_id": str(saved.assessment_id),
                }
            ),
        )
    )
    assert restored == saved
    draft = later["create_rfq_from_assessment"]
    args = {
        "assessment_id": str(saved.assessment_id),
        "title": "Motor components",
        "base_currency": "BRL",
    }
    # Nothing is created until the buyer confirms, in a later message.
    proposed = AssessmentRfqOut.model_validate(await draft.invoke(ctx, draft.parse_input(args)))
    assert proposed.awaiting_confirmation and proposed.rfq_id is None
    assert proposed.preview is not None and proposed.preview.issues == []
    assert [(line.part_number, line.unit) for line in proposed.preview.lines] == [
        (saved.lines[0].part_number, saved.lines[0].unit)
    ]
    confirm = replace(ctx, run_id=uuid4())
    result = AssessmentRfqOut.model_validate(
        await draft.invoke(
            confirm, draft.parse_input({**args, "proposal_id": str(proposed.proposal_id)})
        )
    )
    assert result.lines == 1 and result.assessment_id == saved.assessment_id
    assert not mailboxes.calls and not mailboxes.sender.sent


@pytest.mark.db
async def test_a_plant_the_sources_lack_is_a_validation_error_listing_the_plants(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    principal = buyer_of(copilot_rows)
    storage = TrackingStorage()
    data, mapper = motor_workbook()
    upload = await onboarding.store_upload(
        app_engine, storage, principal, filename="erp.xlsx", content_type=XLSX, data=data
    )
    tools = {
        tool.name: tool
        for tool in copilot_tools(
            CopilotToolDeps(
                db=app_engine,
                agents=fake_agents(),
                mailboxes=FakeMailboxes(),
                storage=storage,
                source_mapper=mapper,
                attachment_ids=(upload.id,),
            )
        )
    }
    analyze = tools["analyze_procurement_sources"]
    args = analyze.parse_input(
        {
            "upload_ids": [str(upload.id)],
            "objective": "O que comprar no centro 4000?",
            "scope": {"product": "Motor MTR-100", "plants": ["4000"]},
        }
    )

    with pytest.raises(ToolValidationError) as caught:
        await analyze.invoke(context_of(principal), args)

    error = caught.value
    assert (error.code, error.field, error.value) == ("unknown_plant", "scope.plants", "4000")
    assert error.valid_values == ["Plant A", "Plant B"]
    assert error.payload()["error"] == "validation_error"
    # A column value put where a field should be is an argument error, not silently dropped.
    with pytest.raises(ValidationError):
        analyze.parse_input(
            {
                "upload_ids": [str(upload.id)],
                "objective": "x",
                "scope": {"NecDep": True},
            }
        )
    # The MRP element and the plant selector are read as the buyer says them.
    parsed = analyze.parse_input(
        {
            "upload_ids": [str(upload.id)],
            "objective": "x",
            "scope": {"plants": "3", "mrp_element": "NecDep"},
        }
    )
    assert parsed.scope is not None
    assert (parsed.scope.plants, parsed.scope.mrp_element) == (3, "NecDep")
