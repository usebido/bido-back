"""register_quote: a supplier's quote the buyer attached in the chat, stored like an emailed reply.

The extraction agent is replaced by a fake that answers a scripted
`QuoteExtractionOutput` citing the PDF the buyer attached; validation,
storage, the comparison and the activity feed are the real ones, against
Postgres (marked `db`).
"""

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.catalog.contracts import QuoteExtractionInput, QuoteExtractionOutput
from bido_back.agents.context import ToolContext
from bido_back.agents.tools import ToolError
from bido_back.auth import Principal
from bido_back.comparison.loader import load_comparison
from bido_back.db import tenant_session
from bido_back.db.models import (
    Attachment,
    AuditEvent,
    Invite,
    Message,
    Proposal,
    ProposalLine,
    RfqLine,
)
from bido_back.onboarding import service as onboarding
from bido_back.rfqs import service
from bido_back.rfqs.activity import rfq_activity
from bido_back.rfqs.schemas import RfqCreated
from tests.approvals.conftest import People
from tests.documents.fixtures import PdfPage, PdfText, build_pdf
from tests.quotes.test_extract_job import FakeStorage
from tests.rfqs.conftest import FakeIntake

from .conftest import buyer_of, context_of, invite, make_tools, supplier

pytestmark = pytest.mark.db

TODAY = date(2026, 10, 7)
PDF = "application/pdf"
SUPPLIER = "Arthurmotores"
# What the supplier's PDF says, one line each; the fake extraction cites these lines.
QUOTE_LINES = (
    "ARTHUR TESTE MOTORES LTDA",
    "PROPOSTA COMERCIAL N. 2026-0417 - Ref. RFQ",
    "Item 1 - MLCC 100nF - MPN GRM188R71H104KA93D",
    "Quantidade: 4000 pcs",
    "Preco unitario: R$ 0,45",
    "Prazo de entrega: 25 dias",
    "Moeda: BRL",
    "Condicao de pagamento: 28 DDL",
    "Validade da proposta: 31/12/2026",
    "Incoterm: DAP Curitiba",
)


def quote_pdf(lines: tuple[str, ...] = QUOTE_LINES) -> bytes:
    page = PdfPage(texts=[PdfText(50, 800 - 16 * i, line) for i, line in enumerate(lines)])
    return build_pdf([page])


# --- The scripted extraction -----------------------------------------------------------------

_NONE: dict[str, object] = {"value": None, "source": None, "confidence": 0}


def _pdf(value: str, quote: str, attachment: str = "proposta.pdf") -> dict[str, object]:
    return {
        "value": value,
        "source": {"kind": "pdf", "attachment": attachment, "page": 1, "quote": quote},
        "confidence": 0.95,
    }


def line_output(position: int | None = 1, *, attachment: str = "proposta.pdf") -> dict[str, object]:
    return {
        "rfq_line_position": position,
        "offered_mpn": _pdf("GRM188R71H104KA93D", "MPN GRM188R71H104KA93D", attachment),
        "is_alternative": False,
        "min_quantity": _pdf("4000", "Quantidade: 4000 pcs", attachment),
        "unit_price": _pdf("0.45", "Preco unitario: R$ 0,45", attachment),
        "unit": _pdf("pcs", "Quantidade: 4000 pcs", attachment),
        "tooling_cost": _NONE,
        "moq": _NONE,
        "lead_time_days": _pdf("25", "Prazo de entrega: 25 dias", attachment),
        "origin_country": _NONE,
        "qualification": _NONE,
    }


def quote_output(
    *lines: dict[str, object], attachment: str = "proposta.pdf", **overrides: Any
) -> QuoteExtractionOutput:
    data: dict[str, object] = {
        "is_quote": True,
        "declined": False,
        "currency": _pdf("BRL", "Moeda: BRL", attachment),
        "valid_until": _pdf("2026-12-31", "Validade da proposta: 31/12/2026", attachment),
        "incoterm": _pdf("DAP", "Incoterm: DAP Curitiba", attachment),
        "delivery_location": _pdf("Curitiba", "Incoterm: DAP Curitiba", attachment),
        "payment_terms": _pdf("28 DDL", "Condicao de pagamento: 28 DDL", attachment),
        "lines": list(lines) or [line_output(attachment=attachment)],
        "missing_fields": ["origin_country"],
        **overrides,
    }
    return QuoteExtractionOutput.model_validate(data)


