"""`release_po_milestone` against Postgres: approved by a person, claimed once, in order.

The chain is `FakePaymentChain`; the guard, the executor and the database are real. A
milestone is submitted with `support.submit_directly` (recorded like the factory's
submit), an approver approves it and the worker's execution claims it; the release job
itself is tested in `test_release_db`. Also: a rejection sends the milestone back, and a
person retries a release that failed (`retry_release`).
"""

import asyncio
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.agents.context import ToolContext
from bido_back.agents.tools import Tool
from bido_back.api.approvals import approvals_router
from bido_back.api.deps import get_db, get_job_queue, get_principal
from bido_back.approvals import decide_approval
from bido_back.auth import Principal
from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import (
    AuditEvent,
    PoMilestoneDocument,
    PoMilestoneSubmission,
    PoPayment,
    PoPaymentMilestone,
)
from bido_back.payments.errors import (
    PaymentForbidden,
    PaymentInvalidState,
    PaymentNotFound,
    ProviderError,
)
from bido_back.payments.milestones.execute import (
    MILESTONE_RELEASE_RETRIED,
    MilestoneActDeps,
    milestone_tools,
    retry_release,
)
from bido_back.payments.milestones.hooks import MILESTONE_REJECT_HOOKS
from bido_back.payments.milestones.proposals import (
    MILESTONE_CHANGES_REQUESTED,
    MILESTONE_RELEASE_FAILED,
    MILESTONE_RELEASING,
)
from bido_back.payments.milestones.release import milestone_release_envelope
from bido_back.payments.milestones.schemas import ReleaseMilestoneArgs, ReleaseMilestoneResult
from bido_back.worker.jobs import JobQueue
from tests.approvals.conftest import FakeJobQueue, load
from tests.awards.test_awards_db import approver
from tests.payments.milestones.conftest import Funded
from tests.payments.milestones.support import (
    Submitted,
    approve_release,
    file_sha256,
    milestone_row,
    release_handler,
    release_jobs,
    release_rows,
    release_tools,
    submission_row,
    submit_directly,
)

pytestmark = pytest.mark.db

_MILESTONE_ACTIONS = (
    MILESTONE_RELEASING,
    MILESTONE_RELEASE_FAILED,
    MILESTONE_CHANGES_REQUESTED,
    MILESTONE_RELEASE_RETRIED,
    "purchase_order.milestone_released",
    "purchase_order.payment_completed",
)


def act(funded: Funded, *, wake: bool = True) -> Tool[ReleaseMilestoneArgs, ReleaseMilestoneResult]:
    """The act tool as the worker registers it; `wake`: with the job queue."""
    jobs = funded.jobs if wake else None
    deps = MilestoneActDeps(db=funded.engine, providers=funded.providers.bundle(), jobs=jobs)
    [tool] = milestone_tools(deps)
    return tool


async def approve_only(funded: Funded, submitted: Submitted) -> None:
    """Approved through the guard; its execution is left to the test."""
    await decide_approval(
        funded.engine,
        approver(funded.people),
        submitted.approval_id,
        "approve",
        jobs=FakeJobQueue(),
    )


def as_approver(funded: Funded, run_id: UUID) -> ToolContext:
    """The execution's context: the approval's tenant and run, the approver acting."""
    return ToolContext(tenant_id=funded.tenant, user_id=funded.people.approver, run_id=run_id)


def result(
    funded: Funded, outcome: str, index: int = 0, code: str | None = None
) -> ReleaseMilestoneResult:
    return ReleaseMilestoneResult.model_validate(
        {
            "outcome": outcome,
            "payment_id": funded.payment_id,
            "milestone_index": index,
            "failure_code": code,
        }
    )


async def events(funded: Funded, action: str | None = None) -> list[AuditEvent]:
    """The milestone events of the payment, oldest first."""
    async with tenant_session(funded.engine, funded.tenant) as session:
        rows = await session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.payload["payment_id"].astext == str(funded.payment_id),
                AuditEvent.action.in_([action] if action else _MILESTONE_ACTIONS),
            )
            .order_by(AuditEvent.created_at, AuditEvent.id)
        )
        return list(rows)


