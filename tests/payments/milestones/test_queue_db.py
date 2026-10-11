"""The approval queue shows a milestone release exactly as approved.

The milestone, its share of the order and of the card charge, the documents and the
evidence hash binding them come from the approval's arguments; the supplier's name, who
sent the documents and their note from the rows; the labels and the checks to make
from the catalog. The submission is built by hand here, the way the submit does it
(`payments.milestones.service.submit_milestone`), so the queue is tested on its own.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

import pytest
from pydantic import JsonValue
from sqlalchemy import select

from bido_back.agents.approvals import ApprovalProposal
from bido_back.agents.events import args_hash
from bido_back.approvals import PostgresApprovalSink
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    IssuedPurchaseOrder,
    PoFactoryLink,
    PoMilestoneDocument,
    PoMilestoneSubmission,
    PoPayment,
)
from bido_back.payments.milestones.catalog import EVIDENCE, TOOL_START, decimal_shares
from bido_back.payments.milestones.proposals import RELEASE_PO_MILESTONE, release_proposal_ids
from bido_back.payments.milestones.schemas import MilestoneDocumentRef, ReleaseMilestoneArgs
from bido_back.payments.milestones.state import ensure_milestones, evidence_hash
from bido_back.payments.pipeline import PaymentAdvanceHandler
from bido_back.payments.proposals import PAY_PURCHASE_ORDER
from bido_back.payments.schemas import MilestoneInput
from bido_back.rfqs import service as rfq_service
from bido_back.rfqs.schemas import ApprovalQueueItem
from tests.awards.test_awards_db import World, approver
from tests.payments.fakes import FakeProviders
from tests.payments.flow.conftest import propose_payment
from tests.payments.flow.test_execute_db import start_paying
from tests.payments.milestones.conftest import Funded

pytestmark = pytest.mark.db


PNG = b"\x89PNG\r\n\x1a\n"
PDF = b"%PDF-1.4\n"


@dataclass(frozen=True, slots=True)
class File:
    doc_type: str
    filename: str
    content_type: str
    data: bytes


def png(doc_type: str, filename: str) -> File:
    return File(doc_type, filename, "image/png", PNG + secrets.token_bytes(64))


def pdf(doc_type: str, filename: str) -> File:
    return File(doc_type, filename, "application/pdf", PDF + secrets.token_bytes(64))


@dataclass(frozen=True, slots=True)
class Proposed:
    approval_id: UUID
    submission_id: UUID
    args: ReleaseMilestoneArgs


async def propose_release(
    world: World,
    payment_id: UUID,
    index: int,
    files: list[File],
    *,
    source: Literal["factory", "buyer"],
    note: str | None,
) -> Proposed:
    """A submission of `files` for milestone `index`, its pending `release_po_milestone`
    approval (requested on behalf of the payment's requester), both rows `submitted`."""
    tenant, owner = world.people.tenant, world.people.owner
    now = datetime.now(UTC)
    async with tenant_session(world.engine, tenant) as session:
        payment = await session.get(PoPayment, payment_id)
        assert payment is not None and payment.escrow_address is not None
        po = await session.get(IssuedPurchaseOrder, payment.purchase_order_id)
        assert po is not None
        milestone = (await ensure_milestones(session, payment))[index]
        link: PoFactoryLink | None = None
        if source == "factory":
            link = PoFactoryLink(
                tenant_id=tenant,
                payment_id=payment.id,
                token_hash=secrets.token_hex(32),
                created_by=owner,
                expires_at=now + timedelta(days=90),
            )
            session.add(link)
            await session.flush()
        submission = PoMilestoneSubmission(
            tenant_id=tenant,
            payment_id=payment.id,
            milestone_id=milestone.id,
            milestone_index=index,
            attempt=1,
            source=source,
            factory_link_id=link.id if link is not None else None,
            submitted_by=owner if source == "buyer" else None,
        )
        session.add(submission)
        await session.flush()
        documents: list[PoMilestoneDocument] = []
        for file in files:
            sha256 = hashlib.sha256(file.data).hexdigest()
            extension = file.filename.rsplit(".", 1)[-1]
            document = PoMilestoneDocument(
                tenant_id=tenant,
                payment_id=payment.id,
                milestone_id=milestone.id,
                submission_id=submission.id,
                doc_type=file.doc_type,
                filename=file.filename,
                content_type=file.content_type,
                size_bytes=len(file.data),
                sha256=sha256,
                storage_key=f"milestones/{tenant}/{payment.id}/{index}/{sha256}.{extension}",
                source=source,
                factory_link_id=link.id if link is not None else None,
                uploaded_by=owner if source == "buyer" else None,
            )
            session.add(document)
            documents.append(document)
        await session.flush()
        bps = [int(m["bps"]) for m in payment.milestones]
        args = ReleaseMilestoneArgs(
            payment_id=payment.id,
            purchase_order_id=po.id,
            purchase_order_ref=po.ref,
            rfq_id=po.rfq_id,
            invite_id=po.invite_id,
            supplier_id=po.supplier_id,
            requested_by=payment.requested_by,
            milestone_id=milestone.id,
            milestone_index=index,
            milestone_key=milestone.key,
            milestone_name=milestone.name,
            bps=milestone.bps,
            token_amount=str(int(milestone.token_amount)),
            po_currency=payment.po_currency,
            po_amount=f"{decimal_shares(payment.po_total, bps)[index]:f}",
            charge_currency="USD",
            charge_amount=f"{decimal_shares(payment.charge_amount, bps)[index]:f}",
            chain_po_id=payment.chain_po_id,
            escrow_address=payment.escrow_address,
            content_hash=payment.po_hash,
            submission_id=submission.id,
            attempt=submission.attempt,
            evidence_hash=evidence_hash(payment.chain_po_id, index, documents),
            documents=[
                MilestoneDocumentRef(
                    document_id=d.id,
                    doc_type=d.doc_type,
                    filename=d.filename,
                    size_bytes=d.size_bytes,
                    sha256=d.sha256,
                )
                for d in documents
            ],
            simulated=payment.charge_mode == "simulated",
        )
    payload = cast(dict[str, JsonValue], args.model_dump(mode="json"))
    run_id, call_id = release_proposal_ids(payment_id, index, 0)
    await PostgresApprovalSink(world.engine).request_approval(
        ApprovalProposal(
            tenant_id=tenant,
            run_id=run_id,
            session_id=f"rfq:{args.rfq_id}",
            tool_call_id=call_id,
            tool=RELEASE_PO_MILESTONE,
            args=payload,
            args_hash=args_hash(payload),
            requested_by=args.requested_by,
        )
    )
    async with tenant_session(world.engine, tenant) as session:
        approval_id = await session.scalar(
            select(Approval.id).where(
                Approval.run_id == str(run_id), Approval.tool_call_id == call_id
            )
        )
        assert approval_id is not None
        row = await session.get(PoMilestoneSubmission, args.submission_id)
        assert row is not None
        row.state, row.approval_id, row.note = "submitted", approval_id, note
        row.evidence_hash, row.submitted_at = args.evidence_hash, now
        payment = await session.get(PoPayment, payment_id)
        assert payment is not None
        milestone = (await ensure_milestones(session, payment))[index]
        milestone.state, milestone.approval_id = "submitted", approval_id
        milestone.submission_id, milestone.evidence_hash = row.id, args.evidence_hash
    return Proposed(approval_id, args.submission_id, args)


async def queue_item(world: World, approval_id: UUID) -> ApprovalQueueItem:
    queue = await rfq_service.list_approvals(world.engine, approver(world.people), "pending")
    [item] = [i for i in queue if i.id == approval_id]
    assert all(i.milestone_release is None for i in queue if i.tool != RELEASE_PO_MILESTONE)
    return item


async def test_the_queue_shows_exactly_the_milestone_release_to_approve(funded: Funded) -> None:
    photo = png("tool_start_photo", "steel-1.png")
    certificate = pdf("steel_certificate", "mill-certificate.pdf")
    proposed = await propose_release(
        funded.world,
        funded.payment_id,
        0,
        [photo, certificate],
        source="factory",
        note="Steel arrived on 3 October",
    )

    item = await queue_item(funded.world, proposed.approval_id)

    assert (item.tool, item.status, item.requested_by) == (
        RELEASE_PO_MILESTONE,
        "pending",
        funded.people.owner,
    )
    # The release is no payment proposal and sends no email: nothing else is shown.
    assert item.payment is None and item.recipient_checks is None
    view = item.milestone_release
    assert view is not None
    assert (view.payment_id, view.purchase_order_id, view.purchase_order_ref) == (
        funded.payment_id,
        funded.po_id,
        "PO-1",
    )
    assert view.supplier_name == "Rhein"
    assert (view.milestone_index, view.milestone_key, view.milestone_name, view.label) == (
        0,
        "tool_start",
        "Tool start",
        TOOL_START.label,
    )
    # 30% of EUR 1650.00 and of the USD 1815.00 charge.
    assert (view.bps, view.po_currency, view.po_amount) == (3_000, "EUR", Decimal("495.00"))
    assert (view.charge_currency, view.charge_amount) == ("USD", Decimal("544.50"))
    assert (view.attempt, view.submission_id, view.source, view.note) == (
        1,
        proposed.submission_id,
        "factory",
        "Steel arrived on 3 October",
    )
    assert view.evidence_hash == proposed.args.evidence_hash
    assert [(d.doc_type, d.label, d.filename, d.size_bytes, d.sha256) for d in view.documents] == [
        (
            file.doc_type,
            label,
            file.filename,
            len(file.data),
            hashlib.sha256(file.data).hexdigest(),
        )
        for file, label in (
            (photo, TOOL_START.documents[0].label),
            (certificate, TOOL_START.documents[1].label),
        )
    ]
    assert [d.document_id for d in view.documents] == [
        d.document_id for d in proposed.args.documents
    ]
    assert view.checks == list(TOOL_START.checks)
    assert view.simulated is False


async def test_a_buyers_submission_shows_as_theirs(funded: Funded) -> None:
    proposed = await propose_release(
        funded.world,
        funded.payment_id,
        0,
        [png("tool_start_photo", "steel.png"), pdf("steel_certificate", "certificate.pdf")],
        source="buyer",
        note=None,
    )

    view = (await queue_item(funded.world, proposed.approval_id)).milestone_release

    assert view is not None
    assert (view.source, view.note) == ("buyer", None)


async def test_a_custom_milestone_shows_its_name_and_the_generic_evidence(
    world: World, providers: FakeProviders
) -> None:
    paying = await start_paying(world, providers)
    proposed = await propose_payment(
        world.engine,
        paying.po,
        requested_by=world.people.owner,
        payment_method_id=paying.method_id,
        payout_account_id=paying.account_id,
        milestones=[
            MilestoneInput(name="Deposit", bps=2_000),
            MilestoneInput(name="Delivery", bps=8_000),
        ],
    )
    await world.approve(proposed.approval_id)
    pipeline = PaymentAdvanceHandler(world.engine, providers.bundle(), None, poll_budget=0)
    states = [await pipeline.advance(world.people.tenant, proposed.payment_id) for _ in range(3)]
    assert states == ["minted", "escrow_created", "funded"]
    release = await propose_release(
        world,
        proposed.payment_id,
        0,
        [pdf("evidence", "deposit-invoice.pdf")],
        source="buyer",
        note=None,
    )

    view = (await queue_item(world, release.approval_id)).milestone_release

    assert view is not None
    assert (view.milestone_key, view.milestone_name, view.label, view.bps) == (
        None,
        "Deposit",
        "Deposit",
        2_000,
    )
    assert (view.po_amount, view.charge_amount) == (Decimal("330.00"), Decimal("363.00"))
    assert [(d.doc_type, d.label) for d in view.documents] == [(EVIDENCE.code, EVIDENCE.label)]
    assert view.checks == ["The evidence shows the milestone is reached."]


async def test_arguments_that_no_longer_validate_show_no_details(funded: Funded) -> None:
    """Logged, and shown without details: the act tool refuses them anyway."""
    payload: dict[str, JsonValue] = {"payment_id": str(funded.payment_id), "milestone_index": 0}
    run_id, call_id = release_proposal_ids(funded.payment_id, 0, 0)
    await PostgresApprovalSink(funded.engine).request_approval(
        ApprovalProposal(
            tenant_id=funded.tenant,
            run_id=run_id,
            session_id=f"rfq:{funded.paying.po.rfq_id}",
            tool_call_id=call_id,
            tool=RELEASE_PO_MILESTONE,
            args=payload,
            args_hash=args_hash(payload),
            requested_by=funded.people.owner,
        )
    )
    people = funded.people
    pending = await rfq_service.list_approvals(funded.engine, approver(people), "pending")
    executed = await rfq_service.list_approvals(funded.engine, approver(people), "executed")

    [item] = [i for i in pending if i.tool == RELEASE_PO_MILESTONE]
    assert item.milestone_release is None
    # The payment's own proposal, executed, still shows its details.
    [paid] = [i for i in executed if i.tool == PAY_PURCHASE_ORDER]
    assert paid.payment is not None and paid.milestone_release is None
