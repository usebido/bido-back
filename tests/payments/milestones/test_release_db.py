"""The `milestone.release` job against Postgres and the fake chain: each milestone once.

`FakePaymentChain` lands a transaction when it is sent unless told to drop or fail it; a
gate's attestation exists once, and a milestone is released once and only against it.
The handler never polls here (`poll_budget=0`): the status is read once, after the send.
A milestone is claimed through the real guard and executor first (`support`).
"""

import asyncio
import dataclasses
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from bido_back.db import tenant_session
from bido_back.db.models import AuditEvent, PoMilestoneDocument
from bido_back.payments.errors import (
    PaymentNotFound,
    PaymentRefused,
    PaymentsNotConfigured,
    ProviderError,
)
from bido_back.payments.milestones.catalog import TOOLING_PLAN, milestone_amounts
from bido_back.payments.milestones.proposals import (
    MILESTONE_RELEASE,
    MILESTONE_RELEASE_FAILED,
    MILESTONE_RELEASED,
    MILESTONE_RELEASING,
    PAYMENT_COMPLETED,
)
from bido_back.payments.milestones.release import (
    MAX_DECISIONS,
    InvalidMilestoneJob,
    MilestoneReleaseHandler,
    milestone_release_envelope,
)
from bido_back.payments.pipeline import EXPIRY_MARGIN, StepPending
from bido_back.payments.ports import GateAttestation, ReleaseSpec, SignedTransaction
from bido_back.worker.envelope import Envelope
from tests.payments.fakes import FakePaymentChain, address
from tests.payments.milestones.conftest import Funded
from tests.payments.milestones.support import (
    Submitted,
    approve_release,
    file_sha256,
    milestone_row,
    release_all,
    release_handler,
    release_rows,
    release_transactions,
    submission_row,
    submit_directly,
)

pytestmark = pytest.mark.db

OTHER_EVIDENCE = "ab" * 32


@dataclass
class Releasing:
    """Milestone 0 approved and claimed (`releasing`): its release job is due."""

    funded: Funded
    submitted: Submitted
    job: Envelope
    handler: MilestoneReleaseHandler

    @property
    def chain(self) -> FakePaymentChain:
        return self.funded.chain

    @property
    def evidence(self) -> str:
        return self.submitted.args.evidence_hash

    async def escrow_address(self) -> str:
        escrow = (await self.funded.payment()).escrow_address
        assert escrow is not None
        return escrow

    def on(self, chain: FakePaymentChain) -> MilestoneReleaseHandler:
        """The same job on another chain (a subclass that misbehaves), set up like this one."""
        chain.escrows = self.chain.escrows
        chain.balances = self.chain.balances
        chain.attestations = self.chain.attestations
        chain.attester, chain.payout = self.chain.attester, self.chain.payout
        providers = dataclasses.replace(self.funded.providers.bundle(), chain=chain)
        return MilestoneReleaseHandler(
            self.funded.engine, providers, None, poll_budget=0, attester=chain.attester
        )

    async def release(self, handler: MilestoneReleaseHandler | None = None) -> str:
        return await (handler or self.handler).release(
            self.funded.tenant, self.funded.payment_id, 0
        )

    def built(self) -> list[ReleaseSpec]:
        """The release transactions sent, oldest first (what each one did)."""
        specs: list[ReleaseSpec] = []
        for step, signature in self.chain.built:
            sent = self.chain.sent.get(signature)
            if step == "release" and sent is not None and sent.release is not None:
                specs.append(sent.release)
        return specs

    def releases_built(self, chain: FakePaymentChain | None = None) -> int:
        return sum(1 for step, _ in (chain or self.chain).built if step == "release")

    async def state(self) -> tuple[str, str | None]:
        row = await milestone_row(self.funded, 0)
        return row.state, row.failure_code


@pytest_asyncio.fixture
async def releasing(funded: Funded) -> Releasing:
    submitted = await submit_directly(funded, 0)
    [job] = await approve_release(funded, submitted.approval_id)
    return Releasing(funded, submitted, job, release_handler(funded))


