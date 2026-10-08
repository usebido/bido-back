"""Spreadsheet specialists use the real Agno runner with a scripted provider."""

from uuid import uuid4

from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.purchasing.inspection import SpreadsheetWindow, inspect_spreadsheet
from bido_back.purchasing.planning_agents import default_workbook_agents
from bido_back.purchasing.planning_contracts import (
    PlannerInput,
    ReviewInput,
    SheetAnalysisInput,
    SheetTaskProposal,
    SheetUnderstanding,
    WorkbookPlanProposal,
    WorkbookReview,
)
from bido_back.purchasing.workbook import WorkbookImportReport
from tests.agents.test_runner import Script, final, script
from tests.purchasing.test_planning import stock, stock_mapping
from tests.purchasing.test_workbook import workbook

__all__ = ["script"]


async def test_three_specialists_return_neutral_structured_contracts(script: Script) -> None:
    data = workbook({"Stock": stock()})
    manifest = inspect_spreadsheet(data, "test.xlsx", SpreadsheetWindow())
    page = inspect_spreadsheet(data, "test.xlsx", SpreadsheetWindow(sheet="Stock"))
    proposal = WorkbookPlanProposal(tasks=[SheetTaskProposal(sheet="Stock", objective="Map stock")])
    understanding = SheetUnderstanding(
        role="inventory", summary="Stock table", mapping=stock_mapping()
    )
    review = WorkbookReview(summary="Reviewed", issues=[])
    stub = script(
        final(**proposal.model_dump(mode="json")),
        final(**understanding.model_dump(mode="json")),
        final(**review.model_dump(mode="json")),
    )
    agents = default_workbook_agents(RunnerConfig(openai_api_key="test-not-a-real-key"))
    ctx = ToolContext(tenant_id=uuid4(), user_id=uuid4(), run_id=uuid4())
    assert (
        await agents.plan(
            PlannerInput(objective="Read", clarification="", workbook=manifest, samples=[page]), ctx
        )
        == proposal
    )
    assert (
        await agents.analyze(
            SheetAnalysisInput(
                objective="Map stock", clarification="", page=page, previous=None, dependencies=[]
            ),
            ctx,
        )
        == understanding
    )
    validation = WorkbookImportReport(
        upload_id=uuid4(),
        filename="test.xlsx",
        status="needs_confirmation",
        sections=[],
        questions=[],
        problems=[],
    )
    assert (
        await agents.review(
            ReviewInput(
                objective="Read",
                clarification="",
                sheets=[understanding],
                samples=[page],
                validation=validation,
            ),
            ctx,
        )
        == review
    )
    assert len(stub.calls) == 3
    for call in stub.calls:
        assert not call.get("tools")
        assert call["model"] == "gpt-6.1-sol"
