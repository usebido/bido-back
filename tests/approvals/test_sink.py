"""PostgresApprovalSink: the runner's proposal becomes a pending row with the same hash."""

from typing import cast
from uuid import uuid4

import pytest
from pydantic import JsonValue
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.context import ToolContext
from bido_back.agents.runner import Failed, NeedsApproval, run_to_completion
from bido_back.approvals import (
    ApprovalConflict,
    ApprovalIntegrityError,
    ApprovalRequesterMissing,
    PostgresApprovalSink,
)
from bido_back.approvals.integrity import payload_matches
from bido_back.db import tenant_session
from bido_back.db.models import Approval
from tests.agents.test_runner import (
    CONFIG,
    TASK,
    StubOpenAI,
    agent,
    install_stub,
    reply,
    send_calls,
    tool_use,
)

from .conftest import People, audit_actions, load, proposal


@pytest.fixture
def scripted_model(monkeypatch: pytest.MonkeyPatch) -> StubOpenAI:
    """The model proposes one send_payment call (an act tool)."""
    stub = StubOpenAI(
        [reply(tool_use("fc_1", "send_payment", supplier_id="sup_9", amount_cents=50_000))]
    )
    install_stub(monkeypatch, stub)
    return stub


@pytest.mark.db
async def test_runner_and_sink_record_a_pending_row_matching_the_event(
    app_engine: AsyncEngine, people: People, scripted_model: StubOpenAI
) -> None:
    context = ToolContext(tenant_id=people.tenant, user_id=people.member, run_id=uuid4())

    result = await run_to_completion(
        agent(),
        TASK,
        context,
        session="rfq-7",
        approvals=PostgresApprovalSink(app_engine),
        config=CONFIG,
    )

    assert isinstance(result, NeedsApproval)
    assert send_calls == []
    event = result.approval
    async with tenant_session(app_engine, people.tenant) as session:
        row = await session.scalar(
            select(Approval).where(
                Approval.run_id == str(context.run_id), Approval.tool_call_id == event.call_id
            )
        )
    assert row is not None
    assert row.status == "pending"
    assert row.tool == event.tool == "send_payment"
    assert row.args == event.args
    assert row.args_hash == event.args_hash
    assert payload_matches(row)
    assert row.requested_by == people.member
    assert row.session_id == "rfq-7"
    assert (row.decided_by, row.decided_at, row.executed_at) == (None, None, None)
    assert await audit_actions(app_engine, people.tenant, row.id) == ["approval.requested"]


@pytest.mark.db
async def test_background_run_cannot_request_approval(
    app_engine: AsyncEngine, people: People, scripted_model: StubOpenAI
) -> None:
    context = ToolContext(tenant_id=people.tenant, user_id=None, run_id=uuid4())

    result = await run_to_completion(
        agent(), TASK, context, approvals=PostgresApprovalSink(app_engine), config=CONFIG
    )

    assert isinstance(result, Failed)
    assert result.failure.code == "approval_not_recorded"
    async with tenant_session(app_engine, people.tenant) as session:
        count = await session.scalar(
            select(func.count()).where(Approval.run_id == str(context.run_id))
        )
    assert count == 0

    with pytest.raises(ApprovalRequesterMissing):
        await PostgresApprovalSink(app_engine).request_approval(proposal(people.tenant, None))


@pytest.mark.db
async def test_reproposal_is_idempotent_but_a_changed_payload_is_refused(
    app_engine: AsyncEngine, people: People
) -> None:
    sink = PostgresApprovalSink(app_engine)
    first = proposal(people.tenant, people.member)
    await sink.request_approval(first)
    await sink.request_approval(first)  # a retried run records nothing new

    changed = first.model_copy(
        update={
            "args": {"supplier_id": "sup_1", "amount_cents": 1},
            "args_hash": proposal(
                people.tenant, people.member, {"supplier_id": "sup_1", "amount_cents": 1}
            ).args_hash,
        }
    )
    with pytest.raises(ApprovalConflict):
        await sink.request_approval(changed)

    async with tenant_session(app_engine, people.tenant) as session:
        rows = (
            await session.scalars(select(Approval).where(Approval.run_id == str(first.run_id)))
        ).all()
    assert [r.args_hash for r in rows] == [first.args_hash]
    assert await audit_actions(app_engine, people.tenant, rows[0].id) == ["approval.requested"]


@pytest.mark.db
async def test_stored_args_still_match_their_hash_after_jsonb(
    app_engine: AsyncEngine, people: People
) -> None:
    # jsonb reorders keys and normalizes spacing; canonical JSON must not care.
    args: dict[str, JsonValue] = {
        "zeta": 'Grüße, 東京   "quoted"',
        "alpha": [1, 2.5, 0.1, 0.0, 1e-07, None, True],
        "nested": {"b": {"y": 1, "x": 2}, "a": []},
        "big": 9_007_199_254_740_993,
        "price": 1250.0,
    }
    p = proposal(people.tenant, people.member, args)
    await PostgresApprovalSink(app_engine).request_approval(p)

    async with tenant_session(app_engine, people.tenant) as session:
        approval_id = await session.scalar(
            select(Approval.id).where(Approval.run_id == str(p.run_id))
        )
    assert approval_id is not None
    assert payload_matches(await load(app_engine, people.tenant, approval_id))


async def test_proposal_whose_hash_does_not_match_its_args_is_refused() -> None:
    p = proposal(uuid4(), uuid4())
    forged = p.model_copy(update={"args": {**p.args, "amount_cents": 1}})
    # No database needed: the sink checks before connecting.
    with pytest.raises(ApprovalIntegrityError):
        await PostgresApprovalSink(cast(AsyncEngine, None)).request_approval(forged)


@pytest.mark.db
@pytest.mark.parametrize("value", [-0.0, 1e20])
async def test_args_that_jsonb_rewrites_are_refused_at_proposal_time(
    app_engine: AsyncEngine, people: People, value: float
) -> None:
    # jsonb stores -0.0 as 0.0 and 1e+20 as 100000000000000000000: the hash
    # would never match again, so no one is asked to approve it.
    p = proposal(people.tenant, people.member, {"supplier_id": "sup_1", "factor": value})

    with pytest.raises(ApprovalIntegrityError, match="survive storage"):
        await PostgresApprovalSink(app_engine).request_approval(p)

    async with tenant_session(app_engine, people.tenant) as session:
        count = await session.scalar(select(func.count()).where(Approval.run_id == str(p.run_id)))
    assert count == 0
