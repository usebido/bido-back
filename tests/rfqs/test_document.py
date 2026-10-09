"""The RFQ document PDF: content, isolation per supplier, attachment and staleness."""

import io
import urllib.request
from datetime import date
from uuid import UUID

import pdfplumber
import pytest
from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.context import ToolContext
from bido_back.agents.events import args_hash
from bido_back.approvals import ActToolRegistry, ApprovalExecutor, decide_approval
from bido_back.db import tenant_session
from bido_back.db.models import Approval, Invite, RfqLine
from bido_back.rfqs import service
from bido_back.rfqs.document import (
    DocumentBranding,
    DocumentLine,
    RfqDocument,
    document_filename,
    document_html,
    render_html_pdf,
    render_pdf,
    stale_reasons,
)
from bido_back.rfqs.errors import InvalidState
from bido_back.rfqs.send import (
    SEND_RFQ_EMAIL,
    RfqActDeps,
    SendRfqEmailArgs,
    register_act_tools,
    send_proposal_ids,
)
from tests.approvals.conftest import FakeJobQueue, People

from .conftest import FakeMailboxes, FakeWriter
from .test_service import EMAIL_DOMAIN, approver, buyer, make_rfq, make_supplier


def _text(pdf: bytes) -> str:
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        return "\n".join(page.extract_text() or "" for page in doc.pages)


def _doc(**overrides: object) -> RfqDocument:
    base: dict[str, object] = {
        "language": "pt",
        "rfq_ref": "RFQ-3",
        "title": "Necessidades de materiais BR01",
        "issued_on": date(2026, 10, 8),
        "buyer_company": "Indústria Demo Ltda.",
        "buyer_name": "Ana Souza",
        "supplier_name": "Metalúrgica Horizonte Ltda.",
        "reply_by": date(2026, 10, 15),
        "currency": "BRL",
        "payment_terms": "28 DDL",
        "quote_validity_days": 30,
        "lines": [
            DocumentLine(
                position=1,
                description="AÇO CARBONO SAE 1020 CHAPA 3MM",
                part_number="100000101",
                quantities=["7500"],
                unit="KG",
                need_by=date(2026, 10, 31),
                incoterm="DAP",
                delivery_location="Vila Solana 732",
            )
        ],
        "asks": ["Prazo de entrega", "Lote mínimo (MOQ)"],
        "branding": DocumentBranding(company_name="Indústria Demo Ltda."),
    }
    return RfqDocument.model_validate({**base, **overrides})


def test_pdf_shows_the_rfq_in_the_suppliers_language() -> None:
    text = _text(render_pdf(_doc()))
    for expected in (
        "Solicitação de cotação",
        "Necessidades de materiais BR01",
        "RFQ-3",
        "08/10/2026",
        "Metalúrgica Horizonte Ltda.",
        "Ana Souza · Indústria Demo Ltda.",
        "Vila Solana 732",
        "15/10/2026",
        "AÇO CARBONO SAE 1020 CHAPA 3MM",
        "Código 100000101",
        "7500",
        "KG",
        "31/10/2026",
        "DAP",
        "28 DDL",
        "30 dias",
        "BRL",
        "Lote mínimo (MOQ)",
    ):
        assert expected in text, expected

    german = _text(render_pdf(_doc(language="de")))
    assert "Angebotsanfrage" in german and "31.10.2026" in german


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (40, 12), (10, 127, 90)).save(out, format="PNG")
    return out.getvalue()


_PNG = _png()


def test_pdf_goes_out_in_the_buyer_companys_name() -> None:
    plain = document_html(_doc())
    assert '<span class="company">Indústria Demo Ltda.</span>' in plain  # text header, no logo
    assert "<img" not in plain  # no Bido logo anywhere
    text = _text(render_pdf(_doc()))
    assert "Indústria Demo Ltda." in text and "Enviado pelo Bido" in text

    branded = _doc(
        branding=DocumentBranding.create(
            company_name="Demo",
            legal_name="Indústria Demo Ltda.",
            tax_id="12.345.678/0001-90",
            address="Av. Paulista 1000, São Paulo",
            footer_text="Condições gerais de compra: demo.example/cgc",
            accent_color="#0a7f5a",
            logo=_PNG,
            logo_media_type="image/png",
        )
    )
    markup = document_html(branded)
    assert 'src="data:image/png;base64,' in markup and "#0a7f5a" in markup
    text = _text(render_pdf(branded))
    for expected in (
        "Indústria Demo Ltda.",
        "CNPJ 12.345.678/0001-90",
        "Av. Paulista 1000, São Paulo",
        "Condições gerais de compra",
    ):
        assert expected in text, expected