class FakeExtractor:
    """The extraction seam: records the task it was given, answers the script."""

    def __init__(self, output: QuoteExtractionOutput | None = None) -> None:
        self.output = output or quote_output()
        self.calls: list[QuoteExtractionInput] = []

    async def __call__(
        self, task: QuoteExtractionInput, context: ToolContext
    ) -> QuoteExtractionOutput:
        self.calls.append(task)
        return self.output


# --- The scene: an RFQ with one invited supplier and the quote PDF attached -----------------


@dataclass
class Scene:
    principal: Principal
    rfq: RfqCreated
    supplier_id: UUID
    invite_id: UUID
    upload_id: UUID
    storage: FakeStorage
    engine: AsyncEngine

    async def upload(self, filename: str, data: bytes) -> UUID:
        view = await onboarding.store_upload(
            self.engine,
            self.storage,
            self.principal,
            filename=filename,
            content_type=PDF,
            data=data,
        )
        return view.id

    async def messages(self) -> list[Message]:
        async with tenant_session(self.engine, self.principal.tenant_id) as session:
            rows = await session.scalars(
                select(Message)
                .where(Message.invite_id == self.invite_id)
                .order_by(Message.created_at)
            )
            return list(rows)

    async def proposals(self) -> list[tuple[Proposal, list[ProposalLine]]]:
        async with tenant_session(self.engine, self.principal.tenant_id) as session:
            proposals = list(
                await session.scalars(
                    select(Proposal)
                    .where(Proposal.invite_id == self.invite_id)
                    .order_by(Proposal.version)
                )
            )
            return [
                (
                    proposal,
                    list(
                        await session.scalars(
                            select(ProposalLine).where(ProposalLine.proposal_id == proposal.id)
                        )
                    ),
                )
                for proposal in proposals
            ]


async def scene_of(engine: AsyncEngine, people: People) -> Scene:
    principal = buyer_of(people)
    storage = FakeStorage()
    # In the quote's currency, with line 1 asking DAP Curitiba (tests.rfqs.conftest):
    # the scripted quote answers the RFQ exactly, so the clean path stays clean.
    rfq = await service.create_rfq_from_text(
        engine, principal, "Need MLCC", FakeIntake(), currency="BRL"
    )
    seller = await supplier(engine, principal, SUPPLIER)
    invite_id = await invite(engine, principal, rfq, seller)
    scene = Scene(principal, rfq, seller.id, invite_id, uuid4(), storage, engine)
    scene.upload_id = await scene.upload("proposta.pdf", quote_pdf())
    return scene


# --- Tests -------------------------------------------------------------------------------