def releases_built(funded: Funded) -> int:
    return sum(1 for step, _ in funded.chain.built if step == "release")


async def refused(funded: Funded, submitted: Submitted, index: int, code: str) -> None:
    """Refused at the claim: back to the factory with the code, nothing reached the chain."""
    row = await milestone_row(funded, index)
    assert (row.state, row.failure_code) == ("open", code)
    assert (row.approval_id, row.submission_id, row.evidence_hash) == (None, None, None)
    submission = await submission_row(funded, submitted.submission_id)
    assert (submission.state, submission.failure_code) == ("cancelled", code)
    assert submission.decided_by == funded.people.approver
    approval = await load(funded.engine, funded.tenant, submitted.approval_id)
    assert approval.status == "executed"
    assert release_jobs(funded) == []
    assert releases_built(funded) == 0
    [event] = await events(funded, MILESTONE_RELEASE_FAILED)
    assert event.payload["failure_code"] == code
    assert event.payload["milestone_index"] == index
    assert event.payload["approval_id"] == str(submitted.approval_id)


# --- Claimed ---------------------------------------------------------------------------------


async def test_an_approved_milestone_is_claimed_once_and_its_release_woken(funded: Funded) -> None:
    submitted = await submit_directly(funded, 0)

    jobs = await approve_release(funded, submitted.approval_id)

    # The release is woken after the commit, keyed by the milestone.
    assert jobs == [milestone_release_envelope(funded.tenant, funded.payment_id, 0)]
    assert jobs[0].idempotency_key == f"milestone:{funded.payment_id}:0:release"
    assert jobs[0].payload == {"payment_id": str(funded.payment_id), "milestone_index": 0}
    row = await milestone_row(funded, 0)
    assert (row.state, row.approval_id, row.submission_id, row.failure_code) == (
        "releasing",
        submitted.approval_id,
        submitted.submission_id,
        None,
    )
    assert row.evidence_hash == submitted.args.evidence_hash
    approval = await load(funded.engine, funded.tenant, submitted.approval_id)
    assert approval.status == "executed"
    assert approval.decided_by == funded.people.approver
    assert approval.requested_by == funded.people.owner  # the payment's requester
    submission = await submission_row(funded, submitted.submission_id)
    assert (submission.state, submission.decided_by, submission.decided_at) == (
        "approved",
        funded.people.approver,
        approval.decided_at,
    )
    # Nothing reached the chain yet: the job releases it.
    assert releases_built(funded) == 0
    assert (await milestone_row(funded, 1)).state == "open"
    [event] = await events(funded)
    assert event.action == MILESTONE_RELEASING
    assert event.actor_user_id == funded.people.approver
    payload = event.payload
    assert payload["rfq_id"] == str(submitted.args.rfq_id)
    assert payload["purchase_order_id"] == str(funded.po_id)
    assert payload["approval_id"] == str(submitted.approval_id)
    assert payload["submission_id"] == str(submitted.submission_id)
    assert payload["requested_by"] == str(funded.people.owner)
    assert (payload["milestone_index"], payload["milestone_key"]) == (0, "tool_start")
    # Amounts as text; ids and codes, never a document or a name.
    assert payload["token_amount"] == submitted.args.token_amount
    assert payload["charge_amount"] == submitted.args.charge_amount
    assert payload["po_amount"] == submitted.args.po_amount
    assert payload["evidence_hash"] == submitted.args.evidence_hash
    assert not {"documents", "filename", "note", "milestone_name"} & set(payload)


