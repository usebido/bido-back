"""The approval guard: the one place an `act` proposal is approved or rejected.

Nothing a client sends counts as approval except a call to `decide_approval`
by an authenticated principal. The decision is made in ONE transaction in the
principal's tenant, checking, in order:

1. the principal is a member of the tenant with role `approver` or `owner`
   (read from `memberships` now, never from the token; the row is share-locked
   so a concurrent demotion cannot slip in between check and decision) -> 403;
2. the approval exists in that tenant (forced RLS: another tenant's id is
   simply not found) and is locked `FOR UPDATE` -> 404;
3. the principal is not the requester (also a database CHECK) -> 403;
4. it is still `pending`, so a decision is made exactly once and a replayed
   request changes nothing -> 409;
5. its stored args still hash to the proposed `args_hash` -> 409, nothing is
   decided, and an `approval.integrity_failed` audit event is kept.

Then the status, `decided_by` and `decided_at` are set and an audit event is
written (ids and hash only, never the args), in the same transaction.

Approving does not execute anything. After the commit, an `approval.execute`
job is enqueued (idempotency key `approval:<id>`); the worker runs the tool
(`executor.py`). Until the transactional outbox exists, the enqueue happens
after the commit: if it fails, the approval stays `approved` with no job, the
error propagates (the caller sees a 5xx and it reaches Sentry), and the job
must be re-enqueued (a sweeper over approved-but-unexecuted rows, or the
outbox, closes this gap).
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.approvals.audit import approval_audit
from bido_back.approvals.errors import (
    ApprovalForbidden,
    ApprovalIntegrityError,
    ApprovalNotFound,
    ApprovalNotPending,
)
from bido_back.approvals.executor import approval_execute_envelope
from bido_back.approvals.integrity import payload_matches
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import Approval, Membership
from bido_back.worker.jobs import JobQueue

logger = logging.getLogger(__name__)

Decision = Literal["approve", "reject"]
DecidedStatus = Literal["approved", "rejected"]

APPROVER_ROLES: Final = frozenset({"approver", "owner"})

_STATUS: Final[dict[Decision, DecidedStatus]] = {"approve": "approved", "reject": "rejected"}


@dataclass(frozen=True, slots=True)
class DecisionOutcome:
    approval_id: UUID
    status: DecidedStatus


async def decide_approval(
    db: AsyncEngine | async_sessionmaker[AsyncSession],
    principal: Principal,
    approval_id: UUID,
    decision: Decision,
    *,
    jobs: JobQueue,
) -> DecisionOutcome:
    """Record `principal`'s decision on one approval; enqueue execution if approved.

    Raises ApprovalForbidden, ApprovalNotFound, ApprovalNotPending or
    ApprovalIntegrityError; on any of them nothing was decided.
    """
    status = _STATUS[decision]
    integrity_failed = False

    async with tenant_session(db, principal.tenant_id) as session:
        role = await session.scalar(
            select(Membership.role)
            .where(
                Membership.tenant_id == principal.tenant_id,
                Membership.user_id == principal.user_id,
            )
            .with_for_update(read=True)
        )
        if role not in APPROVER_ROLES:
            raise ApprovalForbidden("the approver or owner role is required")

        approval = await session.scalar(
            select(Approval).where(Approval.id == approval_id).with_for_update()
        )
        if approval is None or approval.tenant_id != principal.tenant_id:
            raise ApprovalNotFound(f"approval {approval_id} not found")
        if approval.requested_by == principal.user_id:
            raise ApprovalForbidden("the requester cannot decide their own approval")
        if approval.status != "pending":
            raise ApprovalNotPending(f"approval {approval_id} is {approval.status}")

        if not payload_matches(approval):
            # Keep the evidence (committed), decide nothing, refuse below.
            session.add(approval_audit(approval, "approval.integrity_failed", principal.user_id))
            integrity_failed = True
        else:
            approval.status = status
            approval.decided_by = principal.user_id
            approval.decided_at = datetime.now(UTC)
            session.add(approval_audit(approval, f"approval.{status}", principal.user_id))
        await session.flush()

    if integrity_failed:
        logger.error("approval %s refused: stored args do not match args_hash", approval_id)
        raise ApprovalIntegrityError(f"approval {approval_id}: args do not match hash")

    logger.info(
        "approval %s %s tenant=%s by=%s",
        approval_id,
        status,
        principal.tenant_id,
        principal.user_id,
    )
    if status == "approved":
        try:
            await jobs.enqueue(approval_execute_envelope(principal.tenant_id, approval_id))
        except Exception:
            logger.exception("approval %s approved but its job was not enqueued", approval_id)
            raise
    return DecisionOutcome(approval_id=approval_id, status=status)
