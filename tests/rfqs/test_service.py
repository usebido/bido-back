"""The RFQ services against Postgres, with fake agents and a fake mailbox."""

from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.context import ToolContext
from bido_back.approvals import (
    ActToolRegistry,
    ApprovalExecutor,
    approval_execute_envelope,
    decide_approval,
)
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    AuditEvent,
    Invite,
    Message,
    Rfq,
    RfqLine,
    RfqTemplate,
    Tenant,
)
from bido_back.rfqs import service, suppliers
from bido_back.rfqs.errors import Duplicate, InvalidState, MailboxNotConnected, NotFound
from bido_back.rfqs.schemas import (
    ContactIn,
    CreateSupplierRequest,
    RfqCreated,
    SupplierView,
    UpdateLineRequest,
)
from bido_back.rfqs.send import (
    REJECT_HOOKS,
    SEND_RFQ_EMAIL,
    RfqActDeps,
    SendRfqEmailArgs,
    register_act_tools,
    send_proposal_ids,
)
from bido_back.templates import service as templates_service
from bido_back.templates.schemas import TemplateEdit
from tests.approvals.conftest import FakeJobQueue, People

from .conftest import FakeIntake, FakeMailboxes, FakeWriter, user_email

pytestmark = pytest.mark.db

EMAIL_DOMAIN = "quotes.example.test"


def buyer(people: People) -> Principal:
    return Principal(tenant_id=people.tenant, user_id=people.owner, role="owner")


def approver(people: People) -> Principal:
    return Principal(tenant_id=people.tenant, user_id=people.approver, role="approver")


async def make_supplier(engine: AsyncEngine, principal: Principal, name: str) -> SupplierView:
    return await suppliers.create_supplier(
        engine,
        principal,
        CreateSupplierRequest(
            name=name,
            language="de",
            contacts=[ContactIn(name="Jonas Weber", email=f"sales@{name.lower()}.example.test")],
        ),
    )


async def make_rfq(engine: AsyncEngine, principal: Principal) -> RfqCreated:
    return await service.create_rfq_from_text(
        engine, principal, "Need 4k and 10k MLCC 100nF, 500 connectors", FakeIntake()
    )


