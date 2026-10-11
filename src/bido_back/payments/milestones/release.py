"""The `milestone.release` job: release one approved milestone of a funded escrow, once.

`milestone.release` (payload `{"payment_id", "milestone_index"}`, key
`release_job_key`) is enqueued by the `release_po_milestone` act once it claimed the
milestone (`releasing`), and by the scheduled sweep (`milestones.reconcile`) when that
job was lost. It sends ONE transaction, `[CU, CU, CreateAttestation(gate i), release(i)]`
(signed by the relayer and the gate attester; gate = milestone index), or the crank
`[CU, CU, release(i)]` when the gate's attestation already exists and is ours, then
records the milestone `released`; the last release completes the payment (its
`completed_at`; the payment stays `funded`).

Exactly once. The milestone row is the outbox: the payment's row lock, then the
milestone's, serialize every job of a milestone while it decides and records, and only a
`releasing` milestone moves.

1. A release transaction of this milestone still `sent` is resolved first (the rule of
   `payments.pipeline`): `confirmed` releases the milestone; `failed` is noted; `unknown`
   raises `StepPending` while its blockhash may still land (the finalized block height
   not past its last valid block height + `EXPIRY_MARGIN`) and is marked `expired` only
   once it cannot (after a second status read). Never a new attempt while an earlier
   one can land.
2. The chain says where the milestone stands, before anything is built: the escrow
   (ours: buyer, PO id, total, mint, PO hash) released this milestone with our evidence
   hash: done, with no signature of ours (`signature` None); with another evidence:
   `failed` (`evidence_mismatch`); refunded, not funded, another gate: `failed`. Then the
   gate's attestation: none: attest and release; ours (our attester, the escrow's PO
   hash, the gate, our evidence): the crank; anything else: `failed`
   (`attestation_mismatch`).
3. What the person approved still holds, re-checked before every attempt (`vouched`):
   the approval (`approved`/`executed`, its payload, the requester), the rows' terms and
   the files (`approval_mismatch`, `evidence_mismatch`), a funded payment and every
   earlier milestone released. Checked after 1 and 2 on purpose: what already happened
   on chain is recorded as it happened; only a NEW attempt needs the approval to vouch.
4. Build, INSERT the transaction (`step='release'`, `milestone_index`, attempts counted
   per milestone, `sent`) and COMMIT; only then send. A retryable send error raises (the
   row stays `sent`: the next run asks the chain); a refused preflight is a failed
   landing. `PaymentsNotConfigured` (no attester key yet) raises: retried, the sweep
   wakes it again a bounded number of times. `PaymentRefused` (the chain code refused
   what it read) asks the chain again: released is done, anything else fails
   (`chain_release_failed`).
5. Poll for at most `poll_budget` seconds. Confirmed: `released` (`released_at`,
   `release_signature`), the submission `released`, audit `MILESTONE_RELEASED`; when every
   milestone is released the payment is complete (`completed_at`; it stays `funded`;
   audit `PAYMENT_COMPLETED`). Unknown: `StepPending`. Failed: never decided from the error code
   (a `CreateAttestation` code is ambiguous): the chain is read again (2). Released:
   done. When the chain now needs the OTHER transaction (the attestation appeared: crank;
   it vanished: attest again), it is decided again, at most `MAX_DECISIONS` times per
   job; when it needs the same one, no retry can change it: `failed`
   (`chain_release_failed`), and a person looks (and may retry, `execute.retry_release`).

A `failed` milestone keeps its approved submission: the money is still in the escrow.
Nothing is logged but ids, indexes, signatures and codes.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.approvals.integrity import payload_matches
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    AuditEvent,
    IssuedPurchaseOrder,
    PaymentChainTransaction,
    PoMilestoneSubmission,
    PoPayment,
    PoPaymentMilestone,
)
from bido_back.payments.config import PaymentProviders
from bido_back.payments.errors import PaymentError, PaymentNotFound, PaymentRefused, ProviderError
from bido_back.payments.milestones.proposals import (
    MILESTONE_RELEASE,
    MILESTONE_RELEASE_FAILED,
    MILESTONE_RELEASED,
    PAYMENT_COMPLETED,
    RELEASE_PO_MILESTONE,
    release_job_key,
)
from bido_back.payments.milestones.schemas import ReleaseMilestoneArgs
from bido_back.payments.milestones.state import (
    FUNDED,
    active_documents,
    documents_match,
    locked_milestone,
    release_terms_match,
)
from bido_back.payments.pipeline import EXPIRY_MARGIN, Clock, Db, StepPending, locked_payment
from bido_back.payments.ports import (
    EscrowState,
    GateAttestation,
    ReleaseSpec,
    SignedTransaction,
    TransactionStatus,
)
from bido_back.rfqs.audit import rfq_event
from bido_back.worker.envelope import Envelope
from bido_back.worker.jobs import JobQueue

logger = logging.getLogger(__name__)

RELEASE_STEP: Final = "release"
RELEASING: Final = "releasing"
RELEASED: Final = "released"
FAILED: Final = "failed"
# The highest milestone index (po_payment_milestones_index_check).
MAX_INDEX: Final = 3
# Decisions one job makes after failed landings before a person must look: the chain
# legitimately needs at most two (attest+release found the attestation written: crank).
MAX_DECISIONS: Final = 3

# Failure codes (po_payment_milestones_failure_code_check: `^[a-z0-9_]{1,64}$`).
APPROVAL_MISMATCH: Final = "approval_mismatch"
EVIDENCE_MISMATCH: Final = "evidence_mismatch"
PAYMENT_NOT_FUNDED: Final = "payment_not_funded"
OUT_OF_ORDER: Final = "out_of_order"
ESCROW_MISMATCH: Final = "escrow_mismatch"
ESCROW_REFUNDED: Final = "escrow_refunded"
ESCROW_NOT_FUNDED: Final = "escrow_not_funded"
GATE_MISMATCH: Final = "gate_mismatch"
ATTESTATION_MISMATCH: Final = "attestation_mismatch"
CHAIN_RELEASE_FAILED: Final = "chain_release_failed"

# A milestone's approval vouches for it once approved (`executed` after the act ran).
_APPROVED: Final = frozenset({"approved", "executed"})
_ERROR_LIMIT: Final = 120  # payment_chain_transactions_error_check


class InvalidMilestoneJob(PaymentError):
    """A `milestone.release` envelope that can never run (left for the dead-letter queue)."""


def milestone_release_envelope(tenant_id: UUID, payment_id: UUID, milestone_index: int) -> Envelope:
    """The release job of one claimed milestone (one key per milestone)."""
    return Envelope(
        type=MILESTONE_RELEASE,
        tenant_id=tenant_id,
        idempotency_key=release_job_key(payment_id, milestone_index),
        payload={"payment_id": str(payment_id), "milestone_index": milestone_index},
    )


def _job(envelope: Envelope) -> tuple[UUID, int]:
    payload = envelope.payload
    raw_id = payload.get("payment_id")
    raw_index = payload.get("milestone_index")
    if (
        set(payload) != {"payment_id", "milestone_index"}
        or not isinstance(raw_id, str)
        or not isinstance(raw_index, int)
        or isinstance(raw_index, bool)
    ):
        raise InvalidMilestoneJob("payload must be exactly {payment_id, milestone_index}")
    try:
        payment_id = UUID(raw_id)
    except ValueError:
        raise InvalidMilestoneJob("payment_id is not a UUID") from None
    if not 0 <= raw_index <= MAX_INDEX:
        raise InvalidMilestoneJob("milestone_index is out of range")
    if envelope.idempotency_key != release_job_key(payment_id, raw_index):
        raise InvalidMilestoneJob("idempotency_key does not match the milestone")
    return payment_id, raw_index


# --- What the approval vouches for (shared with the act and a person's retry) --------------


@dataclass(frozen=True, slots=True)
class Vouched:
    """A milestone's approval, re-read, and the submission it approved: they still hold."""

    approval: Approval
    args: ReleaseMilestoneArgs
    submission: PoMilestoneSubmission


