"""decide_approval: only an authorized person decides, once, on an unchanged payload.

By default the requester may decide their own request (the AI prepared it for
them); a four-eyes tenant requires an approver or owner who is not the requester.
"""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.approvals import (
    APPROVAL_EXECUTE_JOB,
    ApprovalExecutor,
    ApprovalForbidden,
    ApprovalIntegrityError,
    ApprovalNotExecutable,
    ApprovalNotFound,
    ApprovalNotPending,
    Decision,
    DecisionOutcome,
    approval_execute_envelope,
    decide_approval,
)
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import Approval, AuditEvent, Tenant

from .conftest import (
    PAY_ARGS,
    FakeJobQueue,
    PaySupplier,
    People,
    audit_actions,
    load,
    propose,
    tamper,
)

pytestmark = pytest.mark.db


def as_user(people: People, user_id: UUID, tenant_id: UUID | None = None) -> Principal:
    return Principal(tenant_id=tenant_id or people.tenant, user_id=user_id)


async def assert_untouched(engine: AsyncEngine, people: People, approval_id: UUID) -> None:
    approval = await load(engine, people.tenant, approval_id)
    assert (approval.status, approval.decided_by, approval.decided_at) == ("pending", None, None)
    actions = await audit_actions(engine, people.tenant, approval_id)
    assert not {"approval.approved", "approval.rejected"} & set(actions)


async def test_member_cannot_approve(app_engine: AsyncEngine, people: People) -> None:
    approval_id = await propose(app_engine, people.tenant, people.owner)
    jobs = FakeJobQueue()

    with pytest.raises(ApprovalForbidden):
        await decide_approval(
            app_engine, as_user(people, people.member), approval_id, "approve", jobs=jobs
        )

    await assert_untouched(app_engine, people, approval_id)
    assert jobs.jobs == []


async def _four_eyes(migrator_engine: AsyncEngine, tenant_id: UUID) -> None:
    # The app role cannot UPDATE tenants: the policy is set by the migrator.
    async with tenant_session(migrator_engine, tenant_id) as session:
        await session.execute(
            update(Tenant).where(Tenant.id == tenant_id).values(allow_self_approval=False)
        )


async def _self_approved_marks(
    engine: AsyncEngine, tenant_id: UUID, approval_id: UUID
) -> tuple[bool, list[bool]]:
    async with tenant_session(engine, tenant_id) as session:
        row = await session.scalar(select(Approval).where(Approval.id == approval_id))
        assert row is not None
        events = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action.in_(("approval.approved", "approval.rejected")),
                    AuditEvent.payload["approval_id"].astext == str(approval_id),
                )
            )
        ).all()
        return row.self_approved, [bool(e.payload["self_approved"]) for e in events]


@pytest.mark.parametrize("requester", ["approver", "member"])
async def test_requester_may_decide_own_request_by_default(
    app_engine: AsyncEngine, people: People, requester: str
) -> None:
    # The AI prepared the act for this buyer; the buyer reviews and approves it,
    # whatever their role. Hash check and execute-once are unchanged.
    user: UUID = getattr(people, requester)
    approval_id = await propose(app_engine, people.tenant, user)
    jobs = FakeJobQueue()

    outcome = await decide_approval(
        app_engine, as_user(people, user), approval_id, "approve", jobs=jobs
    )

    assert outcome == DecisionOutcome(approval_id=approval_id, status="approved")
    approval = await load(app_engine, people.tenant, approval_id)
    assert (approval.status, approval.decided_by) == ("approved", user)
    assert await _self_approved_marks(app_engine, people.tenant, approval_id) == (True, [True])
    assert [j.idempotency_key for j in jobs.jobs] == [f"approval:{approval_id}"]


async def test_deciding_someone_elses_request_is_not_self_approved(
    app_engine: AsyncEngine, people: People
) -> None:
    approval_id = await propose(app_engine, people.tenant, people.owner)

    await decide_approval(
        app_engine, as_user(people, people.approver), approval_id, "reject", jobs=FakeJobQueue()
    )

    assert await _self_approved_marks(app_engine, people.tenant, approval_id) == (False, [False])