async def actions(funded: Funded) -> list[str]:
    """The release events of the payment, oldest first."""
    async with tenant_session(funded.engine, funded.tenant) as session:
        rows = await session.scalars(
            select(AuditEvent)
            .where(
                AuditEvent.payload["payment_id"].astext == str(funded.payment_id),
                AuditEvent.action.in_(
                    [
                        MILESTONE_RELEASING,
                        MILESTONE_RELEASED,
                        MILESTONE_RELEASE_FAILED,
                        PAYMENT_COMPLETED,
                    ]
                ),
            )
            .order_by(AuditEvent.created_at, AuditEvent.id)
        )
        return [event.action for event in rows]


async def event(funded: Funded, action: str) -> AuditEvent:
    async with tenant_session(funded.engine, funded.tenant) as session:
        [found] = list(
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.payload["payment_id"].astext == str(funded.payment_id),
                    AuditEvent.action == action,
                )
            )
        )
    return found


def ours(escrow: str, chain: FakePaymentChain, evidence: str, **changes: object) -> GateAttestation:
    """The gate 0 attestation Bido writes, with `changes`."""
    state = chain.escrows[escrow]
    attestation = GateAttestation(
        address=address(f"attestation:{escrow}:0"),
        signer=chain.attester,
        po_hash=state.po_hash,
        gate=0,
        evidence_hash=evidence,
    )
    return dataclasses.replace(attestation, **changes)


# --- Released --------------------------------------------------------------------------------


async def test_an_approved_milestone_is_released_once(releasing: Releasing) -> None:
    funded = releasing.funded
    assert releasing.job.type == MILESTONE_RELEASE

    await releasing.handler(releasing.job)

    row = await milestone_row(funded, 0)
    [transaction] = await release_transactions(funded)
    assert (transaction.milestone_index, transaction.attempt, transaction.state) == (
        0,
        1,
        "confirmed",
    )
    assert (row.state, row.release_signature, row.failure_code) == (
        "released",
        transaction.signature,
        None,
    )
    assert row.released_at is not None
    submission = await submission_row(funded, releasing.submitted.submission_id)
    assert submission.state == "released"
    # One transaction: the gate's attestation (the approved files' evidence), then the release.
    escrow = await releasing.escrow_address()
    [spec] = releasing.built()
    assert spec == ReleaseSpec(
        escrow=escrow, milestone=0, evidence_hash=releasing.evidence, attest=True
    )
    # The milestone's own amount went to the escrow's fixed payout, once.
    payment = await funded.payment()
    total = int(payment.token_amount)
    amounts = milestone_amounts(total, [m.bps for m in TOOLING_PLAN])
    assert int(row.token_amount) == amounts[0]
    chain = releasing.chain
    assert chain.balances[chain.payout] == amounts[0]
    assert payment.vault_address is not None
    assert chain.balances[payment.vault_address] == total - amounts[0]
    held = chain.escrows[escrow].milestones[0]
    assert (held.status, held.evidence_hash, held.approved_by) == (
        "released",
        releasing.evidence,
        chain.attester,
    )
    assert chain.escrows[escrow].status == "funded"
    assert (payment.state, payment.completed_at) == ("funded", None)  # two to go
    released = await event(funded, MILESTONE_RELEASED)
    assert released.actor_user_id is None
    assert released.payload["signature"] == transaction.signature
    assert released.payload["token_amount"] == str(amounts[0])
    assert released.payload["evidence_hash"] == releasing.evidence
    assert released.payload["approval_id"] == str(releasing.submitted.approval_id)
    assert released.payload["rfq_id"] == str(releasing.submitted.args.rfq_id)
    assert (released.payload["milestone_index"], released.payload["escrow_address"]) == (0, escrow)
    assert await actions(funded) == [MILESTONE_RELEASING, MILESTONE_RELEASED]
    # Run again (a redelivered job): nothing more happens.
    assert await releasing.release() == "released"
    await releasing.handler(releasing.job)
    assert releasing.releases_built() == 1
    assert chain.balances[chain.payout] == amounts[0]


