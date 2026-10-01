"""Fixtures for the approval guard tests.

The database fixtures come from tests/db/conftest.py, with the same rule: on
a laptop without Postgres the db tests are skipped, in CI they fail.

`people` gives tenant A one user per role (the fixture's owner, two
approvers, a member) and tenant B one approver.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest_asyncio
from pydantic import BaseModel, JsonValue
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.approvals import ApprovalProposal
from bido_back.agents.context import ToolContext
from bido_back.agents.events import args_hash
from bido_back.agents.tools import Tool
from bido_back.approvals import ActToolRegistry, PostgresApprovalSink
from bido_back.db import tenant_session
from bido_back.db.models import Approval, AuditEvent, Membership, User
from bido_back.worker.envelope import Envelope
from tests.db.conftest import TwoTenants, app_engine, migrator_engine, two_tenants

__all__ = ["app_engine", "migrator_engine", "two_tenants"]

PAY_ARGS: dict[str, JsonValue] = {"supplier_id": "sup_1", "amount_cents": 125_000}


@dataclass(frozen=True)
class People:
    tenant: UUID
    owner: UUID
    approver: UUID
    second_approver: UUID
    member: UUID
    other_tenant: UUID
    other_approver: UUID


async def _add_user(engine: AsyncEngine, tenant_id: UUID, role: str) -> UUID:
    async with tenant_session(engine, tenant_id) as session:
        suffix = uuid4().hex
        user = User(clerk_user_id=f"user_test_{role}_{suffix}", email=f"{role}@example.test")
        session.add(user)
        await session.flush()
        session.add(Membership(tenant_id=tenant_id, user_id=user.id, role=role))
        await session.flush()
        return user.id


@pytest_asyncio.fixture
async def people(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, two_tenants: TwoTenants
) -> AsyncIterator[People]:
    a, b = two_tenants.a.tenant_id, two_tenants.b.tenant_id
    created: list[UUID] = []
    try:
        for tenant_id, role in ((a, "approver"), (a, "approver"), (a, "member"), (b, "approver")):
            created.append(await _add_user(app_engine, tenant_id, role))
        yield People(
            tenant=a,
            owner=two_tenants.a.user_id,
            approver=created[0],
            second_approver=created[1],
            member=created[2],
            other_tenant=b,
            other_approver=created[3],
        )
    finally:
        # Rows referencing these users go first; two_tenants cleans up the rest.
        for tenant_id in (a, b):
            async with tenant_session(migrator_engine, tenant_id) as session:
                await session.execute(delete(Approval).where(Approval.tenant_id == tenant_id))
                await session.execute(delete(AuditEvent).where(AuditEvent.tenant_id == tenant_id))
                await session.execute(delete(Membership).where(Membership.user_id.in_(created)))
        async with migrator_engine.begin() as conn:
            await conn.execute(delete(User).where(User.id.in_(created)))


def proposal(
    tenant_id: UUID,
    requested_by: UUID | None,
    args: dict[str, JsonValue] | None = None,
    *,
    tool: str = "pay_supplier",
    run_id: UUID | None = None,
    tool_call_id: str | None = None,
) -> ApprovalProposal:
    payload = PAY_ARGS if args is None else args
    run = run_id or uuid4()
    return ApprovalProposal(
        tenant_id=tenant_id,
        run_id=run,
        session_id=f"session-{run}",
        tool_call_id=tool_call_id or f"call_{uuid4().hex}",
        tool=tool,
        args=payload,
        args_hash=args_hash(payload),
        requested_by=requested_by,
    )


async def propose(
    engine: AsyncEngine,
    tenant_id: UUID,
    requested_by: UUID,
    args: dict[str, JsonValue] | None = None,
    *,
    tool: str = "pay_supplier",
) -> UUID:
    """Record a pending approval through the real sink and return its id."""
    p = proposal(tenant_id, requested_by, args, tool=tool)
    await PostgresApprovalSink(engine).request_approval(p)
    async with tenant_session(engine, tenant_id) as session:
        approval_id = await session.scalar(
            select(Approval.id).where(
                Approval.run_id == str(p.run_id), Approval.tool_call_id == p.tool_call_id
            )
        )
    assert approval_id is not None
    return approval_id


async def load(engine: AsyncEngine, tenant_id: UUID, approval_id: UUID) -> Approval:
    async with tenant_session(engine, tenant_id) as session:
        approval = await session.scalar(select(Approval).where(Approval.id == approval_id))
    assert approval is not None
    return approval


async def audit_actions(engine: AsyncEngine, tenant_id: UUID, approval_id: UUID) -> list[str]:
    async with tenant_session(engine, tenant_id) as session:
        events = await session.scalars(
            select(AuditEvent)
            .where(AuditEvent.payload["approval_id"].astext == str(approval_id))
            .order_by(AuditEvent.created_at, AuditEvent.id)
        )
        return [e.action for e in events]


async def tamper(migrator_engine: AsyncEngine, tenant_id: UUID, approval_id: UUID) -> None:
    """Rewrite the stored args behind the app's back (the app role cannot)."""
    async with tenant_session(migrator_engine, tenant_id) as session:
        approval = await session.scalar(select(Approval).where(Approval.id == approval_id))
        assert approval is not None
        approval.args = {**approval.args, "amount_cents": 999_999_999}
        await session.flush()


class FakeJobQueue:
    def __init__(self) -> None:
        self.jobs: list[Envelope] = []

    async def enqueue(self, envelope: Envelope) -> None:
        self.jobs.append(envelope)


# --- An act tool that records its calls --------------------------------------


class PayIn(BaseModel):
    supplier_id: str
    amount_cents: int


class PayOut(BaseModel):
    paid: bool


class PaySupplier:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[ToolContext, PayIn]] = []
        self.fail = fail

    async def __call__(self, ctx: ToolContext, args: PayIn) -> PayOut:
        self.calls.append((ctx, args))
        if self.fail:
            raise RuntimeError("payment provider unavailable")
        return PayOut(paid=True)

    def tool(self) -> Tool[PayIn, PayOut]:
        return Tool(
            name="pay_supplier",
            description="Pay a supplier.",
            input_model=PayIn,
            output_model=PayOut,
            kind="act",
            handler=self,
        )

    def registry(self) -> ActToolRegistry:
        return ActToolRegistry([self.tool()])
