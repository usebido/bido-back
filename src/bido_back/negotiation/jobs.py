"""The `negotiation.sweep` job: evaluate offers, propose counter-offers, close at deadlines.

EventBridge Scheduler puts it on the jobs queue every 15 minutes with the
all-zeros tenant id (`ALL_TENANTS`), read as "every tenant with a sent or
answered invite", found through the same SECURITY DEFINER `followup_tenants()`
as the follow-ups (migration 0008; tenant ids only). Each tenant is swept in
its own `tenant_session`. A real tenant id sweeps just that tenant. Running it
twice at once is safe: proposals are keyed by (invite, round).
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.followups.jobs import followup_tenants
from bido_back.mandates.service import auto_approve_pending
from bido_back.negotiation.agents import NegotiationWriter
from bido_back.negotiation.service import sweep_tenant
from bido_back.worker.envelope import Envelope
from bido_back.worker.jobs import JobQueue

logger = logging.getLogger(__name__)

ALL_TENANTS: Final = UUID(int=0)

Db = AsyncEngine | async_sessionmaker[AsyncSession]


@dataclass(frozen=True, slots=True)
class NegotiationSweepHandler:
    db: Db
    writer: NegotiationWriter
    # Tests pin the clock; None = now.
    now: datetime | None = None
    # Mandates: after each tenant's sweep, approve its in-bounds emails and
    # enqueue their execution (`bido_back.mandates`). None: mandates idle.
    jobs: JobQueue | None = None

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
                logger.exception("negotiation.sweep failed tenant=%s", tenant_id)
                if single:
                    raise