async def test_the_preview_reads_the_quote_and_stores_nothing(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    scene = await scene_of(app_engine, copilot_rows)
    extractor = FakeExtractor()
    tools = make_tools(
        app_engine,
        attachment_ids=(scene.upload_id,),
        storage=scene.storage,
        quote_extractor=extractor,
    )
    ctx = context_of(scene.principal)

    out = await tools.call(
        ctx,
        "register_quote",
        rfq=scene.rfq.ref,
        supplier="arthur",
        upload_ids=[str(scene.upload_id)],
    )

    assert out.awaiting_confirmation is True and out.proposal_id is not None
    assert out.ref == scene.rfq.ref and out.supplier_name == SUPPLIER
    preview = out.preview
    assert preview is not None and preview.action == "register_quote"
    assert [s.name for s in preview.suppliers] == [SUPPLIER]
    assert preview.suppliers[0].email is not None
    assert preview.files == ["proposta.pdf"]
    assert (preview.currency, preview.payment_terms, preview.incoterm) == ("BRL", "28 DDL", "DAP")
    assert preview.valid_until == date(2026, 12, 31)
    [quoted] = preview.quoted
    assert quoted.rfq_line_position == 1 and quoted.description == "MLCC 100nF 50V X7R 0603"
    assert (quoted.quantity, quoted.unit, quoted.unit_price, quoted.currency) == (
        Decimal(4000),
        "pcs",
        Decimal("0.45"),
        "BRL",
    )
    assert quoted.lead_time_days == 25 and quoted.issues == []
    assert preview.issues == ["missing:origin_country"] and out.issues == preview.issues

    # The same extraction the email path runs, over the file's text, with no email.
    [task] = extractor.calls
    assert task.rfq_ref == scene.rfq.ref and (task.email_subject, task.email_body) == ("", "")
    assert "[proposta.pdf p.1]" in task.attachments_text
    assert "Preco unitario: R$ 0,45" in task.attachments_text
    assert [line.position for line in task.requested_lines] == [1, 2]

    # A preview only: nothing on the invite yet.
    assert await scene.messages() == [] and await scene.proposals() == []


async def test_confirming_stores_the_quote_as_an_email_reply_would(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    scene = await scene_of(app_engine, copilot_rows)
    tools = make_tools(
        app_engine,
        attachment_ids=(scene.upload_id,),
        storage=scene.storage,
        quote_extractor=FakeExtractor(),
    )
    ctx = context_of(scene.principal)

    out = await tools.confirmed(
        ctx,
        "register_quote",
        rfq=scene.rfq.ref,
        supplier=str(scene.invite_id),
        upload_ids=[str(scene.upload_id)],
    )

    assert out.awaiting_confirmation is False and out.version == 1 and out.needs_review is False
    assert out.supplier_name == SUPPLIER and out.issues == ["missing:origin_country"]

    # The same tables as an emailed quote: a message with its attachment, a proposal.
    [message] = await scene.messages()
    assert (message.direction, message.origin, message.status) == (
        "in",
        "copilot_upload",
        "extracted",
    )
    assert message.message_id_header and message.message_id_header.startswith("<upload-")
    [(proposal, lines)] = await scene.proposals()
    assert proposal.message_id == message.id and proposal.version == 1
    assert (proposal.currency, proposal.payment_terms, proposal.incoterm) == (
        "BRL",
        "28 DDL",
        "DAP",
    )
    assert proposal.valid_until == date(2026, 12, 31)
    assert proposal.fields["_extraction"]["origin"] == "copilot_upload"
    assert proposal.fields["currency"]["source"]["quote"] == "Moeda: BRL"
    [line] = lines
    assert (line.min_quantity, line.unit_price, line.lead_time_days, line.unit) == (
        Decimal(4000),
        Decimal("0.45"),
        25,
        "pcs",
    )
    assert line.rfq_line_id is not None and line.needs_review is False
    async with tenant_session(app_engine, scene.principal.tenant_id) as session:
        [attachment] = list(
            await session.scalars(select(Attachment).where(Attachment.message_id == message.id))
        )
        found = await session.scalar(select(Invite).where(Invite.id == scene.invite_id))
        events = list(
            await session.scalars(select(AuditEvent).where(AuditEvent.action == "quote.registered"))
        )
    # The file is copied under the intake's key scheme and downloadable like a reply's.
    assert attachment.filename == "proposta.pdf"
    assert (
        attachment.storage_key
        == f"attachments/{scene.principal.tenant_id}/{message.id}/1-proposta.pdf"
    )
    assert scene.storage.blobs[attachment.storage_key] == quote_pdf()
    # The invite responded, as the intake marks it for a reply.
    assert found is not None and found.status == "responded" and found.responded_at is not None

    # The comparison shows it like any quote, marked as uploaded.
    comparison = await load_comparison(app_engine, scene.principal, scene.rfq.id, TODAY)
    [column] = comparison.suppliers
    assert column.supplier_name == SUPPLIER and column.proposal_id == proposal.id
    assert column.origin == "copilot_upload"
    assert any(cell.unit_price == Decimal("0.45") for cell in column.cells)
    listed = await tools.call(ctx, "list_proposals", rfq=scene.rfq.ref)
    assert [p.supplier_name for p in listed.proposals] == [SUPPLIER]

    # The buyer's step is on the record, and the feed names it, not a "reply".
    [event] = events
    assert event.actor_user_id == scene.principal.user_id
    assert event.payload["rfq_id"] == str(scene.rfq.id)
    assert event.payload["invite_id"] == str(scene.invite_id)
    assert event.payload["origin"] == "copilot_upload" and event.payload["version"] == 1
    assert event.payload["proposal_id"] == str(proposal.id)
    assert event.payload["upload_ids"] == [str(scene.upload_id)]
    feed = await rfq_activity(app_engine, scene.principal, scene.rfq.id)
    [item] = [i for i in feed if i.action == "quote.registered"]
    assert item.kind == "user" and item.supplier_name == SUPPLIER
    assert "uploaded document" in item.summary and "version 1" in item.summary
    assert not any(i.action in ("reply", "quote.received") for i in feed)


async def test_a_line_the_rfq_does_not_have_is_an_issue_and_stored_for_review(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    scene = await scene_of(app_engine, copilot_rows)
    tools = make_tools(
        app_engine,
        attachment_ids=(scene.upload_id,),
        storage=scene.storage,
        quote_extractor=FakeExtractor(quote_output(line_output(3))),
    )
    ctx = context_of(scene.principal)
    args: dict[str, object] = {
        "rfq": scene.rfq.ref,
        "supplier": SUPPLIER,
        "upload_ids": [str(scene.upload_id)],
    }

    proposed = await tools.call(ctx, "register_quote", **args)

    [quoted] = proposed.preview.quoted
    assert quoted.rfq_line_position == 3 and quoted.description is None
    assert quoted.issues == ["unknown_rfq_line"]
    assert "line 3: unknown_rfq_line" in proposed.issues

    later = replace(ctx, run_id=uuid4())
    out = await tools.call(later, "register_quote", **args, proposal_id=str(proposed.proposal_id))

    assert out.version == 1 and out.needs_review is True
    [(_, [line])] = await scene.proposals()
    assert line.rfq_line_id is None and line.needs_review is True


# What the supplier's PDF said for RFQ-3 in production (2026-10-09): 50 pcs CIF in
# 25 business days, against a line asking 1,500 pcs FOB Joinville-SC.
RFQ3_LINES = (
    "ARTHUR TESTE MOTORES LTDA",
    "PROPOSTA COMERCIAL N. 2026-0421 - Ref. RFQ-3",
    "Item 1 - MLCC 100nF - MPN GRM188R71H104KA93D",
    "Quantidade: 50 pcs",
    "Preco unitario: R$ 1.284,50",
    "Preco total: R$ 64.225,00",
    "Prazo de entrega: 25 dias uteis apos pedido - entrega CIF Joinville/SC",
    "Condicao de pagamento: 28 DDL",
    "Garantia: 12 meses",
    "Validade da proposta: 30 dias",
)


def rfq3_output(attachment: str = "proposta-rfq3.pdf") -> QuoteExtractionOutput:
    lead = "Prazo de entrega: 25 dias uteis apos pedido - entrega CIF Joinville/SC"
    return quote_output(
        {
            **line_output(attachment=attachment),
            "min_quantity": _pdf("50", "Quantidade: 50 pcs", attachment),
            "unit_price": _pdf("1284.50", "Preco unitario: R$ 1.284,50", attachment),
            "unit": _pdf("pcs", "Quantidade: 50 pcs", attachment),
            "lead_time_days": _pdf("25", lead, attachment),
        },
        attachment=attachment,
        currency=_pdf("BRL", "Preco unitario: R$ 1.284,50", attachment),
        # The model computed the date from "30 dias": the file does not state it.
        valid_until=_pdf("2026-11-08", "Validade da proposta: 30 dias", attachment),
        incoterm=_pdf("CIF", "entrega CIF Joinville/SC", attachment),
        delivery_location=_pdf("Joinville/SC", "entrega CIF Joinville/SC", attachment),
        missing_fields=[],
    )


async def test_the_preview_lists_what_differs_from_the_rfq_and_business_day_lead_times(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    scene = await scene_of(app_engine, copilot_rows)
    async with tenant_session(app_engine, scene.principal.tenant_id) as session:
        first = await session.scalar(
            select(RfqLine).where(RfqLine.rfq_id == scene.rfq.id, RfqLine.position == 1)
        )
        assert first is not None
        first.quantities = [1500]
        first.incoterm = "FOB"
        first.delivery_location = "Joinville-SC"
    upload_id = await scene.upload("proposta-rfq3.pdf", quote_pdf(RFQ3_LINES))
    tools = make_tools(
        app_engine,
        attachment_ids=(upload_id,),
        storage=scene.storage,
        quote_extractor=FakeExtractor(rfq3_output()),
    )
    ctx = context_of(scene.principal)
    args: dict[str, object] = {
        "rfq": scene.rfq.ref,
        "supplier": SUPPLIER,
        "upload_ids": [str(upload_id)],
    }

    proposed = await tools.call(ctx, "register_quote", **args)

    preview = proposed.preview
    [quoted] = preview.quoted
    assert quoted.quantity == Decimal(50) and quoted.unit_price == Decimal("1284.50")
    # 25 business days from 7 October 2026 in Brazil (a BRL quote), which skip 12 October
    # and 2 November: 13 November, stored as 37 calendar days.
    assert quoted.lead_time_days == 37
    assert quoted.issues == ["quantity_mismatch"]
    assert preview.incoterm == "CIF" and preview.currency == "BRL"
    # Never "none": the RFQ asked 1,500 pcs FOB; the file quotes 50 pcs CIF.
    assert "line 1: quantity_mismatch" in preview.issues
    assert "incoterm_mismatch" in preview.issues
    assert "valid_until:not_grounded" in preview.issues
    assert "delivery_location_mismatch" not in preview.issues  # Joinville/SC is Joinville-SC
    assert "currency_mismatch" not in preview.issues  # the RFQ is in BRL
    assert proposed.issues == preview.issues

    later = replace(ctx, run_id=uuid4())
    out = await tools.call(later, "register_quote", **args, proposal_id=str(proposed.proposal_id))

    # Registered all the same, held for review, with the differences on the record.
    assert out.version == 1 and out.needs_review is True and out.issues == preview.issues
    [(proposal, [line])] = await scene.proposals()
    assert proposal.incoterm == "CIF"
    assert "incoterm_mismatch" in proposal.fields["_review"]["issues"]
    assert proposal.fields["delivery_location"]["parsed"] == "Joinville/SC"
    assert line.needs_review is True and line.min_quantity == Decimal(50)
    assert "quantity_mismatch" in line.fields["_review"]["issues"]
    assert line.lead_time_days == 37
    assert line.fields["lead_time_days"]["value"] == "25"
    conversion = line.fields["lead_time_days"]["conversion"]
    assert {k: conversion[k] for k in ("quoted", "unit", "calendar_days", "holiday_calendar")} == {
        "quoted": 25,
        "unit": "business_days",
        "calendar_days": 37,
        "holiday_calendar": "BR",
    }
    assert conversion["reference_date"] == "2026-10-07"


async def test_the_same_file_registers_once_and_another_file_is_the_next_version(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    scene = await scene_of(app_engine, copilot_rows)
    revised = await scene.upload("proposta-rev2.pdf", quote_pdf((*QUOTE_LINES, "Revisao 2")))
    extractor = FakeExtractor()
    tools = make_tools(
        app_engine,
        attachment_ids=(scene.upload_id, revised),
        storage=scene.storage,
        quote_extractor=extractor,
    )
    ctx = context_of(scene.principal)
    first = await tools.confirmed(
        ctx,
        "register_quote",
        rfq=scene.rfq.ref,
        supplier=SUPPLIER,
        upload_ids=[str(scene.upload_id)],
    )
    assert first.version == 1

    # The same file again: refused, as a redelivered email is.
    with pytest.raises(ToolError, match="already registered.*version 1"):
        await tools.confirmed(
            ctx,
            "register_quote",
            rfq=scene.rfq.ref,
            supplier=SUPPLIER,
            upload_ids=[str(scene.upload_id)],
        )

    # Another file for the same supplier: the next version, as a second reply would be.
    extractor.output = quote_output(attachment="proposta-rev2.pdf")
    second = await tools.confirmed(
        ctx, "register_quote", rfq=scene.rfq.ref, supplier=SUPPLIER, upload_ids=[str(revised)]
    )
    assert second.version == 2 and second.needs_review is False
    versions = [proposal.version for proposal, _ in await scene.proposals()]
    assert versions == [1, 2]
    assert [m.origin for m in await scene.messages()] == ["copilot_upload", "copilot_upload"]
    comparison = await load_comparison(app_engine, scene.principal, scene.rfq.id, TODAY)
    assert [c.proposal_version for c in comparison.suppliers] == [2]


async def test_a_document_that_is_not_a_quote_is_refused_and_nothing_is_stored(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    scene = await scene_of(app_engine, copilot_rows)
    not_a_quote = quote_output(is_quote=False, lines=[], declined=True)
    tools = make_tools(
        app_engine,
        attachment_ids=(scene.upload_id,),
        storage=scene.storage,
        quote_extractor=FakeExtractor(not_a_quote),
    )

    with pytest.raises(ToolError, match="does not read as a priced supplier quote.*declines"):
        await tools.call(
            context_of(scene.principal),
            "register_quote",
            rfq=scene.rfq.ref,
            supplier=SUPPLIER,
            upload_ids=[str(scene.upload_id)],
        )

    assert await scene.messages() == [] and await scene.proposals() == []


async def test_only_the_conversation_files_of_an_invited_supplier_register(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    scene = await scene_of(app_engine, copilot_rows)
    extractor = FakeExtractor()
    tools = make_tools(
        app_engine,
        attachment_ids=(scene.upload_id,),
        storage=scene.storage,
        quote_extractor=extractor,
    )
    ctx = context_of(scene.principal)

    with pytest.raises(ToolError, match="not one of the files attached"):
        await tools.call(
            ctx,
            "register_quote",
            rfq=scene.rfq.ref,
            supplier=SUPPLIER,
            upload_ids=[str(uuid4()), str(scene.upload_id)],
        )
    with pytest.raises(ToolError, match="no invited supplier named"):
        await tools.call(
            ctx,
            "register_quote",
            rfq=scene.rfq.ref,
            supplier="Rhein",
            upload_ids=[str(scene.upload_id)],
        )
    with pytest.raises(ToolError, match="no RFQ"):
        await tools.call(
            ctx,
            "register_quote",
            rfq="RFQ-999999",
            supplier=SUPPLIER,
            upload_ids=[str(scene.upload_id)],
        )
    assert extractor.calls == [] and await scene.messages() == []

    # A wrong id with exactly one file in this message means that file (models copy
    # ids wrongly); the preview names the file read. With two files, it is refused.
    elsewhere = await scene.upload("outra.pdf", quote_pdf())
    lenient = await tools.call(
        ctx, "register_quote", rfq=scene.rfq.ref, supplier=SUPPLIER, upload_ids=[str(elsewhere)]
    )
    assert lenient.preview.files == ["proposta.pdf"]
    two_files = make_tools(
        app_engine,
        attachment_ids=(scene.upload_id, await scene.upload("outra2.pdf", quote_pdf())),
        storage=scene.storage,
        quote_extractor=extractor,
    )
    with pytest.raises(ToolError, match="not one of the files attached"):
        await two_files.call(
            ctx, "register_quote", rfq=scene.rfq.ref, supplier=SUPPLIER, upload_ids=[str(elsewhere)]
        )
    assert await scene.messages() == []


async def test_a_confirmation_needs_the_buyers_later_message(
    app_engine: AsyncEngine, copilot_rows: People
) -> None:
    scene = await scene_of(app_engine, copilot_rows)
    tools = make_tools(
        app_engine,
        attachment_ids=(scene.upload_id,),
        storage=scene.storage,
        quote_extractor=FakeExtractor(),
    )
    ctx = context_of(scene.principal)
    args: dict[str, object] = {
        "rfq": scene.rfq.ref,
        "supplier": SUPPLIER,
        "upload_ids": [str(scene.upload_id)],
    }
    proposed = await tools.call(ctx, "register_quote", **args)

    # Same run: the buyer has not answered yet.
    with pytest.raises(ToolError, match="has not confirmed yet"):
        await tools.call(ctx, "register_quote", **args, proposal_id=str(proposed.proposal_id))
    assert await scene.proposals() == []

    later = replace(ctx, run_id=uuid4())
    out = await tools.call(later, "register_quote", **args, proposal_id=str(proposed.proposal_id))
    assert out.version == 1
    [(proposal, _)] = await scene.proposals()
    assert proposal.invite_id == scene.invite_id