async def test_three_milestones_are_released_in_order_and_complete_the_payment(
    funded: Funded,
) -> None:
    signatures = await release_all(funded)

    assert len(set(signatures)) == 3
    assert await release_rows(funded) == [
        (0, 1, "confirmed"),
        (1, 1, "confirmed"),
        (2, 1, "confirmed"),
    ]
    payment = await funded.payment()
    assert payment.state == "funded" and payment.completed_at is not None
    total = int(payment.token_amount)
    chain = funded.chain
    assert payment.escrow_address is not None and payment.vault_address is not None
    escrow = chain.escrows[payment.escrow_address]
    assert (escrow.status, escrow.released) == ("completed", total)
    assert chain.balances[chain.payout] == total
    assert chain.balances[payment.vault_address] == 0
    # Each milestone through its own gate, each against its own files.
    specs = [chain.sent[s].release for s in signatures]
    assert [(s.milestone, s.attest) for s in specs if s is not None] == [
        (0, True),
        (1, True),
        (2, True),
    ]
    assert [(m.index, m.gate, m.status) for m in escrow.milestones] == [
        (0, 0, "released"),
        (1, 1, "released"),
        (2, 2, "released"),
    ]
    recorded = await actions(funded)
    # The last release and the completion are one transaction (one timestamp).
    assert [a for a in recorded if a != PAYMENT_COMPLETED] == [
        MILESTONE_RELEASING,
        MILESTONE_RELEASED,
    ] * 3
    assert recorded.count(PAYMENT_COMPLETED) == 1
    completed = await event(funded, PAYMENT_COMPLETED)
    assert completed.payload["token_amount"] == str(total)
    assert completed.payload["milestones"] == 3
    assert completed.payload["approval_id"] == str(payment.approval_id)


async def test_concurrent_jobs_of_one_milestone_release_once(releasing: Releasing) -> None:
    results = await asyncio.gather(releasing.release(), releasing.release(), return_exceptions=True)

    assert "released" in results
    assert all(r == "released" or isinstance(r, StepPending) for r in results)
    assert releasing.releases_built() == 1
    chain = releasing.chain
    assert chain.balances[chain.payout] == int(
        (await milestone_row(releasing.funded, 0)).token_amount
    )


async def test_only_a_releasing_milestone_moves(releasing: Releasing) -> None:
    funded = releasing.funded
    handler = releasing.handler

    # T1 is not approved: its job (sent by mistake) does nothing.
    assert await handler.release(funded.tenant, funded.payment_id, 1) == "open"
    with pytest.raises(PaymentNotFound):
        await handler.release(funded.tenant, uuid4(), 0)
    assert releasing.releases_built() == 0


async def test_bad_jobs_are_refused(releasing: Releasing) -> None:
    funded = releasing.funded
    payment_id = funded.payment_id

    def job(key: str, payload: dict[str, object]) -> Envelope:
        return Envelope.model_validate(
            {
                "type": MILESTONE_RELEASE,
                "tenant_id": funded.tenant,
                "idempotency_key": key,
                "payload": payload,
            }
        )

    key = f"milestone:{payment_id}:0:release"
    for bad in (
        job(key, {"payment_id": str(payment_id)}),
        job(key, {"payment_id": "not-a-uuid", "milestone_index": 0}),
        job(key, {"payment_id": str(payment_id), "milestone_index": "0"}),
        job(key, {"payment_id": str(payment_id), "milestone_index": True}),
        job(key, {"payment_id": str(payment_id), "milestone_index": 4}),
        job(key, {"payment_id": str(payment_id), "milestone_index": 0, "extra": 1}),
        job(
            f"milestone:{payment_id}:1:release",
            {"payment_id": str(payment_id), "milestone_index": 0},
        ),
        job(
            f"milestone:{uuid4()}:0:release", {"payment_id": str(payment_id), "milestone_index": 0}
        ),
    ):
        with pytest.raises(InvalidMilestoneJob):
            await releasing.handler(bad)
    assert releasing.releases_built() == 0
    assert releasing.job == milestone_release_envelope(funded.tenant, payment_id, 0)


# --- A transaction that may still land ------------------------------------------------------