async def test_a_replayed_execution_wakes_the_release_again_and_claims_nothing_twice(
    funded: Funded,
) -> None:
    submitted = await submit_directly(funded, 0)
    [job] = await approve_release(funded, submitted.approval_id)
    funded.jobs.jobs.clear()
    tool = act(funded)

    # Its release job may have been lost with the first run: woken again.
    assert await tool.invoke(as_approver(funded, submitted.run_id), submitted.args) == result(
        funded, "releasing"
    )
    assert release_jobs(funded) == [job]
    assert len(await events(funded, MILESTONE_RELEASING)) == 1

    await release_handler(funded)(job)
    funded.jobs.jobs.clear()
    again = await tool.invoke(as_approver(funded, submitted.run_id), submitted.args)

    assert again == result(funded, "already_released")
    assert release_jobs(funded) == []
    assert releases_built(funded) == 1


async def test_duplicate_executions_claim_once(funded: Funded) -> None:
    submitted = await submit_directly(funded, 0)
    release_tools(funded)
    jobs = FakeJobQueue()
    await decide_approval(
        funded.engine, approver(funded.people), submitted.approval_id, "approve", jobs=jobs
    )
    [job] = jobs.jobs
    funded.jobs.jobs.clear()

    await asyncio.gather(funded.world.executor(job), funded.world.executor(job))

    assert release_jobs(funded) == [milestone_release_envelope(funded.tenant, funded.payment_id, 0)]
    assert len(await events(funded, MILESTONE_RELEASING)) == 1
    assert (await milestone_row(funded, 0)).state == "releasing"


async def test_without_a_queue_the_claim_waits_for_the_sweep(funded: Funded) -> None:
    submitted = await submit_directly(funded, 0)
    await approve_only(funded, submitted)
    funded.jobs.jobs.clear()

    claimed = await act(funded, wake=False).invoke(
        as_approver(funded, submitted.run_id), submitted.args
    )

    assert claimed == result(funded, "releasing")
    assert (await milestone_row(funded, 0)).state == "releasing"
    assert release_jobs(funded) == []


async def test_a_release_is_executed_for_the_person_who_approved_it(funded: Funded) -> None:
    submitted = await submit_directly(funded, 0)
    await approve_only(funded, submitted)
    context = ToolContext(tenant_id=funded.tenant, user_id=None, run_id=submitted.run_id)

    with pytest.raises(PaymentInvalidState):
        await act(funded).invoke(context, submitted.args)

    assert (await milestone_row(funded, 0)).state == "submitted"


# --- Refused at the claim: what the approval assumed changed ---------------------------------


async def test_a_milestone_out_of_order_is_refused_at_the_claim(funded: Funded) -> None:
    # T1 sent for approval while Tool start is not released (the service never allows it).
    submitted = await submit_directly(funded, 1)

    jobs = await approve_release(funded, submitted.approval_id)

    assert jobs == []
    await refused(funded, submitted, 1, "out_of_order")
    # Tool start is still the one to send.
    assert (await milestone_row(funded, 0)).state == "open"


async def test_files_changed_after_the_approval_are_not_released(funded: Funded) -> None:
    submitted = await submit_directly(funded, 0)
    swapped = submitted.args.documents[0].document_id
    async with tenant_session(funded.engine, funded.tenant) as session:
        # A file swapped behind the approval's back.
        await session.execute(
            update(PoMilestoneDocument)
            .where(PoMilestoneDocument.id == swapped)
            .values(sha256=file_sha256(submitted.submission_id, "swapped"))
        )

    await approve_release(funded, submitted.approval_id)

    await refused(funded, submitted, 0, "evidence_mismatch")


