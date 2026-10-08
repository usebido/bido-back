"""The copilot's tools against Postgres: tenant isolation, history queries, draft actions."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.tools import ToolError
from bido_back.db import tenant_session
from bido_back.db.models import Approval, Invite, RfqTemplate, RfqTemplateVersion
from bido_back.rfqs.send import SEND_RFQ_EMAIL
from tests.approvals.conftest import People

from .conftest import (
    MPN,
    buyer_of,
    context_of,
    invite,
    make_rfq,
    make_tools,
    other_buyer_of,
    seed_quote,
    supplier,
)

pytestmark = pytest.mark.db


async def test_tools_only_see_the_callers_tenant(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    mine, theirs = buyer_of(copilot_rows), other_buyer_of(copilot_rows)
    my_rfq = await make_rfq(app_engine, mine)
    my_supplier = await supplier(app_engine, mine, "Rhein")
    await seed_quote(
        app_engine,
        mine.tenant_id,
        await invite(app_engine, mine, my_rfq, my_supplier),
        my_rfq.lines[0].id,
        "0.10",
    )
    their_rfq = await make_rfq(app_engine, theirs)
    their_supplier = await supplier(app_engine, theirs, "Elsewhere")
    await seed_quote(
        app_engine,
        theirs.tenant_id,
        await invite(app_engine, theirs, their_rfq, their_supplier),
        their_rfq.lines[0].id,
        "9.99",
    )
    tools = make_tools(app_engine)
    ctx = context_of(mine)

    listed = await tools.call(ctx, "list_rfqs")
    history = await tools.call(ctx, "price_history", mpn=MPN.lower())
    suggested = await tools.call(ctx, "suggest_suppliers", mpn=MPN)
    status = await tools.call(ctx, "get_rfq_status", rfq="RFQ-1")

    assert [r.id for r in listed.rfqs] == [my_rfq.id]
    assert [(p.supplier_name, p.unit_price) for p in history.points] == [
        ("Rhein", Decimal("0.100000"))
    ]
    assert [s.name for s in suggested.suppliers] == ["Rhein"] and suggested.matched_history
    assert status.id == my_rfq.id  # both tenants have an RFQ-1; ours is resolved
    with pytest.raises(ToolError):
        await tools.call(ctx, "get_rfq_status", rfq=str(their_rfq.id))


async def test_price_history_is_newest_first(app_engine: AsyncEngine, copilot_rows: People) -> None:
    principal = buyer_of(copilot_rows)
    rhein = await supplier(app_engine, principal, "Rhein")
    lyon = await supplier(app_engine, principal, "Lyon")
    now = datetime.now(UTC)
    for name, sup, price, days_ago in (("old", rhein, "0.12", 60), ("new", lyon, "0.09", 3)):
        rfq = await make_rfq(app_engine, principal)
        await seed_quote(
            app_engine,
            principal.tenant_id,
            await invite(app_engine, principal, rfq, sup),
            rfq.lines[0].id,
            price,
            quoted_at=now - timedelta(days=days_ago),
        )
        assert name

    history = await make_tools(app_engine).call(
        context_of(principal), "price_history", mpn="GRM188-R71H104KA93D"
    )

    assert [(p.rfq_ref, p.supplier_name, p.unit_price) for p in history.points] == [
        ("RFQ-2", "Lyon", Decimal("0.090000")),
        ("RFQ-1", "Rhein", Decimal("0.120000")),
    ]


async def test_who_responded_and_what_is_missing(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    principal = buyer_of(copilot_rows)
    rfq = await make_rfq(app_engine, principal)
    rhein = await supplier(app_engine, principal, "Rhein")
    lyon = await supplier(app_engine, principal, "Lyon")
    await seed_quote(
        app_engine,
        principal.tenant_id,
        await invite(app_engine, principal, rfq, rhein),
        rfq.lines[0].id,
        "0.10",
        missing_fields=["payment_terms"],
        needs_review=True,
    )
    await invite(app_engine, principal, rfq, lyon)
    tools = make_tools(app_engine)
    ctx = context_of(principal)

    who = await tools.call(ctx, "who_responded", rfq=rfq.ref)
    missing = await tools.call(ctx, "what_is_missing", rfq="rfq 1")

    assert [i.supplier_name for i in who.responded] == ["Rhein"]
    assert [i.supplier_name for i in who.not_responded] == ["Lyon"]
    assert [i.supplier_name for i in missing.awaiting_suppliers] == ["Lyon"]
    assert [line.position for line in missing.lines_without_quote] == [2]
    assert [
        (g.supplier_name, g.missing_fields, g.lines_needing_review) for g in missing.proposal_gaps
    ] == [("Rhein", ["payment_terms"], 1)]


async def test_prepare_send_records_approvals_and_sends_nothing(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    principal = buyer_of(copilot_rows)
    rhein = await supplier(app_engine, principal, "Rhein")
    tools = make_tools(app_engine, attachments_text="[bom.xlsx] 4000 MLCC")
    ctx = context_of(principal)

    created = await tools.call(ctx, "create_rfq", text="MLCC and connectors")
    invited = await tools.call(
        ctx, "invite_suppliers", rfq=created.ref, supplier_ids=[str(rhein.id)]
    )
    prepared = await tools.call(ctx, "prepare_send", rfq=created.ref)

    assert created.status == "draft" and [r.detail for r in invited.results] == ["invited"]
    assert len(prepared.approvals) == 1 and "Nothing was sent" in prepared.note
    assert tools.mailboxes.sender.sent == []
    async with tenant_session(app_engine, principal.tenant_id) as session:
        approval = await session.scalar(
            select(Approval).where(Approval.id == prepared.approvals[0].approval_id)
        )
        found = await session.scalar(select(Invite).where(Invite.rfq_id == created.id))
    assert approval is not None and approval.status == "pending"
    assert approval.tool == SEND_RFQ_EMAIL and approval.requested_by == principal.user_id
    assert found is not None and found.status == "pending_approval"


async def test_create_rfq_reads_the_attachments_and_no_tool_acts(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    tools = make_tools(app_engine, attachments_text="[bom.xlsx] 4000 MLCC")
    ctx = context_of(buyer_of(copilot_rows))

    await tools.call(ctx, "create_rfq", text="Quote this BOM")
    await tools.call(ctx, "create_rfq", text="Only this", include_attachments=False)

    texts = [call[0].text for call in tools.intake.calls]
    assert texts == ["Quote this BOM\n\n[bom.xlsx] 4000 MLCC", "Only this"]
    assert {tool.kind for tool in tools.tools.values()} == {"read", "draft"}


async def test_create_rfq_reads_earlier_files_only_when_this_message_has_none(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    ctx = context_of(buyer_of(copilot_rows))
    later = make_tools(
        app_engine, earlier_attachment_ids=(uuid4(),), earlier_attachments_text="[bom.xlsx] old"
    )
    fresh = make_tools(
        app_engine,
        attachments_text="[new.xlsx] new",
        attachment_ids=(uuid4(),),
        earlier_attachment_ids=(uuid4(),),
        earlier_attachments_text="[bom.xlsx] old",
    )

    await later.call(ctx, "create_rfq", text="Quote the BOM I sent")
    await fresh.call(ctx, "create_rfq", text="Quote this one")

    assert [call[0].text for call in later.intake.calls] == [
        "Quote the BOM I sent\n\n[bom.xlsx] old"
    ]
    assert [call[0].text for call in fresh.intake.calls] == ["Quote this one\n\n[new.xlsx] new"]


async def test_create_rfq_hands_the_buyers_own_words_to_intake(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    said = ("Preciso de 4000 CAP-0603-104, DAP Curitiba", "sim, cria a RFQ")
    tools = make_tools(app_engine, buyer_messages=said)
    ctx = context_of(buyer_of(copilot_rows))

    created = await tools.call(ctx, "create_rfq", text="4000 capacitors")

    [(task, _)] = tools.intake.calls
    assert task.text == "4000 capacitors" and task.buyer_messages == list(said)
    # The buyer's code, the MPN and the terms all come back for the Copilot to show.
    first = created.lines[0]
    assert (first.part_number, first.mpn, first.incoterm, first.delivery_location) == (
        "CAP-0603-104",
        "GRM188R71H104KA93D",
        "DAP",
        "Curitiba",
    )


async def test_template_tools(app_engine: AsyncEngine, copilot_rows: People) -> None:
    tools = make_tools(app_engine)
    ctx = context_of(buyer_of(copilot_rows))

    before = await tools.call(ctx, "get_rfq_template")
    updated = await tools.call(ctx, "update_rfq_template", footer_fields=["Frete", "Validade"])
    after = await tools.call(ctx, "get_rfq_template")

    assert before.exists is False
    assert updated.exists and updated.footer == "Frete · Validade"
    assert after.template_id == updated.template_id and after.fields  # from the standard set
    with pytest.raises(ToolError):
        await tools.call(ctx, "update_rfq_template")


async def test_template_edits_are_new_versions(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    tools = make_tools(app_engine)
    ctx = context_of(buyer_of(copilot_rows))

    first = await tools.call(ctx, "update_rfq_template", footer_fields=["Frete"])
    second = await tools.call(ctx, "update_rfq_template", footer_fields=["Frete", "Validade"])

    assert second.template_id == first.template_id
    async with tenant_session(app_engine, copilot_rows.tenant) as session:
        template = await session.get(RfqTemplate, first.template_id)
        versions = (
            await session.scalars(
                select(RfqTemplateVersion.version)
                .where(RfqTemplateVersion.template_id == first.template_id)
                .order_by(RfqTemplateVersion.version)
            )
        ).all()
    assert template is not None
    # The standard template is created as v1, then each copilot edit is a new
    # version; every version, the current one included, stays as a snapshot.
    assert template.version == 3
    assert versions == [1, 2, 3]
