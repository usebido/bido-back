"""Sending a milestone for approval: exactly its files, in order, once; what happens when a
person decides in the instant before the submission is recorded, when recording fails, and
when the buyer asks for changes (the next draft starts with the refused files).

`submit_required` and `mark_released` are shared with the other milestone tests.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.agents.approvals import ApprovalProposal
from bido_back.agents.events import args_hash
from bido_back.approvals import Decision, PostgresApprovalSink, decide_approval
from bido_back.approvals.integrity import payload_matches
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    IssuedPurchaseOrder,
    PoMilestoneSubmission,
    PoPayment,
    PoPaymentMilestone,
)
from bido_back.payments.chain.hashes import compute_evidence_hash
from bido_back.payments.errors import (
    PaymentInvalidState,
    PaymentRefused,
    PaymentsNotConfigured,
)
from bido_back.payments.milestones import service
from bido_back.payments.milestones.documents import Uploader, remove_document
from bido_back.payments.milestones.hooks import MILESTONE_REJECT_HOOKS
from bido_back.payments.milestones.proposals import (
    MILESTONE_CHANGES_REQUESTED,
    MILESTONE_SUBMITTED,
    RELEASE_PO_MILESTONE,
    release_proposal_ids,
)
from bido_back.payments.milestones.schemas import (
    MilestoneDocumentRef,
    ReleaseMilestoneArgs,
    SubmitMilestoneRequest,
)
from bido_back.payments.milestones.service import request_milestone_changes, submit_milestone
from bido_back.payments.milestones.state import (
    active_documents,
    documents_match,
    release_terms_match,
)
from bido_back.payments.milestones.views import MilestoneDocumentView, MilestoneSubmitted
from bido_back.settings import Settings
from tests.approvals.conftest import FakeJobQueue
from tests.awards.test_awards_db import approver, buyer
from tests.payments.fakes import address
from tests.payments.milestones.conftest import Funded
from tests.payments.milestones.test_documents_db import (
    SETTINGS,
    FakeStorage,
    audit_payloads,
    definition_at,
    factory_link,
    jpeg,
    release_settings,
    upload,
    upload_required,
)

pytestmark = pytest.mark.db


async def submit(
    funded: Funded,
    submitter: Uploader,
    index: int,
    *,
    note: str | None = None,
    settings: Settings = SETTINGS,
) -> MilestoneSubmitted:
    return await submit_milestone(
        funded.engine,
        submitter,
        funded.payment_id,
        index,
        SubmitMilestoneRequest(note=note),
        settings=settings,
    )


async def submit_required(
    funded: Funded,
    index: int,
    *,
    storage: FakeStorage,
    submitter: Uploader | None = None,
) -> MilestoneSubmitted:
    """Gather milestone `index`'s required documents and send it (by the payment's
    requester unless `submitter` is given): a pending `release_po_milestone` approval."""
    who = submitter or buyer(funded.people)
    await upload_required(funded, who, index, storage=storage)
    return await submit(funded, who, index)


async def mark_released(funded: Funded, index: int) -> str:
    """What the release job records once its transaction is confirmed (the release agent's
    part): the milestone and its submission `released`. Returns the signature."""
    signature = address(f"release-{funded.payment_id}-{index}")
    async with tenant_session(funded.engine, funded.tenant) as session:
        row = await milestone_row_in(session, funded, index)
        assert row.state == "submitted" and row.submission_id is not None
        row.state = "released"
        row.released_at = datetime.now(UTC)
        row.release_signature = signature
        submission = await session.get(PoMilestoneSubmission, row.submission_id)
        assert submission is not None
        submission.state = "released"
        await session.flush()
    return signature


async def milestone_row_in(session: AsyncSession, funded: Funded, index: int) -> PoPaymentMilestone:
    row = await session.scalar(
        select(PoPaymentMilestone).where(
            PoPaymentMilestone.payment_id == funded.payment_id,
            PoPaymentMilestone.milestone_index == index,
        )
    )
    assert row is not None
    return row


async def milestone_row(funded: Funded, index: int) -> PoPaymentMilestone:
    async with tenant_session(funded.engine, funded.tenant) as session:
        return await milestone_row_in(session, funded, index)


async def submissions(funded: Funded, index: int) -> list[PoMilestoneSubmission]:
    """Milestone `index`'s submissions, oldest first."""
    async with tenant_session(funded.engine, funded.tenant) as session:
        return list(
            await session.scalars(
                select(PoMilestoneSubmission)
                .where(
                    PoMilestoneSubmission.payment_id == funded.payment_id,
                    PoMilestoneSubmission.milestone_index == index,
                )
                .order_by(PoMilestoneSubmission.attempt)
            )
        )


