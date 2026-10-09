"""approval.execute: runs only approved, unchanged payloads, once, in the right tenant."""

import asyncio
import json
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, JsonValue
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bido_back.agents.context import ToolContext
from bido_back.agents.tools import Tool
from bido_back.approvals import (
    APPROVAL_EXECUTE_JOB,
    ActToolRegistry,
    ApprovalExecutor,
    ApprovalIntegrityError,
    ApprovalNotExecutable,
    ApprovalNotFound,
    InvalidApprovalJob,
    UnknownActTool,
    approval_execute_envelope,
    decide_approval,
)
from bido_back.auth import Principal
from bido_back.worker.envelope import Envelope
from bido_back.worker.main import QueueMessage, Worker, build_registry

from .conftest import (
    FakeJobQueue,
    PayIn,
    PayOut,
    PaySupplier,
    People,
    audit_actions,
    load,
    propose,
    tamper,
)


async def approved(engine: AsyncEngine, people: People, *, tool: str = "pay_supplier") -> UUID:
    approval_id = await propose(engine, people.tenant, people.owner, tool=tool)
    principal = Principal(tenant_id=people.tenant, user_id=people.approver)
    await decide_approval(engine, principal, approval_id, "approve", jobs=FakeJobQueue())
    return approval_id


# --- Database ----------------------------------------------------------------


@pytest.mark.db
async def test_pending_approval_is_not_executed(app_engine: AsyncEngine, people: People) -> None:
    approval_id = await propose(app_engine, people.tenant, people.owner)
    pay = PaySupplier()

    with pytest.raises(ApprovalNotExecutable, match="pending"):
        await ApprovalExecutor(app_engine, pay.registry())(
            approval_execute_envelope(people.tenant, approval_id)
        )

    assert pay.calls == []
    assert (await load(app_engine, people.tenant, approval_id)).status == "pending"


@pytest.mark.db
async def test_job_in_another_tenant_finds_nothing(app_engine: AsyncEngine, people: People) -> None:
    approval_id = await approved(app_engine, people)
    pay = PaySupplier()

    with pytest.raises(ApprovalNotFound):
        await ApprovalExecutor(app_engine, pay.registry())(
            approval_execute_envelope(people.other_tenant, approval_id)
        )

    assert pay.calls == []
    assert (await load(app_engine, people.tenant, approval_id)).status == "approved"


@pytest.mark.db
async def test_args_tampered_after_approval_are_not_executed(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, people: People
) -> None:
    approval_id = await approved(app_engine, people)
    await tamper(migrator_engine, people.tenant, approval_id)
    pay = PaySupplier()

    with pytest.raises(ApprovalIntegrityError):
        await ApprovalExecutor(app_engine, pay.registry())(
            approval_execute_envelope(people.tenant, approval_id)
        )

    assert pay.calls == []
    assert (await load(app_engine, people.tenant, approval_id)).status == "approved"
    actions = await audit_actions(app_engine, people.tenant, approval_id)
    assert actions[-1] == "approval.integrity_failed"


@pytest.mark.db
async def test_unknown_tool_fails_clearly(app_engine: AsyncEngine, people: People) -> None:
    approval_id = await approved(app_engine, people, tool="wire_money")

    with pytest.raises(UnknownActTool, match="wire_money"):
        await ApprovalExecutor(app_engine, ActToolRegistry())(
            approval_execute_envelope(people.tenant, approval_id)
        )

    assert (await load(app_engine, people.tenant, approval_id)).status == "approved"


class StrictPayIn(BaseModel):
    supplier_id: str
    amount_cents: int
    currency: str = "USD"


class PaidOut(BaseModel):
    paid: bool


