"""Approvals stay tenant-isolated and tamper-resistant; Agno stays in its schema."""

import hashlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.db import tenant_session
from bido_back.db.models import Approval, Membership, User

from .conftest import TwoTenants

pytestmark = pytest.mark.db


def _canonical_hash(args: dict[str, Any]) -> str:
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _approval(tenant_id: UUID, requested_by: UUID, **overrides: Any) -> Approval:
    args: dict[str, Any] = {"supplier_id": "sup_1", "amount_cents": 125_000}
    fields: dict[str, Any] = {
        "tenant_id": tenant_id,
        "run_id": f"run_{uuid4().hex}",
        "session_id": f"{tenant_id}:session_{uuid4().hex}",
        "tool_call_id": f"call_{uuid4().hex}",
        "tool": "send_purchase_order",
        "args": args,
        "args_hash": _canonical_hash(args),
        "requested_by": requested_by,
    }
    fields.update(overrides)
    return Approval(**fields)


@dataclass(frozen=True)
class Approvers:
    """One approver-role user per tenant, distinct from the tenant's owner."""

    a: UUID
    b: UUID


@pytest_asyncio.fixture
async def approvers(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, two_tenants: TwoTenants
) -> AsyncIterator[Approvers]:
    created: dict[UUID, UUID] = {}
    try:
        for t in (two_tenants.a, two_tenants.b):
            async with tenant_session(app_engine, t.tenant_id) as session:
                suffix = uuid4().hex
                user = User(clerk_user_id=f"user_test_approver_{suffix}", email="ap@example.test")
                session.add(user)
                await session.flush()
                session.add(Membership(tenant_id=t.tenant_id, user_id=user.id, role="approver"))
                await session.flush()
                created[t.tenant_id] = user.id
        yield Approvers(a=created[two_tenants.a.tenant_id], b=created[two_tenants.b.tenant_id])
    finally:
        # Approvals reference these users, so they go first (as the owner).
        for tenant_id, user_id in created.items():
            async with tenant_session(migrator_engine, tenant_id) as session:
                await session.execute(delete(Approval).where(Approval.tenant_id == tenant_id))
                await session.execute(delete(Membership).where(Membership.user_id == user_id))
        async with migrator_engine.begin() as conn:
            await conn.execute(delete(User).where(User.id.in_(list(created.values()))))


