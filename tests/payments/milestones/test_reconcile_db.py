"""Claimed milestones whose release job was lost are woken again, a bounded number of times."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.catalog.contracts import EmailNoteOutput, FollowupEmailInput
from bido_back.agents.context import ToolContext
from bido_back.db import tenant_session
from bido_back.db.models import AuditEvent, PoPaymentMilestone
from bido_back.followups.jobs import ALL_TENANTS, FollowupSweepHandler
from bido_back.payments.milestones.proposals import MILESTONE_RELEASE
from bido_back.payments.milestones.reconcile import (
    MAX_REQUEUES,
    REQUEUED_ACTION,
    milestone_retry_tenants,
    requeue_stalled_releases,
)
from bido_back.payments.milestones.release import milestone_release_envelope
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import FOLLOWUPS_SWEEP
from tests.approvals.conftest import FakeJobQueue
from tests.payments.milestones.conftest import Funded
from tests.payments.milestones.support import (
    approve_release,
    milestone_row,
    release_handler,
    submit_directly,
)

pytestmark = pytest.mark.db


async def age(migrator_engine: AsyncEngine, funded: Funded, index: int, ago: timedelta) -> None:
    """As if the milestone was last touched `ago` (its job lost meanwhile)."""
    async with tenant_session(migrator_engine, funded.tenant) as session:
        await session.execute(
            update(PoPaymentMilestone)
            .where(
                PoPaymentMilestone.payment_id == funded.payment_id,
                PoPaymentMilestone.milestone_index == index,
            )
            .values(updated_at=func.now() - ago)
        )


async def claimed(funded: Funded) -> None:
    """Milestone 0 approved and claimed; its release job is lost."""
    submitted = await submit_directly(funded, 0)
    await approve_release(funded, submitted.approval_id)
    funded.jobs.jobs.clear()
    assert (await milestone_row(funded, 0)).state == "releasing"


def woken(jobs: FakeJobQueue, funded: Funded) -> list[str]:
    return [
        job.idempotency_key
        for job in jobs.jobs
        if job.type == MILESTONE_RELEASE and job.payload["payment_id"] == str(funded.payment_id)
    ]


async def test_a_claimed_milestone_whose_job_was_lost_is_woken_again(
    funded: Funded, migrator_engine: AsyncEngine
) -> None:
    await claimed(funded)
    await age(migrator_engine, funded, 0, timedelta(minutes=10))
    jobs = FakeJobQueue()

    result = await requeue_stalled_releases(funded.engine, jobs)

    assert result.requeued >= 1
    assert milestone_release_envelope(funded.tenant, funded.payment_id, 0) in jobs.jobs
    async with tenant_session(funded.engine, funded.tenant) as session:
        [event] = list(
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == REQUEUED_ACTION,
                    AuditEvent.payload["payment_id"].astext == str(funded.payment_id),
                )
            )
        )
    assert event.payload == {
        "payment_id": str(funded.payment_id),
        "milestone_index": 0,
        "attempt": 1,
    }
    # The woken job releases the milestone it missed.
    [job] = [j for j in jobs.jobs if j.payload["payment_id"] == str(funded.payment_id)]
    await release_handler(funded)(job)
    assert (await milestone_row(funded, 0)).state == "released"


async def test_recent_and_old_releases_are_left_alone(
    funded: Funded, migrator_engine: AsyncEngine
) -> None:
    await claimed(funded)  # its own job may still be running
    jobs = FakeJobQueue()
    await requeue_stalled_releases(funded.engine, jobs)
    assert woken(jobs, funded) == []

    # Too old to wake on its own: a person looks at it.
    await age(migrator_engine, funded, 0, timedelta(days=8))
    await requeue_stalled_releases(funded.engine, jobs)
    assert woken(jobs, funded) == []


async def test_only_claimed_milestones_are_woken(
    funded: Funded, migrator_engine: AsyncEngine
) -> None:
    await submit_directly(funded, 0)  # under review: a person decides first
    await age(migrator_engine, funded, 0, timedelta(minutes=10))
    await age(migrator_engine, funded, 1, timedelta(minutes=10))  # open
    jobs = FakeJobQueue()

    await requeue_stalled_releases(funded.engine, jobs)

    assert woken(jobs, funded) == []


async def test_gives_up_after_the_cap(funded: Funded, migrator_engine: AsyncEngine) -> None:
    await claimed(funded)
    await age(migrator_engine, funded, 0, timedelta(minutes=10))
    jobs = FakeJobQueue()

    results = [await requeue_stalled_releases(funded.engine, jobs) for _ in range(MAX_REQUEUES + 2)]

    assert woken(jobs, funded) == [f"milestone:{funded.payment_id}:0:release"] * MAX_REQUEUES
    assert results[-1].gave_up >= 1
    assert (await milestone_row(funded, 0)).state == "releasing"  # waits for a person


async def test_the_tenant_lookup_returns_ids_only_for_stalled_releases(
    funded: Funded, migrator_engine: AsyncEngine
) -> None:
    await claimed(funded)
    assert funded.tenant not in await milestone_retry_tenants(funded.engine)  # too recent

    await age(migrator_engine, funded, 0, timedelta(minutes=10))

    tenants = await milestone_retry_tenants(funded.engine)
    assert funded.tenant in tenants
    assert funded.people.other_tenant not in tenants


async def no_writer(task: FollowupEmailInput, context: ToolContext) -> EmailNoteOutput:
    raise AssertionError("no follow-up to write")


async def test_the_scheduled_sweep_wakes_releases_on_servers_with_payments(
    funded: Funded, migrator_engine: AsyncEngine
) -> None:
    await claimed(funded)
    await age(migrator_engine, funded, 0, timedelta(minutes=10))
    sweep = Envelope(type=FOLLOWUPS_SWEEP, tenant_id=ALL_TENANTS, idempotency_key="sweep-1")
    jobs = FakeJobQueue()

    await FollowupSweepHandler(db=funded.engine, writer=no_writer, jobs=jobs)(sweep)
    assert woken(jobs, funded) == []  # no payments on this server

    await FollowupSweepHandler(db=funded.engine, writer=no_writer, jobs=jobs, payments=True)(sweep)
    assert woken(jobs, funded) == [f"milestone:{funded.payment_id}:0:release"]
