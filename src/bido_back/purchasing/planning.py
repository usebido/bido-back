"""Persisted plan -> page analysis -> deterministic validation -> semantic review.

Model calls run outside database transactions. A short lease and compare-and-swap
checkpoint prevent concurrent executions from overwriting one another. Only
source-backed, fully read plans can reach the existing buyer-confirmed importer.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from bido_back.agents.catalog.contracts import SheetMappingOutput
from bido_back.agents.context import ToolContext
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import SpreadsheetPlan
from bido_back.purchasing.inspection import (
    SpreadsheetInspection,
    SpreadsheetWindow,
    inspect_spreadsheet,
)
from bido_back.purchasing.planning_agents import PlanningAgentError, WorkbookAgents
from bido_back.purchasing.planning_contracts import (
    PlannerInput,
    PlanTask,
    ReviewInput,
    SheetAnalysisInput,
    SheetUnderstanding,
    WorkbookIssue,
    WorkbookPlanProposal,
    WorkbookPlanState,
    WorkbookPlanView,
    WorkbookReview,
)
from bido_back.purchasing.workbook import (
    MAX_SHEETS,
    Db,
    WorkbookFileError,
    WorkbookImportReport,
    import_workbook,
    load_workbook_upload,
)
from bido_back.storage import BlobStorage

MAX_FAILURES = 3
MAX_PAGES_PER_SHEET = 1000
LEASE_SECONDS = 300


def _initial(objective: str) -> PlanTask:
    return PlanTask(id="plan", kind="plan", objective=objective)


def _view(row: SpreadsheetPlan) -> WorkbookPlanView:
    return WorkbookPlanView.model_validate(
        {
            **row.state,
            "plan_id": row.id,
            "upload_id": row.upload_id,
            "busy": row.lease_until is not None and row.lease_until > datetime.now(UTC),
        }
    )


async def get_plan(db: Db, principal: Principal, plan_id: UUID) -> WorkbookPlanView:
    async with tenant_session(db, principal.tenant_id) as session:
        row = await session.scalar(
            select(SpreadsheetPlan).where(
                SpreadsheetPlan.id == plan_id, SpreadsheetPlan.user_id == principal.user_id
            )
        )
        if row is None:
            raise WorkbookFileError("spreadsheet plan not found")
        return _view(row)


async def create_plan(
    db: Db, storage: BlobStorage, principal: Principal, upload_id: UUID, objective: str
) -> WorkbookPlanView:
    filename, data = await load_workbook_upload(db, storage, principal, upload_id)
    manifest = inspect_spreadsheet(data, filename, SpreadsheetWindow())
    if not manifest.sheets or len(manifest.sheets) > MAX_SHEETS:
        raise WorkbookFileError(f"planning requires 1 to {MAX_SHEETS} sheets; split larger files")
    if not objective.strip() or len(objective) > 4000:
        raise WorkbookFileError("provide a workbook objective of 1 to 4000 characters")
    digest = hashlib.sha256(data).hexdigest()
    state = WorkbookPlanState(
        objective=objective, manifest=manifest, tasks=[_initial("Plan every workbook sheet")]
    )
    async with tenant_session(db, principal.tenant_id) as session:
        await session.execute(
            insert(SpreadsheetPlan)
            .values(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                upload_id=upload_id,
                source_sha256=digest,
                state=state.model_dump(mode="json"),
            )
            .on_conflict_do_nothing(constraint="spreadsheet_plans_source_key")
        )
        row = await session.scalar(
            select(SpreadsheetPlan).where(
                SpreadsheetPlan.user_id == principal.user_id,
                SpreadsheetPlan.upload_id == upload_id,
                SpreadsheetPlan.source_sha256 == digest,
            )
        )
        assert row is not None
        return _view(row)


def compile_tasks(proposal: WorkbookPlanProposal, state: WorkbookPlanState) -> list[PlanTask]:
    """Reject omissions and cycles rather than silently repairing a planner's output."""
    names = {s.name for s in state.manifest.sheets}
    proposed = [t.sheet for t in proposal.tasks]
    if len(proposed) != len(set(proposed)) or set(proposed) != names:
        raise PlanningAgentError("planner must cover every sheet exactly once")
    completed: set[str] = set()
    tasks: list[PlanTask] = []
    pending = list(proposal.tasks)
    ids = {t.sheet: f"sheet-{i}" for i, t in enumerate(proposal.tasks, start=1)}
    while pending:
        ready = [t for t in pending if set(t.depends_on) <= completed]
        if not ready:
            raise PlanningAgentError("planner dependencies are cyclic or reference unknown sheets")
        for task in ready:
            tasks.append(
                PlanTask(
                    id=ids[task.sheet],
                    kind="sheet",
                    sheet=task.sheet,
                    objective=task.objective,
                    depends_on=["plan", *[ids[name] for name in task.depends_on]],
                )
            )
            completed.add(task.sheet)
            pending.remove(task)
    tasks.append(
        PlanTask(
            id="review",
            kind="review",
            objective="Validate mappings and reconcile all sheets",
            depends_on=[t.id for t in tasks],
        )
    )
    return tasks


