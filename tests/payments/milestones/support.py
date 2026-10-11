"""Milestone helpers shared by the tests: submit without the factory's service, release.

* `submit_directly(funded, index)` records a milestone submission the way
  `service.submit_milestone` does: a draft with one file per required document type of
  the milestone (`catalog.definition_for(...).required_codes`, fake sha256 values), the
  `release_po_milestone` approval proposed through `PostgresApprovalSink` with
  `release_proposal_ids` and exactly-built `ReleaseMilestoneArgs`, then the submission
  and the milestone `submitted`. No ordering check: a test may submit out of order.
* `release_tools(funded)` registers the act tool in the World's executor (once), waking
  `funded.jobs`; `release_handler(funded)` is the job handler on the fake chain (no
  polling).
* `approve_release(funded, approval_id)` approves through the guard (an approver) and
  runs the execution; it returns the `milestone.release` jobs the claim enqueued.
* `release_all(funded)` releases every milestone, in order: the payment ends complete.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, cast
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import func, select

from bido_back.agents.approvals import ApprovalProposal
from bido_back.agents.events import args_hash
from bido_back.approvals import PostgresApprovalSink
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    IssuedPurchaseOrder,
    PaymentChainTransaction,
    PoMilestoneDocument,
    PoMilestoneSubmission,
    PoPayment,
    PoPaymentMilestone,
)
from bido_back.payments.milestones.catalog import decimal_shares, definition_for
from bido_back.payments.milestones.execute import MilestoneActDeps, register_milestone_act_tools
from bido_back.payments.milestones.proposals import (
    MILESTONE_RELEASE,
    RELEASE_PO_MILESTONE,
    release_proposal_ids,
)
from bido_back.payments.milestones.release import (
    MilestoneReleaseHandler,
    milestone_release_envelope,
)
from bido_back.payments.milestones.schemas import MilestoneDocumentRef, ReleaseMilestoneArgs
from bido_back.payments.milestones.state import ensure_milestones, evidence_hash, locked_milestone
from bido_back.worker.envelope import Envelope
from tests.payments.flow.conftest import chain_transactions
from tests.payments.milestones.conftest import Funded

CONTENT_TYPES: Final[dict[str, str]] = {
    "pdf": "application/pdf",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
}


@dataclass(frozen=True, slots=True)
class Submitted:
    """A milestone under review: its pending approval and what it approves."""

    approval_id: UUID
    run_id: UUID
    submission_id: UUID
    milestone_id: UUID
    args: ReleaseMilestoneArgs


def release_tools(funded: Funded) -> None:
    """`release_po_milestone` in the World's executor (registered once), waking `funded.jobs`."""
    registry = funded.world.registry
    if registry.get(RELEASE_PO_MILESTONE) is None:
        register_milestone_act_tools(
            registry,
            MilestoneActDeps(
                db=funded.engine, providers=funded.providers.bundle(), jobs=funded.jobs
            ),
        )


def release_handler(funded: Funded, *, attester: str | None = None) -> MilestoneReleaseHandler:
    """The `milestone.release` handler on the fake chain: one status read, no polling."""
    return MilestoneReleaseHandler(
        funded.engine,
        funded.providers.bundle(),
        funded.jobs,
        poll_budget=0,
        attester=attester or funded.chain.attester,
    )


def release_jobs(funded: Funded) -> list[Envelope]:
    return [job for job in funded.jobs.jobs if job.type == MILESTONE_RELEASE]


def file_sha256(submission_id: UUID, doc_type: str, n: int = 0) -> str:
    """A fake file's sha256 (the files themselves are never stored here)."""
    return hashlib.sha256(f"{submission_id}:{doc_type}:{n}".encode()).hexdigest()