async def test_a_dropped_release_is_waited_for_and_never_sent_twice(releasing: Releasing) -> None:
    releasing.chain.next_landing["release"] = "drop"

    with pytest.raises(StepPending):
        await releasing.release()
    with pytest.raises(StepPending):  # its blockhash is still valid: nothing is rebuilt
        await releasing.release()
    assert await release_rows(releasing.funded) == [(0, 1, "sent")]
    assert await releasing.state() == ("releasing", None)

    # It was only slow.
    [sent] = await release_transactions(releasing.funded)
    releasing.chain.land_dropped(sent.signature)
    assert await releasing.release() == "released"

    assert await release_rows(releasing.funded) == [(0, 1, "confirmed")]
    assert releasing.releases_built() == 1
    assert (await milestone_row(releasing.funded, 0)).release_signature == sent.signature


async def test_a_dropped_release_is_rebuilt_only_once_it_can_no_longer_land(
    releasing: Releasing,
) -> None:
    chain = releasing.chain
    chain.next_landing["release"] = "drop"
    with pytest.raises(StepPending):
        await releasing.release()
    [first] = await release_transactions(releasing.funded)

    chain.height = first.last_valid_block_height + EXPIRY_MARGIN  # within the margin
    with pytest.raises(StepPending):
        await releasing.release()
    assert releasing.releases_built() == 1

    chain.height = first.last_valid_block_height + EXPIRY_MARGIN + 1
    assert await releasing.release() == "released"

    assert await release_rows(releasing.funded) == [(0, 1, "expired"), (0, 2, "confirmed")]
    # The first never landed: the milestone was paid once, by the second.
    row = await milestone_row(releasing.funded, 0)
    assert chain.balances[chain.payout] == int(row.token_amount)
    [_, second] = await release_transactions(releasing.funded)
    assert row.release_signature == second.signature


@dataclass
class LandsInTheLastBlock(FakePaymentChain):
    """A dropped transaction that landed in its last valid block: the first status read
    misses it, a read after the block height shows it (`armed`)."""

    armed: bool = False

    async def block_height(self) -> int:
        if self.armed:
            for signature, record in list(self.sent.items()):
                if record.landing == "drop" and signature not in self.landed:
                    self.land_dropped(signature)
        return self.height


async def test_a_release_that_landed_in_its_last_valid_block_is_not_sent_again(
    releasing: Releasing,
) -> None:
    late = LandsInTheLastBlock()
    handler = releasing.on(late)
    late.next_landing["release"] = "drop"
    with pytest.raises(StepPending):
        await releasing.release(handler)
    [sent] = await release_transactions(releasing.funded)

    late.height = sent.last_valid_block_height + EXPIRY_MARGIN + 1
    late.armed = True
    assert await releasing.release(handler) == "released"

    assert await release_rows(releasing.funded) == [(0, 1, "confirmed")]
    assert releasing.releases_built(late) == 1


async def test_a_send_that_may_have_been_forwarded_is_asked_about_before_a_rebuild(
    releasing: Releasing,
) -> None:
    chain = releasing.chain
    chain.send_error["release"] = ProviderError(
        "solana", "sendTransaction", "timeout", retryable=True
    )
    with pytest.raises(ProviderError):
        await releasing.release()
    assert await release_rows(releasing.funded) == [(0, 1, "sent")]
    with pytest.raises(StepPending):
        await releasing.release()

    [first] = await release_transactions(releasing.funded)
    chain.height = first.last_valid_block_height + EXPIRY_MARGIN + 1
    chain.node_behind = 1  # a lagging node never expires a transaction
    with pytest.raises(ProviderError, match="node_behind"):
        await releasing.release()
    assert await release_rows(releasing.funded) == [(0, 1, "sent")]

    assert await releasing.release() == "released"
    assert await release_rows(releasing.funded) == [(0, 1, "expired"), (0, 2, "confirmed")]


async def test_a_confirmation_lost_with_its_job_is_found_on_the_next_run(
    releasing: Releasing,
) -> None:
    releasing.chain.node_behind = 1  # the status read after the send fails: the job dies

    with pytest.raises(ProviderError, match="node_behind"):
        await releasing.release()
    [sent] = await release_transactions(releasing.funded)
    assert (sent.state, await releasing.state()) == ("sent", ("releasing", None))

    assert await releasing.release() == "released"

    assert await release_rows(releasing.funded) == [(0, 1, "confirmed")]
    assert (await milestone_row(releasing.funded, 0)).release_signature == sent.signature
    assert releasing.releases_built() == 1