async def test_approvals_are_isolated_per_tenant(
    app_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    a, b = two_tenants.a, two_tenants.b
    async with tenant_session(app_engine, a.tenant_id) as session:
        approval_a = _approval(a.tenant_id, a.user_id)
        session.add(approval_a)
        await session.flush()
    async with tenant_session(app_engine, b.tenant_id) as session:
        approval_b = _approval(b.tenant_id, b.user_id)
        session.add(approval_b)
        await session.flush()

    async with tenant_session(app_engine, a.tenant_id) as session:
        seen = (await session.scalars(select(Approval))).all()
    assert [x.id for x in seen] == [approval_a.id]

    async with app_engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM approvals")) == 0

    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_session(app_engine, a.tenant_id) as session:
            session.add(_approval(b.tenant_id, b.user_id))
            await session.flush()


async def test_approval_lifecycle_by_another_member(
    app_engine: AsyncEngine, two_tenants: TwoTenants, approvers: Approvers
) -> None:
    a = two_tenants.a
    async with tenant_session(app_engine, a.tenant_id) as session:
        approval = _approval(a.tenant_id, a.user_id)
        session.add(approval)
        await session.flush()
        approval_id = approval.id

    now = datetime.now(UTC)
    async with tenant_session(app_engine, a.tenant_id) as session:
        await session.execute(
            update(Approval)
            .where(Approval.id == approval_id)
            .values(status="approved", decided_by=approvers.a, decided_at=now)
        )
        await session.execute(
            update(Approval)
            .where(Approval.id == approval_id)
            .values(status="executed", executed_at=now)
        )
        status = await session.scalar(select(Approval.status).where(Approval.id == approval_id))
    assert status == "executed"


async def test_requester_cannot_approve_own_request(
    app_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    a = two_tenants.a
    with pytest.raises(IntegrityError, match="approvals_not_self_approved_check"):
        async with tenant_session(app_engine, a.tenant_id) as session:
            session.add(
                _approval(
                    a.tenant_id,
                    a.user_id,
                    status="approved",
                    decided_by=a.user_id,
                    decided_at=datetime.now(UTC),
                )
            )
            await session.flush()


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"args_hash": "A" * 64}, "approvals_args_hash_check"),
        ({"args_hash": "ab" * 31}, "approvals_args_hash_check"),
        ({"status": "approved"}, "approvals_decision_check"),
        ({"status": "executed"}, "approvals_decision_check"),
        # Unknown statuses also fail the decision check; either name is fine.
        ({"status": "done"}, "approvals_(status|decision)_check"),
    ],
)
async def test_invalid_approval_rows_are_rejected(
    app_engine: AsyncEngine,
    two_tenants: TwoTenants,
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    a = two_tenants.a
    with pytest.raises(IntegrityError, match=constraint):
        async with tenant_session(app_engine, a.tenant_id) as session:
            session.add(_approval(a.tenant_id, a.user_id, **overrides))
            await session.flush()


async def test_app_role_cannot_delete_or_rewrite_payload(
    app_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    a = two_tenants.a
    async with tenant_session(app_engine, a.tenant_id) as session:
        approval = _approval(a.tenant_id, a.user_id)
        session.add(approval)
        await session.flush()
        approval_id, original_hash = approval.id, approval.args_hash

    for statement in (
        text("DELETE FROM approvals WHERE id = :id"),
        text("UPDATE approvals SET args = '{}'::jsonb WHERE id = :id"),
        text("UPDATE approvals SET args_hash = repeat('0', 64) WHERE id = :id"),
        text("UPDATE approvals SET requested_by = requested_by WHERE id = :id"),
    ):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with tenant_session(app_engine, a.tenant_id) as session:
                await session.execute(statement, {"id": approval_id})

    with pytest.raises(DBAPIError, match="permission denied"):
        async with app_engine.begin() as conn:
            await conn.execute(text("TRUNCATE approvals"))

    async with tenant_session(app_engine, a.tenant_id) as session:
        stored = await session.scalar(select(Approval.args_hash).where(Approval.id == approval_id))
    assert stored == original_hash


@pytest.mark.parametrize("table", ["memberships", "approvals", "tenants", "users"])
async def test_agno_role_cannot_read_business_tables(agno_engine: AsyncEngine, table: str) -> None:
    with pytest.raises(DBAPIError, match="permission denied"):
        async with agno_engine.connect() as conn:
            await conn.execute(text(f"SELECT 1 FROM public.{table} LIMIT 1"))  # noqa: S608


async def test_agno_role_is_confined_to_its_schema(agno_engine: AsyncEngine) -> None:
    async with agno_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, "
                    "has_schema_privilege('public', 'USAGE'), "
                    "has_schema_privilege('public', 'CREATE'), "
                    "has_database_privilege(current_database(), 'CREATE') "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
    assert tuple(row) == (False, False, False, False, False, False, False)

    with pytest.raises(DBAPIError, match="permission denied"):
        async with agno_engine.begin() as conn:
            await conn.execute(text("CREATE TABLE public.agno_probe (id int)"))


async def test_agno_role_can_create_tables_in_agno_schema(agno_engine: AsyncEngine) -> None:
    table = f"agno_probe_{uuid4().hex[:8]}"
    async with agno_engine.connect() as conn:
        trans = await conn.begin()
        try:
            # Unqualified: the role's search_path is `agno` only.
            await conn.execute(text(f"CREATE TABLE {table} (id int)"))
            await conn.execute(text(f"INSERT INTO {table} VALUES (1)"))  # noqa: S608
            schema = await conn.scalar(
                text("SELECT schemaname FROM pg_tables WHERE tablename = :t"), {"t": table}
            )
        finally:
            await trans.rollback()
    assert schema == "agno"


async def test_business_roles_cannot_access_agno_schema(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine
) -> None:
    for engine in (app_engine, migrator_engine):
        async with engine.connect() as conn:
            usage = await conn.scalar(text("SELECT has_schema_privilege('agno', 'USAGE')"))
            create = await conn.scalar(text("SELECT has_schema_privilege('agno', 'CREATE')"))
        assert (usage, create) == (False, False), engine.url.username

        with pytest.raises(DBAPIError, match="permission denied"):
            async with engine.begin() as conn:
                await conn.execute(text("CREATE TABLE agno.business_probe (id int)"))
