"""Wake milestone releases whose job was lost.

A claimed milestone is its own outbox: `releasing` says its release is due, and the
`release_po_milestone` act enqueues the `milestone.release` job after its commit
(`milestones.execute`). A crash or an SQS error in between, a job retried until the
dead-letter queue, or one that waits for the attester's key leaves a milestone in
`releasing` with nothing to wake it. The scheduled follow-ups sweep calls
`requeue_stalled_releases` (on servers with payments):

* a `releasing` milestone untouched for `MIN_AGE` (time for its own job and the queue's
  retries) and touched less than `MAX_AGE` ago is enqueued again, keyed like its own job;
* the job locks the payment and the milestone and asks the chain before it builds
  anything, so a job that was only late does nothing twice;
* each wake-up is a `milestone.requeued` audit event, and a milestone is woken at most
  `MAX_REQUEUES` times: one that keeps failing waits for a person (the money stays in
  the escrow).

Tenants come from the SECURITY DEFINER `milestone_retry_tenants(min_age, max_age)`
(migration 0043): tenant ids only.
"""

import logging
from datetime import timedelta
from typing import Final
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import AuditEvent, PoPaymentMilestone
from bido_back.payments.milestones.release import RELEASING, milestone_release_envelope
from bido_back.payments.pipeline import Db
from bido_back.payments.reconcile import RequeueResult
from bido_back.worker.jobs import JobQueue

logger = logging.getLogger(__name__)

MIN_AGE: Final = timedelta(minutes=2)
MAX_AGE: Final = timedelta(days=7)
MAX_REQUEUES: Final = 20
REQUEUED_ACTION: Final = "milestone.requeued"

_TENANTS = text("SELECT tenant_id FROM milestone_retry_tenants(:min_age, :max_age)")


async def milestone_retry_tenants(db: Db) -> list[UUID]:
    """Tenants with a `releasing` milestone in the wake-up window (tenant ids only)."""
    sessionmaker = db if isinstance(db, async_sessionmaker) else create_sessionmaker(db)
    async with sessionmaker() as session, session.begin():
        rows = await session.execute(_TENANTS, {"min_age": MIN_AGE, "max_age": MAX_AGE})
        tenants: list[UUID] = []
        for (tenant_id,) in rows:
            if not isinstance(tenant_id, UUID):
                raise TypeError("milestone_retry_tenants returned a non-uuid")
            tenants.append(tenant_id)
        return tenants


async def requeue_stalled_releases(db: Db, jobs: JobQueue) -> RequeueResult:
    """Enqueue `milestone.release` again for claimed milestones whose release never ran."""
    requeued = gave_up = 0
    for tenant_id in await milestone_retry_tenants(db):
        to_wake: list[tuple[UUID, int]] = []
        async with tenant_session(db, tenant_id) as session:
            stalled = (
                await session.execute(
                    select(PoPaymentMilestone.payment_id, PoPaymentMilestone.milestone_index)
                    .where(
                        PoPaymentMilestone.state == RELEASING,
                        PoPaymentMilestone.updated_at < func.now() - MIN_AGE,
                        PoPaymentMilestone.updated_at > func.now() - MAX_AGE,
                    )
                    .order_by(PoPaymentMilestone.payment_id, PoPaymentMilestone.milestone_index)
                )
            ).all()
            for payment_id, milestone_index in stalled:
                earlier = (
                    await session.scalar(
                        select(func.count())
                        .select_from(AuditEvent)
                        .where(
                            AuditEvent.action == REQUEUED_ACTION,
                            AuditEvent.payload["payment_id"].astext == str(payment_id),
                            AuditEvent.payload["milestone_index"].astext == str(milestone_index),
                        )
                    )
                    or 0
                )
                if earlier >= MAX_REQUEUES:
                    gave_up += 1
                    continue
                session.add(
                    AuditEvent(
                        tenant_id=tenant_id,
                        actor_user_id=None,
                        action=REQUEUED_ACTION,
                        payload={
                            "payment_id": str(payment_id),
                            "milestone_index": milestone_index,
                            "attempt": earlier + 1,
                        },
                    )
                )
                to_wake.append((payment_id, milestone_index))
        # After the commit, like every enqueue: a lost one is caught by the next sweep.
        for payment_id, milestone_index in to_wake:
            await jobs.enqueue(milestone_release_envelope(tenant_id, payment_id, milestone_index))
            requeued += 1
    if requeued or gave_up:
        logger.warning("milestone releases re-enqueued=%d gave_up=%d", requeued, gave_up)
    return RequeueResult(requeued=requeued, gave_up=gave_up)