async def test_a_failure_found_by_a_later_run_is_decided_from_the_chain(
    releasing: Releasing,
) -> None:
    chain = releasing.chain
    chain.next_landing["release"] = "fail"
    chain.node_behind = 1  # the job dies before it reads the failure
    with pytest.raises(ProviderError, match="node_behind"):
        await releasing.release()

    # Nothing landed: the chain still needs the attestation and the release.
    assert await releasing.release() == "released"

    assert await release_rows(releasing.funded) == [(0, 1, "failed"), (0, 2, "confirmed")]
    assert [spec.attest for spec in releasing.built()] == [True, True]


# --- The chain already shows it ---------------------------------------------------------------


async def test_an_attestation_without_its_release_is_cranked(releasing: Releasing) -> None:
    escrow = await releasing.escrow_address()
    releasing.chain.write_attestation(escrow, 0, releasing.evidence)

    assert await releasing.release() == "released"

    [spec] = releasing.built()
    assert spec == ReleaseSpec(
        escrow=escrow, milestone=0, evidence_hash=releasing.evidence, attest=False
    )
    assert await release_rows(releasing.funded) == [(0, 1, "confirmed")]


@pytest.mark.parametrize(
    "changes",
    [
        {"evidence_hash": OTHER_EVIDENCE},
        {"signer": address("squatter")},
        {"po_hash": "cd" * 32},
    ],
    ids=["other_files", "other_signer", "other_order"],
)
async def test_an_attestation_that_is_not_ours_fails_the_release(
    releasing: Releasing, changes: dict[str, object]
) -> None:
    escrow = await releasing.escrow_address()
    releasing.chain.attestations[(escrow, 0)] = ours(
        escrow, releasing.chain, releasing.evidence, **changes
    )

    assert await releasing.release() == "failed"

    assert await releasing.state() == ("failed", "attestation_mismatch")
    assert releasing.releases_built() == 0
    assert await release_rows(releasing.funded) == []
    failed = await event(releasing.funded, MILESTONE_RELEASE_FAILED)
    assert failed.payload["failure_code"] == "attestation_mismatch"
    # The approved submission stays: the money is still in the escrow.
    submission = await submission_row(releasing.funded, releasing.submitted.submission_id)
    assert submission.state == "approved"


async def release_elsewhere(releasing: Releasing, evidence: str) -> None:
    """Milestone 0 released on chain by a transaction Bido has no record of."""
    chain = releasing.chain
    escrow = await releasing.escrow_address()
    transaction = await chain.build_release(
        ReleaseSpec(escrow=escrow, milestone=0, evidence_hash=evidence, attest=True)
    )
    await chain.send(transaction)
    assert chain.escrows[escrow].milestones[0].status == "released"


async def test_a_milestone_already_released_on_chain_is_recorded_without_sending(
    releasing: Releasing,
) -> None:
    await release_elsewhere(releasing, releasing.evidence)

    assert await releasing.release() == "released"

    row = await milestone_row(releasing.funded, 0)
    assert (row.state, row.release_signature) == ("released", None)  # not ours to show
    assert await release_rows(releasing.funded) == []
    assert releasing.releases_built() == 1  # only the one made elsewhere
    released = await event(releasing.funded, MILESTONE_RELEASED)
    assert released.payload["signature"] is None


async def test_a_milestone_released_on_chain_against_other_files_fails(
    releasing: Releasing,
) -> None:
    await release_elsewhere(releasing, OTHER_EVIDENCE)

    assert await releasing.release() == "failed"

    assert await releasing.state() == ("failed", "evidence_mismatch")
    assert releasing.releases_built() == 1


async def test_a_refunded_escrow_fails_the_release(releasing: Releasing) -> None:
    escrow = await releasing.escrow_address()
    chain = releasing.chain
    chain.escrows[escrow] = dataclasses.replace(chain.escrows[escrow], status="refunded")

    assert await releasing.release() == "failed"

    assert await releasing.state() == ("failed", "escrow_refunded")
    assert releasing.releases_built() == 0


async def test_an_escrow_not_visible_yet_is_waited_for(releasing: Releasing) -> None:
    escrow = await releasing.escrow_address()
    chain = releasing.chain
    held = chain.escrows.pop(escrow)  # a node that lags behind the funding

    with pytest.raises(StepPending):
        await releasing.release()
    assert await releasing.state() == ("releasing", None)

    chain.escrows[escrow] = held
    assert await releasing.release() == "released"


