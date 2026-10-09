"""The copilot's tools against Postgres: tenant isolation, history queries, draft actions."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.tools import ToolError
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    CopilotProposal,
    Invite,
    Rfq,
    RfqLine,
    RfqTemplate,
    RfqTemplateVersion,
)
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
    assert listed.by_status == {"draft": 1}  # the other tenant's RFQ is not counted either
    assert [(p.supplier_name, p.unit_price) for p in history.points] == [
        ("Rhein", Decimal("0.100000"))
    ]
    assert [s.name for s in suggested.suppliers] == ["Rhein"] and suggested.matched_history
    assert status.id == my_rfq.id  # both tenants have an RFQ-1; ours is resolved
    with pytest.raises(ToolError):
        await tools.call(ctx, "get_rfq_status", rfq=str(their_rfq.id))


async def test_list_rfqs_counts_every_status_even_when_the_filter_matches_nothing(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    """A buyer's "open RFQs" are drafts and sent RFQs. The model is told so, and even
    when it filters by `open` it still sees that drafts are waiting."""
    principal = buyer_of(copilot_rows)
    draft, sent, done = [await make_rfq(app_engine, principal) for _ in range(3)]
    async with tenant_session(app_engine, principal.tenant_id) as session:
        for rfq, status in ((sent, "open"), (done, "closed")):
            await session.execute(update(Rfq).where(Rfq.id == rfq.id).values(status=status))
    tools = make_tools(app_engine)
    ctx = context_of(principal)
    counts = {"draft": 1, "open": 1, "closed": 1}

    everything = await tools.call(ctx, "list_rfqs")
    only_sent = await tools.call(ctx, "list_rfqs", status="open")
    nothing = await tools.call(ctx, "list_rfqs", status="cancelled")
    one = await tools.call(ctx, "list_rfqs", limit=1)

    assert [r.ref for r in everything.rfqs] == [done.ref, sent.ref, draft.ref]
    assert everything.by_status == counts
    assert [(r.ref, r.status) for r in only_sent.rfqs] == [(sent.ref, "open")]
    assert only_sent.by_status == counts  # the counts ignore the filter...
    assert nothing.rfqs == [] and nothing.by_status == counts
    assert [r.ref for r in one.rfqs] == [done.ref] and one.by_status == counts  # ...and the limit

    tool = tools.tools["list_rfqs"]
    status_hint = tool.input_model.model_fields["status"].description
    for text in (tool.description, status_hint):
        assert text is not None
        assert "`draft` = being prepared, not sent" in text
        assert "`open` = sent to suppliers" in text
        assert "draft AND open" in text and "leave `status` empty" in text


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

    created = await tools.confirmed(ctx, "create_rfq", currency="EUR", text="MLCC and connectors")
    invited = await tools.confirmed(
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

    await tools.confirmed(ctx, "create_rfq", currency="EUR", text="Quote this BOM")
    await tools.confirmed(
        ctx, "create_rfq", currency="EUR", text="Only this", include_attachments=False
    )

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

    await later.confirmed(ctx, "create_rfq", currency="EUR", text="Quote the BOM I sent")
    await fresh.confirmed(ctx, "create_rfq", currency="EUR", text="Quote this one")

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

    created = await tools.confirmed(ctx, "create_rfq", currency="EUR", text="4000 capacitors")

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


async def test_editing_a_draft_rfq_by_position(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    principal = buyer_of(copilot_rows)
    tools = make_tools(app_engine)
    ctx = context_of(principal)
    created = await tools.confirmed(ctx, "create_rfq", currency="EUR", text="MLCC and connectors")
    ref = created.ref

    edited = await tools.call(
        ctx, "update_rfq_line", rfq=ref, position=1, quantities=[5000], clear=["manufacturer"]
    )
    added = await tools.call(
        ctx,
        "add_rfq_line",
        rfq=ref,
        description="Cable 2.5 mm2",
        quantities=[50],
        unit="m",
        part_number="CB-2.5",
    )
    renamed = await tools.call(
        ctx, "update_rfq_line", rfq=ref, position=1, part_number="CAP-0603-104-B"
    )
    removed = await tools.call(ctx, "remove_rfq_line", rfq=ref, position=2)
    terms = await tools.call(
        ctx,
        "set_rfq_terms",
        rfq=ref,
        incoterm="FCA",
        delivery_location="Joinville",
        response_due=datetime(2026, 11, 20, tzinfo=UTC).date(),
    )

    assert edited.lines[0].quantities == [Decimal(5000)]
    assert [(line.part_number, line.description) for line in added.lines][-1] == (
        "CB-2.5",
        "Cable 2.5 mm2",
    )
    assert renamed.lines[0].part_number == "CAP-0603-104-B"
    # The connector (line 2) is gone and the cable moved up to position 2.
    assert [(line.position, line.description) for line in removed.lines] == [
        (1, "MLCC 100nF 50V X7R 0603"),
        (2, "Cable 2.5 mm2"),
    ]
    assert terms.response_due == datetime(2026, 11, 20, tzinfo=UTC).date()
    assert {(line.incoterm, line.delivery_location) for line in terms.lines} == {
        ("FCA", "Joinville")
    }
    async with tenant_session(app_engine, principal.tenant_id) as session:
        lines = list(
            await session.scalars(
                select(RfqLine).where(RfqLine.rfq_id == created.id).order_by(RfqLine.position)
            )
        )
    assert [(line.incoterm, line.delivery_location) for line in lines] == [
        ("FCA", "Joinville"),
        ("FCA", "Joinville"),
    ]
    assert lines[0].manufacturer is None and lines[0].unit == "pcs"

    with pytest.raises(ToolError, match="no line 9"):
        await tools.call(ctx, "update_rfq_line", rfq=ref, position=9, unit="kg")
    with pytest.raises(ToolError, match="nothing to change"):
        await tools.call(ctx, "update_rfq_line", rfq=ref, position=1)
    with pytest.raises(ToolError, match="invalid value for: quantities"):
        await tools.call(ctx, "update_rfq_line", rfq=ref, position=1, quantities=[-1])
    await tools.call(ctx, "remove_rfq_line", rfq=ref, position=2)
    with pytest.raises(ToolError, match="at least one"):
        await tools.call(ctx, "remove_rfq_line", rfq=ref, position=1)
    assert {tool.kind for tool in tools.tools.values()} == {"read", "draft"}


async def test_lines_are_frozen_once_emails_are_prepared(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    principal = buyer_of(copilot_rows)
    rhein = await supplier(app_engine, principal, "Rhein")
    tools = make_tools(app_engine)
    ctx = context_of(principal)
    created = await tools.confirmed(ctx, "create_rfq", currency="EUR", text="MLCC and connectors")
    await tools.confirmed(ctx, "invite_suppliers", rfq=created.ref, supplier_ids=[str(rhein.id)])
    await tools.call(ctx, "prepare_send", rfq=created.ref)

    with pytest.raises(ToolError, match="frozen"):
        await tools.call(ctx, "update_rfq_line", rfq=created.ref, position=1, quantities=[1])
    with pytest.raises(ToolError, match="frozen"):
        await tools.call(ctx, "set_rfq_terms", rfq=created.ref, incoterm="EXW")


async def test_without_a_reply_by_date_the_model_is_told_to_ask_for_one(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    principal = buyer_of(copilot_rows)
    rhein = await supplier(app_engine, principal, "Rhein")
    tools = make_tools(app_engine)
    ctx = context_of(principal)
    for name in ("prepare_send", "propose_negotiation_mandate"):
        assert "reply-by date" in tools.tools[name].description
        assert "set_rfq_terms" in tools.tools[name].description

    created = await tools.confirmed(ctx, "create_rfq", text="MLCC and connectors", currency="EUR")
    await tools.confirmed(ctx, "invite_suppliers", rfq=created.ref, supplier_ids=[str(rhein.id)])
    async with tenant_session(app_engine, principal.tenant_id) as session:
        await session.execute(update(Rfq).where(Rfq.id == created.id).values(response_due=None))

    ask = rf"{created.ref} has no reply-by date.*set_rfq_terms\(response_due="
    with pytest.raises(ToolError, match=ask):
        await tools.call(ctx, "prepare_send", rfq=created.ref)
    with pytest.raises(ToolError, match=ask):
        await tools.call(
            ctx,
            "propose_negotiation_mandate",
            rfq=created.ref,
            deadline=datetime.now(UTC).date() + timedelta(days=30),
            limits=[
                {"position": p, "target_unit_price": "1.00", "max_unit_price": "1.50"}
                for p in (1, 2)
            ],
        )
    async with tenant_session(app_engine, principal.tenant_id) as session:
        assert list(await session.scalars(select(Approval))) == []

    # The buyer gave the date: the model sets it and prepares the send.
    due = datetime.now(UTC).date() + timedelta(days=14)
    await tools.call(ctx, "set_rfq_terms", rfq=created.ref, response_due=due)
    prepared = await tools.call(ctx, "prepare_send", rfq=created.ref)
    assert len(prepared.approvals) == 1


async def test_set_rfq_terms_stores_and_clears_payment_terms_and_quote_validity(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    principal = buyer_of(copilot_rows)
    tools = make_tools(app_engine)
    ctx = context_of(principal)
    created = await tools.confirmed(ctx, "create_rfq", text="MLCC and connectors", currency="EUR")
    ref = created.ref

    termed = await tools.call(
        ctx, "set_rfq_terms", rfq=ref, payment_terms="28 DDL", quote_validity_days=30
    )
    status = await tools.call(ctx, "get_rfq_status", rfq=ref)
    # Another term leaves them as they are.
    renamed = await tools.call(ctx, "set_rfq_terms", rfq=ref, title="Capacitors")
    cleared = await tools.call(
        ctx, "set_rfq_terms", rfq=ref, clear=["payment_terms", "quote_validity_days"]
    )

    # The result is what is stored, so the copilot reports exactly that.
    assert (termed.payment_terms, termed.quote_validity_days) == ("28 DDL", 30)
    assert (status.payment_terms, status.quote_validity_days) == ("28 DDL", 30)
    assert (renamed.payment_terms, renamed.quote_validity_days) == ("28 DDL", 30)
    assert (cleared.payment_terms, cleared.quote_validity_days) == (None, None)
    assert cleared.title == "Capacitors"
    async with tenant_session(app_engine, principal.tenant_id) as session:
        stored = await session.get(Rfq, created.id)
    assert stored is not None
    assert (stored.payment_terms, stored.quote_validity_days) == (None, None)

    for days in (0, -1, 366):
        with pytest.raises(ValidationError):
            await tools.call(ctx, "set_rfq_terms", rfq=ref, quote_validity_days=days)
    with pytest.raises(ValidationError):
        await tools.call(ctx, "set_rfq_terms", rfq=ref, clear=["title"])
    with pytest.raises(ToolError, match="nothing to change"):
        await tools.call(ctx, "set_rfq_terms", rfq=ref)


async def test_a_preliminary_rfq_is_not_prepared_for_sending(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    principal = buyer_of(copilot_rows)
    rhein = await supplier(app_engine, principal, "Rhein")
    tools = make_tools(app_engine)
    ctx = context_of(principal)

    created = await tools.confirmed(
        ctx,
        "create_rfq",
        text="MLCC and connectors",
        currency="EUR",
        pending_validation="engineering validation of the BOM",
    )
    await tools.confirmed(ctx, "invite_suppliers", rfq=created.ref, supplier_ids=[str(rhein.id)])

    assert created.title.startswith("PRELIMINARY (pending engineering validation of the BOM): ")
    with pytest.raises(ToolError, match="is preliminary"):
        await tools.call(ctx, "prepare_send", rfq=created.ref)
    with pytest.raises(ToolError, match="is preliminary"):
        await tools.call(
            ctx,
            "propose_negotiation_mandate",
            rfq=created.ref,
            deadline=datetime(2026, 12, 1, tzinfo=UTC).date(),
        )

    # The buyer confirmed the validation: the mark goes, and the RFQ can be prepared.
    await tools.call(ctx, "set_rfq_terms", rfq=created.ref, title="Capacitors and connectors")
    prepared = await tools.call(ctx, "prepare_send", rfq=created.ref)
    assert len(prepared.approvals) == 1


async def test_create_rfq_never_guesses_the_currency(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    tools = make_tools(app_engine)
    ctx = context_of(buyer_of(copilot_rows))

    # No purchase history and the buyer named no currency: the model is told to ask.
    with pytest.raises(ToolError, match=r"ask the buyer which currency this RFQ is in"):
        await tools.confirmed(ctx, "create_rfq", text="400 rolamentos 6205")
    with pytest.raises(ToolError, match="ISO 4217"):
        await tools.confirmed(ctx, "create_rfq", text="400 rolamentos 6205", currency="reais")
    assert tools.intake.calls == []

    created = await tools.confirmed(ctx, "create_rfq", text="400 rolamentos 6205", currency="brl")
    assert (created.currency, created.currency_source) == ("BRL", "stated")

    changed = await tools.call(ctx, "set_rfq_terms", rfq=created.ref, currency="USD")
    assert changed.currency == "USD"
    assert changed.note is None  # no negotiation limits to convert
    with pytest.raises(ToolError, match="ISO 4217"):
        await tools.call(ctx, "set_rfq_terms", rfq=created.ref, currency="dollars")


async def test_creation_tools_take_a_currency_and_report_its_source(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    tools = make_tools(app_engine)

    for name in ("create_rfq", "create_rfq_from_analysis", "set_rfq_terms"):
        assert "currency" in tools.tools[name].input_model.model_json_schema()["properties"]
    for name in ("create_rfq", "create_rfq_from_analysis"):
        assert "currency_source" in tools.tools[name].output_model.model_json_schema()["properties"]
        assert "currency_source" in tools.tools[name].description


async def test_nothing_is_created_until_the_buyer_confirms_in_a_later_message(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    principal = buyer_of(copilot_rows)
    tools = make_tools(app_engine)
    ctx = context_of(principal)

    proposed = await tools.call(ctx, "create_rfq", text="MLCC and connectors", currency="EUR")

    assert proposed.awaiting_confirmation and proposed.id is None
    assert proposed.preview is not None
    assert [(line.description, line.unit) for line in proposed.preview.lines] == [
        ("MLCC 100nF 50V X7R 0603", "pcs"),
        ("Board-to-board connector 40 pin", "pcs"),
    ]
    # The preview shows the buyer's code and the MPN apart, so the model can confirm both.
    assert [(line.part_number, line.mpn) for line in proposed.preview.lines] == [
        ("CAP-0603-104", "GRM188R71H104KA93D"),
        (None, None),
    ]
    async with tenant_session(app_engine, principal.tenant_id) as session:
        assert list(await session.scalars(select(Rfq))) == []
    proposal_id = str(proposed.proposal_id)

    # The same run cannot confirm its own proposal: the buyer has not answered.
    with pytest.raises(ToolError, match="has not confirmed yet"):
        await tools.call(ctx, "create_rfq", text="MLCC and connectors", proposal_id=proposal_id)

    # The buyer's next message: the STORED draft is created, whatever the model passes now,
    # and the intake agent is not asked again.
    later = replace(ctx, run_id=uuid4())
    created = await tools.call(later, "create_rfq", text="something else", proposal_id=proposal_id)
    assert not created.awaiting_confirmation and created.ref == "RFQ-1"
    assert [line.description for line in created.lines] == [
        "MLCC 100nF 50V X7R 0603",
        "Board-to-board connector 40 pin",
    ]
    # The stored RFQ keeps the MPN the preview showed.
    assert [(line.part_number, line.mpn) for line in created.lines] == [
        ("CAP-0603-104", "GRM188R71H104KA93D"),
        (None, None),
    ]
    assert [call[0].text for call in tools.intake.calls] == ["MLCC and connectors"]

    with pytest.raises(ToolError, match="already confirmed"):
        await tools.call(
            replace(ctx, run_id=uuid4()), "create_rfq", text="x", proposal_id=proposal_id
        )


async def test_a_proposal_is_only_confirmed_by_its_owner_in_its_conversation(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    principal = buyer_of(copilot_rows)
    tools = make_tools(app_engine)
    ctx = context_of(principal)
    rhein = await supplier(app_engine, principal, "Rhein")
    rfq = await make_rfq(app_engine, principal)

    proposed = await tools.call(ctx, "invite_suppliers", rfq=rfq.ref, supplier_ids=[str(rhein.id)])
    assert proposed.preview is not None and [
        (s.name, s.email) for s in proposed.preview.suppliers
    ] == [("Rhein", "sales@rhein.example.test")]
    pid = str(proposed.proposal_id)
    args = {"rfq": rfq.ref, "supplier_ids": [str(rhein.id)], "proposal_id": pid}
    later = replace(ctx, run_id=uuid4())

    # Another conversation of the same buyer, another tenant's buyer, another action.
    with pytest.raises(ToolError, match="no invite_suppliers proposal"):
        await make_tools(app_engine).call(later, "invite_suppliers", **args)
    with pytest.raises(ToolError):
        other = context_of(other_buyer_of(copilot_rows))
        await make_tools(app_engine, conversation_id=tools.conversation_id).call(
            other, "invite_suppliers", **args
        )
    with pytest.raises(ToolError, match="no create_rfq proposal"):
        await tools.call(later, "create_rfq", text="x", proposal_id=pid)

    # Older than a day: proposed again, not confirmed.
    async with tenant_session(app_engine, principal.tenant_id) as session:
        await session.execute(
            update(CopilotProposal)
            .where(CopilotProposal.id == proposed.proposal_id)
            .values(created_at=datetime.now(UTC) - timedelta(hours=25))
        )
    with pytest.raises(ToolError, match="expired"):
        await tools.call(later, "invite_suppliers", **args)
    async with tenant_session(app_engine, principal.tenant_id) as session:
        assert list(await session.scalars(select(Invite).where(Invite.rfq_id == rfq.id))) == []
