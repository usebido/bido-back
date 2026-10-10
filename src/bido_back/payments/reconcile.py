"""Wake charged payments whose next step's job was lost.

A charged payment is its own outbox: its state says which chain step is next, and each
step enqueues the next `payment.advance` after its commit (`payments.pipeline`). A crash
or an SQS error in between, or a job retried until the dead-letter queue, leaves a
payment in `charged`, `minted` or `escrow_created` with nothing to wake it. The scheduled
follow-ups sweep calls `requeue_stalled_payments` (on servers with payments):

* a payment in one of those states untouched for `MIN_AGE` (time for its own job and
  the queue's retries) and touched less than `MAX_AGE` ago is enqueued again, keyed like
  its own job;
* the pipeline locks the payment and asks the chain before it builds anything, so a job
  that was only late does nothing twice;
* each wake-up is a `payment.requeued` audit event, and a payment is woken at most
  `MAX_REQUEUES` times: one that keeps failing waits for a person (charged, not funded).

Tenants come from the SECURITY DEFINER `payment_retry_tenants(min_age, max_age)`
(migration 0040): tenant ids only.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Final
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import AuditEvent, PoPayment
from bido_back.payments.pipeline import STEPS, Db, payment_advance_envelope
from bido_back.worker.jobs import JobQueue

logger = logging.getLogger(__name__)

MIN_AGE: Final = timedelta(minutes=2)
MAX_AGE: Final = timedelta(days=7)
MAX_REQUEUES: Final = 20
REQUEUED_ACTION: Final = "payment.requeued"

_TENANTS = text("SELECT tenant_id FROM payment_retry_tenants(:min_age, :max_age)")


@dataclass(frozen=True, slots=True)
class RequeueResult:
    requeued: int = 0
    gave_up: int = 0


async def payment_retry_tenants(db: Db) -> list[UUID]:
    """Tenants with a charged payment in the wake-up window (tenant ids only)."""
    sessionmaker = db if isinstance(db, async_sessionmaker) else create_sessionmaker(db)
    async with sessionmaker() as session, session.begin():
        rows = await session.execute(_TENANTS, {"min_age": MIN_AGE, "max_age": MAX_AGE})
        tenants: list[UUID] = []
        for (tenant_id,) in rows:
            if not isinstance(tenant_id, UUID):
                raise TypeError("payment_retry_tenants returned a non-uuid")
            tenants.append(tenant_id)
        return tenants


async def requeue_stalled_payments(db: Db, jobs: JobQueue) -> RequeueResult:
    """Enqueue `payment.advance` again for charged payments whose next step never ran."""
    requeued = gave_up = 0
    for tenant_id in await payment_retry_tenants(db):
        to_wake: list[tuple[UUID, str]] = []
        async with tenant_session(db, tenant_id) as session:
            stalled = (
                await session.execute(
                    select(PoPayment.id, PoPayment.state).where(
                        PoPayment.state.in_(list(STEPS)),
                        PoPayment.updated_at < func.now() - MIN_AGE,
                        PoPayment.updated_at > func.now() - MAX_AGE,
                    )
                )
            ).all()
            for payment_id, state in stalled:
                earlier = await session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(
                        AuditEvent.action == REQUEUED_ACTION,
                        AuditEvent.payload["payment_id"].astext == str(payment_id),
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
                        payload={
                            "payment_id": str(payment_id),
                            "state": state,
                            "attempt": (earlier or 0) + 1,
                        },
                    )
                )
                to_wake.append((payment_id, state))
        # After the commit, like every enqueue: a lost one is caught by the next sweep.
        for payment_id, state in to_wake:
            await jobs.enqueue(payment_advance_envelope(tenant_id, payment_id, state))
            requeued += 1
    if requeued or gave_up:
        logger.warning("payments re-enqueued=%d gave_up=%d", requeued, gave_up)
    return RequeueResult(requeued=requeued, gave_up=gave_up)
