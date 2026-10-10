"""The Copilot hands over a whole plan as a file and pages, never only what fits an answer."""

from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.tools import ToolError
from bido_back.copilot.plan_tools import PLAN_ARTIFACT, PlanFileOut, PlanLinesOut
from bido_back.copilot.tools import CopilotToolDeps, copilot_tools
from bido_back.onboarding import service as onboarding
from tests.approvals.conftest import People
from tests.purchasing.test_workbook import XLSX, workbook
from tests.rfqs.conftest import FakeMailboxes, fake_agents

from .conftest import buyer_of, context_of
from .test_source_tools import SourceMapper, TrackingStorage


def test_the_export_needs_a_saved_assessment_or_the_analysis() -> None:
    tools = {
        tool.name: tool
        for tool in copilot_tools(
            CopilotToolDeps(db=None, agents=fake_agents(), mailboxes=FakeMailboxes())  # type: ignore[arg-type]
        )
    }
    export = tools["export_procurement_plan"]
    assert export.artifact_kind == PLAN_ARTIFACT
    with pytest.raises(ValidationError):
        export.parse_input({"language": "pt"})
    with pytest.raises(ValidationError):
        tools["procurement_plan_lines"].parse_input({"assessment_id": str(uuid4()), "limit": 101})


@pytest.mark.db
async def test_the_analysis_is_exported_and_paged_from_one_saved_assessment(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    principal = buyer_of(copilot_rows)
    ctx = context_of(principal)
    storage, mapper = TrackingStorage(), SourceMapper()
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
    export = tools["export_procurement_plan"]
    request = {"upload_ids": [str(upload.id)], "objective": "Analyze motor demand"}
    exported = PlanFileOut.model_validate(
        await export.invoke(ctx, export.parse_input({**request, "language": "en"}))
    )
    assert exported.download_path == (
        f"/v1/procurement-assessments/{exported.assessment_id}/plan.xlsx?lang=en"
    )
    assert exported.rows == sum(s.rows for s in exported.by_status) == 1
    # The same analysis saved again is the same assessment, as save_procurement_assessment.
    again = PlanFileOut.model_validate(
        await export.invoke(ctx, export.parse_input({"assessment_id": str(exported.assessment_id)}))
    )
    assert again.assessment_id == exported.assessment_id and again.language == "pt"
    saved = await tools["save_procurement_assessment"].invoke(
        ctx, tools["save_procurement_assessment"].parse_input(request)
    )
    assert saved.assessment_id == exported.assessment_id  # pyright: ignore[reportAttributeAccessIssue]

    lines = tools["procurement_plan_lines"]
    page = PlanLinesOut.model_validate(
        await lines.invoke(
            ctx, lines.parse_input({"assessment_id": str(exported.assessment_id), "limit": 1})
        )
    )
    assert (page.matched, page.page, page.pages) == (1, 1, 1)
    assert page.rows[0].material == "000010"
    assert "last rows" in page.note
    nothing = PlanLinesOut.model_validate(
        await lines.invoke(
            ctx,
            lines.parse_input({"assessment_id": str(exported.assessment_id), "plant": "9999"}),
        )
    )
    assert nothing.matched == 0 and nothing.rows == []
    # Another buyer's assessment is not found, and a file of another conversation is refused.
    with pytest.raises(ToolError):
        await lines.invoke(ctx, lines.parse_input({"assessment_id": str(uuid4())}))
    with pytest.raises(ToolError):
        await export.invoke(
            ctx, export.parse_input({"upload_ids": [str(uuid4())], "objective": "x"})
        )
