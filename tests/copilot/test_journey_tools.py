"""The copilot's journey tools against Postgres: contacts, extras, outcome, guards."""

from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.tools import ToolError
from bido_back.db import tenant_session
from bido_back.db.models import Approval, Contact, Proposal, Supplier
from bido_back.onboarding import service as onboarding
from bido_back.rfqs.language import supplier_email_language
from tests.approvals.conftest import People
from tests.purchasing.conftest import FakeStorage, purchasing_rows, xlsx

from .conftest import buyer_of, context_of, invite, make_rfq, make_tools, seed_quote, supplier

pytestmark = pytest.mark.db

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
__all__ = ["purchasing_rows"]


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


async def test_a_contact_from_the_chat_carries_the_suppliers_locale(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    """The RFQ document's language follows the supplier's country: given by the model,
    else inferred from an unambiguous email TLD or legal suffix, else asked, never guessed
    (2026-10-09: a Brazilian supplier saved from the chat got an English RFQ)."""
    principal = buyer_of(copilot_rows)
    tools = make_tools(app_engine)
    ctx = context_of(principal)

    stated = await tools.call(
        ctx,
        "add_supplier_contact",
        supplier="Magnetica Europe",
        email="sales@magnetica.com",
        country="de",
    )
    inferred = await tools.call(
        ctx,
        "add_supplier_contact",
        supplier="Cobre Sul Fios Esmaltados S.A.",
        email="vendas@cobresul.com.br",
    )
    unknown = await tools.call(
        ctx, "add_supplier_contact", supplier="Acme Corp", email="sales@acme.com"
    )

    assert (stated.country, stated.document_language, stated.question) == ("DE", "de", None)
    assert (inferred.country, inferred.document_language, inferred.question) == ("BR", "pt", None)
    assert (unknown.country, unknown.document_language) == (None, "en")
    assert unknown.question is not None and "Acme Corp" in unknown.question

    async with tenant_session(app_engine, principal.tenant_id) as session:
        rows = {
            s.name: (s.country, s.language, supplier_email_language(s))
            for s in await session.scalars(select(Supplier))
        }
    assert rows["Magnetica Europe"] == ("DE", "en", "de")
    assert rows["Cobre Sul Fios Esmaltados S.A."] == ("BR", "pt", "pt")
    assert rows["Acme Corp"] == (None, "en", "en")

    # The buyer answers the question: the same call with the country records it.
    answered = await tools.call(
        ctx, "add_supplier_contact", supplier="Acme Corp", email="sales@acme.com", country="BR"
    )
    assert (answered.supplier_created, answered.country, answered.document_language) == (
        False,
        "BR",
        "pt",
    )
    assert answered.question is None
    async with tenant_session(app_engine, principal.tenant_id) as session:
        acme = await session.scalar(select(Supplier).where(Supplier.name == "Acme Corp"))
        emails = list(
            await session.scalars(select(Contact.email).where(Contact.kind == "supplier"))
        )
    assert acme is not None and supplier_email_language(acme) == "pt"
    assert emails.count("sales@acme.com") == 1


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
        assert out.uncovered_lines >= 1
        assert out.readiness == "needs_review"
        assert any(line.readiness == "no_feasible_offer" for line in out.lines)
    assert out.lines[0].evidence_gaps
    assert out.lines[0].performance.observations == 0
    assert out.lines[0].justification


async def test_the_buyer_can_change_comparison_policy_without_starting_negotiation(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    principal = buyer_of(copilot_rows)
    rfq = await make_rfq(app_engine, principal)
    tools = make_tools(app_engine)
    ctx = context_of(principal)
    policy = await tools.call(
        ctx,
        "set_comparison_policy",
        rfq=rfq.ref,
        policy={"preference": "reliability_first", "allow_unknown_history": False},
    )
    comparison = await tools.call(ctx, "get_comparison", rfq=rfq.ref)
    outcome = await tools.call(ctx, "get_negotiation_outcome", rfq=rfq.ref)
    assert comparison.policy == policy
    assert outcome.policy == policy
    assert comparison.readiness == "no_feasible_offer"
    assert outcome.negotiation_status == "not_started"
    assert not tools.mailboxes.sender.sent


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


async def test_only_the_buyer_confirms_an_import(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    principal = buyer_of(purchasing_rows)
    ctx = context_of(principal)
    storage = FakeStorage()
    upload = await onboarding.store_upload(
        app_engine,
        storage,
        principal,
        filename="stock.xlsx",
        content_type=XLSX,
        data=xlsx([["Código", "Estoque"], ["A-1", 10], ["A-2", "n/d"], ["A-3", "n/d"]]),
    )

    # The message that attaches the file cannot confirm its own questions.
    first = make_tools(app_engine, attachment_ids=(upload.id,), storage=storage)
    report = await first.call(ctx, "import_spreadsheet", upload_id=str(upload.id), confirmed=True)
    assert report.status == "needs_confirmation"

    # The buyer's next message: an earlier file, confirmed, but not twice in one go.
    later = make_tools(app_engine, earlier_attachment_ids=(upload.id,), storage=storage)
    again = await later.call(ctx, "import_spreadsheet", upload_id=str(upload.id))
    assert again.status == "needs_confirmation"
    held = await later.call(ctx, "import_spreadsheet", upload_id=str(upload.id), confirmed=True)
    assert held.status == "needs_confirmation"

    answer = make_tools(app_engine, earlier_attachment_ids=(upload.id,), storage=storage)
    done = await answer.call(ctx, "import_spreadsheet", upload_id=str(upload.id), confirmed=True)
    assert done.status == "imported"
