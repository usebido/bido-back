"""The copilot manages a persisted workbook workflow through bounded typed tools."""

from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, Field

from bido_back.agents.context import ToolContext
from bido_back.agents.tools import AnyTool, Tool, ToolError
from bido_back.copilot.journey_tools import conversation_upload, journey_principal
from bido_back.purchasing import planning
from bido_back.purchasing.planning_agents import default_workbook_agents
from bido_back.purchasing.planning_contracts import WorkbookPlanView
from bido_back.purchasing.workbook import WorkbookFileError

if TYPE_CHECKING:
    from bido_back.copilot.tools import CopilotToolDeps


class StartWorkbookPlan(BaseModel):
    upload_id: UUID
    objective: str = Field(min_length=1, max_length=4000)


class WorkbookPlanId(BaseModel):
    plan_id: UUID


class AdvanceWorkbookPlan(WorkbookPlanId):
    max_steps: int = Field(default=3, ge=1, le=3)


class ReviseWorkbookPlan(WorkbookPlanId):
    clarification: str = Field(
        min_length=1,
        max_length=4000,
        description="Only explicit buyer clarification; never invent answers to blockers",
    )


def workbook_tools(deps: "CopilotToolDeps") -> tuple[AnyTool, ...]:
    async def authorized(ctx: ToolContext, plan_id: UUID) -> WorkbookPlanView:
        try:
            plan = await planning.get_plan(deps.db, journey_principal(ctx), plan_id)
        except WorkbookFileError as exc:
            raise ToolError(str(exc)) from None
        if plan.upload_id not in (*deps.attachment_ids, *deps.earlier_attachment_ids):
            raise ToolError("the plan's file is not attached in this conversation")
        return plan

    async def start(ctx: ToolContext, args: StartWorkbookPlan) -> WorkbookPlanView:
        upload_id = conversation_upload(
            args.upload_id, deps.attachment_ids, deps.earlier_attachment_ids
        )
        if deps.storage is None:
            raise ToolError("file storage is not available")
        try:
            return await planning.create_plan(
                deps.db, deps.storage, journey_principal(ctx), upload_id, args.objective
            )
        except WorkbookFileError as exc:
            raise ToolError(str(exc)) from None

    async def get(ctx: ToolContext, args: WorkbookPlanId) -> WorkbookPlanView:
        return await authorized(ctx, args.plan_id)

    async def advance(ctx: ToolContext, args: AdvanceWorkbookPlan) -> WorkbookPlanView:
        await authorized(ctx, args.plan_id)
        if deps.storage is None:
            raise ToolError("file storage is not available")
        try:
            return await planning.advance_plan(
                deps.db,
                deps.storage,
                journey_principal(ctx),
                args.plan_id,
                deps.workbook_agents or default_workbook_agents(),
                ctx,
                max_steps=args.max_steps,
            )
        except WorkbookFileError as exc:
            raise ToolError(str(exc)) from None

    async def revise(ctx: ToolContext, args: ReviseWorkbookPlan) -> WorkbookPlanView:
        await authorized(ctx, args.plan_id)
        try:
            return await planning.revise_plan(
                deps.db, journey_principal(ctx), args.plan_id, args.clarification
            )
        except WorkbookFileError as exc:
            raise ToolError(str(exc)) from None

    return (
        Tool(
            "plan_spreadsheet",
            "Create or recover the persisted task plan for an attached "
            "workbook. No purchase data is imported. Then call execute_spreadsheet_plan.",
            StartWorkbookPlan,
            WorkbookPlanView,
            "draft",
            start,
            artifact_kind="spreadsheet_plan",
        ),
        Tool(
            "get_spreadsheet_plan",
            "Get saved tasks, coverage, blockers and review for a plan "
            "in this conversation. State is server-owned and survives later messages.",
            WorkbookPlanId,
            WorkbookPlanView,
            "read",
            get,
        ),
        Tool(
            "execute_spreadsheet_plan",
            "Execute up to three ready steps: plan all sheets, "
            "read and normalize a page, or independently review. Repeat while pending/running. "
            "Never imports or sends. ready means reviewed, not imported; needs_input/failed "
            "requires examining issues. A busy plan is already executing; do not spin.",
            AdvanceWorkbookPlan,
            WorkbookPlanView,
            "draft",
            advance,
            artifact_kind="spreadsheet_plan",
        ),
        Tool(
            "revise_spreadsheet_plan",
            "Invalidate a stopped plan's mapping and replan using "
            "the buyer's explicit clarification. Preserves file identity and increments revision. "
            "Do not use this to invent missing units or bypass review blockers.",
            ReviseWorkbookPlan,
            WorkbookPlanView,
            "draft",
            revise,
            artifact_kind="spreadsheet_plan",
        ),
    )