async def vouched_release(
    session: AsyncSession,
    payment: PoPayment,
    milestone: PoPaymentMilestone,
    *,
    submission_state: Literal["submitted", "approved"] = "approved",
) -> Vouched | str:
    """What the milestone's approval vouches for, or the code why it no longer does:
    `approval_mismatch` (the approval, its stored payload, the requester or the rows'
    terms) or `evidence_mismatch` (the files are not the approved ones any more).

    The app role cannot change an approval's args or hash, so they are the record of what
    the person approved; the rows are checked against them. `submission_state`: the
    submission's state at this point (`submitted` while the act claims it).
    """
    if milestone.approval_id is None or milestone.submission_id is None:
        return APPROVAL_MISMATCH
    approval = await session.get(Approval, milestone.approval_id)
    if (
        approval is None
        or approval.tool != RELEASE_PO_MILESTONE
        or approval.status not in _APPROVED
        or approval.requested_by != payment.requested_by
        or not payload_matches(approval)
    ):
        return APPROVAL_MISMATCH
    try:
        args = ReleaseMilestoneArgs.model_validate(approval.args)
    except ValidationError:
        return APPROVAL_MISMATCH  # never chained: its text holds the values
    submission = await session.get(PoMilestoneSubmission, milestone.submission_id)
    if (
        submission is None
        or submission.state != submission_state
        or submission.approval_id != approval.id
        or milestone.evidence_hash != args.evidence_hash
        or not release_terms_match(payment, milestone, submission, args)
    ):
        return APPROVAL_MISMATCH
    if not documents_match(args, await active_documents(session, submission.id)):
        return EVIDENCE_MISMATCH
    return Vouched(approval, args, submission)


