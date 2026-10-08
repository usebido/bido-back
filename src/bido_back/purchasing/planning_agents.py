"""Small structured specialists; all model execution remains in the Agno runner."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from uuid import uuid4

from pydantic import BaseModel

from bido_back.agents.catalog.definitions import EXTRACTION_MODEL
from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.agents.definition import AgentDefinition
from bido_back.agents.runner import Completed, run_to_completion
from bido_back.purchasing.planning_contracts import (
    PlannerInput,
    ReviewInput,
    SheetAnalysisInput,
    SheetUnderstanding,
    WorkbookPlanProposal,
    WorkbookReview,
)


class PlanningAgentError(Exception):
    """A model call failed; no provider message or customer data is exposed."""


def planner_agent(model: str = EXTRACTION_MODEL) -> AgentDefinition[WorkbookPlanProposal]:
    return AgentDefinition(
        name="workbook_planner",
        prompt_file="workbook_planner.v1.md",
        prompt_version=1,
        model=model,
        output_model=WorkbookPlanProposal,
        max_turns=2,
        effort="medium",
    )


def analyst_agent(model: str = EXTRACTION_MODEL) -> AgentDefinition[SheetUnderstanding]:
    return AgentDefinition(
        name="workbook_analyst",
        prompt_file="workbook_analyst.v1.md",
        prompt_version=1,
        model=model,
        output_model=SheetUnderstanding,
        max_turns=2,
        effort="high",
    )


def reviewer_agent(model: str = EXTRACTION_MODEL) -> AgentDefinition[WorkbookReview]:
    return AgentDefinition(
        name="workbook_reviewer",
        prompt_file="workbook_reviewer.v1.md",
        prompt_version=1,
        model=model,
        output_model=WorkbookReview,
        max_turns=2,
        effort="high",
    )


@dataclass(frozen=True)
class WorkbookAgents:
    plan: Callable[[PlannerInput, ToolContext], Awaitable[WorkbookPlanProposal]]
    analyze: Callable[[SheetAnalysisInput, ToolContext], Awaitable[SheetUnderstanding]]
    review: Callable[[ReviewInput, ToolContext], Awaitable[WorkbookReview]]


def default_workbook_agents(config: RunnerConfig | None = None) -> WorkbookAgents:
    async def invoke[Out: BaseModel](
        definition: AgentDefinition[Out], task: BaseModel, context: ToolContext
    ) -> Out:
        if len(task.model_dump_json()) > 150_000:
            raise PlanningAgentError("specialist input budget exceeded; split the workbook")
        result = await run_to_completion(
            definition, task, replace(context, run_id=uuid4()), config=config
        )
        if not isinstance(result, Completed):
            raise PlanningAgentError("spreadsheet specialist did not complete")
        return result.output

    async def plan(task: PlannerInput, ctx: ToolContext) -> WorkbookPlanProposal:
        return await invoke(planner_agent(), task, ctx)

    async def analyze(task: SheetAnalysisInput, ctx: ToolContext) -> SheetUnderstanding:
        return await invoke(analyst_agent(), task, ctx)

    async def review(task: ReviewInput, ctx: ToolContext) -> WorkbookReview:
        return await invoke(reviewer_agent(), task, ctx)

    return WorkbookAgents(plan=plan, analyze=analyze, review=review)
