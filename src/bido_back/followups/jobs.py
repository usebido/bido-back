"""The `followups.sweep` job: chases, clarifications and their cleanup, per tenant.

EventBridge Scheduler puts it on the jobs queue every 15 minutes with the
all-zeros tenant id (`ALL_TENANTS`), read as "every tenant with a sent or
answered invite", found through the SECURITY DEFINER `followup_tenants()`
(migration 0008; tenant ids only). Each tenant is swept in its own
`tenant_session`. A real tenant id sweeps just that tenant. Running it twice
at once is safe: proposals are keyed by (invite, kind, sequence).

The every-tenant run also re-enqueues approved actions whose execution job was
lost between the approval's commit and its enqueue (`approvals.reconcile`) and,
on servers with payments, wakes charged payments whose next step's job was lost
(`payments.reconcile`) and approved milestone releases whose job was lost
(`payments.milestones.reconcile`).
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.approvals.reconcile import requeue_stalled_approvals
from bido_back.db import create_sessionmaker
from bido_back.followups.agents import FollowupWriter
from bido_back.followups.service import sweep_tenant
from bido_back.mandates.service import auto_approve_pending
from bido_back.payments.milestones.reconcile import requeue_stalled_releases
from bido_back.payments.reconcile import requeue_stalled_payments
from bido_back.worker.envelope import Envelope
from bido_back.worker.jobs import JobQueue

logger = logging.getLogger(__name__)

ALL_TENANTS: Final = UUID(int=0)
_TENANTS = text("SELECT tenant_id FROM followup_tenants()")

Db = AsyncEngine | async_sessionmaker[AsyncSession]


def _sessionmaker(db: Db) -> async_sessionmaker[AsyncSession]:
    return db if isinstance(db, async_sessionmaker) else create_sessionmaker(db)


async def followup_tenants(db: Db) -> list[UUID]:
    async with _sessionmaker(db)() as session, session.begin():
        tenants: list[UUID] = []
        for (tenant_id,) in await session.execute(_TENANTS):
            if not isinstance(tenant_id, UUID):
                raise TypeError("followup_tenants returned a non-uuid")
            tenants.append(tenant_id)
        return tenants


@dataclass(frozen=True, slots=True)
class FollowupSweepHandler:
    db: Db
    writer: FollowupWriter
    # Tests pin the clock; None = now.
    now: datetime | None = None
    # Mandates: after each tenant's sweep, approve its in-bounds emails and
    # enqueue their execution (`bido_back.mandates`). None: mandates idle.
    jobs: JobQueue | None = None
    # Payments: the every-tenant run also wakes charged payments whose next step's
    # job was lost (`payments.reconcile`) and claimed milestone releases whose job was
    # lost (`payments.milestones.reconcile`). False on servers without payments.
    payments: bool = False

    async def __call__(self, envelope: Envelope) -> None:
        single = envelope.tenant_id != ALL_TENANTS
        tenants: Sequence[UUID] = (
            [envelope.tenant_id] if single else await followup_tenants(self.db)
        )
        for tenant_id in tenants:
            try:
                await sweep_tenant(self.db, tenant_id, writer=self.writer, now=self.now)
                if self.jobs is not None:
                    await auto_approve_pending(self.db, tenant_id, jobs=self.jobs, now=self.now)
            except Exception:
                logger.exception("followups.sweep failed tenant=%s", tenant_id)
                if single:
                    raise
        if not single and self.jobs is not None:
            # Approved actions whose execution job was lost (`approvals.reconcile`).
            try:
                await requeue_stalled_approvals(self.db, self.jobs)
            except Exception:
                logger.exception("followups.sweep: re-enqueueing stalled approvals failed")
            if self.payments:
                try:
                    await requeue_stalled_payments(self.db, self.jobs)
                except Exception:
                    logger.exception("followups.sweep: waking stalled payments failed")
                try:
                    await requeue_stalled_releases(self.db, self.jobs)
                except Exception:
                    logger.exception("followups.sweep: waking stalled milestone releases failed")