async def order_code(session: AsyncSession, payment: PoPayment, milestone_index: int) -> str | None:
    """Why the milestone cannot be released now: the escrow is not funded
    (`payment_not_funded`) or an earlier milestone is not released (`out_of_order`)."""
    if payment.state != FUNDED:
        return PAYMENT_NOT_FUNDED
    earlier = await session.scalars(
        select(PoPaymentMilestone.state).where(
            PoPaymentMilestone.payment_id == payment.id,
            PoPaymentMilestone.milestone_index < milestone_index,
        )
    )
    states = list(earlier)
    if len(states) != milestone_index or any(state != RELEASED for state in states):
        return OUT_OF_ORDER
    return None


async def milestone_event(
    session: AsyncSession,
    payment: PoPayment,
    actor: UUID | None,
    action: str,
    **fields: object,
) -> AuditEvent:
    """An audit event of the payment, keyed by the PO's RFQ so its activity feed shows it.
    Ids, indexes, codes and amounts as text: never a document, a name or a note."""
    po = await session.get(IssuedPurchaseOrder, payment.purchase_order_id)
    if po is None:
        raise PaymentNotFound(f"purchase order {payment.purchase_order_id} not found")
    return rfq_event(
        payment.tenant_id,
        actor,
        action,
        po.rfq_id,
        payment_id=payment.id,
        purchase_order_id=po.id,
        purchase_order_ref=po.ref,
        invite_id=po.invite_id,
        **fields,
    )


# --- The job ---------------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _mark(row: PaymentChainTransaction, state: str, error: str | None = None) -> None:
    row.state = state
    row.error = error[:_ERROR_LIMIT] if error else None
    row.updated_at = func.now()


def _escrow_is_ours(escrow: EscrowState, payment: PoPayment, mint: str) -> bool:
    return (
        escrow.address == payment.escrow_address
        and escrow.buyer == payment.wallet_address
        and escrow.chain_po_id == payment.chain_po_id
        and escrow.total == int(payment.token_amount)
        and escrow.mint == mint
        and escrow.po_hash == payment.po_hash
    )


type Verdict = Literal["released", "attest", "crank", "failed"]


@dataclass(frozen=True, slots=True)
class _OnChain:
    """What the chain says of the milestone: released, what to send next, or why not."""

    verdict: Verdict
    code: str | None = None


@dataclass(frozen=True, slots=True)
class _Outcome:
    """What a decision committed: the milestone's state now; `again`: decide once more
    (a failed landing changed what the chain needs)."""

    state: str
    again: bool = False


@dataclass(frozen=True, slots=True)
class _Attempt:
    """A signed release transaction, recorded `sent` and committed: it may be sent now."""

    transaction: SignedTransaction
    attest: bool