async def release_approvals(funded: Funded) -> list[Approval]:
    async with tenant_session(funded.engine, funded.tenant) as session:
        return list(
            await session.scalars(
                select(Approval)
                .where(Approval.tool == RELEASE_PO_MILESTONE)
                .order_by(Approval.created_at)
            )
        )


async def approval_row(funded: Funded, approval_id: UUID) -> Approval:
    async with tenant_session(funded.engine, funded.tenant) as session:
        approval = await session.get(Approval, approval_id)
        assert approval is not None
        return approval


def refs(documents: list[MilestoneDocumentView]) -> list[MilestoneDocumentRef]:
    return [
        MilestoneDocumentRef(
            document_id=d.id,
            doc_type=d.doc_type,
            filename=d.filename,
            size_bytes=d.size_bytes,
            sha256=d.sha256,
        )
        for d in documents
    ]


# --- Sending ---------------------------------------------------------------------------------


async def test_a_milestone_is_sent_for_approval_with_exactly_its_files(funded: Funded) -> None:
    storage = FakeStorage()
    owner = buyer(funded.people)
    definition = await definition_at(funded, 0)
    files = await upload_required(funded, owner, 0, storage=storage)
    optional = next(d for d in definition.documents if not d.required)
    files.append(await upload(funded, owner, 0, optional.code, storage=storage))

    submitted = await submit(funded, owner, 0, note="Both halves, steel stamped.")

    payment = await funded.payment()
    row = await milestone_row(funded, 0)
    [draft] = await submissions(funded, 0)
    async with tenant_session(funded.engine, funded.tenant) as session:
        po = await session.get(IssuedPurchaseOrder, payment.purchase_order_id)
    assert po is not None and payment.escrow_address is not None
    approval = await approval_row(funded, submitted.approval_id)
    run_id, call_id = release_proposal_ids(funded.payment_id, 0, 0)
    assert (approval.tool, approval.status, approval.requested_by) == (
        RELEASE_PO_MILESTONE,
        "pending",
        payment.requested_by,
    )
    assert (approval.run_id, approval.tool_call_id, approval.session_id) == (
        str(run_id),
        call_id,
        f"rfq:{po.rfq_id}",
    )
    assert payload_matches(approval) and approval.args_hash == args_hash(approval.args)
    args = ReleaseMilestoneArgs.model_validate(approval.args)
    assert args == ReleaseMilestoneArgs(
        payment_id=funded.payment_id,
        purchase_order_id=po.id,
        purchase_order_ref=po.ref,
        rfq_id=po.rfq_id,
        invite_id=po.invite_id,
        supplier_id=po.supplier_id,
        requested_by=funded.people.owner,
        milestone_id=row.id,
        milestone_index=0,
        milestone_key=row.key,
        milestone_name=row.name,
        bps=row.bps,
        # The program's split of 1815 USD in 6-decimal base units; 30% of the order.
        token_amount=str(544_500_000),
        po_currency="EUR",
        po_amount="495.00",
        charge_currency="USD",
        charge_amount="544.50",
        chain_po_id=payment.chain_po_id,
        escrow_address=payment.escrow_address,
        content_hash=payment.po_hash,
        submission_id=draft.id,
        attempt=1,
        evidence_hash=compute_evidence_hash(payment.chain_po_id, 0, [f.sha256 for f in files]),
        documents=refs(files),
        simulated=False,
    )
    assert int(row.token_amount) == 544_500_000

    # The milestone waits for the approval; the draft became the submission under review.
    assert (row.state, row.approval_id, row.submission_id, row.evidence_hash) == (
        "submitted",
        submitted.approval_id,
        draft.id,
        args.evidence_hash,
    )
    assert (draft.state, draft.approval_id, draft.evidence_hash, draft.note) == (
        "submitted",
        submitted.approval_id,
        args.evidence_hash,
        "Both halves, steel stamped.",
    )
    assert (draft.source, draft.submitted_by, draft.factory_link_id) == (
        "buyer",
        funded.people.owner,
        None,
    )
    assert draft.submitted_at is not None
    # What the release checks every time holds for what was recorded.
    async with tenant_session(funded.engine, funded.tenant) as session:
        documents = await active_documents(session, draft.id)
    assert release_terms_match(payment, row, draft, args)
    assert documents_match(args, documents)

    view = submitted.milestone
    assert (view.status, view.approval_id, view.evidence_hash) == (
        "in_review",
        submitted.approval_id,
        args.evidence_hash,
    )
    assert [s.state for s in view.submissions] == ["submitted"]
    assert [d.id for d in view.submissions[0].documents] == [f.id for f in files]
    [event] = await audit_payloads(funded, MILESTONE_SUBMITTED)
    assert event["approval_id"] == str(submitted.approval_id)
    assert (event["milestone_index"], event["attempt"], event["source"]) == (0, 1, "buyer")
    assert event["document_count"] == len(files)
    assert "Both halves" not in str(event)


