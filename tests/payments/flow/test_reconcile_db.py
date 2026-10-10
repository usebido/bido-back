"""Charged payments whose next step's job was lost are woken again, a bounded number of times."""

from datetime import timedelta
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.catalog.contracts import EmailNoteOutput, FollowupEmailInput
from bido_back.agents.context import ToolContext
from bido_back.db import tenant_session
from bido_back.db.models import AuditEvent, PoPayment
from bido_back.followups.jobs import ALL_TENANTS, FollowupSweepHandler
from bido_back.payments.pipeline import PaymentAdvanceHandler, payment_advance_envelope
from bido_back.payments.reconcile import (
    MAX_REQUEUES,
    REQUEUED_ACTION,
    payment_retry_tenants,
    requeue_stalled_payments,
)
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import FOLLOWUPS_SWEEP
from tests.approvals.conftest import FakeJobQueue
from tests.awards.test_awards_db import World
from tests.payments.fakes import FakeProviders
from tests.payments.flow.test_execute_db import Paying, start_paying

pytestmark = pytest.mark.db


@pytest_asyncio.fixture
async def paying(world: World, providers: FakeProviders) -> Paying:
    return await start_paying(world, providers)


async def age(
    migrator_engine: AsyncEngine, paying: Paying, payment_id: UUID, ago: timedelta
) -> None:
    """As if the payment was last touched `ago` (its job lost meanwhile)."""
    async with tenant_session(migrator_engine, paying.tenant) as session:
        await session.execute(
            update(PoPayment).where(PoPayment.id == payment_id).values(updated_at=func.now() - ago)
        )


def woken(jobs: FakeJobQueue, payment_id: UUID) -> list[str]:
    return [j.idempotency_key for j in jobs.jobs if j.payload["payment_id"] == str(payment_id)]


async def test_a_charged_payment_whose_job_was_lost_is_woken_again(
    paying: Paying, migrator_engine: AsyncEngine
) -> None:
    proposed = await paying.charged()
    await age(migrator_engine, paying, proposed.payment_id, timedelta(minutes=10))
    jobs = FakeJobQueue()

    result = await requeue_stalled_payments(paying.engine, jobs)

    assert result.requeued >= 1
    assert payment_advance_envelope(paying.tenant, proposed.payment_id, "charged") in jobs.jobs
    async with tenant_session(paying.engine, paying.tenant) as session:
        [event] = list(
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == REQUEUED_ACTION,
                    AuditEvent.payload["payment_id"].astext == str(proposed.payment_id),
                )
            )
        )
    assert event.payload == {
        "payment_id": str(proposed.payment_id),
        "state": "charged",
        "attempt": 1,
    }
    # The woken job runs the step it missed.
    handler = PaymentAdvanceHandler(paying.engine, paying.providers.bundle(), None, poll_budget=0)
    [job] = [j for j in jobs.jobs if j.payload["payment_id"] == str(proposed.payment_id)]
    await handler(job)
    assert (await paying.payment(proposed.payment_id)).state == "minted"


async def test_each_state_is_woken_with_its_own_step(
    paying: Paying, migrator_engine: AsyncEngine
) -> None:
    proposed = await paying.charged()
    handler = PaymentAdvanceHandler(paying.engine, paying.providers.bundle(), None, poll_budget=0)
    assert await handler.advance(paying.tenant, proposed.payment_id) == "minted"
    await age(migrator_engine, paying, proposed.payment_id, timedelta(minutes=10))
    jobs = FakeJobQueue()

    await requeue_stalled_payments(paying.engine, jobs)

    assert woken(jobs, proposed.payment_id) == [f"payment:{proposed.payment_id}:minted"]


async def test_recent_and_old_payments_are_left_alone(
    paying: Paying, migrator_engine: AsyncEngine
) -> None:
    proposed = await paying.charged()  # its own job may still be running
    jobs = FakeJobQueue()
    await requeue_stalled_payments(paying.engine, jobs)
    assert woken(jobs, proposed.payment_id) == []

    # Too old to wake on its own: a person looks at it.
    await age(migrator_engine, paying, proposed.payment_id, timedelta(days=8))
    await requeue_stalled_payments(paying.engine, jobs)
    assert woken(jobs, proposed.payment_id) == []


async def test_payments_with_no_step_left_are_not_woken(
    paying: Paying, migrator_engine: AsyncEngine
) -> None:
    waiting = await paying.propose()  # not charged: its approval is pending
    await age(migrator_engine, paying, waiting.payment_id, timedelta(minutes=10))
    jobs = FakeJobQueue()

    await requeue_stalled_payments(paying.engine, jobs)

    assert woken(jobs, waiting.payment_id) == []


async def test_gives_up_after_the_cap(paying: Paying, migrator_engine: AsyncEngine) -> None:
    proposed = await paying.charged()
    await age(migrator_engine, paying, proposed.payment_id, timedelta(minutes=10))
    jobs = FakeJobQueue()

    results = [await requeue_stalled_payments(paying.engine, jobs) for _ in range(MAX_REQUEUES + 2)]

    assert len(woken(jobs, proposed.payment_id)) == MAX_REQUEUES
    assert results[-1].gave_up >= 1


async def test_the_tenant_lookup_returns_ids_only_for_stalled_payments(
    paying: Paying, migrator_engine: AsyncEngine
) -> None:
    proposed = await paying.charged()
    assert paying.tenant not in await payment_retry_tenants(paying.engine)  # too recent

    await age(migrator_engine, paying, proposed.payment_id, timedelta(minutes=10))

    tenants = await payment_retry_tenants(paying.engine)
    assert paying.tenant in tenants
    assert paying.world.people.other_tenant not in tenants


async def no_writer(task: FollowupEmailInput, context: ToolContext) -> EmailNoteOutput:
    raise AssertionError("no follow-up to write")


async def test_the_scheduled_sweep_wakes_payments_on_servers_with_payments(
    paying: Paying, migrator_engine: AsyncEngine
) -> None:
    proposed = await paying.charged()
    await age(migrator_engine, paying, proposed.payment_id, timedelta(minutes=10))
    sweep = Envelope(type=FOLLOWUPS_SWEEP, tenant_id=ALL_TENANTS, idempotency_key="sweep-1")
    jobs = FakeJobQueue()

    await FollowupSweepHandler(db=paying.engine, writer=no_writer, jobs=jobs)(sweep)
    assert woken(jobs, proposed.payment_id) == []  # no payments on this server

    await FollowupSweepHandler(db=paying.engine, writer=no_writer, jobs=jobs, payments=True)(sweep)
    assert woken(jobs, proposed.payment_id) == [f"payment:{proposed.payment_id}:charged"]