async def test_create_rfq_persists_lines_and_returns_questions(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    intake = FakeIntake()
    first = await service.create_rfq_from_text(app_engine, buyer(rfq_rows), "some text", intake)
    second = await make_rfq(app_engine, buyer(rfq_rows))

    assert (first.ref, second.ref) == ("RFQ-1", "RFQ-2")
    assert first.status == "draft" and first.base_currency == "EUR"
    assert first.questions == ["Which delivery address?"]
    assert [(line.position, line.quantities) for line in first.lines] == [
        (1, [Decimal(4000), Decimal(10000)]),
        (2, [Decimal(500)]),
    ]
    assert intake.calls[0][0].text == "some text"
    assert intake.calls[0][1].tenant_id == rfq_rows.tenant

    async with tenant_session(app_engine, rfq_rows.tenant) as session:
        stored = await session.scalar(select(Rfq).where(Rfq.id == first.id))
        lines = list(await session.scalars(select(RfqLine).where(RfqLine.rfq_id == first.id)))
    assert stored is not None and stored.created_by == rfq_rows.owner
    assert stored.source_text == "some text"
    assert sorted(line.quantities for line in lines) == [[500], [4000, 10000]]
    assert [
        (line.part_number, line.mpn, line.incoterm, line.delivery_location) for line in first.lines
    ] == [
        ("CAP-0603-104", "GRM188R71H104KA93D", "DAP", "Curitiba"),
        (None, None, None, None),
    ]
    assert intake.calls[0][0].buyer_messages == []

    # Another tenant neither sees it nor numbers after it.
    other = Principal(tenant_id=rfq_rows.other_tenant, user_id=rfq_rows.other_approver)
    with pytest.raises(NotFound):
        await service.get_rfq(app_engine, other, first.id)
    assert (await make_rfq(app_engine, other)).ref == "RFQ-1"


async def test_line_edits_and_invites(app_engine: AsyncEngine, rfq_rows: People) -> None:
    principal = buyer(rfq_rows)
    rfq = await make_rfq(app_engine, principal)
    supplier = await make_supplier(app_engine, principal, "Rhein")

    edited = await service.update_line(
        app_engine,
        principal,
        rfq.id,
        rfq.lines[1].id,
        UpdateLineRequest(quantities=[Decimal(250), Decimal(1000)], incoterm="FCA"),
    )
    assert edited.quantities == [Decimal(250), Decimal(1000)] and edited.incoterm == "FCA"
    with pytest.raises(InvalidState):
        await service.update_line(
            app_engine, principal, rfq.id, rfq.lines[1].id, UpdateLineRequest(description=None)
        )

    invite = await service.add_invite(
        app_engine, principal, rfq.id, supplier.id, supplier.contacts[0].id
    )
    assert invite.status == "draft" and invite.supplier_name == "Rhein"
    with pytest.raises(Duplicate):
        await service.add_invite(
            app_engine, principal, rfq.id, supplier.id, supplier.contacts[0].id
        )
    other_supplier = await make_supplier(app_engine, principal, "Lyon")
    with pytest.raises(NotFound):  # a contact of another supplier
        await service.add_invite(
            app_engine, principal, rfq.id, other_supplier.id, supplier.contacts[0].id
        )

    async with tenant_session(app_engine, rfq_rows.tenant) as session:
        token = await session.scalar(select(Invite.reply_token).where(Invite.id == invite.id))
    assert token is not None and len(token) == 12

    listed = await suppliers.list_suppliers(app_engine, principal)
    assert {s.name for s in listed} == {"Rhein", "Lyon"}
    assert all(len(s.contacts) == 1 for s in listed)


async def prepared(
    engine: AsyncEngine, people: People, mailboxes: FakeMailboxes
) -> tuple[RfqCreated, UUID, UUID, str]:
    """An RFQ with one invite, prepared for sending; (rfq, invite_id, approval_id, to)."""
    principal = buyer(people)
    rfq = await make_rfq(engine, principal)
    supplier = await make_supplier(engine, principal, "Rhein")
    invite = await service.add_invite(
        engine, principal, rfq.id, supplier.id, supplier.contacts[0].id
    )
    result = await service.prepare_send(engine, principal, rfq.id, FakeWriter(), mailboxes)
    assert [p.invite_id for p in result.approvals] == [invite.id]
    to = supplier.contacts[0].email
    assert to is not None
    return rfq, invite.id, result.approvals[0].approval_id, to


async def test_prepare_send_records_pending_approvals_with_the_exact_email(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    mailboxes = FakeMailboxes()
    rfq, invite_id, approval_id, to = await prepared(app_engine, rfq_rows, mailboxes)

    async with tenant_session(app_engine, rfq_rows.tenant) as session:
        approval = await session.get(Approval, approval_id)
        invite = await session.get(Invite, invite_id)
    assert approval is not None and invite is not None
    assert approval.status == "pending" and approval.tool == SEND_RFQ_EMAIL
    assert approval.requested_by == rfq_rows.owner
    preview = await service.preview_email(
        app_engine, buyer(rfq_rows), rfq.id, invite_id, FakeWriter()
    )
    assert approval.args == {
        "invite_id": str(invite_id),
        "sender_user_id": str(rfq_rows.owner),
        "to": [to],
        "cc": [],
        # The supplier writes German; no template yet, so Bido's standard one.
        "subject": f"[{rfq.ref}] Angebotsanfrage: {rfq.title}",
        "body": preview.text,  # the preview is exactly what goes to approval
    }
    body = preview.text
    company = await _tenant_name(app_engine, rfq_rows.tenant)
    assert body.startswith("<de> Olá, gostaríamos de receber a cotação")  # localized greeting
    assert f"Unsere Referenz: {rfq.ref}." in body
    assert "GRM188R71H104KA93D" in body and "4000 / 10000" in body
    assert "Board-to-board connector 40 pin" in body
    assert "Incoterm" in body and "DAP" in body
    # The standard footer, localized, then the required questions it does not cover.
    for ask in (
        "- <de> Validade da proposta",
        "- <de> Condição de pagamento",
        "- Verpackungseinheit",
        "- RoHS- und REACH-Erklärungen",
        "- Lieferzeit",
        "- Mindestbestellmenge (MOQ)",
    ):
        assert ask in body
    assert "Gültigkeit des Angebots" not in body  # covered by "Validade da proposta"
    assert "Bitte senden Sie Ihr Angebot bis 01.11.2026." in body
    assert body.endswith(f"{company} purchasing\n{company}\n")
    assert preview.template_id is None and preview.template_version == 0
    assert "<table" in preview.html and "GRM188R71H104KA93D" in preview.html
    assert invite.status == "pending_approval"
    async with tenant_session(app_engine, rfq_rows.tenant) as session:
        pinned = await session.get(Rfq, rfq.id)
    assert pinned is not None
    assert (pinned.template_id, pinned.template_version) == (None, 0)
    assert mailboxes.calls == [(rfq_rows.tenant, rfq_rows.owner)]

    # Preparing again proposes nothing new.
    again = await service.prepare_send(app_engine, buyer(rfq_rows), rfq.id, FakeWriter(), mailboxes)
    assert again.approvals == []

    queue = await service.list_approvals(app_engine, approver(rfq_rows), "pending")
    item = next(i for i in queue if i.id == approval_id)
    assert item.rfq_email is not None
    assert item.rfq_email.subject == approval.args["subject"]
    assert item.rfq_email.body == approval.args["body"]
    assert item.rfq_email.rfq_ref == rfq.ref and item.rfq_email.supplier_name == "Rhein"
    assert item.requested_by_me is False


async def _tenant_name(engine: AsyncEngine, tenant_id: UUID) -> str:
    async with tenant_session(engine, tenant_id) as session:
        tenant = await session.get(Tenant, tenant_id)
    assert tenant is not None
    return tenant.name


async def test_prepare_send_renders_the_tenants_default_template_and_pins_its_version(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, rfq_rows: People
) -> None:
    principal = buyer(rfq_rows)
    rfq = await make_rfq(app_engine, principal)
    supplier = await make_supplier(app_engine, principal, "Rhein")
    other = await make_supplier(app_engine, principal, "Lyon")
    await service.add_invite(app_engine, principal, rfq.id, supplier.id, supplier.contacts[0].id)
    async with tenant_session(app_engine, rfq_rows.tenant) as session:
        default = RfqTemplate(
            tenant_id=rfq_rows.tenant,
            source="search",
            status="ready",
            language="de",
            greeting="Sehr geehrte Damen und Herren, bitte bieten Sie an:",
            columns=["Pos.", "Bezeichnung", "Menge", "Stückpreis"],
            footer="Lieferzeit · Gültigkeit",
            fields=["Menge", "Lieferzeit"],
            is_default=True,
        )
        session.add_all(
            [
                default,
                # Not the default: ignored.
                RfqTemplate(
                    tenant_id=rfq_rows.tenant, source="none", status="ready", greeting="Olá"
                ),
            ]
        )
        await session.flush()
        default_id = default.id
    writer = FakeWriter()
    try:
        await service.prepare_send(app_engine, principal, rfq.id, writer, FakeMailboxes())
        # The template changes after the first send...
        await service.add_invite(app_engine, principal, rfq.id, other.id, other.contacts[0].id)
        await templates_service.edit_template(
            app_engine,
            principal,
            default_id,
            TemplateEdit(
                language="de",
                greeting="Changed later",
                columns=["Pos.", "Bezeichnung", "Menge", "Stückpreis"],
                footer_fields=["Lieferzeit", "Gültigkeit"],
                fields=["Menge", "Lieferzeit"],
            ),
        )
        result = await service.prepare_send(app_engine, principal, rfq.id, writer, FakeMailboxes())
        async with tenant_session(app_engine, rfq_rows.tenant) as session:
            pinned = await session.get(Rfq, rfq.id)
            approvals = list(
                await session.scalars(
                    select(Approval).where(
                        Approval.id.in_([p.approval_id for p in result.approvals])
                    )
                )
            )
    finally:
        async with tenant_session(migrator_engine, rfq_rows.tenant) as session:
            await session.execute(update(Rfq).values(template_id=None))
            await session.execute(
                delete(RfqTemplate).where(RfqTemplate.tenant_id == rfq_rows.tenant)
            )

    # Same language (de): the buyer's own words, no model call.
    assert writer.calls == []
    assert pinned is not None and (pinned.template_id, pinned.template_version) == (default_id, 1)
    [approval] = approvals
    body = str(approval.args["body"])
    # ...to version 2, but the RFQ is pinned: the second email still uses version 1.
    assert body.startswith("Sehr geehrte Damen und Herren, bitte bieten Sie an:")
    header = next(line for line in body.splitlines() if line.startswith("Pos."))
    assert [h.strip() for h in header.split("|")] == [
        "Pos.",
        "Bezeichnung",
        "Menge",
        "Stückpreis",  # the supplier fills it
        "MPN",
        "Hersteller",
        "Einheit",
        "Incoterm",  # only one line has it, so per line rather than under the table
    ]
    assert "- Lieferzeit\n- Gültigkeit\n" in body
    assert "Gültigkeit des Angebots" not in body  # "Gültigkeit" covers it


async def test_prepare_send_without_a_mailbox_fails_before_any_draft(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    principal = buyer(rfq_rows)
    rfq = await make_rfq(app_engine, principal)
    supplier = await make_supplier(app_engine, principal, "Rhein")
    invite = await service.add_invite(
        app_engine, principal, rfq.id, supplier.id, supplier.contacts[0].id
    )
    writer = FakeWriter()

    with pytest.raises(MailboxNotConnected):
        await service.prepare_send(
            app_engine, principal, rfq.id, writer, FakeMailboxes(disconnected={rfq_rows.owner})
        )

    assert writer.calls == []
    detail = await service.get_rfq(app_engine, principal, rfq.id)
    assert [i.status for i in detail.invites if i.id == invite.id] == ["draft"]


async def test_approved_send_goes_out_from_the_requesters_mailbox(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    mailboxes = FakeMailboxes()
    rfq, invite_id, approval_id, to = await prepared(app_engine, rfq_rows, mailboxes)
    mailboxes.calls.clear()

    jobs = FakeJobQueue()
    await decide_approval(app_engine, approver(rfq_rows), approval_id, "approve", jobs=jobs)
    registry = ActToolRegistry()
    register_act_tools(
        registry, RfqActDeps(db=app_engine, email_domain=EMAIL_DOMAIN, mailbox_senders=mailboxes)
    )
    executor = ApprovalExecutor(app_engine, registry)
    await executor(jobs.jobs[0])

    [email] = mailboxes.sender.sent
    assert mailboxes.calls == [(rfq_rows.tenant, rfq_rows.owner)]  # the requester, not approver
    assert email.from_address == await user_email(app_engine, rfq_rows.owner)
    assert email.to == (to,) and email.cc == ()
    assert email.subject.startswith(f"[{rfq.ref}] ")
    assert email.reply_to is None
    assert email.message_id is not None and email.message_id.endswith(f"@{EMAIL_DOMAIN}>")

    async with tenant_session(app_engine, rfq_rows.tenant) as session:
        invite = await session.get(Invite, invite_id)
        stored_rfq = await session.get(Rfq, rfq.id)
        approval = await session.get(Approval, approval_id)
        messages = list(
            await session.scalars(select(Message).where(Message.invite_id == invite_id))
        )
        audits = list(
            await session.scalars(select(AuditEvent).where(AuditEvent.action == "rfq.email_sent"))
        )
    assert invite is not None and stored_rfq is not None and approval is not None
    assert invite.status == "sent" and invite.sent_at is not None
    assert invite.thread_message_id == email.message_id
    assert stored_rfq.status == "open"
    assert approval.status == "executed"
    assert [(m.direction, m.message_id_header, m.subject, m.body_text) for m in messages] == [
        ("out", email.message_id, email.subject, email.text_body)
    ]
    [audit] = audits
    assert audit.actor_user_id == rfq_rows.approver
    assert audit.payload["requested_by"] == str(rfq_rows.owner)
    assert audit.payload["approved_by"] == str(rfq_rows.approver)

    # A redelivered job and a direct re-run send nothing more.
    await executor(approval_execute_envelope(rfq_rows.tenant, approval_id))
    tool = registry.get(SEND_RFQ_EMAIL)
    assert tool is not None
    run_id, _ = send_proposal_ids(invite_id)
    again = await tool.invoke(
        ToolContext(tenant_id=rfq_rows.tenant, user_id=rfq_rows.approver, run_id=run_id),
        SendRfqEmailArgs.model_validate(approval.args),
    )
    assert again.already_sent is True
    assert len(mailboxes.sender.sent) == 1


async def test_send_refuses_a_sender_who_did_not_request_it(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    mailboxes = FakeMailboxes()
    _, invite_id, approval_id, _ = await prepared(app_engine, rfq_rows, mailboxes)
    async with tenant_session(app_engine, rfq_rows.tenant) as session:
        approval = await session.get(Approval, approval_id)
    assert approval is not None
    forged = SendRfqEmailArgs.model_validate(
        {**approval.args, "sender_user_id": str(rfq_rows.approver)}
    )
    registry = ActToolRegistry()
    register_act_tools(
        registry, RfqActDeps(db=app_engine, email_domain=EMAIL_DOMAIN, mailbox_senders=mailboxes)
    )
    tool = registry.get(SEND_RFQ_EMAIL)
    assert tool is not None
    run_id, _ = send_proposal_ids(invite_id)
    with pytest.raises(InvalidState, match="not the requester"):
        await tool.invoke(
            ToolContext(tenant_id=rfq_rows.tenant, user_id=rfq_rows.approver, run_id=run_id),
            forged,
        )
    assert mailboxes.sender.sent == []


async def test_rejected_send_returns_the_invite_to_draft_and_can_be_proposed_again(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    mailboxes = FakeMailboxes()
    rfq, invite_id, rejected_id, _ = await prepared(app_engine, rfq_rows, mailboxes)
    jobs = FakeJobQueue()
    await decide_approval(
        app_engine, approver(rfq_rows), rejected_id, "reject", jobs=jobs, on_reject=REJECT_HOOKS
    )
    async with tenant_session(app_engine, rfq_rows.tenant) as session:
        invite = await session.get(Invite, invite_id)
    assert invite is not None and invite.status == "draft"
    assert jobs.jobs == []

    # A second proposal is a new attempt: a new approval, not the rejected one.
    again = await service.prepare_send(app_engine, buyer(rfq_rows), rfq.id, FakeWriter(), mailboxes)
    [proposal] = again.approvals
    assert proposal.invite_id == invite_id and proposal.approval_id != rejected_id
    await decide_approval(
        app_engine, approver(rfq_rows), proposal.approval_id, "approve", jobs=jobs
    )
    registry = ActToolRegistry()
    register_act_tools(
        registry, RfqActDeps(db=app_engine, email_domain=EMAIL_DOMAIN, mailbox_senders=mailboxes)
    )
    await ApprovalExecutor(app_engine, registry)(jobs.jobs[0])

    assert len(mailboxes.sender.sent) == 1
    async with tenant_session(app_engine, rfq_rows.tenant) as session:
        invite = await session.get(Invite, invite_id)
    assert invite is not None and invite.status == "sent"


async def test_rejected_proposal_is_refused_by_the_send_tool(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    mailboxes = FakeMailboxes()
    _, _, approval_id, _ = await prepared(app_engine, rfq_rows, mailboxes)
    await decide_approval(
        app_engine,
        approver(rfq_rows),
        approval_id,
        "reject",
        jobs=FakeJobQueue(),
        on_reject=REJECT_HOOKS,
    )
    async with tenant_session(app_engine, rfq_rows.tenant) as session:
        approval = await session.get(Approval, approval_id)
    assert approval is not None
    registry = ActToolRegistry()
    register_act_tools(
        registry, RfqActDeps(db=app_engine, email_domain=EMAIL_DOMAIN, mailbox_senders=mailboxes)
    )
    tool = registry.get(SEND_RFQ_EMAIL)
    assert tool is not None
    with pytest.raises(InvalidState, match="rejected"):
        await tool.invoke(
            ToolContext(
                tenant_id=rfq_rows.tenant, user_id=rfq_rows.approver, run_id=UUID(approval.run_id)
            ),
            SendRfqEmailArgs.model_validate(approval.args),
        )
    assert mailboxes.sender.sent == []
