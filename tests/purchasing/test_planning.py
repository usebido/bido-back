"""Planner checkpoints and import safety with structured fakes, never live models."""

import asyncio
from collections.abc import Sequence
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bido_back.agents.catalog.contracts import ColumnAssignment, SheetMappingOutput, SheetSection
from bido_back.agents.context import ToolContext
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import InventoryRow, Item, SpreadsheetPlan
from bido_back.onboarding import service as onboarding
from bido_back.purchasing import planning
from bido_back.purchasing import service as purchasing_service
from bido_back.purchasing.importers import ParsedTable
from bido_back.purchasing.inspection import SpreadsheetWindow, inspect_spreadsheet
from bido_back.purchasing.planning_agents import (
    PlanningAgentError,
    WorkbookAgents,
    analyst_agent,
    planner_agent,
    reviewer_agent,
)
from bido_back.purchasing.planning_contracts import (
    CellEvidence,
    PlannerInput,
    ReviewInput,
    SheetAnalysisInput,
    SheetTaskProposal,
    SheetUnderstanding,
    WorkbookIssue,
    WorkbookPlanProposal,
    WorkbookPlanState,
    WorkbookPlanView,
    WorkbookReview,
)
from bido_back.purchasing.workbook import WorkbookFileError
from tests.approvals.conftest import People
from tests.purchasing.conftest import FakeStorage
from tests.purchasing.test_workbook import XLSX, workbook


def buyer(people: People) -> Principal:
    return Principal(tenant_id=people.tenant, user_id=people.owner, role="owner")


def context(who: Principal) -> ToolContext:
    return ToolContext(tenant_id=who.tenant_id, user_id=who.user_id, run_id=uuid4())


def stock(rows: int = 1, columns: int = 2) -> list[list[object]]:
    return [
        ["Code", "Available", *[f"Extra {i}" for i in range(columns - 2)]],
        *[[f"P-{i}", 10, *["note"] * (columns - 2)] for i in range(rows)],
    ]


def stock_mapping(sheet: str = "Stock") -> SheetMappingOutput:
    return SheetMappingOutput(
        sections=[
            SheetSection(
                sheet=sheet,
                import_type="inventory",
                header_row=1,
                first_data_row=2,
                last_data_row=None,
                columns=[
                    ColumnAssignment(field="part_number", column=0),
                    ColumnAssignment(field="on_hand", column=1),
                ],
                confidence=0.95,
                question=None,
            )
        ]
    )


class Specialists:
    def __init__(self) -> None:
        self.plans: list[PlannerInput] = []
        self.pages: list[SheetAnalysisInput] = []
        self.reviews: list[ReviewInput] = []
        self.issues: list[WorkbookIssue] = []
        self.omit_sheet = False

    async def plan(self, task: PlannerInput, ctx: ToolContext) -> WorkbookPlanProposal:
        self.plans.append(task)
        sheets = task.workbook.sheets[:1] if self.omit_sheet else task.workbook.sheets
        return WorkbookPlanProposal(
            tasks=[
                SheetTaskProposal(sheet=s.name, objective="Read and map this sheet") for s in sheets
            ]
        )

    async def analyze(self, task: SheetAnalysisInput, ctx: ToolContext) -> SheetUnderstanding:
        self.pages.append(task)
        return SheetUnderstanding(
            role="inventory" if task.page.sheet == "Stock" else "notes",
            summary="Source inventory" if task.page.sheet == "Stock" else "Unsupported notes",
            mapping=stock_mapping()
            if task.page.sheet == "Stock"
            else SheetMappingOutput(sections=[]),
            issues=list(self.issues),
        )

    async def review(self, task: ReviewInput, ctx: ToolContext) -> WorkbookReview:
        self.reviews.append(task)
        return WorkbookReview(summary="Reviewed source mappings", issues=[])

    def agents(self) -> WorkbookAgents:
        return WorkbookAgents(plan=self.plan, analyze=self.analyze, review=self.review)


async def create(
    db: AsyncEngine,
    storage: FakeStorage,
    who: Principal,
    rows: Sequence[Sequence[object]] | None = None,
) -> WorkbookPlanView:
    upload = await onboarding.store_upload(
        db,
        storage,
        who,
        filename="varied.xlsx",
        content_type=XLSX,
        data=workbook({"Stock": stock() if rows is None else rows, "Notes": [["Not imported"]]}),
    )
    return await planning.create_plan(db, storage, who, upload.id, "Understand and import stock")