async def test_an_extra_file_after_the_approval_is_not_released(funded: Funded) -> None:
    submitted = await submit_directly(funded, 0)
    first = submitted.args.documents[0]
    async with tenant_session(funded.engine, funded.tenant) as session:
        original = await session.get(PoMilestoneDocument, first.document_id)
        assert original is not None
        extra = file_sha256(submitted.submission_id, "extra")
        session.add(
            PoMilestoneDocument(
                tenant_id=funded.tenant,
                payment_id=funded.payment_id,
                milestone_id=submitted.milestone_id,
                submission_id=submitted.submission_id,
                doc_type=original.doc_type,
                filename=original.filename,
                content_type=original.content_type,
                size_bytes=original.size_bytes,
                sha256=extra,
                storage_key=original.storage_key.replace(original.sha256, extra),
                source="buyer",
                uploaded_by=funded.people.owner,
            )
        )

    await approve_release(funded, submitted.approval_id)

    await refused(funded, submitted, 0, "evidence_mismatch")


async def test_a_milestone_whose_terms_changed_is_not_released(funded: Funded) -> None:
    submitted = await submit_directly(funded, 0)
    async with tenant_session(funded.engine, funded.tenant) as session:
        await session.execute(
            update(PoPaymentMilestone)
            .where(PoPaymentMilestone.id == submitted.milestone_id)
            .values(token_amount=PoPaymentMilestone.token_amount + 1)
        )

    await approve_release(funded, submitted.approval_id)

    await refused(funded, submitted, 0, "approval_mismatch")


async def test_a_release_requested_for_someone_else_is_not_released(funded: Funded) -> None:
    # Approvals of a payment's milestones are requested for the payment's requester.
    submitted = await submit_directly(funded, 0, requested_by=funded.people.member)

    await approve_release(funded, submitted.approval_id)

    await refused(funded, submitted, 0, "approval_mismatch")


async def test_a_payment_no_longer_funded_is_not_released(funded: Funded) -> None:
    submitted = await submit_directly(funded, 0)
    async with tenant_session(funded.engine, funded.tenant) as session:
        await session.execute(
            update(PoPayment)
            .where(PoPayment.id == funded.payment_id)
            .values(state="failed", failure_code="escrow_mismatch", updated_at=func.now())
        )

    await approve_release(funded, submitted.approval_id)

    await refused(funded, submitted, 0, "payment_not_funded")


async def test_a_refused_claim_answers_the_same_when_replayed(funded: Funded) -> None:
    submitted = await submit_directly(funded, 1)
    await approve_release(funded, submitted.approval_id)

    # The execution's commit was lost after the claim's: it runs again.
    replayed = await act(funded).invoke(as_approver(funded, submitted.run_id), submitted.args)

    assert replayed == result(funded, "failed", 1, "out_of_order")
    assert len(await events(funded, MILESTONE_RELEASE_FAILED)) == 1


# --- Not this milestone's approval ------------------------------------------------------------


async def test_an_execution_that_does_not_hold_the_milestone_touches_nothing(
    funded: Funded,
) -> None:
    first = await submit_directly(funded, 0)
    await decide_approval(
        funded.engine,
        approver(funded.people),
        first.approval_id,
        "reject",
        jobs=FakeJobQueue(),
        on_reject=MILESTONE_REJECT_HOOKS,
    )
    second = await submit_directly(funded, 0)
    assert second.run_id != first.run_id and second.args.attempt == 2
    tool = act(funded)

    # The rejected approval run by hand: the milestone is under review with another one.
    stale = await tool.invoke(as_approver(funded, first.run_id), first.args)
    # A run that is no release approval at all.
    unknown = await tool.invoke(as_approver(funded, uuid4()), first.args)

    assert stale == result(funded, "skipped", code="approval_mismatch")
    assert unknown == result(funded, "failed", code="approval_mismatch")
    row = await milestone_row(funded, 0)
    assert (row.state, row.approval_id, row.submission_id) == (
        "submitted",
        second.approval_id,
        second.submission_id,
    )
    assert (await submission_row(funded, second.submission_id)).state == "submitted"
    assert release_jobs(funded) == []


