"""Prove that RLS isolates tenants for the app role, and fails closed."""

from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.db import TenantContextMissing, tenant_session
from bido_back.db.models import AuditEvent, Membership, Tenant

from .conftest import TwoTenants

pytestmark = pytest.mark.db

_TENANT_TABLES = ("tenants", "memberships", "audit_events")


async def test_tenant_sees_only_its_own_rows(
    app_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    a, b = two_tenants.a, two_tenants.b
    async with tenant_session(app_engine, a.tenant_id) as session:
        memberships = (await session.scalars(select(Membership))).all()
        tenants = (await session.scalars(select(Tenant))).all()

    assert [m.id for m in memberships] == [a.membership_id]
    assert {m.tenant_id for m in memberships} == {a.tenant_id}
    assert [t.id for t in tenants] == [a.tenant_id]
    assert b.membership_id not in {m.id for m in memberships}


async def test_no_tenant_context_sees_zero_rows(
    app_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    async with app_engine.connect() as conn:
        for table in _TENANT_TABLES:
            count = await conn.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            assert count == 0, f"{table} visible without a tenant"

        # An empty setting (what a finished transaction leaves behind) is
        # also "no tenant", not an error that might be caught and ignored.
        await conn.execute(text("SELECT set_config('app.tenant_id', '', true)"))
        count = await conn.scalar(text("SELECT count(*) FROM memberships"))
        assert count == 0


async def test_tenant_session_requires_a_tenant(app_engine: AsyncEngine) -> None:
    with pytest.raises(TenantContextMissing):
        async with tenant_session(app_engine, None):
            pytest.fail("tenant_session yielded without a tenant")


async def test_cannot_insert_row_for_another_tenant(
    app_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    a, b = two_tenants.a, two_tenants.b
    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_session(app_engine, a.tenant_id) as session:
            session.add(AuditEvent(tenant_id=b.tenant_id, action="test.cross_tenant"))
            await session.flush()


async def test_cannot_move_row_to_another_tenant(
    app_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    a, b = two_tenants.a, two_tenants.b
    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_session(app_engine, a.tenant_id) as session:
            await session.execute(
                update(Membership)
                .where(Membership.id == a.membership_id)
                .values(tenant_id=b.tenant_id)
            )


async def test_no_carry_over_between_tenants_on_one_connection(
    single_connection_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    a, b = two_tenants.a, two_tenants.b
    engine = single_connection_engine
    pid_query = text("SELECT pg_backend_pid()")

    async with tenant_session(engine, a.tenant_id) as session:
        pid_a = await session.scalar(pid_query)
        seen_a = set((await session.scalars(select(Membership.tenant_id))).all())

    async with tenant_session(engine, b.tenant_id) as session:
        pid_b = await session.scalar(pid_query)
        seen_b = set((await session.scalars(select(Membership.tenant_id))).all())

    # A failed transaction must not leave its tenant behind either.
    with pytest.raises(RuntimeError, match="boom"):
        async with tenant_session(engine, a.tenant_id):
            raise RuntimeError("boom")

    async with engine.connect() as conn:
        pid_after = await conn.scalar(pid_query)
        leftover = await conn.scalar(text("SELECT current_setting('app.tenant_id', true)"))
        visible = await conn.scalar(text("SELECT count(*) FROM memberships"))

    assert pid_a == pid_b == pid_after, "test needs a single physical connection"
    assert seen_a == {a.tenant_id}
    assert seen_b == {b.tenant_id}
    assert leftover in (None, "")
    assert visible == 0


async def test_audit_events_are_append_only_for_app_role(
    app_engine: AsyncEngine, two_tenants: TwoTenants
) -> None:
    a = two_tenants.a
    async with tenant_session(app_engine, a.tenant_id) as session:
        event = AuditEvent(tenant_id=a.tenant_id, actor_user_id=a.user_id, action="test.created")
        session.add(event)
        await session.flush()
        event_id = event.id

    for statement in (
        text("UPDATE audit_events SET action = 'tampered' WHERE id = :id"),
        text("DELETE FROM audit_events WHERE id = :id"),
    ):
        with pytest.raises(DBAPIError, match="permission denied"):
            async with tenant_session(app_engine, a.tenant_id) as session:
                await session.execute(statement, {"id": event_id})

    async with tenant_session(app_engine, a.tenant_id) as session:
        action = await session.scalar(select(AuditEvent.action).where(AuditEvent.id == event_id))
    assert action == "test.created"


async def test_app_role_cannot_create_tables(app_engine: AsyncEngine) -> None:
    table = f"rls_probe_{uuid4().hex[:8]}"
    with pytest.raises(DBAPIError, match="permission denied"):
        async with app_engine.begin() as conn:
            await conn.execute(text(f"CREATE TABLE public.{table} (id int)"))


async def test_app_role_attributes(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
        owned = await conn.scalar(
            text("SELECT count(*) FROM pg_tables WHERE tableowner = current_user")
        )
    assert tuple(row) == (False, False, False, False)
    assert owned == 0