async def finish(
    db: AsyncEngine,
    storage: FakeStorage,
    who: Principal,
    plan_id: UUID,
    agents: WorkbookAgents,
) -> WorkbookPlanView:
    for _ in range(20):
        result = await planning.advance_plan(
            db, storage, who, plan_id, agents, context(who), max_steps=3
        )
        if result.status not in ("pending", "running"):
            return result
    raise AssertionError("plan did not finish within its test budget")


def test_specialists_have_no_tools_or_side_effect_capabilities() -> None:
    definitions = (planner_agent(), analyst_agent(), reviewer_agent())
    assert len({d.name for d in definitions}) == 3
    for definition in definitions:
        assert not definition.tools
        assert definition.max_turns == 2
        assert definition.prompt_version == 1


@pytest.mark.parametrize("dependencies", [["B"], ["missing"]])
def test_planner_cannot_omit_sheets_or_create_cycles(dependencies: list[str]) -> None:
    manifest = inspect_spreadsheet(
        workbook({"A": [[1]], "B": [[2]]}), "test.xlsx", SpreadsheetWindow()
    )
    state = WorkbookPlanState(objective="Read", manifest=manifest, tasks=[])
    with pytest.raises(PlanningAgentError, match="every sheet"):
        planning.compile_tasks(
            WorkbookPlanProposal(tasks=[SheetTaskProposal(sheet="A", objective="Read")]), state
        )
    proposal = WorkbookPlanProposal(
        tasks=[
            SheetTaskProposal(sheet="A", objective="Read", depends_on=dependencies),
            SheetTaskProposal(sheet="B", objective="Read", depends_on=["A"]),
        ]
    )
    with pytest.raises(PlanningAgentError, match="cyclic or reference"):
        planning.compile_tasks(proposal, state)


def test_planner_dependency_order_is_server_controlled() -> None:
    manifest = inspect_spreadsheet(
        workbook({"A": [[1]], "B": [[2]]}), "test.xlsx", SpreadsheetWindow()
    )
    state = WorkbookPlanState(objective="Read", manifest=manifest, tasks=[])
    tasks = planning.compile_tasks(
        WorkbookPlanProposal(
            tasks=[
                SheetTaskProposal(sheet="A", objective="Join B", depends_on=["B"]),
                SheetTaskProposal(sheet="B", objective="Read identifiers"),
            ]
        ),
        state,
    )
    assert [t.sheet for t in tasks] == ["B", "A", None]
    assert tasks[1].depends_on == ["plan", tasks[0].id]
    assert tasks[-1].depends_on == [tasks[0].id, tasks[1].id]


