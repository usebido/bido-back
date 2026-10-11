"""The `release_po_milestone` act tool: claim one approved milestone for its release, once.

Nothing here is offered to a model. The factory (or a buyer on its behalf) submits a
milestone's documents; `service.submit_milestone` records the pending approval (its
requester is the payment's) and marks the submission and the milestone `submitted`. A
person approves it through the guard; the worker's `ApprovalExecutor` runs this handler
once, with the APPROVED arguments, as the APPROVER.

The claim is one transaction, the payment row locked, then the milestone's:

* The executing approval (by its run) must be the milestone's. Its submission not
  recorded yet (the service records it right after the proposal): raised, so the
  execution is retried until it is. Another approval holds the milestone: `skipped`,
  nothing touched. One this claim already refused: `failed` again, its code.
* `releasing`: claimed by an earlier run whose job may have been lost: woken again.
  `released`: `already_released`. `failed` or back to `open`: `skipped`.
* From `submitted`: the approval still vouches for exactly these rows and files
  (`release.vouched_release`: `approval_mismatch`, `evidence_mismatch`, the evidence
  hash recomputed from the files), the escrow is funded (`payment_not_funded`) and
  every earlier milestone is released (`out_of_order`: strictly in order, here too).
  Any of them refuses: the submission `cancelled` with the code, the milestone `open`
  again with it (the factory sends again), audit `MILESTONE_RELEASE_FAILED`; nothing
  reached the chain. Otherwise the submission is `approved` (by the approver, when the
  approval was decided), the milestone `releasing`, audit `MILESTONE_RELEASING`.

After the commit the `milestone.release` job is enqueued (`release`, exactly once on
chain); a lost one is woken by the scheduled sweep (`milestones.reconcile`). Refusals
and no-ops RETURN their outcome (the approval becomes `executed`); only transient
problems RAISE (the approval stays `approved` and the execution is retried).

`retry_release`: a person retries a release that failed (after fixing what failed,
e.g. the attester's key or its SAS authorization) while its approval still vouches.

Rejected: `hooks.MILESTONE_REJECT_HOOKS` (the milestone goes back to the factory).
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.agents.context import ToolContext
from bido_back.agents.tools import Tool
from bido_back.approvals.executor import ActToolRegistry
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    Membership,
    PoMilestoneSubmission,
    PoPayment,
    PoPaymentMilestone,
)
from bido_back.payments.config import PaymentProviders
from bido_back.payments.errors import PaymentForbidden, PaymentInvalidState, PaymentNotFound
from bido_back.payments.milestones.proposals import (
    MILESTONE_RELEASE_FAILED,
    MILESTONE_RELEASING,
    RELEASE_PO_MILESTONE,
)
from bido_back.payments.milestones.release import (
    APPROVAL_MISMATCH,
    FAILED,
    RELEASING,
    milestone_event,
    milestone_release_envelope,
    order_code,
    vouched_release,
)
from bido_back.payments.milestones.schemas import (
    ReleaseMilestoneArgs,
    ReleaseMilestoneResult,
    ReleaseOutcome,
)
from bido_back.payments.milestones.state import ensure_milestones, locked_milestone
from bido_back.payments.pipeline import Clock, Db, locked_payment
from bido_back.worker.jobs import JobQueue

logger = logging.getLogger(__name__)

# A person retried a failed release (`retry_release`), keyed by the RFQ like the others.
MILESTONE_RELEASE_RETRIED: Final = "purchase_order.milestone_release_retried"


@dataclass(frozen=True, slots=True)
class MilestoneActDeps:
    db: Db
    providers: PaymentProviders
    # Wakes the release after the claim; None: the scheduled sweep does it.
    jobs: JobQueue | None
    # Tests pin the clock; None = now.
    now: Clock | None = None


@dataclass(frozen=True, slots=True)
class _Claimed:
    """What the claim committed; `wake`: enqueue the milestone's release after the commit."""

    result: ReleaseMilestoneResult
    wake: bool = False


def _result(
    args: ReleaseMilestoneArgs, outcome: ReleaseOutcome, code: str | None = None
) -> ReleaseMilestoneResult:
    return ReleaseMilestoneResult(
        outcome=outcome,
        payment_id=args.payment_id,
        milestone_index=args.milestone_index,
        failure_code=code,
    )


async def _wake(jobs: JobQueue | None, tenant_id: UUID, payment_id: UUID, index: int) -> None:
    if jobs is None:
        return  # the scheduled sweep wakes it
    try:
        await jobs.enqueue(milestone_release_envelope(tenant_id, payment_id, index))
    except Exception:
        # The claim is committed: the scheduled sweep wakes it (`milestones.reconcile`).
        logger.exception(
            "payment %s milestone %d: its release job was not enqueued", payment_id, index
        )