async def test_an_approval_not_recorded_on_its_milestone_yet_is_retried(funded: Funded) -> None:
    submitted = await submit_directly(funded, 0)
    await approve_only(funded, submitted)
    # Approved and executing between the service's proposal and its recording of the
    # submission (the service records it right after the proposal).
    async with tenant_session(funded.engine, funded.tenant) as session:
        await session.execute(
            update(PoPaymentMilestone)
            .where(PoPaymentMilestone.id == submitted.milestone_id)
            .values(state="open", approval_id=None, submission_id=None, evidence_hash=None)
        )
        await session.execute(
            update(PoMilestoneSubmission)
            .where(PoMilestoneSubmission.id == submitted.submission_id)
            .values(state="draft", approval_id=None, evidence_hash=None, submitted_at=None)
        )
    tool = act(funded)

    with pytest.raises(PaymentInvalidState, match="not recorded"):
        await tool.invoke(as_approver(funded, submitted.run_id), submitted.args)

    async with tenant_session(funded.engine, funded.tenant) as session:
        await session.execute(
            update(PoMilestoneSubmission)
            .where(PoMilestoneSubmission.id == submitted.submission_id)
            .values(
                state="submitted",
                approval_id=submitted.approval_id,
                evidence_hash=submitted.args.evidence_hash,
                submitted_at=func.now(),
            )
        )
        await session.execute(
            update(PoPaymentMilestone)
            .where(PoPaymentMilestone.id == submitted.milestone_id)
            .values(
                state="submitted",
                approval_id=submitted.approval_id,
                submission_id=submitted.submission_id,
                evidence_hash=submitted.args.evidence_hash,
            )
        )
    claimed = await tool.invoke(as_approver(funded, submitted.run_id), submitted.args)
    assert claimed == result(funded, "releasing")


# --- Rejected: back to the factory ------------------------------------------------------------


async def test_a_rejected_release_goes_back_to_the_factory(funded: Funded) -> None:
    submitted = await submit_directly(funded, 0)
    jobs = FakeJobQueue()

    await decide_approval(
        funded.engine,
        approver(funded.people),
        submitted.approval_id,
        "reject",
        jobs=jobs,
        on_reject=MILESTONE_REJECT_HOOKS,
    )

    assert jobs.jobs == []  # nothing is executed
    row = await milestone_row(funded, 0)
    assert (row.state, row.approval_id, row.submission_id, row.evidence_hash) == (
        "open",
        None,
        None,
        None,
    )
    submission = await submission_row(funded, submitted.submission_id)
    assert (submission.state, submission.decided_by) == ("rejected", funded.people.approver)
    [event] = await events(funded)
    assert event.action == MILESTONE_CHANGES_REQUESTED
    assert event.payload["approval_id"] == str(submitted.approval_id)
    assert releases_built(funded) == 0


async def test_the_approvals_route_sends_a_rejected_release_back(funded: Funded) -> None:
    submitted = await submit_directly(funded, 0)
    app = FastAPI()
    app.include_router(approvals_router())
    sessionmaker = create_sessionmaker(funded.engine)
    jobs = FakeJobQueue()

    def principal() -> Principal:
        return approver(funded.people)

    def db() -> async_sessionmaker[AsyncSession]:
        return sessionmaker

    def queue() -> JobQueue:
        return jobs

    app.dependency_overrides[get_principal] = principal
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_job_queue] = queue
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post(
            f"/v1/approvals/{submitted.approval_id}/decision", json={"decision": "reject"}
        )

    assert response.status_code == 200, response.text
    assert response.json() == {"approval_id": str(submitted.approval_id), "status": "rejected"}
    assert (await milestone_row(funded, 0)).state == "open"
    assert (await submission_row(funded, submitted.submission_id)).state == "rejected"


# --- A person retries a failed release -------------------------------------------------------


def member(funded: Funded) -> Principal:
    return Principal(tenant_id=funded.tenant, user_id=funded.people.member, role="member")