async def test_an_escrow_with_other_terms_fails_the_release(releasing: Releasing) -> None:
    escrow = await releasing.escrow_address()
    chain = releasing.chain
    chain.escrows[escrow] = dataclasses.replace(chain.escrows[escrow], po_hash="ef" * 32)

    assert await releasing.release() == "failed"

    assert await releasing.state() == ("failed", "escrow_mismatch")
    assert releasing.releases_built() == 0


# --- What the person approved still holds ----------------------------------------------------


async def test_files_changed_after_the_claim_are_never_sent(releasing: Releasing) -> None:
    funded = releasing.funded
    async with tenant_session(funded.engine, funded.tenant) as session:
        await session.execute(
            update(PoMilestoneDocument)
            .where(PoMilestoneDocument.id == releasing.submitted.args.documents[0].document_id)
            .values(sha256=file_sha256(releasing.submitted.submission_id, "swapped"))
        )

    assert await releasing.release() == "failed"

    assert await releasing.state() == ("failed", "evidence_mismatch")
    assert releasing.releases_built() == 0


# --- Waiting and failures ---------------------------------------------------------------------


async def test_without_the_attester_key_the_release_waits(releasing: Releasing) -> None:
    releasing.chain.can_attest = False

    with pytest.raises(PaymentsNotConfigured):
        await releasing.release()
    with pytest.raises(PaymentsNotConfigured):
        await releasing.handler(releasing.job)  # retried: the job fails
    assert await releasing.state() == ("releasing", None)
    assert await release_rows(releasing.funded) == []

    releasing.chain.can_attest = True  # the key is set
    assert await releasing.release() == "released"
    assert await release_rows(releasing.funded) == [(0, 1, "confirmed")]


async def test_a_refused_preflight_fails_the_release(releasing: Releasing) -> None:
    releasing.chain.send_error["release"] = ProviderError(
        "solana", "sendTransaction", "InstructionError", retryable=False
    )

    assert await releasing.release() == "failed"

    assert await releasing.state() == ("failed", "chain_release_failed")
    [transaction] = await release_transactions(releasing.funded)
    assert (transaction.state, transaction.error) == ("failed", "InstructionError")
    escrow = await releasing.escrow_address()
    assert releasing.chain.escrows[escrow].milestones[0].status == "pending"
    assert await actions(releasing.funded) == [MILESTONE_RELEASING, MILESTONE_RELEASE_FAILED]
    assert await releasing.release() == "failed"  # nothing more happens
    assert releasing.releases_built() == 1


async def test_a_failed_landing_fails_the_release(releasing: Releasing) -> None:
    releasing.chain.next_landing["release"] = "fail"

    assert await releasing.release() == "failed"

    assert await releasing.state() == ("failed", "chain_release_failed")
    assert await release_rows(releasing.funded) == [(0, 1, "failed")]
    assert releasing.chain.balances.get(releasing.chain.payout, 0) == 0


async def test_a_failed_crank_with_the_chain_unchanged_fails_the_release(
    releasing: Releasing,
) -> None:
    escrow = await releasing.escrow_address()
    releasing.chain.write_attestation(escrow, 0, releasing.evidence)
    releasing.chain.next_landing["release"] = "fail"

    assert await releasing.release() == "failed"

    assert await releasing.state() == ("failed", "chain_release_failed")
    assert [s.attest for s in releasing.built()] == [False]  # never loops on the same refusal


@dataclass
class Recording(FakePaymentChain):
    """Keeps every release it builds, sent or refused at preflight."""

    releases: list[ReleaseSpec] = field(default_factory=list[ReleaseSpec])

    async def build_release(self, spec: ReleaseSpec) -> SignedTransaction:
        built = await super().build_release(spec)
        self.releases.append(spec)
        return built


@dataclass
class AttestedMeanwhile(Recording):
    """The gate's attestation is written (ours) just before our attest+release is sent:
    preflight refuses it like the program's `CreateAttestation` on an existing account."""

    def _attest_first(self, spec: ReleaseSpec) -> None:
        self.write_attestation(spec.escrow, spec.milestone, spec.evidence_hash)

    async def send(self, transaction: SignedTransaction) -> None:
        record = self._pending.get(transaction.signature)
        spec = record.release if record is not None else None
        if spec is not None and spec.attest and transaction.signature not in self.sent:
            self._attest_first(spec)
        await super().send(transaction)