def test_branding_is_validated_and_covered_by_the_approval_hash() -> None:
    with pytest.raises(ValueError):
        DocumentBranding(company_name="X", accent_color="red")
    with pytest.raises(ValueError):
        DocumentBranding(company_name="X", logo_base64="AAAA")  # no media type
    with pytest.raises(ValueError):
        DocumentBranding(company_name="X", logo_media_type="image/png", logo_base64="%%%")
    plain, branded = _doc(), _doc(branding=DocumentBranding(company_name="Other Co"))
    assert args_hash(plain.model_dump(mode="json")) != args_hash(branded.model_dump(mode="json"))
    with_logo = _doc(
        branding=DocumentBranding.create(
            company_name="Indústria Demo Ltda.", logo=_PNG, logo_media_type="image/png"
        )
    )
    assert args_hash(plain.model_dump(mode="json")) != args_hash(with_logo.model_dump(mode="json"))
    assert with_logo.branding.logo == _PNG


def test_pdf_names_only_its_own_supplier() -> None:
    # The document has no field for other invitees; the addressee is the only supplier.
    text = _text(render_pdf(_doc(supplier_name="Rolatec Rolamentos Ltda.")))
    assert "Rolatec Rolamentos Ltda." in text
    assert "Metalúrgica Horizonte" not in text


def test_document_without_a_supplier_has_no_addressee() -> None:
    # The RFQ's own PDF (the buyer's download) names nobody: no "To" pair at all,
    # and a file name without a supplier slug. The rest is the supplier's document.
    doc = _doc(supplier_name="")
    markup = document_html(doc)
    assert ">Para<" not in markup and ">To<" not in markup
    assert ">De<" in markup
    assert "Metalúrgica Horizonte" not in markup
    assert document_filename(doc) == "RFQ-3.pdf"
    text = _text(render_pdf(doc))
    assert "Necessidades de materiais BR01" in text
    assert "Metalúrgica Horizonte" not in text
    # The addressed document still says "To".
    assert ">Para<" in document_html(_doc())


def test_values_are_escaped() -> None:
    markup = document_html(_doc(title='<img src="http://evil.example/x.png"> Q4'))
    assert '<img src="http://evil' not in markup
    assert "&lt;img" in markup


def test_external_urls_are_never_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    fetched: list[str] = []

    def spy(self: urllib.request.OpenerDirector, fullurl: object, *args: object) -> object:
        fetched.append(str(getattr(fullurl, "full_url", fullurl)))
        raise OSError("network disabled in tests")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", spy)
    pdf = render_html_pdf(
        '<html><body><img src="http://127.0.0.1:9/x.png">'
        '<img src="file:///etc/hostname"><p>ok</p></body></html>'
    )
    assert pdf.startswith(b"%PDF")
    assert not any(url.startswith(("http", "file")) for url in fetched)


def test_file_name_and_staleness() -> None:
    doc = _doc()
    assert document_filename(doc) == "RFQ-3-metalurgica-horizonte-ltda.pdf"
    assert stale_reasons(doc, doc) == []
    changed_line = doc.lines[0].model_copy(update={"quantities": ["8000"]})
    assert stale_reasons(doc, doc.model_copy(update={"lines": [changed_line]})) == ["lines"]
    assert stale_reasons(doc, doc.model_copy(update={"reply_by": date(2026, 10, 20)})) == [
        "reply_by"
    ]
    # Wording (asks, language) is not an RFQ fact.
    assert stale_reasons(doc, doc.model_copy(update={"asks": []})) == []


async def _prepare_two(
    engine: AsyncEngine, people: People, mailboxes: FakeMailboxes
) -> tuple[UUID, dict[str, UUID]]:
    principal = buyer(people)
    rfq = await make_rfq(engine, principal)
    invites: dict[str, UUID] = {}
    for name in ("Rhein", "Donau"):
        supplier = await make_supplier(engine, principal, name)
        invite = await service.add_invite(
            engine, principal, rfq.id, supplier.id, supplier.contacts[0].id
        )
        invites[name] = invite.id
    result = await service.prepare_send(engine, principal, rfq.id, FakeWriter(), mailboxes)
    assert len(result.approvals) == 2
    return rfq.id, invites


