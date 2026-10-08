"""create_rfq_from_assessment passes the buyer's whole request through to the handoff."""

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bido_back.agents.tools import ToolError
from bido_back.purchasing.sources.snapshots import AssessmentRfqResult
from tests.approvals.conftest import People
from tests.purchasing.test_source_handoff import assessment, counts, line, saved, supplier

from .conftest import buyer_of, context_of, make_tools


def test_the_tool_takes_every_field_of_the_handoff_request() -> None:
    tools = make_tools(create_async_engine("postgresql+asyncpg://app@localhost:5432/unused"))
    tool = tools.tools["create_rfq_from_assessment"]

    assert tool.kind == "draft"
    properties = tool.input_schema()["properties"]
    assert isinstance(properties, dict)
    assert {"pending_validation", "max_suppliers_per_line"} <= set(properties)


@pytest.mark.db
async def test_a_preliminary_draft_with_capped_invites_is_created_through_the_copilot(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    principal = buyer_of(copilot_rows)
    ctx = context_of(principal)
    tools = make_tools(app_engine)
    assessment_id = await saved(
        app_engine,
        principal,
        assessment(
            line(
                "000101",
                suppliers=[
                    supplier("MetalNova Teste", "vendas@metalnova.example.test"),
                    supplier("PrecisaoSul Teste", "vendas@precisaosul.example.test"),
                ],
            ),
            # The BOM link is not validated yet: only a PRELIMINARY draft may carry it.
            line(
                "000102",
                suppliers=[supplier("MetalNova Teste", "vendas@metalnova.example.test")],
                ready=False,
            ),
        ),
    )
    args = {
        "assessment_id": str(assessment_id),
        "title": "MTR-100 components",
        "base_currency": "BRL",
    }

    with pytest.raises(ToolError, match="evidence issues"):
        await tools.call(ctx, "create_rfq_from_assessment", **args)
    result = AssessmentRfqResult.model_validate(
        await tools.call(
            ctx,
            "create_rfq_from_assessment",
            **args,
            pending_validation="engineering validation of the BOM",
            max_suppliers_per_line=1,
        )
    )

    assert result.preliminary and not result.reused
    assert result.title == (
        "PRELIMINARY (pending engineering validation of the BOM): MTR-100 components"
    )
    assert [(s.name, s.part_numbers) for s in result.invited] == [
        ("MetalNova Teste", ["000101", "000102"])
    ]
    assert result.lines_without_limits == [1, 2]
    after = await counts(app_engine, principal.tenant_id)
    assert (after["invites"], after["approvals"], after["messages"]) == (1, 0, 0)
    assert tools.mailboxes.calls == [] and tools.mailboxes.sender.sent == []