async def test_an_attestation_written_meanwhile_turns_the_next_attempt_into_a_crank(
    releasing: Releasing,
) -> None:
    chain = AttestedMeanwhile()
    handler = releasing.on(chain)

    assert await releasing.release(handler) == "released"

    assert [spec.attest for spec in chain.releases] == [True, False]
    rows = await release_transactions(releasing.funded)
    # Decided from the chain, not from the (ambiguous) code at the attestation's index.
    assert [(t.attempt, t.state, t.error) for t in rows] == [
        (1, "failed", "InstructionError(2, Custom(0))"),
        (2, "confirmed", None),
    ]
    assert (await milestone_row(releasing.funded, 0)).release_signature == rows[1].signature


@dataclass
class ClosedMeanwhile(Recording):
    """The gate's attestation is closed just before our crank is sent (`AttestationMissing`)."""

    async def send(self, transaction: SignedTransaction) -> None:
        record = self._pending.get(transaction.signature)
        spec = record.release if record is not None else None
        if spec is not None and not spec.attest:
            self.attestations.pop((spec.escrow, spec.milestone), None)
        await super().send(transaction)


async def test_a_crank_whose_attestation_was_closed_attests_again(releasing: Releasing) -> None:
    escrow = await releasing.escrow_address()
    releasing.chain.write_attestation(escrow, 0, releasing.evidence)
    chain = ClosedMeanwhile()
    handler = releasing.on(chain)

    assert await releasing.release(handler) == "released"

    assert [spec.attest for spec in chain.releases] == [False, True]
    rows = await release_transactions(releasing.funded)
    assert [(t.attempt, t.state, t.error) for t in rows] == [
        (1, "failed", "AttestationMissing"),
        (2, "confirmed", None),
    ]


@dataclass
class Flipping(AttestedMeanwhile):
    """Every attempt finds the chain changed: an attestation appears before an
    attest+release lands and vanishes before a crank lands."""

    async def send(self, transaction: SignedTransaction) -> None:
        record = self._pending.get(transaction.signature)
        spec = record.release if record is not None else None
        if spec is not None and not spec.attest:
            self.attestations.pop((spec.escrow, spec.milestone), None)
        await super().send(transaction)


async def test_a_chain_that_changes_under_every_attempt_is_left_to_a_person(
    releasing: Releasing,
) -> None:
    chain = Flipping()
    handler = releasing.on(chain)

    assert await releasing.release(handler) == "failed"

    assert await releasing.state() == ("failed", "chain_release_failed")
    assert [spec.attest for spec in chain.releases] == [True, False, True][:MAX_DECISIONS]
    assert [state for _, _, state in await release_rows(releasing.funded)] == [
        "failed"
    ] * MAX_DECISIONS


@dataclass
class RefusesTheRelease(FakePaymentChain):
    """`build_release` refuses what it reads (the chain code's own checks); optionally
    because the milestone was just released by another transaction."""

    released_meanwhile: bool = False

    async def build_release(self, spec: ReleaseSpec) -> SignedTransaction:
        if self.released_meanwhile:
            self.released_meanwhile = False
            await self.send(await super().build_release(spec))
        raise PaymentRefused("the milestone is not pending")


async def test_a_release_the_chain_code_refuses_fails(releasing: Releasing) -> None:
    handler = releasing.on(RefusesTheRelease())

    assert await releasing.release(handler) == "failed"

    assert await releasing.state() == ("failed", "chain_release_failed")
    assert await release_rows(releasing.funded) == []


async def test_a_release_refused_because_it_was_released_meanwhile_is_recorded(
    releasing: Releasing,
) -> None:
    chain = RefusesTheRelease(released_meanwhile=True)
    handler = releasing.on(chain)

    assert await releasing.release(handler) == "released"

    row = await milestone_row(releasing.funded, 0)
    assert (row.state, row.release_signature) == ("released", None)
    assert await release_rows(releasing.funded) == []