class _ReleasePoMilestone:
    def __init__(self, deps: MilestoneActDeps) -> None:
        self._deps = deps

    async def __call__(
        self, ctx: ToolContext, args: ReleaseMilestoneArgs
    ) -> ReleaseMilestoneResult:
        if ctx.user_id is None:
            raise PaymentInvalidState("a release is executed for the person who approved it")
        async with tenant_session(self._deps.db, ctx.tenant_id) as session:
            claimed = await self._claim(session, ctx, ctx.user_id, args)
        if claimed.wake:
            await _wake(self._deps.jobs, ctx.tenant_id, args.payment_id, args.milestone_index)
        return claimed.result

    async def _claim(
        self, session: AsyncSession, ctx: ToolContext, approver: UUID, args: ReleaseMilestoneArgs
    ) -> _Claimed:
        payment = await locked_payment(session, args.payment_id)
        milestone = await locked_milestone(session, args.payment_id, args.milestone_index)
        approval = await session.scalar(
            select(Approval).where(
                Approval.run_id == str(ctx.run_id), Approval.tool == RELEASE_PO_MILESTONE
            )
        )
        if approval is None or approval.id != milestone.approval_id:
            return await self._not_the_milestones(session, ctx, args, milestone, approval)
        match milestone.state:
            case "submitted":
                return await self._check(session, ctx, approver, args, payment, milestone, approval)
            case "releasing":
                # Claimed by an earlier run: its release job may have been lost.
                return _Claimed(_result(args, "releasing"), wake=True)
            case "released":
                return _Claimed(_result(args, "already_released"))
            case _:
                # `failed` (a person settles it) or `open` again: nothing to claim.
                return _Claimed(_result(args, "skipped", milestone.failure_code))

    async def _not_the_milestones(
        self,
        session: AsyncSession,
        ctx: ToolContext,
        args: ReleaseMilestoneArgs,
        milestone: PoPaymentMilestone,
        approval: Approval | None,
    ) -> _Claimed:
        """The executing approval does not hold the milestone: never touch it."""
        if approval is None:
            logger.error(
                "payment %s milestone %d: run %s is no release approval; not claimed",
                args.payment_id,
                args.milestone_index,
                ctx.run_id,
            )
            return _Claimed(_result(args, "failed", APPROVAL_MISMATCH))
        submission = await session.scalar(
            select(PoMilestoneSubmission).where(
                PoMilestoneSubmission.approval_id == approval.id,
                PoMilestoneSubmission.milestone_id == milestone.id,
            )
        )
        if submission is None and milestone.state == "open" and milestone.approval_id is None:
            # Proposed, and recorded right after (`service.submit_milestone`): retried until
            # the submission is recorded.
            raise PaymentInvalidState(
                f"payment {args.payment_id} milestone {args.milestone_index}: approval "
                f"{approval.id} is not recorded on the milestone yet"
            )
        if submission is not None and submission.state == "cancelled":
            # Refused by an earlier run of this execution: the same answer.
            return _Claimed(_result(args, "failed", submission.failure_code))
        logger.warning(
            "payment %s milestone %d: approval %s does not hold it; skipped",
            args.payment_id,
            args.milestone_index,
            approval.id,
        )
        return _Claimed(_result(args, "skipped", APPROVAL_MISMATCH))

    async def _check(
        self,
        session: AsyncSession,
        ctx: ToolContext,
        approver: UUID,
        args: ReleaseMilestoneArgs,
        payment: PoPayment,
        milestone: PoPaymentMilestone,
        approval: Approval,
    ) -> _Claimed:
        """Everything the approval assumed still holds; then the milestone is `releasing`."""
        vouched = await vouched_release(session, payment, milestone, submission_state="submitted")
        if isinstance(vouched, str):
            return await self._refuse(
                session, approver, args, payment, milestone, approval, vouched
            )
        code = await order_code(session, payment, milestone.milestone_index)
        if code is not None:
            return await self._refuse(session, approver, args, payment, milestone, approval, code)
        submission = vouched.submission
        submission.state = "approved"
        submission.decided_by = approver
        submission.decided_at = approval.decided_at or self._now()
        submission.failure_code = None
        submission.updated_at = func.now()
        milestone.state = RELEASING
        milestone.failure_code = None
        milestone.updated_at = func.now()
        approved = vouched.args
        session.add(
            await milestone_event(
                session,
                payment,
                approver,
                MILESTONE_RELEASING,
                approval_id=approval.id,
                requested_by=approval.requested_by,
                milestone_id=milestone.id,
                milestone_index=milestone.milestone_index,
                milestone_key=milestone.key,
                submission_id=submission.id,
                attempt=submission.attempt,
                token_amount=approved.token_amount,
                po_currency=approved.po_currency,
                po_amount=approved.po_amount,
                charge_currency=approved.charge_currency,
                charge_amount=approved.charge_amount,
                evidence_hash=approved.evidence_hash,
                simulated=approved.simulated,
            )
        )
        await session.flush()
        logger.info(
            "payment %s milestone %d releasing approval=%s tenant=%s",
            payment.id,
            milestone.milestone_index,
            approval.id,
            ctx.tenant_id,
        )
        return _Claimed(_result(args, "releasing"), wake=True)

    async def _refuse(
        self,
        session: AsyncSession,
        approver: UUID,
        args: ReleaseMilestoneArgs,
        payment: PoPayment,
        milestone: PoPaymentMilestone,
        approval: Approval,
        code: str,
    ) -> _Claimed:
        """Not released, and this approval never will be: the milestone goes back to the
        factory with the code; its submission is cancelled."""
        submission = (
            await session.get(PoMilestoneSubmission, milestone.submission_id)
            if milestone.submission_id is not None
            else None
        )
        if submission is not None and submission.state == "submitted":
            submission.state = "cancelled"
            submission.failure_code = code
            submission.decided_by = approver
            submission.decided_at = approval.decided_at or self._now()
            submission.updated_at = func.now()
        session.add(
            await milestone_event(
                session,
                payment,
                approver,
                MILESTONE_RELEASE_FAILED,
                approval_id=approval.id,
                milestone_id=milestone.id,
                milestone_index=milestone.milestone_index,
                submission_id=milestone.submission_id,
                failure_code=code,
            )
        )
        milestone.state = "open"
        milestone.failure_code = code
        milestone.approval_id = None
        milestone.submission_id = None
        milestone.evidence_hash = None
        milestone.updated_at = func.now()
        await session.flush()
        logger.warning(
            "payment %s milestone %d refused code=%s approval=%s tenant=%s: not released",
            payment.id,
            milestone.milestone_index,
            code,
            approval.id,
            payment.tenant_id,
        )
        return _Claimed(_result(args, "failed", code))

    def _now(self) -> datetime:
        return self._deps.now() if self._deps.now is not None else datetime.now(UTC)


