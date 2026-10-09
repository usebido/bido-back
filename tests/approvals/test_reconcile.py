"""Approved actions whose execution job was lost are enqueued again, a bounded number of times."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.approvals import approval_execute_envelope, decide_approval
from bido_back.approvals.reconcile import (
    MAX_REQUEUES,
    REQUEUED_ACTION,
    approval_retry_tenants,
    requeue_stalled_approvals,
)
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import Approval
from bido_back.followups.jobs import ALL_TENANTS, FollowupSweepHandler
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import FOLLOWUPS_SWEEP

from .conftest import FakeJobQueue, People, audit_actions, propose

pytestmark = pytest.mark.db


def _approver(people: People) -> Principal:
    return Principal(tenant_id=people.tenant, user_id=people.approver, role="approver")


class LostQueue:
    """SQS is down at the moment of approval: the decision commits, the job is lost."""

    async def enqueue(self, envelope: Envelope) -> None:
        raise ConnectionError("queue unavailable")


async def _approved(app_engine: AsyncEngine, people: People) -> UUID:
    approval_id = await propose(app_engine, people.tenant, people.owner)
    with pytest.raises(ConnectionError):
        await decide_approval(
            app_engine, _approver(people), approval_id, "approve", jobs=LostQueue()
        )
    return approval_id


async def _decided(
    migrator_engine: AsyncEngine, people: People, approval_id: UUID, ago: timedelta
) -> None:
    async with tenant_session(migrator_engine, people.tenant) as session:
        await session.execute(
            update(Approval)
            .where(Approval.id == approval_id)
            .values(decided_at=datetime.now(UTC) - ago)
        )


async def test_a_lost_job_is_enqueued_again(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, people: People
) -> None:
    approval_id = await _approved(app_engine, people)
    await _decided(migrator_engine, people, approval_id, timedelta(minutes=10))
    jobs = FakeJobQueue()

    result = await requeue_stalled_approvals(app_engine, jobs)

    assert result.requeued == 1
    assert jobs.jobs == [approval_execute_envelope(people.tenant, approval_id)]
    assert REQUEUED_ACTION in await audit_actions(app_engine, people.tenant, approval_id)


async def test_recent_and_old_approvals_are_left_alone(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, people: People
) -> None:
    recent = await _approved(app_engine, people)  # its own job may still be running
    old = await _approved(app_engine, people)
    await _decided(migrator_engine, people, old, timedelta(days=3))
    jobs = FakeJobQueue()

    await requeue_stalled_approvals(app_engine, jobs)

    queued = {UUID(str(j.payload["approval_id"])) for j in jobs.jobs}
    assert recent not in queued and old not in queued


async def test_gives_up_after_the_cap(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, people: People
) -> None:
    approval_id = await _approved(app_engine, people)
    await _decided(migrator_engine, people, approval_id, timedelta(minutes=10))
    jobs = FakeJobQueue()

    for _ in range(MAX_REQUEUES + 2):
        await requeue_stalled_approvals(app_engine, jobs)

    mine = [j for j in jobs.jobs if j.payload["approval_id"] == str(approval_id)]
    assert len(mine) == MAX_REQUEUES


async def test_the_tenant_lookup_returns_ids_only_for_stalled_approvals(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, people: People
) -> None:
    assert people.tenant not in await approval_retry_tenants(app_engine)
    approval_id = await _approved(app_engine, people)
    assert people.tenant not in await approval_retry_tenants(app_engine)  # too recent
    await _decided(migrator_engine, people, approval_id, timedelta(minutes=10))
    assert people.tenant in await approval_retry_tenants(app_engine)
    assert people.other_tenant not in await approval_retry_tenants(app_engine)


async def test_the_scheduled_followups_sweep_runs_the_reconciler(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, people: People
) -> None:
    approval_id = await _approved(app_engine, people)
    await _decided(migrator_engine, people, approval_id, timedelta(minutes=10))
    jobs = FakeJobQueue()

    async def no_writer(*_: object) -> object:
        raise AssertionError("no follow-up to write")

    handler = FollowupSweepHandler(db=app_engine, writer=no_writer, jobs=jobs)  # pyright: ignore[reportArgumentType]
    await handler(Envelope(type=FOLLOWUPS_SWEEP, tenant_id=ALL_TENANTS, idempotency_key="sweep-1"))

    assert approval_execute_envelope(people.tenant, approval_id) in jobs.jobs