@pytest.mark.db
async def test_tool_that_no_longer_reproduces_the_payload_is_refused(
    app_engine: AsyncEngine, people: People
) -> None:
    # The tool's input model gained a defaulted field after the proposal: what
    # would run is no longer byte-for-byte what was approved.
    approval_id = await approved(app_engine, people)
    calls: list[StrictPayIn] = []

    async def handler(ctx: ToolContext, args: StrictPayIn) -> PaidOut:
        calls.append(args)
        return PaidOut(paid=True)

    changed = Tool(
        name="pay_supplier",
        description="Pay a supplier.",
        input_model=StrictPayIn,
        output_model=PaidOut,
        kind="act",
        handler=handler,
    )

    with pytest.raises(ApprovalNotExecutable, match="reproduces"):
        await ApprovalExecutor(app_engine, ActToolRegistry([changed]))(
            approval_execute_envelope(people.tenant, approval_id)
        )
    assert calls == []


class OptionalMemoPayIn(BaseModel):
    supplier_id: str
    amount_cents: int
    # Added after the proposal, absent means "none": the approval still stands.
    memo: str | None = None


@pytest.mark.db
async def test_an_optional_field_added_after_the_proposal_keeps_the_approval_executable(
    app_engine: AsyncEngine, people: People
) -> None:
    # Approved before the deploy that added `memo` (email audit F19): its stored
    # args have no `memo`, and the executor must not refuse it.
    approval_id = await approved(app_engine, people)
    calls: list[OptionalMemoPayIn] = []

    async def handler(ctx: ToolContext, args: OptionalMemoPayIn) -> PaidOut:
        calls.append(args)
        return PaidOut(paid=True)

    widened = Tool(
        name="pay_supplier",
        description="Pay a supplier.",
        input_model=OptionalMemoPayIn,
        output_model=PaidOut,
        kind="act",
        handler=handler,
    )

    await ApprovalExecutor(app_engine, ActToolRegistry([widened]))(
        approval_execute_envelope(people.tenant, approval_id)
    )
    assert [(c.supplier_id, c.amount_cents, c.memo) for c in calls] == [("sup_1", 125_000, None)]
    assert (await load(app_engine, people.tenant, approval_id)).status == "executed"


@pytest.mark.db
async def test_failed_tool_rolls_back_and_the_retry_executes(
    app_engine: AsyncEngine, people: People
) -> None:
    approval_id = await approved(app_engine, people)
    job = approval_execute_envelope(people.tenant, approval_id)
    pay = PaySupplier(fail=True)
    executor = ApprovalExecutor(app_engine, pay.registry())

    with pytest.raises(RuntimeError):
        await executor(job)
    assert (await load(app_engine, people.tenant, approval_id)).status == "approved"

    pay.fail = False
    await executor(job)
    assert (await load(app_engine, people.tenant, approval_id)).status == "executed"
    assert len(pay.calls) == 2  # one failed attempt, one success


@pytest.mark.db
async def test_concurrent_duplicate_jobs_execute_once(
    app_engine: AsyncEngine, people: People
) -> None:
    approval_id = await approved(app_engine, people)
    job = approval_execute_envelope(people.tenant, approval_id)
    pay = PaySupplier()

    async def slow(ctx: ToolContext, args: PayIn) -> PayOut:
        await asyncio.sleep(0.2)  # hold the row lock while the duplicate arrives
        return await pay(ctx, args)

    slow_tool = Tool(
        name="pay_supplier",
        description="Pay a supplier.",
        input_model=PayIn,
        output_model=PayOut,
        kind="act",
        handler=slow,
    )
    executor = ApprovalExecutor(app_engine, ActToolRegistry([slow_tool]))

    await asyncio.gather(executor(job), executor(job))

    assert len(pay.calls) == 1
    assert (await load(app_engine, people.tenant, approval_id)).status == "executed"


