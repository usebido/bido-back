"""Re-enqueue approved actions whose `approval.execute` job was lost.

Approving commits first and enqueues the job after the commit
(`approvals/guard.py`, and the mandates). A crash or an SQS error in between
leaves the approval `approved` with no job, and nothing would ever run it.
The scheduled follow-ups sweep calls `requeue_stalled_approvals` to close that
gap without a transactional outbox:

* an approval still `approved` more than `MIN_AGE` after its decision (time
  for the normal job to have run) and less than `MAX_AGE` ago is enqueued
  again, with the same idempotency key;
* the executor locks the row and treats `executed` as a no-op, so a job that
  was only late runs once, and a send that already reached the provider is not
  sent twice (`MailboxSender.send` looks in the Sent folder first);
* each re-enqueue is an `approval.requeued` audit event, and an approval is
  re-enqueued at most `MAX_REQUEUES` times: one that keeps failing stays in
  the dead-letter queue for a person instead of being retried forever.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Final
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.approvals.executor import approval_execute_envelope
from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import Approval, AuditEvent
from bido_back.worker.jobs import JobQueue

logger = logging.getLogger(__name__)

MIN_AGE: Final = timedelta(minutes=5)
MAX_AGE: Final = timedelta(days=2)
MAX_REQUEUES: Final = 3
REQUEUED_ACTION: Final = "approval.requeued"

_TENANTS = text("SELECT tenant_id FROM approval_retry_tenants(:min_age, :max_age)")

Db = AsyncEngine | async_sessionmaker[AsyncSession]


@dataclass(frozen=True, slots=True)
class RequeueResult:
    requeued: int = 0
    gave_up: int = 0


def _sessionmaker(db: Db) -> async_sessionmaker[AsyncSession]:
    return db if isinstance(db, async_sessionmaker) else create_sessionmaker(db)


async def approval_retry_tenants(db: Db) -> list[UUID]:
    """Tenants with an approval in the re-enqueue window (tenant ids only)."""
    async with _sessionmaker(db)() as session, session.begin():
        rows = await session.execute(_TENANTS, {"min_age": MIN_AGE, "max_age": MAX_AGE})
        tenants: list[UUID] = []
        for (tenant_id,) in rows:
            if not isinstance(tenant_id, UUID):
                raise TypeError("approval_retry_tenants returned a non-uuid")
            tenants.append(tenant_id)
        return tenants


async def requeue_stalled_approvals(db: Db, jobs: JobQueue) -> RequeueResult:
    """Enqueue `approval.execute` again for approved actions that never ran."""
    requeued = gave_up = 0
    for tenant_id in await approval_retry_tenants(db):
        to_enqueue: list[UUID] = []
        async with tenant_session(db, tenant_id) as session:
            stalled = list(
                await session.scalars(
                    select(Approval.id).where(
                        Approval.status == "approved",
                        Approval.decided_at < func.now() - MIN_AGE,
                        Approval.decided_at > func.now() - MAX_AGE,
                    )
                )
            )
            for approval_id in stalled:
                earlier = await session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(
                        AuditEvent.action == REQUEUED_ACTION,
                        AuditEvent.payload["approval_id"].astext == str(approval_id),
                    )
                )
                if (earlier or 0) >= MAX_REQUEUES:
                    gave_up += 1
                    continue
                session.add(
                    AuditEvent(
                        tenant_id=tenant_id,
                        actor_user_id=None,
                        action=REQUEUED_ACTION,
                        payload={"approval_id": str(approval_id), "attempt": (earlier or 0) + 1},
                    )
                )
                to_enqueue.append(approval_id)
        # After the commit, like every enqueue: a lost one is caught next sweep.
        for approval_id in to_enqueue:
            await jobs.enqueue(approval_execute_envelope(tenant_id, approval_id))
            requeued += 1
    if requeued or gave_up:
        logger.warning("approvals re-enqueued=%d gave_up=%d", requeued, gave_up)
    return RequeueResult(requeued=requeued, gave_up=gave_up)