async def test_four_eyes_tenant_forbids_deciding_own_request(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, people: People
) -> None:
    await _four_eyes(migrator_engine, people.tenant)
    # The requester holds the approver role: only the four-eyes policy stops them.
    approval_id = await propose(app_engine, people.tenant, people.approver)
    jobs = FakeJobQueue()

    for decision in ("approve", "reject"):
        with pytest.raises(ApprovalForbidden, match="second person"):
            await decide_approval(
                app_engine, as_user(people, people.approver), approval_id, decision, jobs=jobs
            )

    await assert_untouched(app_engine, people, approval_id)
    assert jobs.jobs == []

    # Someone else with the approver role still can.
    outcome = await decide_approval(
        app_engine, as_user(people, people.second_approver), approval_id, "approve", jobs=jobs
    )
    assert outcome.status == "approved"
    assert await _self_approved_marks(app_engine, people.tenant, approval_id) == (False, [False])


async def test_approver_of_another_tenant_gets_not_found(
    app_engine: AsyncEngine, people: People
) -> None:
    approval_id = await propose(app_engine, people.tenant, people.owner)
    jobs = FakeJobQueue()
    other = as_user(people, people.other_approver, people.other_tenant)

    with pytest.raises(ApprovalNotFound):
        await decide_approval(app_engine, other, approval_id, "approve", jobs=jobs)

    await assert_untouched(app_engine, people, approval_id)
    assert jobs.jobs == []


async def test_non_member_claiming_the_tenant_is_forbidden(
    app_engine: AsyncEngine, people: People
) -> None:
    # Tenant B's approver presented with tenant A: no membership in A, no decision.
    approval_id = await propose(app_engine, people.tenant, people.owner)

    with pytest.raises(ApprovalForbidden):
        await decide_approval(
            app_engine,
            as_user(people, people.other_approver),
            approval_id,
            "approve",
            jobs=FakeJobQueue(),
        )

    await assert_untouched(app_engine, people, approval_id)


async def test_unknown_approval_is_not_found(app_engine: AsyncEngine, people: People) -> None:
    with pytest.raises(ApprovalNotFound):
        await decide_approval(
            app_engine, as_user(people, people.approver), uuid4(), "approve", jobs=FakeJobQueue()
        )


async def test_tampered_args_are_refused(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, people: People
) -> None:
    approval_id = await propose(app_engine, people.tenant, people.owner)
    await tamper(migrator_engine, people.tenant, approval_id)
    jobs = FakeJobQueue()

    for decision in ("approve", "reject"):
        with pytest.raises(ApprovalIntegrityError):
            await decide_approval(
                app_engine, as_user(people, people.approver), approval_id, decision, jobs=jobs
            )

    await assert_untouched(app_engine, people, approval_id)
    assert jobs.jobs == []
    actions = await audit_actions(app_engine, people.tenant, approval_id)
    assert actions == [
        "approval.requested",
        "approval.integrity_failed",
        "approval.integrity_failed",
    ]