async def failed_release(funded: Funded) -> Submitted:
    """Milestone 0 approved, then its transaction refused by the chain: `failed`."""
    submitted = await submit_directly(funded, 0)
    [job] = await approve_release(funded, submitted.approval_id)
    funded.chain.send_error["release"] = ProviderError(
        "solana", "sendTransaction", "InstructionError", retryable=False
    )
    await release_handler(funded)(job)
    row = await milestone_row(funded, 0)
    assert (row.state, row.failure_code) == ("failed", "chain_release_failed")
    funded.jobs.jobs.clear()
    return submitted


async def test_a_person_retries_a_failed_release_once_its_cause_is_fixed(funded: Funded) -> None:
    submitted = await failed_release(funded)

    # Any member may: the approval already decided the money.
    await retry_release(funded.engine, member(funded), funded.po_id, 0, jobs=funded.jobs)

    row = await milestone_row(funded, 0)
    assert (row.state, row.failure_code, row.approval_id) == (
        "releasing",
        None,
        submitted.approval_id,
    )
    [job] = release_jobs(funded)
    assert job == milestone_release_envelope(funded.tenant, funded.payment_id, 0)
    [event] = await events(funded, MILESTONE_RELEASE_RETRIED)
    assert event.actor_user_id == funded.people.member
    assert event.payload["previous_failure_code"] == "chain_release_failed"
    assert event.payload["approval_id"] == str(submitted.approval_id)

    await release_handler(funded)(job)

    row = await milestone_row(funded, 0)
    assert row.state == "released" and row.release_signature is not None
    assert await release_rows(funded) == [(0, 1, "failed"), (0, 2, "confirmed")]
    assert funded.chain.balances[funded.chain.payout] == int(row.token_amount)


async def test_only_a_failed_release_is_retried(funded: Funded) -> None:
    async def retry() -> None:
        await retry_release(funded.engine, member(funded), funded.po_id, 0, jobs=funded.jobs)

    with pytest.raises(PaymentInvalidState, match="open"):
        await retry()
    submitted = await submit_directly(funded, 0)
    with pytest.raises(PaymentInvalidState, match="submitted"):
        await retry()
    [job] = await approve_release(funded, submitted.approval_id)
    with pytest.raises(PaymentInvalidState, match="releasing"):
        await retry()
    await release_handler(funded)(job)
    with pytest.raises(PaymentInvalidState, match="released"):
        await retry()
    assert await events(funded, MILESTONE_RELEASE_RETRIED) == []


async def test_a_release_its_approval_no_longer_vouches_for_is_not_retried(
    funded: Funded,
) -> None:
    submitted = await failed_release(funded)
    async with tenant_session(funded.engine, funded.tenant) as session:
        await session.execute(
            update(PoMilestoneDocument)
            .where(PoMilestoneDocument.id == submitted.args.documents[0].document_id)
            .values(sha256=file_sha256(submitted.submission_id, "swapped"))
        )

    with pytest.raises(PaymentInvalidState, match="evidence_mismatch"):
        await retry_release(funded.engine, member(funded), funded.po_id, 0, jobs=funded.jobs)

    assert (await milestone_row(funded, 0)).state == "failed"
    assert release_jobs(funded) == []


async def test_a_retry_needs_a_member_and_the_orders_payment(funded: Funded) -> None:
    await failed_release(funded)
    stranger = Principal(tenant_id=funded.tenant, user_id=uuid4(), role="owner")
    other = Principal(
        tenant_id=funded.people.other_tenant, user_id=funded.people.other_approver, role="approver"
    )

    with pytest.raises(PaymentForbidden):
        await retry_release(funded.engine, stranger, funded.po_id, 0, jobs=funded.jobs)
    with pytest.raises(PaymentNotFound):  # another company's order is not found
        await retry_release(funded.engine, other, funded.po_id, 0, jobs=funded.jobs)
    with pytest.raises(PaymentNotFound):
        await retry_release(funded.engine, member(funded), funded.po_id, 3, jobs=funded.jobs)

    assert (await milestone_row(funded, 0)).state == "failed"
    assert release_jobs(funded) == []