async def _approval(engine: AsyncEngine, tenant: UUID, invite_id: UUID) -> Approval:
    async with tenant_session(engine, tenant) as session:
        approval = await session.scalar(
            select(Approval).where(Approval.tool_call_id == send_proposal_ids(invite_id)[1])
        )
    assert approval is not None
    return approval


@pytest.mark.db
async def test_each_supplier_gets_its_own_pdf_attached(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    mailboxes = FakeMailboxes()
    rfq_id, invites = await _prepare_two(app_engine, rfq_rows, mailboxes)

    registry = ActToolRegistry()
    register_act_tools(
        registry, RfqActDeps(db=app_engine, email_domain=EMAIL_DOMAIN, mailbox_senders=mailboxes)
    )
    executor = ApprovalExecutor(app_engine, registry)
    for name in ("Rhein", "Donau"):
        approval = await _approval(app_engine, rfq_rows.tenant, invites[name])
        args = SendRfqEmailArgs.model_validate(approval.args)
        assert args.document is not None and args.document.supplier_name == name
        jobs = FakeJobQueue()
        await decide_approval(app_engine, approver(rfq_rows), approval.id, "approve", jobs=jobs)
        await executor(jobs.jobs[0])

    rhein, donau = mailboxes.sender.sent
    for email, own, other in ((rhein, "Rhein", "Donau"), (donau, "Donau", "Rhein")):
        [attachment] = email.attachments
        assert attachment.content_type == "application/pdf"
        assert attachment.filename.endswith(f"-{own.lower()}.pdf")
        text = _text(attachment.data)
        assert own in text and other not in text
        assert "Angebotsanfrage" in text  # the suppliers write German
        assert other not in email.text_body and other not in (email.html_body or "")

    # The preview shows the approved (here: sent) document.
    preview = await service.preview_document(
        app_engine, buyer(rfq_rows), rfq_id, invites["Rhein"], FakeWriter()
    )
    assert preview.filename == rhein.attachments[0].filename
    assert "Rhein" in _text(preview.content)


@pytest.mark.db
async def test_preview_before_any_proposal_builds_the_document(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    principal = buyer(rfq_rows)
    rfq = await make_rfq(app_engine, principal)
    supplier = await make_supplier(app_engine, principal, "Isar")
    invite = await service.add_invite(
        app_engine, principal, rfq.id, supplier.id, supplier.contacts[0].id
    )
    preview = await service.preview_document(app_engine, principal, rfq.id, invite.id, FakeWriter())
    assert preview.content.startswith(b"%PDF") and "Isar" in _text(preview.content)


@pytest.mark.db
async def test_a_changed_rfq_after_approval_is_not_sent(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    mailboxes = FakeMailboxes()
    rfq_id, invites = await _prepare_two(app_engine, rfq_rows, mailboxes)
    approval = await _approval(app_engine, rfq_rows.tenant, invites["Rhein"])
    jobs = FakeJobQueue()
    await decide_approval(app_engine, approver(rfq_rows), approval.id, "approve", jobs=jobs)

    # Someone changes a quantity behind the approval (the API refuses; the database does not).
    async with tenant_session(app_engine, rfq_rows.tenant) as session:
        await session.execute(
            update(RfqLine)
            .where(RfqLine.rfq_id == rfq_id, RfqLine.position == 2)
            .values(quantities=[999])
        )

    registry = ActToolRegistry()
    register_act_tools(
        registry, RfqActDeps(db=app_engine, email_domain=EMAIL_DOMAIN, mailbox_senders=mailboxes)
    )
    tool = registry.get(SEND_RFQ_EMAIL)
    assert tool is not None
    run_id, _ = send_proposal_ids(invites["Rhein"])
    with pytest.raises(InvalidState, match="changed after approval"):
        await tool.invoke(
            ToolContext(tenant_id=rfq_rows.tenant, user_id=rfq_rows.approver, run_id=run_id),
            SendRfqEmailArgs.model_validate(approval.args),
        )
    assert mailboxes.sender.sent == []
    async with tenant_session(app_engine, rfq_rows.tenant) as session:
        invite = await session.get(Invite, invites["Rhein"])
    assert invite is not None and invite.status == "pending_approval"


def test_no_need_by_column_or_band_entry_when_no_line_has_a_date() -> None:
    dated = document_html(_doc())
    assert "Necessário em" in dated

    undated_lines = [line.model_copy(update={"need_by": None}) for line in _doc().lines]
    undated = document_html(_doc(lines=undated_lines))
    assert "Necessário em" not in undated
    assert '<col class="col-need">' not in undated