# --- Registration --------------------------------------------------------------------------


def milestone_tools(
    deps: MilestoneActDeps,
) -> tuple[Tool[ReleaseMilestoneArgs, ReleaseMilestoneResult]]:
    return (
        Tool(
            name=RELEASE_PO_MILESTONE,
            description=(
                "Release one approved milestone of a funded purchase order escrow to its "
                "fixed payout."
            ),
            input_model=ReleaseMilestoneArgs,
            output_model=ReleaseMilestoneResult,
            kind="act",
            handler=_ReleasePoMilestone(deps),
        ),
    )


def register_milestone_act_tools(registry: ActToolRegistry, deps: MilestoneActDeps) -> None:
    for tool in milestone_tools(deps):
        registry.register(tool)


# --- A person retries a failed release -----------------------------------------------------


async def retry_release(
    db: Db, principal: Principal, po_id: UUID, index: int, *, jobs: JobQueue | None
) -> None:
    """Release a `failed` milestone again, after a person fixed what failed.

    Only while its approval still vouches for it (the approval approved or executed, its
    payload, the rows' terms and the files), the escrow is funded and every earlier
    milestone is released: the approval already decided the money, so any member of the
    company may ask. Then `releasing` again and the release job is enqueued after the
    commit (a lost one is woken by the sweep). The chain is asked first, as always: a
    milestone released meanwhile is only recorded.

    Raises `PaymentNotFound` (no such order, payment or milestone in the tenant),
    `PaymentForbidden` (not a member) or `PaymentInvalidState` (anything else).
    """
    async with tenant_session(db, principal.tenant_id) as session:
        member = await session.scalar(
            select(Membership.role).where(
                Membership.tenant_id == principal.tenant_id,
                Membership.user_id == principal.user_id,
            )
        )
        if member is None:
            raise PaymentForbidden("only a member of the company can retry a release")
        payment = await session.scalar(
            select(PoPayment)
            .where(PoPayment.purchase_order_id == po_id)
            .order_by(PoPayment.created_at.desc(), PoPayment.id)
            .limit(1)
            .with_for_update()
        )
        if payment is None:
            raise PaymentNotFound(f"purchase order {po_id} has no payment")
        await ensure_milestones(session, payment)  # created on first use
        milestone = await locked_milestone(session, payment.id, index)
        if milestone.state != FAILED:
            raise PaymentInvalidState(
                f"milestone {index} is {milestone.state}: only a failed release is retried"
            )
        vouched = await vouched_release(session, payment, milestone)
        code = vouched if isinstance(vouched, str) else await order_code(session, payment, index)
        if code is not None:
            raise PaymentInvalidState(
                f"milestone {index}: its approval no longer vouches for the release ({code})"
            )
        failed_with = milestone.failure_code
        milestone.state = RELEASING
        milestone.failure_code = None
        milestone.updated_at = func.now()
        session.add(
            await milestone_event(
                session,
                payment,
                principal.user_id,
                MILESTONE_RELEASE_RETRIED,
                approval_id=milestone.approval_id,
                milestone_id=milestone.id,
                milestone_index=index,
                submission_id=milestone.submission_id,
                previous_failure_code=failed_with,
            )
        )
        await session.flush()
        payment_id = payment.id
    logger.info(
        "payment %s milestone %d release retried by=%s tenant=%s",
        payment_id,
        index,
        principal.user_id,
        principal.tenant_id,
    )
    await _wake(jobs, principal.tenant_id, payment_id, index)