def _check_issues(issues: list[WorkbookIssue], state: WorkbookPlanState) -> None:
    sheets = {s.name: s for s in state.manifest.sheets}
    for issue in issues:
        for ref in issue.evidence:
            sheet = sheets.get(ref.sheet)
            if sheet is None or ref.row > sheet.rows or ref.column > sheet.columns:
                raise PlanningAgentError("specialist cited a cell outside the workbook")


@dataclass(frozen=True)
class _Claim:
    token: UUID
    source_sha256: str
    upload_id: UUID
    state: WorkbookPlanState


async def _claim(db: Db, principal: Principal, plan_id: UUID) -> _Claim | None:
    now = datetime.now(UTC)
    async with tenant_session(db, principal.tenant_id) as session:
        row = await session.scalar(
            select(SpreadsheetPlan)
            .where(SpreadsheetPlan.id == plan_id, SpreadsheetPlan.user_id == principal.user_id)
            .with_for_update()
        )
        if row is None:
            raise WorkbookFileError("spreadsheet plan not found")
        state = WorkbookPlanState.model_validate(row.state)
        if state.status not in ("pending", "running"):
            return None
        if row.lease_until is not None and row.lease_until > now:
            return None
        token = uuid4()
        row.lease_token, row.lease_until = token, now + timedelta(seconds=LEASE_SECONDS)
        return _Claim(token, row.source_sha256, row.upload_id, state)


async def _save(db: Db, principal: Principal, plan_id: UUID, claim: _Claim) -> None:
    async with tenant_session(db, principal.tenant_id) as session:
        saved = await session.scalar(
            update(SpreadsheetPlan)
            .where(
                SpreadsheetPlan.id == plan_id,
                SpreadsheetPlan.user_id == principal.user_id,
                SpreadsheetPlan.lease_token == claim.token,
            )
            .values(
                state=claim.state.model_dump(mode="json"),
                lease_token=None,
                lease_until=None,
                updated_at=datetime.now(UTC),
            )
            .returning(SpreadsheetPlan.id)
        )
        if saved is None:
            raise WorkbookFileError("plan execution lease changed; fetch the latest progress")


async def advance_plan(
    db: Db,
    storage: BlobStorage,
    principal: Principal,
    plan_id: UUID,
    agents: WorkbookAgents,
    context: ToolContext,
    *,
    max_steps: int = 1,
) -> WorkbookPlanView:
    if not 1 <= max_steps <= 3:
        raise WorkbookFileError("execute between 1 and 3 steps per call")
    for _ in range(max_steps):
        claim = await _claim(db, principal, plan_id)
        if claim is None:
            break
        state = claim.state
        done = {t.id for t in state.tasks if t.status == "completed"}
        task = next(
            (t for t in state.tasks if t.status == "pending" and set(t.depends_on) <= done), None
        )
        if task is None:
            state.status = "failed"
            await _save(db, principal, plan_id, claim)
            break
        try:
            filename, data = await load_workbook_upload(db, storage, principal, claim.upload_id)
            if hashlib.sha256(data).hexdigest() != claim.source_sha256:
                raise WorkbookFileError("workbook changed; create a new plan")
            state.status = "running"
            await _execute(
                db,
                storage,
                principal,
                claim.upload_id,
                filename,
                data,
                state,
                task,
                agents,
                context,
            )
            task.failures = 0
            task.error = None
        except Exception as exc:
            # Neither provider messages nor source values belong in execution errors.
            task.failures += 1
            task.error = (
                str(exc)
                if isinstance(exc, PlanningAgentError)
                else "step failed; retry or inspect the source workbook"
            )
            if isinstance(exc, WorkbookFileError) or task.failures >= MAX_FAILURES:
                task.status = "failed"
                state.status = "failed"
        await _save(db, principal, plan_id, claim)
        if task.error or state.status not in ("pending", "running"):
            break
    return await get_plan(db, principal, plan_id)