async def test_approve_enqueues_and_the_worker_executes_exactly_once(
    app_engine: AsyncEngine, people: People
) -> None:
    approval_id = await propose(app_engine, people.tenant, people.owner)
    jobs = FakeJobQueue()

    outcome = await decide_approval(
        app_engine, as_user(people, people.approver), approval_id, "approve", jobs=jobs
    )

    assert outcome == DecisionOutcome(approval_id=approval_id, status="approved")
    approval = await load(app_engine, people.tenant, approval_id)
    assert approval.status == "approved"
    assert approval.decided_by == people.approver
    assert approval.decided_at is not None
    assert approval.executed_at is None

    async with tenant_session(app_engine, people.tenant) as session:
        audit = await session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "approval.approved",
                AuditEvent.payload["approval_id"].astext == str(approval_id),
            )
        )
    assert audit is not None
    assert audit.actor_user_id == people.approver
    # Ids and the hash only: never the arguments.
    assert audit.payload == {
        "approval_id": str(approval_id),
        "tool": "pay_supplier",
        "args_hash": approval.args_hash,
        "self_approved": False,
    }

    assert len(jobs.jobs) == 1
    job = jobs.jobs[0]
    assert job.type == APPROVAL_EXECUTE_JOB
    assert job.tenant_id == people.tenant
    assert job.idempotency_key == f"approval:{approval_id}"
    assert job.payload == {"approval_id": str(approval_id)}

    pay = PaySupplier()
    executor = ApprovalExecutor(app_engine, pay.registry())
    await executor(job)
    await executor(job)  # a redelivery is a no-op

    assert len(pay.calls) == 1
    ctx, args = pay.calls[0]
    assert args.model_dump(mode="json") == PAY_ARGS
    assert ctx.tenant_id == people.tenant
    assert ctx.user_id == people.approver
    assert str(ctx.run_id) == approval.run_id

    executed = await load(app_engine, people.tenant, approval_id)
    assert executed.status == "executed"
    assert executed.executed_at is not None
    assert await audit_actions(app_engine, people.tenant, approval_id) == [
        "approval.requested",
        "approval.approved",
        "approval.executed",
    ]


async def test_reject_records_the_decision_and_never_executes(
    app_engine: AsyncEngine, people: People
) -> None:
    approval_id = await propose(app_engine, people.tenant, people.owner)
    jobs = FakeJobQueue()

    outcome = await decide_approval(
        app_engine, as_user(people, people.approver), approval_id, "reject", jobs=jobs
    )

    assert outcome.status == "rejected"
    assert jobs.jobs == []
    approval = await load(app_engine, people.tenant, approval_id)
    assert (approval.status, approval.decided_by) == ("rejected", people.approver)
    assert await audit_actions(app_engine, people.tenant, approval_id) == [
        "approval.requested",
        "approval.rejected",
    ]

    # A forged execution job for the rejected approval is refused.
    pay = PaySupplier()
    with pytest.raises(ApprovalNotExecutable):
        await ApprovalExecutor(app_engine, pay.registry())(
            approval_execute_envelope(people.tenant, approval_id)
        )
    assert pay.calls == []


async def test_a_decision_is_made_exactly_once(app_engine: AsyncEngine, people: People) -> None:
    approval_id = await propose(app_engine, people.tenant, people.owner)
    jobs = FakeJobQueue()
    await decide_approval(
        app_engine, as_user(people, people.approver), approval_id, "approve", jobs=jobs
    )

    # A replay by the same approver, and a later reject by another one, change nothing.
    replays: list[tuple[UUID, Decision]] = [
        (people.approver, "approve"),
        (people.second_approver, "reject"),
    ]
    for user, decision in replays:
        with pytest.raises(ApprovalNotPending):
            await decide_approval(
                app_engine, as_user(people, user), approval_id, decision, jobs=jobs
            )

    approval = await load(app_engine, people.tenant, approval_id)
    assert (approval.status, approval.decided_by) == ("approved", people.approver)
    assert len(jobs.jobs) == 1


async def test_concurrent_decisions_have_one_winner(
    app_engine: AsyncEngine, people: People
) -> None:
    approval_id = await propose(app_engine, people.tenant, people.member)
    jobs = FakeJobQueue()

    results = await asyncio.gather(
        decide_approval(
            app_engine, as_user(people, people.approver), approval_id, "approve", jobs=jobs
        ),
        decide_approval(
            app_engine, as_user(people, people.owner), approval_id, "reject", jobs=jobs
        ),
        return_exceptions=True,
    )

    outcomes = [r for r in results if isinstance(r, DecisionOutcome)]
    refusals = [r for r in results if isinstance(r, ApprovalNotPending)]
    assert (len(outcomes), len(refusals)) == (1, 1)
    approval = await load(app_engine, people.tenant, approval_id)
    assert approval.status == outcomes[0].status
    assert len(jobs.jobs) == (1 if approval.status == "approved" else 0)