@pytest.mark.db
async def test_resume_full_page_coverage_dry_run_and_idempotent_import(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who, storage, fake = buyer(purchasing_rows), FakeStorage(), Specialists()
    plan = await create(app_engine, storage, who, stock(120, 21))
    again = await planning.create_plan(app_engine, storage, who, plan.upload_id, plan.objective)
    assert again.plan_id == plan.plan_id
    progress = await planning.advance_plan(
        app_engine, storage, who, plan.plan_id, fake.agents(), context(who), max_steps=3
    )
    assert progress.status == "running"
    assert progress.tasks[1].pages_read == 2
    assert progress.tasks[1].next_row == 51
    assert progress.tasks[1].next_column == 1
    saved = await planning.get_plan(app_engine, who, plan.plan_id)
    assert saved == progress
    done = await finish(app_engine, storage, who, plan.plan_id, fake.agents())
    assert done.status == "ready"
    assert all(t.status == "completed" for t in done.tasks)
    assert done.tasks[1].pages_read == 6
    assert done.tasks[1].cells_read == 121 * 21
    assert len(fake.plans) == len(fake.reviews) == 1
    assert [
        (p.page.first_row, p.page.first_column) for p in fake.pages if p.page.sheet == "Stock"
    ] == [
        (1, 1),
        (1, 21),
        (51, 1),
        (51, 21),
        (101, 1),
        (101, 21),
    ]
    assert fake.pages[1].previous is not None
    assert done.validation is not None
    assert done.validation.unimported_sheets == ["Notes"]
    async with tenant_session(app_engine, who.tenant_id) as session:
        assert await session.scalar(select(func.count()).select_from(Item)) == 0
    preview = await planning.import_plan(
        app_engine, storage, who, plan.plan_id, plan.upload_id, confirmed=False, revision=1
    )
    assert preview.status == "needs_confirmation"
    report = await planning.import_plan(
        app_engine, storage, who, plan.plan_id, plan.upload_id, confirmed=True, revision=1
    )
    repeated = await planning.import_plan(
        app_engine, storage, who, plan.plan_id, plan.upload_id, confirmed=True, revision=1
    )
    assert repeated == report
    assert report.status == "imported"
    async with tenant_session(app_engine, who.tenant_id) as session:
        assert await session.scalar(select(func.count()).select_from(InventoryRow)) == 120


@pytest.mark.db
async def test_model_approval_cannot_override_invalid_values(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who, storage, fake = buyer(purchasing_rows), FakeStorage(), Specialists()
    plan = await create(app_engine, storage, who, [["Code", "Available"], ["P-1", "invalid"]])
    done = await finish(app_engine, storage, who, plan.plan_id, fake.agents())
    assert done.status == "needs_input"
    assert done.review and any(i.severity == "blocker" for i in done.review.issues)
    with pytest.raises(WorkbookFileError, match="not ready"):
        await planning.import_plan(
            app_engine, storage, who, plan.plan_id, plan.upload_id, confirmed=True, revision=1
        )


@pytest.mark.db
async def test_blocker_survives_later_pages_and_reviewer_then_revision_invalidates_mapping(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who, storage, fake = buyer(purchasing_rows), FakeStorage(), Specialists()
    issue = WorkbookIssue(
        severity="blocker",
        message="Conflicting units without a conversion",
        evidence=[CellEvidence(sheet="Stock", row=2, column=2)],
    )
    fake.issues = [issue]
    plan = await create(app_engine, storage, who, stock(55))
    await planning.advance_plan(
        app_engine, storage, who, plan.plan_id, fake.agents(), context(who), max_steps=2
    )
    fake.issues = []
    done = await finish(app_engine, storage, who, plan.plan_id, fake.agents())
    assert done.status == "needs_input"
    assert done.review and issue in done.review.issues
    revised = await planning.revise_plan(
        app_engine, who, plan.plan_id, "Stock values use KG; UN is a label error."
    )
    assert revised.revision == 2
    assert revised.status == "pending"
    assert revised.mapping is revised.review is revised.validation is None
    assert len(revised.tasks) == 1
    ready = await finish(app_engine, storage, who, plan.plan_id, fake.agents())
    assert ready.status == "ready"
    assert fake.plans[-1].clarification == revised.clarification
    with pytest.raises(WorkbookFileError, match="revision changed"):
        await planning.import_plan(
            app_engine, storage, who, plan.plan_id, plan.upload_id, confirmed=True, revision=1
        )


@pytest.mark.db
async def test_missing_sheet_has_bounded_retries_and_never_advances(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who, storage, fake = buyer(purchasing_rows), FakeStorage(), Specialists()
    fake.omit_sheet = True
    plan = await create(app_engine, storage, who)
    done = await finish(app_engine, storage, who, plan.plan_id, fake.agents())
    assert done.status == "failed"
    assert done.tasks[0].failures == 3
    assert len(fake.plans) == 3
    assert fake.plans[-1].retry_feedback == done.tasks[0].error
    assert not fake.pages and not fake.reviews


@pytest.mark.db
async def test_plans_are_private_to_tenant_and_upload_owner(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who, storage = buyer(purchasing_rows), FakeStorage()
    plan = await create(app_engine, storage, who)
    for outsider in (
        Principal(tenant_id=purchasing_rows.other_tenant, user_id=purchasing_rows.other_approver),
        Principal(tenant_id=who.tenant_id, user_id=purchasing_rows.approver),
    ):
        with pytest.raises(WorkbookFileError, match="not found"):
            await planning.get_plan(app_engine, outsider, plan.plan_id)
        with pytest.raises(WorkbookFileError, match="not found"):
            await planning.create_plan(app_engine, storage, outsider, plan.upload_id, "Read")
    async with tenant_session(app_engine, purchasing_rows.other_tenant) as session:
        assert await session.get(SpreadsheetPlan, plan.plan_id) is None


@pytest.mark.db
async def test_source_change_prevents_execution_and_confirmed_import(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who, storage, fake = buyer(purchasing_rows), FakeStorage(), Specialists()
    plan = await create(app_engine, storage, who)
    ready = await finish(app_engine, storage, who, plan.plan_id, fake.agents())
    assert ready.status == "ready"
    key = next(iter(storage.blobs))
    storage.blobs[key] = workbook({"Stock": stock(2)})
    with pytest.raises(WorkbookFileError, match="workbook changed"):
        await planning.import_plan(
            app_engine, storage, who, plan.plan_id, plan.upload_id, confirmed=True, revision=1
        )
    await planning.revise_plan(app_engine, who, plan.plan_id, "Recheck source")
    failed = await planning.advance_plan(
        app_engine, storage, who, plan.plan_id, fake.agents(), context(who)
    )
    assert failed.status == "failed"
    assert len(fake.plans) == 1


@pytest.mark.db
async def test_concurrent_execution_uses_one_lease(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who, storage, fake = buyer(purchasing_rows), FakeStorage(), Specialists()
    plan = await create(app_engine, storage, who)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(task: PlannerInput, ctx: ToolContext) -> WorkbookPlanProposal:
        entered.set()
        await release.wait()
        return await fake.plan(task, ctx)

    agents = WorkbookAgents(plan=slow, analyze=fake.analyze, review=fake.review)
    running = asyncio.create_task(
        planning.advance_plan(app_engine, storage, who, plan.plan_id, agents, context(who))
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        duplicate = await planning.advance_plan(
            app_engine, storage, who, plan.plan_id, agents, context(who)
        )
        assert duplicate.busy
        assert not fake.plans
    finally:
        release.set()
        result = await asyncio.wait_for(running, timeout=5)
    assert len(fake.plans) == 1
    assert not result.busy
    assert result.tasks[0].status == "completed"


@pytest.mark.db
async def test_provider_errors_do_not_leak_source_or_exception_content(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who, storage, fake = buyer(purchasing_rows), FakeStorage(), Specialists()
    plan = await create(app_engine, storage, who)

    async def broken(task: PlannerInput, ctx: ToolContext) -> WorkbookPlanProposal:
        raise RuntimeError("private source content must not escape")

    agents = WorkbookAgents(plan=broken, analyze=fake.analyze, review=fake.review)
    result = await planning.advance_plan(
        app_engine, storage, who, plan.plan_id, agents, context(who)
    )
    assert result.tasks[0].failures == 1
    assert "private source" not in result.model_dump_json()


@pytest.mark.db
async def test_later_page_cannot_silently_drop_a_previously_mapped_table(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    who, storage, fake = buyer(purchasing_rows), FakeStorage(), Specialists()
    plan = await create(app_engine, storage, who, stock(55))
    await planning.advance_plan(
        app_engine, storage, who, plan.plan_id, fake.agents(), context(who), max_steps=2
    )

    async def forget(task: SheetAnalysisInput, ctx: ToolContext) -> SheetUnderstanding:
        fake.pages.append(task)
        return SheetUnderstanding(
            role="notes", summary="Forgot the table", mapping=SheetMappingOutput(sections=[])
        )

    agents = WorkbookAgents(plan=fake.plan, analyze=forget, review=fake.review)
    result = await finish(app_engine, storage, who, plan.plan_id, agents)
    assert result.status == "failed"
    assert result.tasks[1].pages_read == 1
    assert result.tasks[1].result is not None
    assert result.tasks[1].result.mapping == stock_mapping()
    assert fake.pages[-1].retry_feedback == result.tasks[1].error
    assert not fake.reviews


@pytest.mark.db
async def test_import_failure_rolls_back_data_and_receipt_together(
    app_engine: AsyncEngine,
    purchasing_rows: People,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    who, storage, fake = buyer(purchasing_rows), FakeStorage(), Specialists()
    plan = await create(app_engine, storage, who)
    await finish(app_engine, storage, who, plan.plan_id, fake.agents())
    # Even a model-supplied confirmed flag cannot bypass the first preview.
    preview = await planning.import_plan(
        app_engine, storage, who, plan.plan_id, plan.upload_id, confirmed=True, revision=1
    )
    assert preview.status == "needs_confirmation"
    original = purchasing_service.apply_parsed

    async def fail_after_write(
        session: AsyncSession,
        tenant_id: UUID,
        table: ParsedTable,
    ) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
        await original(session, tenant_id, table)
        raise RuntimeError("simulated transaction failure")

    monkeypatch.setattr(purchasing_service, "apply_parsed", fail_after_write)
    with pytest.raises(RuntimeError, match="simulated transaction failure"):
        await planning.import_plan(
            app_engine, storage, who, plan.plan_id, plan.upload_id, confirmed=True, revision=1
        )
    async with tenant_session(app_engine, who.tenant_id) as session:
        assert await session.scalar(select(func.count()).select_from(Item)) == 0
        assert await session.scalar(select(func.count()).select_from(InventoryRow)) == 0
    assert (await planning.get_plan(app_engine, who, plan.plan_id)).status == "ready"
    monkeypatch.setattr(purchasing_service, "apply_parsed", original)
    report = await planning.import_plan(
        app_engine, storage, who, plan.plan_id, plan.upload_id, confirmed=True, revision=1
    )
    assert report.status == "imported"