async def test_the_factory_sends_a_milestone_through_its_confirmed_link(funded: Funded) -> None:
    storage = FakeStorage()
    _, first = await factory_link(funded)
    await upload_required(funded, first, 0, storage=storage)
    # A new link stops the first one and must confirm the order again.
    _, second = await factory_link(funded, accept=False)
    with pytest.raises(PaymentInvalidState, match="confirm the order"):
        await submit(funded, second, 0)
    _, third = await factory_link(funded)

    submitted = await submit(funded, third, 0)

    [draft] = await submissions(funded, 0)
    assert (draft.source, draft.factory_link_id, draft.submitted_by) == (
        "factory",
        third.link_id,
        None,
    )
    approval = await approval_row(funded, submitted.approval_id)
    # The factory is no user: the release is requested in the payment requester's name.
    assert approval.requested_by == funded.people.owner
    [event] = await audit_payloads(funded, MILESTONE_SUBMITTED)
    assert (event["source"], event["factory_link_id"]) == ("factory", str(third.link_id))


async def test_nothing_is_proposed_without_the_attester_key(funded: Funded) -> None:
    storage = FakeStorage()
    owner = buyer(funded.people)
    await upload_required(funded, owner, 0, storage=storage)

    with pytest.raises(PaymentsNotConfigured):
        await submit(funded, owner, 0, settings=release_settings(solana_gate_attester_key=None))

    assert await release_approvals(funded) == []
    assert (await milestone_row(funded, 0)).state == "open"


async def test_every_required_document_is_needed(funded: Funded) -> None:
    storage = FakeStorage()
    owner = buyer(funded.people)
    required = sorted((await definition_at(funded, 0)).required_codes)

    with pytest.raises(PaymentRefused) as nothing:
        await submit(funded, owner, 0)
    assert all(code in str(nothing.value) for code in required)

    await upload(funded, owner, 0, required[0], storage=storage)
    with pytest.raises(PaymentRefused) as missing:
        await submit(funded, owner, 0)
    assert required[0] not in str(missing.value)
    assert all(code in str(missing.value) for code in required[1:])
    assert await release_approvals(funded) == []


async def test_milestones_are_sent_in_order(funded: Funded) -> None:
    storage = FakeStorage()
    owner = buyer(funded.people)
    await upload_required(funded, owner, 1, storage=storage)
    with pytest.raises(PaymentInvalidState, match="milestone 0 is released"):
        await submit(funded, owner, 1)

    first = await submit_required(funded, 0, storage=storage)
    with pytest.raises(PaymentInvalidState, match="already waiting"):
        await submit(funded, owner, 0)
    with pytest.raises(PaymentInvalidState, match="milestone 0 is released"):
        await submit(funded, owner, 1)

    await mark_released(funded, 0)
    second = await submit(funded, owner, 1)

    approvals = await release_approvals(funded)
    assert [a.id for a in approvals] == [first.approval_id, second.approval_id]
    run_id, call_id = release_proposal_ids(funded.payment_id, 1, 0)
    assert (approvals[1].run_id, approvals[1].tool_call_id) == (str(run_id), call_id)
    assert ReleaseMilestoneArgs.model_validate(approvals[1].args).milestone_index == 1
    with pytest.raises(PaymentInvalidState, match="released"):
        await submit(funded, owner, 0)


async def test_only_a_funded_payment_sends_milestones(funded: Funded) -> None:
    storage = FakeStorage()
    owner = buyer(funded.people)
    await upload_required(funded, owner, 0, storage=storage)
    async with tenant_session(funded.engine, funded.tenant) as session:
        payment = await session.get(PoPayment, funded.payment_id)
        assert payment is not None
        payment.state = "failed"
        payment.failure_code = "chain_fund_failed"
        await session.flush()

    with pytest.raises(PaymentInvalidState, match="failed"):
        await submit(funded, owner, 0)
    assert await release_approvals(funded) == []


