"""The `negotiation.sweep` job: evaluate offers, propose counter-offers, close at deadlines.

EventBridge Scheduler puts it on the jobs queue every 15 minutes with the
all-zeros tenant id (`ALL_TENANTS`), read as "every tenant with a sent or
answered invite", found through the same SECURITY DEFINER `followup_tenants()`
as the follow-ups (migration 0008; tenant ids only). Each tenant is swept in
its own `tenant_session`. A real tenant id sweeps just that tenant. Running it
twice at once is safe: proposals are keyed by (invite, round).

The `negotiation.replan` job (`NegotiationReplanHandler`): one RFQ planned right
after a supplier reply, instead of waiting for the sweep. Quote extraction marks
the RFQ and enqueues it (`bido_back.negotiation.hooks.request_replan`); a burst of
replies enqueues one. It runs the sweep's per-RFQ unit (`replan_rfq`), then the
tenant's mandate pass, like the sweep. Idempotent: with no request pending (a
duplicate delivery, or the sweep got there first) it does nothing; a reply that
arrived while it ran gets one more job.
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
from bido_back.negotiation.hooks import replan_envelope
from bido_back.negotiation.service import replan_rfq, sweep_tenant
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


def _rfq_id(envelope: Envelope) -> UUID | None:
    raw = envelope.payload.get("rfq_id")
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class NegotiationReplanHandler:
    db: Db
    writer: NegotiationWriter
    # Tests pin the clock; None = now.
    now: datetime | None = None
    # Mandate approvals' execution, and the follow-up job when a reply arrived meanwhile.
    # None: mandates idle and no follow-up (the sweep runs it).
    jobs: JobQueue | None = None

    async def __call__(self, envelope: Envelope) -> None:
        rfq_id = _rfq_id(envelope)
        if rfq_id is None or envelope.tenant_id == ALL_TENANTS:
            logger.error(
                "negotiation.replan: invalid payload tenant=%s key=%s",
                envelope.tenant_id,
                envelope.idempotency_key,
            )
            return
        result = await replan_rfq(
            self.db, envelope.tenant_id, rfq_id, writer=self.writer, now=self.now
        )
        if not result.ran or self.jobs is None:
            return
        await auto_approve_pending(self.db, envelope.tenant_id, jobs=self.jobs, now=self.now)
        if result.again is not None:
            try:
                await self.jobs.enqueue(replan_envelope(envelope.tenant_id, rfq_id, result.again))
            except Exception:
                # Still pending in the database: the sweep plans it.
                logger.exception("negotiation.replan for rfq %s not enqueued again", rfq_id)