@pytest.mark.db
async def test_worker_routes_the_job_to_the_executor(
    app_engine: AsyncEngine, people: People
) -> None:
    approval_id = await approved(app_engine, people)
    pay = PaySupplier()
    body = approval_execute_envelope(people.tenant, approval_id).model_dump_json()

    class OneMessage:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        async def receive(self, *, max_messages: int, wait_seconds: int) -> list[QueueMessage]:
            return [QueueMessage(message_id="m1", receipt_handle="rh", body=body)]

        async def delete(self, message: QueueMessage) -> None:
            self.deleted.append(message.message_id)

        async def park(self, message: QueueMessage, reason: str) -> None:
            raise AssertionError(reason)

    queue = OneMessage()
    outcomes = await Worker(queue, build_registry(app_engine, pay.registry())).run_once()

    assert outcomes == ["handled"]
    assert queue.deleted == ["m1"]
    assert len(pay.calls) == 1
    assert (await load(app_engine, people.tenant, approval_id)).status == "executed"


# --- No database -------------------------------------------------------------


class _Line(BaseModel):
    sku: str
    note: str | None = None


class _Branding(BaseModel):
    name: str
    scale: float = 1.0


class _OldArgs(BaseModel):
    to: list[str]
    lines: list[_Line]
    branding: _Branding | None = None


class _NewArgs(_OldArgs):
    # Added later: None-by-default fields (top level and nested) do not count when absent.
    html_body: str | None = None
    cc: list[str] | None = None


def test_the_executor_hash_ignores_only_absent_optional_fields() -> None:
    from bido_back.approvals.integrity import approved_payload

    stored: dict[str, JsonValue] = {
        "to": ["a@example.test"],
        "lines": [{"sku": "A"}, {"sku": "B", "note": None}],
        "branding": {"name": "Voltwerk", "scale": 1.0},
    }
    # The new model reads the old payload back to exactly the old payload.
    assert approved_payload(_NewArgs.model_validate(stored)) == stored
    # Present values, None included, always count.
    with_html: dict[str, JsonValue] = {**stored, "html_body": None}
    assert approved_payload(_NewArgs.model_validate(with_html)) == with_html
    # A new field with a real default would change what runs: it stays, so the hash differs.
    old_branding: dict[str, JsonValue] = {**stored, "branding": {"name": "Voltwerk"}}
    assert approved_payload(_NewArgs.model_validate(old_branding)) != old_branding
    assert approved_payload(_OldArgs.model_validate(stored)) == stored


def _job(**overrides: object) -> Envelope:
    approval_id = uuid4()
    base: dict[str, object] = {
        "type": APPROVAL_EXECUTE_JOB,
        "tenant_id": str(uuid4()),
        "idempotency_key": f"approval:{approval_id}",
        "payload": {"approval_id": str(approval_id)},
    }
    return Envelope.model_validate_json(json.dumps({**base, **overrides}))


@pytest.mark.parametrize(
    "overrides",
    [
        {"idempotency_key": "approval:someone-else"},
        {"payload": {"approval_id": "not-a-uuid", "extra": 1}},
        {"payload": {"approval_id": "not-a-uuid"}},
        {"payload": {}},
    ],
)
async def test_malformed_jobs_are_refused_before_the_database(
    overrides: dict[str, object],
) -> None:
    # Nothing listens on this URL: refusal happens before any connection is made.
    engine = create_async_engine("postgresql+asyncpg://unused@127.0.0.1:9/unused")
    try:
        with pytest.raises(InvalidApprovalJob):
            await ApprovalExecutor(engine, ActToolRegistry())(_job(**overrides))
    finally:
        await engine.dispose()


def test_act_tool_registry_accepts_only_unique_act_tools() -> None:
    pay = PaySupplier()
    registry = ActToolRegistry([pay.tool()])
    assert registry.get("pay_supplier") is not None
    assert registry.get("missing") is None

    with pytest.raises(ValueError, match="already registered"):
        registry.register(pay.tool())

    tool = pay.tool()
    read_tool = Tool(
        name="lookup",
        description="Read only.",
        input_model=tool.input_model,
        output_model=tool.output_model,
        kind="read",
        handler=tool.handler,
    )
    with pytest.raises(ValueError, match="not an act tool"):
        ActToolRegistry([read_tool])