async def _execute(
    db: Db,
    storage: BlobStorage,
    principal: Principal,
    upload_id: UUID,
    filename: str,
    data: bytes,
    state: WorkbookPlanState,
    task: PlanTask,
    agents: WorkbookAgents,
    context: ToolContext,
) -> None:
    if task.kind == "plan":
        proposal = await agents.plan(
            PlannerInput(
                objective=state.objective,
                clarification=state.clarification,
                workbook=state.manifest,
                samples=_samples(filename, data, state),
                retry_feedback=task.error,
            ),
            context,
        )
        children = compile_tasks(proposal, state)
        task.status = "completed"
        state.tasks.extend(children)
    elif task.kind == "sheet":
        await _analyze_page(filename, data, state, task, agents, context)
    else:
        await _review(
            db, storage, principal, upload_id, filename, data, state, task, agents, context
        )


async def _analyze_page(
    filename: str,
    data: bytes,
    state: WorkbookPlanState,
    task: PlanTask,
    agents: WorkbookAgents,
    context: ToolContext,
) -> None:
    if task.pages_read >= MAX_PAGES_PER_SHEET:
        raise PlanningAgentError("sheet page budget exceeded; split the workbook")
    assert task.sheet is not None
    info = next(s for s in state.manifest.sheets if s.name == task.sheet)
    if info.rows == 0 or info.columns == 0:
        task.result = SheetUnderstanding(
            role="notes", summary="Empty sheet", mapping=SheetMappingOutput(sections=[])
        )
        task.status = "completed"
        return
    page = inspect_spreadsheet(
        data,
        filename,
        SpreadsheetWindow(
            sheet=task.sheet,
            first_row=task.next_row,
            first_column=task.next_column,
        ),
    )
    result = await agents.analyze(
        SheetAnalysisInput(
            objective=task.objective,
            clarification=state.clarification,
            page=page,
            previous=task.result,
            retry_feedback=task.error,
            dependencies=[
                t.result for t in state.tasks if t.id in task.depends_on and t.result is not None
            ],
        ),
        context,
    )
    if any(s.sheet != task.sheet for s in result.mapping.sections):
        raise PlanningAgentError("sheet analyst mapped a different sheet")
    _check_issues(result.issues, state)
    if task.result is not None:
        previous_sections = {
            (s.import_type, s.header_row, s.first_data_row) for s in task.result.mapping.sections
        }
        current_sections = {
            (s.import_type, s.header_row, s.first_data_row) for s in result.mapping.sections
        }
        if not previous_sections <= current_sections:
            raise PlanningAgentError(
                "analyst omitted a previously mapped section; return the accumulated mapping"
            )
        # A later page cannot silently erase an unresolved finding. Buyer-led
        # revision clears these after the complete workbook can be reconsidered.
        retained = [i for i in task.result.issues if i.severity == "blocker"]
        for issue in retained:
            if issue not in result.issues:
                result.issues.append(issue)
        if len(result.issues) > 40:
            raise PlanningAgentError("too many unresolved sheet issues; split or clarify the file")
    task.result = result
    task.pages_read += 1
    task.cells_read += len(page.cells)
    # Every column window of this row band is read before advancing vertically.
    if page.next_column is not None:
        task.next_column = page.next_column
    elif page.next_row is not None:
        task.next_row, task.next_column = page.next_row, 1
    else:
        task.status = "completed"


def _samples(filename: str, data: bytes, state: WorkbookPlanState) -> list[SpreadsheetInspection]:
    return [
        inspect_spreadsheet(
            data, filename, SpreadsheetWindow(sheet=s.name, row_count=3, column_count=20)
        )
        for s in state.manifest.sheets
        if s.rows and s.columns
    ]


async def _review(
    db: Db,
    storage: BlobStorage,
    principal: Principal,
    upload_id: UUID,
    filename: str,
    data: bytes,
    state: WorkbookPlanState,
    task: PlanTask,
    agents: WorkbookAgents,
    context: ToolContext,
) -> None:
    findings = [t.result for t in state.tasks if t.kind == "sheet" and t.result is not None]
    mapping = SheetMappingOutput(sections=[s for f in findings for s in f.mapping.sections])
    validation = await import_workbook(
        db,
        storage,
        principal,
        upload_id,
        mapper=None,
        mapping=mapping,
        dry_run=True,
        source=(filename, data),
    )
    review = await agents.review(
        ReviewInput(
            objective=state.objective,
            clarification=state.clarification,
            sheets=findings,
            samples=_samples(filename, data, state),
            validation=validation,
            retry_feedback=task.error,
        ),
        context,
    )
    _check_issues(review.issues, state)
    inherited = [issue for f in findings for issue in f.issues if issue.severity == "blocker"]
    parse_issues = [
        WorkbookIssue(severity="blocker", message=p[:1000]) for p in validation.problems
    ]
    for section in validation.sections:
        if (
            section.rows_failed
            or section.missing_required
            or section.question
            or section.confidence < 0.7
        ):
            parse_issues.append(
                WorkbookIssue(
                    severity="blocker",
                    message=f"Section in {section.sheet} requires mapping/value correction",
                )
            )
    if not any(s.rows_ok for s in validation.sections):
        parse_issues.append(
            WorkbookIssue(severity="blocker", message="No supported rows to import")
        )
    for name in validation.unimported_sheets:
        parse_issues.append(
            WorkbookIssue(severity="warning", message=f"Sheet not imported: {name}")
        )
    issues = [*review.issues, *inherited, *parse_issues]
    issues.sort(key=lambda i: i.severity != "blocker")
    state.review = WorkbookReview(summary=review.summary, issues=issues[:80])
    state.mapping, state.validation = mapping, validation
    state.status = "needs_input" if any(i.severity == "blocker" for i in issues) else "ready"
    task.status = "completed"