async def submit_directly(
    funded: Funded, index: int, *, note: str | None = None, requested_by: UUID | None = None
) -> Submitted:
    """A pending release of milestone `index`, recorded like the factory's submit.

    `requested_by`: the approval's requester (default: the payment's, as the service does).
    """
    engine, tenant = funded.engine, funded.tenant
    async with tenant_session(engine, tenant) as session:
        payment = await session.get(PoPayment, funded.payment_id)
        assert payment is not None and payment.escrow_address is not None
        milestones = await ensure_milestones(session, payment)
        milestone = milestones[index]
        po = await session.get(IssuedPurchaseOrder, payment.purchase_order_id)
        assert po is not None
        attempt = (
            await session.scalar(
                select(func.max(PoMilestoneSubmission.attempt)).where(
                    PoMilestoneSubmission.milestone_id == milestone.id
                )
            )
            or 0
        ) + 1
        submission = PoMilestoneSubmission(
            tenant_id=tenant,
            payment_id=payment.id,
            milestone_id=milestone.id,
            milestone_index=index,
            attempt=attempt,
            state="draft",
            source="buyer",
            submitted_by=payment.requested_by,
        )
        session.add(submission)
        await session.flush()
        definition = definition_for(milestone.key, milestone.name, milestone.bps)
        documents: list[PoMilestoneDocument] = []
        for code in sorted(definition.required_codes):
            doc_type = definition.document(code)
            assert doc_type is not None
            extension = doc_type.accepts[0]
            sha256 = file_sha256(submission.id, code)
            document = PoMilestoneDocument(
                tenant_id=tenant,
                payment_id=payment.id,
                milestone_id=milestone.id,
                submission_id=submission.id,
                doc_type=code,
                filename=f"{code}.{extension}",
                content_type=CONTENT_TYPES.get(extension, "application/octet-stream"),
                size_bytes=1024 + len(documents),
                sha256=sha256,
                storage_key=f"milestones/{tenant}/{payment.id}/{index}/{sha256}.{extension}",
                source="buyer",
                uploaded_by=payment.requested_by,
            )
            session.add(document)
            documents.append(document)
        await session.flush()
        bps = [m.bps for m in milestones]
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
            attempt=attempt,
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
        call_id = release_proposal_ids(payment.id, index, 0)[1]
        proposals = (
            await session.scalar(
                select(func.count()).where(
                    Approval.tool == RELEASE_PO_MILESTONE, Approval.tool_call_id == call_id
                )
            )
            or 0
        )
        requester = requested_by or payment.requested_by
        rfq_id = po.rfq_id
    run_id, call_id = release_proposal_ids(funded.payment_id, index, proposals)
    payload = cast(dict[str, JsonValue], args.model_dump(mode="json"))
    await PostgresApprovalSink(engine).request_approval(
        ApprovalProposal(
            tenant_id=tenant,
            run_id=run_id,
            session_id=f"rfq:{rfq_id}",
            tool_call_id=call_id,
            tool=RELEASE_PO_MILESTONE,
            args=payload,
            args_hash=args_hash(payload),
            requested_by=requester,
        )
    )
    async with tenant_session(engine, tenant) as session:
        approval_id = await session.scalar(
            select(Approval.id).where(
                Approval.run_id == str(run_id), Approval.tool_call_id == call_id
            )
        )
        assert approval_id is not None
        await session.scalar(
            select(PoPayment.id).where(PoPayment.id == funded.payment_id).with_for_update()
        )
        row = await locked_milestone(session, funded.payment_id, index)
        draft = await session.get(PoMilestoneSubmission, args.submission_id)
        assert draft is not None and draft.state == "draft"
        draft.state = "submitted"
        draft.evidence_hash = args.evidence_hash
        draft.approval_id = approval_id
        draft.submitted_at = datetime.now(UTC)
        draft.note = note
        draft.updated_at = func.now()
        row.state = "submitted"
        row.approval_id = approval_id
        row.submission_id = args.submission_id
        row.evidence_hash = args.evidence_hash
        row.failure_code = None
        row.updated_at = func.now()
        await session.flush()
    return Submitted(approval_id, run_id, args.submission_id, args.milestone_id, args)


async def approve_release(funded: Funded, approval_id: UUID) -> list[Envelope]:
    """An approver approves the release and the worker executes it (the act claims the
    milestone); returns the `milestone.release` jobs it enqueued."""
    release_tools(funded)
    await funded.world.approve(approval_id)
    return release_jobs(funded)


async def milestone_row(funded: Funded, index: int) -> PoPaymentMilestone:
    async with tenant_session(funded.engine, funded.tenant) as session:
        row = await session.scalar(
            select(PoPaymentMilestone).where(
                PoPaymentMilestone.payment_id == funded.payment_id,
                PoPaymentMilestone.milestone_index == index,
            )
        )
    assert row is not None
    return row


async def submission_row(funded: Funded, submission_id: UUID) -> PoMilestoneSubmission:
    async with tenant_session(funded.engine, funded.tenant) as session:
        row = await session.get(PoMilestoneSubmission, submission_id)
    assert row is not None
    return row


async def release_transactions(funded: Funded) -> list[PaymentChainTransaction]:
    """The payment's `release` transactions, oldest first."""
    rows = await chain_transactions(funded.engine, funded.tenant, funded.payment_id)
    return [row for row in rows if row.step == "release"]


async def release_rows(funded: Funded) -> list[tuple[int | None, int, str]]:
    """(milestone index, attempt, state) of each `release` transaction, oldest first."""
    return [(t.milestone_index, t.attempt, t.state) for t in await release_transactions(funded)]


async def release_all(funded: Funded) -> list[str]:
    """Submit, approve and release every milestone, in order; returns the release
    signatures. The payment ends complete (`completed_at`, still `funded`) and the escrow
    paid out in full."""
    handler = release_handler(funded)
    count = len((await funded.payment()).milestones)
    signatures: list[str] = []
    for index in range(count):
        submitted = await submit_directly(funded, index)
        jobs = await approve_release(funded, submitted.approval_id)
        # An act tool registered without a queue leaves the wake-up to the sweep.
        await handler(
            jobs[0] if jobs else milestone_release_envelope(funded.tenant, funded.payment_id, index)
        )
        row = await milestone_row(funded, index)
        assert row.state == "released" and row.release_signature is not None, row.state
        signatures.append(row.release_signature)
    payment = await funded.payment()
    assert payment.state == "funded" and payment.completed_at is not None, payment.state
    return signatures
