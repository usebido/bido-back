"""The copilot's journey tools against Postgres: contacts, extras, outcome, guards."""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.tools import ToolError
from bido_back.db import tenant_session
from bido_back.db.models import Approval, Contact, Proposal
from tests.approvals.conftest import People

from .conftest import buyer_of, context_of, invite, make_rfq, make_tools, seed_quote, supplier

pytestmark = pytest.mark.db


async def test_contacts_from_the_chat_are_saved_once(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    principal = buyer_of(copilot_rows)
    tools = make_tools(app_engine)
    ctx = context_of(principal)

    first = await tools.call(
        ctx, "add_supplier_contact", supplier="Rame Italia", email="vendite@rame.example.test"
    )
    again = await tools.call(
        ctx, "add_supplier_contact", supplier="rame italia", email="VENDITE@rame.example.test"
    )

    assert first.supplier_created and not again.supplier_created
    async with tenant_session(app_engine, principal.tenant_id) as session:
        emails = list(
            await session.scalars(select(Contact.email).where(Contact.kind == "supplier"))
        )
    assert emails == ["vendite@rame.example.test"]


async def test_only_this_conversations_attachments_can_be_imported(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    tools = make_tools(app_engine)
    with pytest.raises(ToolError, match="attached in this conversation"):
        await tools.call(
            context_of(buyer_of(copilot_rows)), "import_spreadsheet", upload_id=str(uuid4())
        )


async def test_a_file_from_an_earlier_turn_can_be_imported(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    ctx = context_of(buyer_of(copilot_rows))
    earlier = uuid4()
    one = make_tools(app_engine, earlier_attachment_ids=(earlier,))
    two = make_tools(app_engine, earlier_attachment_ids=(uuid4(), uuid4()))

    # Past the id check (no storage in tests): the earlier file by its id, and,
    # with a single file in the conversation, a miscopied id means that one.
    with pytest.raises(ToolError, match="storage"):
        await one.call(ctx, "import_spreadsheet", upload_id=str(earlier), confirmed=True)
    with pytest.raises(ToolError, match="storage"):
        await one.call(ctx, "import_spreadsheet", upload_id=str(uuid4()), confirmed=True)
    # Several earlier files and an id that is none of them: refused.
    with pytest.raises(ToolError, match="attached in this conversation"):
        await two.call(ctx, "import_spreadsheet", upload_id=str(uuid4()))


async def test_a_miscopied_id_falls_back_to_the_only_attachment(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    ctx = context_of(buyer_of(copilot_rows))
    single = make_tools(app_engine, attachment_ids=(uuid4(),))
    several = make_tools(app_engine, attachment_ids=(uuid4(), uuid4()))

    # Past the id check: the only attachment is used (no storage in tests).
    with pytest.raises(ToolError, match="storage"):
        await single.call(ctx, "import_spreadsheet", upload_id=str(uuid4()))
    # With several attachments a wrong id stays an error.
    with pytest.raises(ToolError, match="attached in this conversation"):
        await several.call(ctx, "import_spreadsheet", upload_id=str(uuid4()))


async def test_extra_offers_are_listed_as_not_requested(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    principal = buyer_of(copilot_rows)
    rfq = await make_rfq(app_engine, principal)
    rhein = await supplier(app_engine, principal, "Rhein")
    invite_id = await invite(app_engine, principal, rfq, rhein)
    await seed_quote(app_engine, principal.tenant_id, invite_id, rfq.lines[0].id, "0.10")
    async with tenant_session(app_engine, principal.tenant_id) as session:
        await session.execute(
            update(Proposal)
            .where(Proposal.invite_id == invite_id)
            .values(
                fields={
                    "_review": {"declined": False},
                    "extra_offers": [
                        {
                            "description": "5% off above 50k pcs",
                            "mpn": None,
                            "quantity": ">50000",
                            "unit_price": None,
                            "currency": None,
                            "conditions": "until December",
                            "quote": "5% Rabatt ab 50.000 Stk",
                        },
                        {"not": "an offer"},
                    ],
                }
            )
        )
    tools = make_tools(app_engine)

    out = await tools.call(context_of(principal), "list_extra_offers", rfq=rfq.ref)

    assert [(o.supplier, o.description) for o in out.offers] == [("Rhein", "5% off above 50k pcs")]
    assert "not requested" in out.note


async def test_the_outcome_names_lines_no_supplier_covers(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    principal = buyer_of(copilot_rows)
    rfq = await make_rfq(app_engine, principal)
    rhein = await supplier(app_engine, principal, "Rhein")
    invite_id = await invite(app_engine, principal, rfq, rhein)
    await seed_quote(app_engine, principal.tenant_id, invite_id, rfq.lines[0].id, "0.10")
    tools = make_tools(app_engine)

    out = await tools.call(context_of(principal), "get_negotiation_outcome", rfq=rfq.ref)

    covered = {line.position: line.covered for line in out.lines}
    assert covered.get(1) is True
    if len(rfq.lines) > 1:
        assert out.uncovered_lines >= 1 and "network" in out.note


async def test_the_mandate_takes_limits_the_buyer_gives_for_lines_without_them(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    principal = buyer_of(copilot_rows)
    ctx = context_of(principal)
    tools = make_tools(app_engine)
    rfq = await make_rfq(app_engine, principal)  # from text: no target or maximum yet
    await invite(app_engine, principal, rfq, await supplier(app_engine, principal, "Murata"))

    with pytest.raises(ToolError, match="target and a maximum"):
        await tools.call(ctx, "propose_negotiation_mandate", rfq=rfq.ref, deadline="2026-10-20")
    with pytest.raises(ToolError, match="no line 9"):
        await tools.call(
            ctx,
            "propose_negotiation_mandate",
            rfq=rfq.ref,
            deadline="2026-10-20",
            limits=[{"position": 9, "target_unit_price": "1", "max_unit_price": "2"}],
        )

    proposed = await tools.call(
        ctx,
        "propose_negotiation_mandate",
        rfq=rfq.ref,
        deadline="2026-10-20",
        limits=[
            {"position": 1, "target_unit_price": "0.0040", "max_unit_price": "0.0050"},
            {"position": 2, "target_unit_price": "0.0100", "max_unit_price": "0.0120"},
        ],
    )

    assert proposed.lines == 2 and proposed.approval_id is not None
    async with tenant_session(app_engine, principal.tenant_id) as session:
        approval = await session.get(Approval, proposed.approval_id)
    assert approval is not None
    limits = {
        line["position"]: Decimal(str(line["max_unit_price"])) for line in approval.args["lines"]
    }
    assert limits == {1: Decimal("0.0050"), 2: Decimal("0.0120")}