async def revise_plan(
    db: Db, principal: Principal, plan_id: UUID, clarification: str
) -> WorkbookPlanView:
    if not clarification.strip() or len(clarification) > 4000:
        raise WorkbookFileError("a clarification of 1 to 4000 characters is required")
    async with tenant_session(db, principal.tenant_id) as session:
        row = await session.scalar(
            select(SpreadsheetPlan)
            .where(
                SpreadsheetPlan.id == plan_id,
                SpreadsheetPlan.user_id == principal.user_id,
            )
            .with_for_update()
        )
        if row is None:
            raise WorkbookFileError("spreadsheet plan not found")
        current = WorkbookPlanState.model_validate(row.state)
        if current.status not in ("needs_input", "failed", "ready") or _view(row).busy:
            raise WorkbookFileError("only a stopped, unimported plan can be revised")
        state = WorkbookPlanState(
            objective=current.objective,
            clarification=clarification,
            revision=current.revision + 1,
            manifest=current.manifest,
            tasks=[_initial("Replan using the buyer's clarification")],
        )
        row.state = state.model_dump(mode="json")
        row.lease_token, row.lease_until = None, None
        row.updated_at = datetime.now(UTC)
        return _view(row)


async def has_plan(db: Db, principal: Principal, upload_id: UUID) -> bool:
    async with tenant_session(db, principal.tenant_id) as session:
        return (
            await session.scalar(
                select(SpreadsheetPlan.id)
                .where(
                    SpreadsheetPlan.upload_id == upload_id,
                    SpreadsheetPlan.user_id == principal.user_id,
                )
                .limit(1)
            )
            is not None
        )


async def import_plan(
    db: Db,
    storage: BlobStorage,
    principal: Principal,
    plan_id: UUID,
    upload_id: UUID,
    *,
    confirmed: bool,
    revision: int,
) -> WorkbookImportReport:
    """Commit the reviewed mapping and its receipt atomically, at most once per plan."""
    async with tenant_session(db, principal.tenant_id) as session:
        row = await session.scalar(
            select(SpreadsheetPlan)
            .where(
                SpreadsheetPlan.id == plan_id,
                SpreadsheetPlan.user_id == principal.user_id,
                SpreadsheetPlan.upload_id == upload_id,
            )
            .with_for_update()
        )
        if row is None:
            raise WorkbookFileError("spreadsheet plan not found for this upload")
        state = WorkbookPlanState.model_validate(row.state)
        if revision != state.revision:
            raise WorkbookFileError(
                "plan revision changed; review and confirm the current revision"
            )
        if state.status == "imported" and state.validation is not None:
            return state.validation
        if state.status != "ready" or state.mapping is None or _view(row).busy:
            raise WorkbookFileError("plan is not ready; finish or revise its tasks")
        source = await load_workbook_upload(db, storage, principal, upload_id)
        if hashlib.sha256(source[1]).hexdigest() != row.source_sha256:
            raise WorkbookFileError("workbook changed; create a new plan")
        if not confirmed or not state.confirmation_requested:
            assert state.validation is not None
            preview = state.validation.model_copy(deep=True)
            preview.status = "needs_confirmation"
            preview.questions = [
                *preview.questions,
                "Confirm the reviewed mapping and disclosed omissions?",
            ]
            state.confirmation_requested = True
            row.state = state.model_dump(mode="json")
            row.updated_at = datetime.now(UTC)
            return preview
        report = await import_workbook(
            db,
            storage,
            principal,
            upload_id,
            mapper=None,
            mapping=state.mapping,
            confirmed=True,
            apply_session=session,
            source=source,
        )
        if report.status != "imported":
            raise WorkbookFileError("reviewed import is no longer valid; revise the plan")
        state.status, state.validation = "imported", report
        row.state = state.model_dump(mode="json")
        row.updated_at = datetime.now(UTC)
        return report