@dataclass(frozen=True, slots=True)
class _Landed:
    """What the milestone's earlier transactions came to (none of them can land any more)."""

    confirmed: str | None = None  # the signature of the one that landed
    failed: bool = False  # one landed with an error (or was refused) and none succeeded


class MilestoneReleaseHandler:
    """The `milestone.release` job handler: one approved milestone of one escrow, once.

    `attester`: the gate attester's public key (`SOLANA_GATE_ATTESTER`): an existing
    attestation is cranked only if it signed it. None leaves that check to the chain
    code (`build_release`) and the program. `jobs` is accepted like every payment
    handler's: a release wakes nothing (the next milestone needs its own approval).
    """

    def __init__(
        self,
        db: Db,
        providers: PaymentProviders,
        jobs: JobQueue | None,
        *,
        poll_interval: float = 1.5,
        poll_budget: float = 20.0,
        now: Clock | None = None,
        attester: str | None = None,
    ) -> None:
        self._db = db
        self._providers = providers
        self._jobs = jobs
        self._poll_interval = poll_interval
        self._poll_budget = poll_budget
        self._now: Clock = now or _utcnow
        self._attester = attester

    async def __call__(self, envelope: Envelope) -> None:
        payment_id, milestone_index = _job(envelope)
        await self.release(envelope.tenant_id, payment_id, milestone_index)

    async def release(self, tenant_id: UUID, payment_id: UUID, milestone_index: int) -> str:
        """Release the milestone if it is `releasing`; returns its state after.

        Raises `StepPending` while a transaction may still land, `PaymentsNotConfigured`
        while the attester's key is missing, and provider errors worth retrying.
        """
        for _ in range(MAX_DECISIONS):
            decided = await self._decide(tenant_id, payment_id, milestone_index)
            if isinstance(decided, _Outcome):
                return decided.state
            status = await self._send(decided)
            if status.state == "unknown":
                raise StepPending(
                    f"payment {payment_id} milestone {milestone_index}: release "
                    f"{decided.transaction.signature} not confirmed yet"
                )
            settled = await self._settle(tenant_id, payment_id, milestone_index, decided, status)
            if not settled.again:
                return settled.state
        return await self._give_up(tenant_id, payment_id, milestone_index)

    # --- 1-4. Decide, under the payment's and the milestone's row locks --------------------

    async def _decide(
        self, tenant_id: UUID, payment_id: UUID, milestone_index: int
    ) -> _Outcome | _Attempt:
        async with tenant_session(self._db, tenant_id) as session:
            payment = await locked_payment(session, payment_id)
            milestone = await locked_milestone(session, payment_id, milestone_index)
            if milestone.state != RELEASING:
                return _Outcome(milestone.state)  # not approved yet, or settled
            landed = await self._resolve_sent(session, payment.id, milestone_index)
            if landed.confirmed is not None:
                return await self._released(session, payment, milestone, landed.confirmed)
            on_chain = await self._on_chain(payment, milestone)
            if on_chain.verdict == "released":
                return await self._released(session, payment, milestone, None)
            if on_chain.verdict == "failed":
                return await self._fail(session, payment, milestone, on_chain.code)
            # A new attempt sends only what the person approved: re-checked every time.
            vouched = await vouched_release(session, payment, milestone)
            code = (
                vouched
                if isinstance(vouched, str)
                else await order_code(session, payment, milestone_index)
            )
            if code is not None:
                return await self._fail(session, payment, milestone, code)
            if landed.failed:
                # Its job stopped before settling it: the chain (read above) decides.
                logger.warning(
                    "payment %s milestone %d: an earlier release failed on chain; deciding again",
                    payment.id,
                    milestone_index,
                )
            return await self._attempt(
                session, payment, milestone, attest=on_chain.verdict == "attest"
            )

    async def _attempt(
        self,
        session: AsyncSession,
        payment: PoPayment,
        milestone: PoPaymentMilestone,
        *,
        attest: bool,
    ) -> _Outcome | _Attempt:
        """Build, record (`sent`) and hand back the next transaction (committed by the caller):
        `attest`: write the gate's attestation first; else the crank."""
        index = milestone.milestone_index
        escrow, evidence = payment.escrow_address, milestone.evidence_hash
        if escrow is None or evidence is None:
            return await self._fail(session, payment, milestone, APPROVAL_MISMATCH)
        attempt = (
            await session.scalar(
                select(func.max(PaymentChainTransaction.attempt)).where(
                    PaymentChainTransaction.payment_id == payment.id,
                    PaymentChainTransaction.step == RELEASE_STEP,
                    PaymentChainTransaction.milestone_index == index,
                )
            )
            or 0
        ) + 1
        try:
            # `PaymentsNotConfigured` (no attester key yet) is raised on: retried later.
            built = await self._providers.chain.build_release(
                ReleaseSpec(escrow=escrow, milestone=index, evidence_hash=evidence, attest=attest)
            )
        except PaymentRefused:
            # The chain code refused what it read: the chain says what happened.
            again = await self._on_chain(payment, milestone)
            if again.verdict == "released":
                return await self._released(session, payment, milestone, None)
            code = again.code if again.verdict == "failed" else None
            return await self._fail(session, payment, milestone, code or CHAIN_RELEASE_FAILED)
        session.add(
            PaymentChainTransaction(
                tenant_id=payment.tenant_id,
                payment_id=payment.id,
                step=RELEASE_STEP,
                milestone_index=index,
                attempt=attempt,
                signature=built.signature,
                last_valid_block_height=built.last_valid_block_height,
                state="sent",
            )
        )
        milestone.updated_at = func.now()
        await session.flush()
        logger.info(
            "payment %s milestone %d release attempt=%d attest=%s signature=%s tenant=%s: "
            "recorded, sending",
            payment.id,
            index,
            attempt,
            attest,
            built.signature,
            payment.tenant_id,
        )
        return _Attempt(built, attest=attest)

    async def _resolve_sent(
        self, session: AsyncSession, payment_id: UUID, milestone_index: int
    ) -> _Landed:
        """Settle the milestone's release transactions still `sent`; raises `StepPending`
        while one may land."""
        rows = await session.scalars(
            select(PaymentChainTransaction)
            .where(
                PaymentChainTransaction.payment_id == payment_id,
                PaymentChainTransaction.step == RELEASE_STEP,
                PaymentChainTransaction.milestone_index == milestone_index,
                PaymentChainTransaction.state == "sent",
            )
            .order_by(PaymentChainTransaction.attempt.desc())
        )
        failed = False
        for row in list(rows):
            status = await self._landing(row)
            if status is None:
                _mark(row, "expired")
                logger.warning(
                    "payment %s milestone %d release signature=%s expired: it can no longer land",
                    payment_id,
                    milestone_index,
                    row.signature,
                )
            elif status.state == "confirmed":
                _mark(row, "confirmed")
                return _Landed(confirmed=row.signature)
            else:
                _mark(row, "failed", status.error)
                failed = True
        return _Landed(failed=failed)

    async def _landing(self, row: PaymentChainTransaction) -> TransactionStatus | None:
        """Where a sent transaction stands; None once it provably cannot land.

        Raises `StepPending` while its blockhash is still valid: it may land yet.
        """
        chain = self._providers.chain
        status = await chain.status(row.signature)
        if status.state != "unknown":
            return status
        if await chain.block_height() <= row.last_valid_block_height + EXPIRY_MARGIN:
            raise StepPending(f"release {row.signature} may still land")
        # Past its last valid block height: it may still have landed in the last block.
        status = await chain.status(row.signature)
        return None if status.state == "unknown" else status

    async def _on_chain(self, payment: PoPayment, milestone: PoPaymentMilestone) -> _OnChain:
        """The escrow's milestone first, then the gate's attestation (gate = index)."""
        chain = self._providers.chain
        index = milestone.milestone_index
        address = payment.escrow_address
        if address is None:
            return _OnChain("failed", ESCROW_MISMATCH)
        escrow = await chain.escrow(address)
        if escrow is None:
            # Funded (confirmed) before anything could be approved: this node lags.
            raise StepPending(f"payment {payment.id}: its escrow is not visible yet")
        if not _escrow_is_ours(escrow, payment, chain.mint) or index >= len(escrow.milestones):
            return _OnChain("failed", ESCROW_MISMATCH)
        held = escrow.milestones[index]
        if held.index != index or held.amount != int(milestone.token_amount):
            return _OnChain("failed", ESCROW_MISMATCH)
        if held.status == "released":
            if held.evidence_hash == milestone.evidence_hash:
                return _OnChain("released")
            return _OnChain("failed", EVIDENCE_MISMATCH)  # released, not against our files
        if held.status == "cancelled" or escrow.status == "refunded":
            return _OnChain("failed", ESCROW_REFUNDED)
        if escrow.status != "funded":
            return _OnChain("failed", ESCROW_NOT_FUNDED)
        if held.gate != index:
            return _OnChain("failed", GATE_MISMATCH)  # Bido pins milestone i to gate i
        try:
            attestation = await chain.gate_attestation(escrow=address, gate=index)
        except PaymentRefused:
            # It reads the escrow again and did not find it (or its gate): this node lags.
            raise StepPending(
                f"payment {payment.id}: its gate {index} is not visible yet"
            ) from None
        except ProviderError as exc:
            if exc.retryable:
                raise
            return _OnChain("failed", ATTESTATION_MISMATCH)  # the account there is not ours
        if attestation is None:
            return _OnChain("attest")
        if self._attested_by_us(attestation, payment, milestone):
            return _OnChain("crank")
        return _OnChain("failed", ATTESTATION_MISMATCH)

    def _attested_by_us(
        self, attestation: GateAttestation, payment: PoPayment, milestone: PoPaymentMilestone
    ) -> bool:
        return (
            attestation.po_hash == payment.po_hash
            and attestation.gate == milestone.milestone_index
            and attestation.evidence_hash == milestone.evidence_hash
            and (self._attester is None or attestation.signer == self._attester)
        )

    # --- Send and wait (no transaction open) -------------------------------------------------

    async def _send(self, attempt: _Attempt) -> TransactionStatus:
        signature = attempt.transaction.signature
        try:
            await self._providers.chain.send(attempt.transaction)
        except ProviderError as exc:
            if exc.retryable:
                # It may have been forwarded: the row stays `sent` and the next run asks.
                logger.warning(
                    "release signature=%s: send failed code=%s; the chain is asked next time",
                    signature,
                    exc.code,
                )
                raise
            # Refused before it was forwarded (preflight): it cannot land.
            logger.warning("release signature=%s refused code=%s", signature, exc.code)
            return TransactionStatus("failed", exc.code)
        return await self._poll(signature)

    async def _poll(self, signature: str) -> TransactionStatus:
        """The status, asked every `poll_interval` for at most `poll_budget` seconds."""
        deadline = time.monotonic() + self._poll_budget
        while True:
            status = await self._providers.chain.status(signature)
            remaining = deadline - time.monotonic()
            if status.state != "unknown" or remaining <= 0:
                return status
            await asyncio.sleep(min(self._poll_interval, remaining))

    # --- 5. Record what landed, under the row locks again ------------------------------------

    async def _settle(
        self,
        tenant_id: UUID,
        payment_id: UUID,
        milestone_index: int,
        attempt: _Attempt,
        status: TransactionStatus,
    ) -> _Outcome:
        async with tenant_session(self._db, tenant_id) as session:
            payment = await locked_payment(session, payment_id)
            milestone = await locked_milestone(session, payment_id, milestone_index)
            row = await session.scalar(
                select(PaymentChainTransaction).where(
                    PaymentChainTransaction.signature == attempt.transaction.signature
                )
            )
            if milestone.state != RELEASING or row is None or row.state != "sent":
                return _Outcome(milestone.state)  # another run of this milestone settled it
            if status.state == "confirmed":
                _mark(row, "confirmed")
                return await self._released(session, payment, milestone, row.signature)
            _mark(row, "failed", status.error)
            # Never decided from the error code: the chain says what happened.
            on_chain = await self._on_chain(payment, milestone)
            if on_chain.verdict == "released":
                return await self._released(session, payment, milestone, None)
            if on_chain.verdict == "failed":
                return await self._fail(session, payment, milestone, on_chain.code)
            if (on_chain.verdict == "attest") == attempt.attest:
                # The chain needs the very transaction that just failed: a refusal (or a
                # setup problem) no retry changes. A person looks.
                return await self._fail(session, payment, milestone, CHAIN_RELEASE_FAILED)
            await session.flush()
            logger.warning(
                "payment %s milestone %d release signature=%s failed; the chain now needs %s",
                payment.id,
                milestone_index,
                row.signature,
                on_chain.verdict,
            )
            return _Outcome(milestone.state, again=True)

    async def _give_up(self, tenant_id: UUID, payment_id: UUID, milestone_index: int) -> str:
        """The chain changed under every attempt of this job: a person looks."""
        async with tenant_session(self._db, tenant_id) as session:
            payment = await locked_payment(session, payment_id)
            milestone = await locked_milestone(session, payment_id, milestone_index)
            if milestone.state != RELEASING:
                return milestone.state
            outcome = await self._fail(session, payment, milestone, CHAIN_RELEASE_FAILED)
            return outcome.state

    async def _released(
        self,
        session: AsyncSession,
        payment: PoPayment,
        milestone: PoPaymentMilestone,
        signature: str | None,
    ) -> _Outcome:
        """The milestone is released (`signature`: our transaction that did it; None: found
        released on chain). The last one completes the payment."""
        milestone.state = RELEASED
        milestone.released_at = self._now()
        milestone.release_signature = signature
        milestone.failure_code = None
        milestone.updated_at = func.now()
        submission = (
            await session.get(PoMilestoneSubmission, milestone.submission_id)
            if milestone.submission_id is not None
            else None
        )
        if submission is not None and submission.state == "approved":
            submission.state = RELEASED
            submission.updated_at = func.now()
        await session.flush()
        session.add(
            await milestone_event(
                session,
                payment,
                None,
                MILESTONE_RELEASED,
                approval_id=milestone.approval_id,
                milestone_id=milestone.id,
                milestone_index=milestone.milestone_index,
                milestone_key=milestone.key,
                submission_id=milestone.submission_id,
                token_amount=str(int(milestone.token_amount)),
                evidence_hash=milestone.evidence_hash,
                escrow_address=payment.escrow_address,
                signature=signature,
            )
        )
        states = list(
            await session.scalars(
                select(PoPaymentMilestone.state).where(PoPaymentMilestone.payment_id == payment.id)
            )
        )
        completed = (
            payment.state == FUNDED
            and payment.completed_at is None
            and all(state == RELEASED for state in states)
        )
        if completed:
            # The payment stays `funded`: the escrow released it all (`completed_at`).
            payment.completed_at = self._now()
            payment.updated_at = func.now()
            session.add(
                await milestone_event(
                    session,
                    payment,
                    None,
                    PAYMENT_COMPLETED,
                    approval_id=payment.approval_id,
                    escrow_address=payment.escrow_address,
                    token_amount=str(int(payment.token_amount)),
                    milestones=len(states),
                )
            )
        await session.flush()
        logger.info(
            "payment %s milestone %d released tenant=%s signature=%s completed=%s",
            payment.id,
            milestone.milestone_index,
            payment.tenant_id,
            signature,
            completed,
        )
        return _Outcome(RELEASED)

    async def _fail(
        self,
        session: AsyncSession,
        payment: PoPayment,
        milestone: PoPaymentMilestone,
        code: str | None,
    ) -> _Outcome:
        """The release stops here; the money stays in the escrow and a person looks."""
        failure = code or CHAIN_RELEASE_FAILED
        milestone.state = FAILED
        milestone.failure_code = failure
        milestone.updated_at = func.now()
        session.add(
            await milestone_event(
                session,
                payment,
                None,
                MILESTONE_RELEASE_FAILED,
                approval_id=milestone.approval_id,
                milestone_id=milestone.id,
                milestone_index=milestone.milestone_index,
                submission_id=milestone.submission_id,
                failure_code=failure,
            )
        )
        await session.flush()
        logger.error(
            "payment %s milestone %d release failed code=%s tenant=%s: needs a person",
            payment.id,
            milestone.milestone_index,
            failure,
            payment.tenant_id,
        )
        return _Outcome(FAILED)
