"""create_rfq_from_assessment: the preview shows the invites and limits, the confirmation
runs the whole stored request."""

from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bido_back.copilot.journey_tools import AssessmentRfqOut
from bido_back.purchasing.sources.contracts import Assumption
from tests.approvals.conftest import People
from tests.purchasing.test_source_handoff import (
    assessment,
    counts,
    estimate,
    line,
    saved,
    supplier,
)

from .conftest import buyer_of, context_of, make_tools


def test_the_tool_takes_every_field_of_the_handoff_request() -> None:
    tools = make_tools(create_async_engine("postgresql+asyncpg://app@localhost:5432/unused"))
    tool = tools.tools["create_rfq_from_assessment"]

    assert tool.kind == "draft"
    properties = tool.input_schema()["properties"]
    assert isinstance(properties, dict)
    assert {"pending_validation", "max_suppliers_per_line", "proposal_id"} <= set(properties)


@pytest.mark.db
async def test_the_preview_shows_invites_and_limits_and_the_confirmation_runs_them(
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
                    supplier("MetalNova Teste", "vendas@metalnova.example.test", code="F001"),
                    supplier("PrecisaoSul Teste", "vendas@precisaosul.example.test"),
                    supplier("Sem Email Teste", None),
                ],
                price=estimate("BRL", "41.28", "43.34"),
            ),
            # The BOM link is not validated yet: only a PRELIMINARY draft may carry it.
            line(
                "000102",
                suppliers=[supplier("MetalNova Teste", "vendas@metalnova.example.test")],
                ready=False,
            ),
        ),
    )
    args: dict[str, object] = {
        "assessment_id": str(assessment_id),
        "title": "MTR-100 components",
        "base_currency": "BRL",
        "pending_validation": "engineering validation of the BOM",
        "max_suppliers_per_line": 1,
    }
    before = await counts(app_engine, principal.tenant_id)

    proposed = AssessmentRfqOut.model_validate(
        await tools.call(ctx, "create_rfq_from_assessment", **args)
    )

    # Nothing was written; the preview is what the confirmation will do.
    assert proposed.awaiting_confirmation and proposed.rfq_id is None
    assert await counts(app_engine, principal.tenant_id) == before
    preview = proposed.preview
    assert preview is not None and preview.title is not None
    assert preview.title.startswith("PRELIMINARY (pending engineering validation of the BOM)")
    assert [(s.name, s.email, s.part_numbers, s.new_supplier) for s in preview.suppliers] == [
        ("MetalNova Teste", "vendas@metalnova.example.test", ["000101", "000102"], True)
    ]
    assert [(p.position, p.target_unit_price, p.max_unit_price) for p in preview.lines] == [
        (1, Decimal("41.28"), Decimal("43.34")),
        (2, None, None),
    ]
    basis = preview.lines[0].limits_basis
    assert basis is not None and basis.received_orders == 3 and basis.window_days == 365
    assert preview.lines_without_limits == [2]

    # A later message (the buyer's confirmation) runs the stored request: preliminary,
    # one supplier per line, whatever the model passes now.
    confirmed = AssessmentRfqOut.model_validate(
        await tools.call(
            replace(ctx, run_id=uuid4()),
            "create_rfq_from_assessment",
            **{**args, "max_suppliers_per_line": 3, "pending_validation": None},
            proposal_id=str(proposed.proposal_id),
        )
    )
    assert confirmed.preliminary and confirmed.rfq_id is not None
    assert [(s.name, s.email, s.part_numbers) for s in confirmed.invited] == [
        (s.name, s.email, s.part_numbers) for s in preview.suppliers
    ]
    assert confirmed.lines_without_limits == preview.lines_without_limits
    after = await counts(app_engine, principal.tenant_id)
    assert (after["invites"], after["approvals"], after["messages"]) == (1, 0, 0)
    assert tools.mailboxes.calls == [] and tools.mailboxes.sender.sent == []


@pytest.mark.db
async def test_an_assessment_with_assumptions_drafts_and_shows_them_per_line(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    principal = buyer_of(copilot_rows)
    ctx = context_of(principal)
    tools = make_tools(app_engine)
    estimated = line("000102").model_copy(
        update={
            "confidence": "low",
            "assumptions": [
                Assumption(
                    code="stock_not_netted",
                    message="Stock is in UN and the demand in KG: stock was not subtracted.",
                    confidence="low",
                    part_numbers=["000102"],
                )
            ],
        }
    )
    assessment_id = await saved(
        app_engine, principal, assessment(line("000101"), estimated), key="estimated"
    )
    args: dict[str, object] = {
        "assessment_id": str(assessment_id),
        "title": "MTR-100 components",
        "base_currency": "BRL",
    }
    proposed = AssessmentRfqOut.model_validate(
        await tools.call(ctx, "create_rfq_from_assessment", **args)
    )
    assert proposed.preview is not None and proposed.preview.issues == []
    shown = {line.part_number: line for line in proposed.preview.lines}
    assert shown["000102"].confidence == "low"
    assert shown["000102"].assumptions == [estimated.assumptions[0].message]
    assert shown["000101"].assumptions == []

    confirmed = AssessmentRfqOut.model_validate(
        await tools.call(
            replace(ctx, run_id=uuid4()),
            "create_rfq_from_assessment",
            **args,
            proposal_id=str(proposed.proposal_id),
        )
    )
    assert confirmed.rfq_id is not None and confirmed.lines == 2 and not confirmed.preliminary