# --- Decided before it was recorded ------------------------------------------------------------


def decide_right_after_proposing(
    monkeypatch: pytest.MonkeyPatch, funded: Funded, decision: Decision
) -> None:
    """A person decides the release in the instant between the proposal and the record."""
    original = PostgresApprovalSink.request_approval

    async def propose_then_decide(self: PostgresApprovalSink, proposal: ApprovalProposal) -> None:
        await original(self, proposal)
        async with tenant_session(funded.engine, funded.tenant) as session:
            approval_id = await session.scalar(
                select(Approval.id).where(
                    Approval.run_id == str(proposal.run_id),
                    Approval.tool_call_id == proposal.tool_call_id,
                )
            )
        assert approval_id is not None
        await decide_approval(
            funded.engine,
            approver(funded.people),
            approval_id,
            decision,
            jobs=FakeJobQueue(),
            on_reject=MILESTONE_REJECT_HOOKS,
        )

    monkeypatch.setattr(PostgresApprovalSink, "request_approval", propose_then_decide)


async def test_a_refusal_before_the_record_is_recorded_as_refused(
    funded: Funded, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FakeStorage()
    owner = buyer(funded.people)
    await upload_required(funded, owner, 0, storage=storage)
    decide_right_after_proposing(monkeypatch, funded, "reject")

    submitted = await submit(funded, owner, 0)

    approval = await approval_row(funded, submitted.approval_id)
    [refused] = await submissions(funded, 0)
    row = await milestone_row(funded, 0)
    assert approval.status == "rejected"
    assert (refused.state, refused.approval_id, refused.decided_by) == (
        "rejected",
        approval.id,
        funded.people.approver,
    )
    assert (row.state, row.approval_id, row.submission_id) == ("open", None, None)
    assert submitted.milestone.status == "changes_requested"
    [event] = await audit_payloads(funded, MILESTONE_CHANGES_REQUESTED)
    assert event["submission_id"] == str(refused.id)


async def test_an_approval_before_the_record_waits_for_the_worker(
    funded: Funded, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FakeStorage()
    owner = buyer(funded.people)
    await upload_required(funded, owner, 0, storage=storage)
    decide_right_after_proposing(monkeypatch, funded, "approve")

    submitted = await submit(funded, owner, 0)

    row = await milestone_row(funded, 0)
    assert (await approval_row(funded, submitted.approval_id)).status == "approved"
    # Recorded as under review: the approved act (retried by the queue) finds it there.
    assert (row.state, row.approval_id) == ("submitted", submitted.approval_id)


async def test_a_failure_after_the_proposal_withdraws_it(
    funded: Funded, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FakeStorage()
    owner = buyer(funded.people)
    definition = await definition_at(funded, 0)
    photos = next(d for d in definition.documents if d.required and "jpg" in d.accepts)
    await upload_required(funded, owner, 0, storage=storage)
    extra = await upload(funded, owner, 0, photos.code, storage=storage, data=jpeg())
    original = PostgresApprovalSink.request_approval

    async def propose_then_change_the_files(
        self: PostgresApprovalSink, proposal: ApprovalProposal
    ) -> None:
        await original(self, proposal)
        await remove_document(funded.engine, owner, funded.payment_id, 0, extra.id)

    monkeypatch.setattr(PostgresApprovalSink, "request_approval", propose_then_change_the_files)
    with pytest.raises(PaymentInvalidState, match="changed"):
        await submit(funded, owner, 0)
    monkeypatch.undo()

    [withdrawn] = await release_approvals(funded)
    [draft] = await submissions(funded, 0)
    assert withdrawn.status == "expired"
    assert (await milestone_row(funded, 0)).state == "open"
    assert (draft.state, draft.approval_id) == ("draft", None)

    # A withdrawn proposal never blocks the next one: the next attempt gets its own ids.
    again = await submit(funded, owner, 0)
    run_id, _ = release_proposal_ids(funded.payment_id, 0, 1)
    assert (await approval_row(funded, again.approval_id)).run_id == str(run_id)
    assert again.approval_id != withdrawn.id


async def test_two_taps_proposed_together_record_one_submission(
    funded: Funded, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FakeStorage()
    owner = buyer(funded.people)
    await upload_required(funded, owner, 0, storage=storage)
    original = PostgresApprovalSink.request_approval
    taps: list[MilestoneSubmitted] = []
    proposals: list[ApprovalProposal] = []

    async def no_earlier_proposal(session: AsyncSession, call_id: str) -> int:
        return 0  # both taps counted before either proposed

    async def the_other_tap_lands_first(
        self: PostgresApprovalSink, proposal: ApprovalProposal
    ) -> None:
        proposals.append(proposal)
        await original(self, proposal)
        if len(proposals) == 1:
            taps.append(await submit(funded, owner, 0))

    monkeypatch.setattr(service, "_proposed_before", no_earlier_proposal)
    monkeypatch.setattr(PostgresApprovalSink, "request_approval", the_other_tap_lands_first)
    outer = await submit(funded, owner, 0)

    # The same proposal twice (one approval); the second recording answers the first.
    [approval] = await release_approvals(funded)
    assert outer.approval_id == taps[0].approval_id == approval.id
    assert proposals[0] == proposals[1]
    assert [s.state for s in await submissions(funded, 0)] == ["submitted"]
    assert len(await audit_payloads(funded, MILESTONE_SUBMITTED)) == 1


async def test_a_tap_that_loses_the_race_withdraws_its_proposal(
    funded: Funded, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = FakeStorage()
    owner = buyer(funded.people)
    await upload_required(funded, owner, 0, storage=storage)
    original = PostgresApprovalSink.request_approval
    taps: list[MilestoneSubmitted] = []

    async def the_other_tap_lands_first(
        self: PostgresApprovalSink, proposal: ApprovalProposal
    ) -> None:
        await original(self, proposal)
        if not taps and len(await release_approvals(funded)) == 1:
            taps.append(await submit(funded, owner, 0))

    monkeypatch.setattr(PostgresApprovalSink, "request_approval", the_other_tap_lands_first)
    with pytest.raises(PaymentInvalidState, match="already waiting"):
        await submit(funded, owner, 0)

    lost, won = await release_approvals(funded)
    assert (lost.status, won.status, won.id) == ("expired", "pending", taps[0].approval_id)
    [submission] = await submissions(funded, 0)
    assert (submission.state, submission.approval_id) == ("submitted", won.id)
    assert (await milestone_row(funded, 0)).approval_id == won.id


# --- Changes requested: the next draft starts with the refused files ---------------------------


async def test_after_changes_are_requested_the_factory_replaces_only_what_was_refused(
    funded: Funded,
) -> None:
    storage = FakeStorage()
    _, access = await factory_link(funded)
    sent = await upload_required(funded, access, 0, storage=storage)
    first = await submit(funded, access, 0)
    view = await request_milestone_changes(
        funded.engine,
        buyer(funded.people),
        funded.po_id,
        0,
        "The second photo is blurred.",
        reason_code="missing_document",
        jobs=FakeJobQueue(),
        settings=SETTINGS,
    )
    assert view.status == "changes_requested"

    # The next file starts a new draft holding copies of the refused files.
    replaced, kept = sent[0], sent[1:]
    added = await upload(funded, access, 0, replaced.doc_type, storage=storage, data=jpeg())
    refused, draft = await submissions(funded, 0)
    assert (refused.state, draft.state, draft.attempt) == ("rejected", "draft", 2)
    async with tenant_session(funded.engine, funded.tenant) as session:
        originals = {d.sha256: d for d in await active_documents(session, refused.id)}
        copies = {d.sha256: d for d in await active_documents(session, draft.id)}
    assert set(copies) == set(originals) | {added.sha256}
    for sha256, original in originals.items():
        copy = copies[sha256]
        assert copy.id != original.id and copy.submission_id == draft.id
        assert (copy.doc_type, copy.filename, copy.storage_key, copy.source) == (
            original.doc_type,
            original.filename,
            original.storage_key,
            original.source,
        )
    assert copies[added.sha256].id == added.id
    await remove_document(funded.engine, access, funded.payment_id, 0, copies[replaced.sha256].id)

    second = await submit(funded, access, 0)

    approval = await approval_row(funded, second.approval_id)
    args = ReleaseMilestoneArgs.model_validate(approval.args)
    run_id, _ = release_proposal_ids(funded.payment_id, 0, 1)
    assert (approval.run_id, args.attempt, args.submission_id) == (str(run_id), 2, draft.id)
    assert {d.sha256 for d in args.documents} == {added.sha256} | {d.sha256 for d in kept}
    assert second.approval_id != first.approval_id
    assert [s.state for s in second.milestone.submissions] == ["submitted", "rejected"]
    assert second.milestone.submissions[1].rejection_code == "missing_document"
    assert Decimal(args.po_amount) == Decimal("495.00")
