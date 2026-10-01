"""Fixtures for tests that need a real Postgres with migrations applied.

Two engines:
  * app engine (DATABASE_URL, role bido_app) - the role under test.
  * migrator engine (MIGRATION_DATABASE_URL, role bido_migrator) - used only
    for cleanup and catalog checks.

If either URL is missing or the database is unreachable, every test that
uses these fixtures is skipped, so `pytest` stays green on a laptop without
`docker compose up db`. In CI (the `CI` env var, set by GitHub Actions) the
same condition FAILS instead: a skipped leak test must never pass a build.
"""

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import NoReturn
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bido_back.db import tenant_session
from bido_back.db.models import AuditEvent, Membership, Tenant, User
from bido_back.settings import Settings, get_settings

_CONNECT_TIMEOUT_S = 3
_IN_CI = os.environ.get("CI", "").lower() in ("1", "true", "yes")


def _unavailable(reason: str) -> NoReturn:
    if _IN_CI:
        pytest.fail(f"db tests cannot run in CI: {reason}")
    pytest.skip(f"skipping db tests: {reason}")


def _settings_or_skip() -> Settings:
    try:
        settings = get_settings()
    except ValidationError:
        _unavailable("DATABASE_URL not set")
    if settings.migration_database_url is None:
        _unavailable("MIGRATION_DATABASE_URL not set")
    return settings


async def _engine_or_skip(url: str, **kwargs: int) -> AsyncEngine:
    engine = create_async_engine(url, connect_args={"timeout": _CONNECT_TIMEOUT_S}, **kwargs)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1 FROM tenants LIMIT 0"))
    except Exception as exc:  # noqa: BLE001 - any failure means "no usable db"
        await engine.dispose()
        _unavailable(f"database not reachable or not migrated: {exc!r}")
    return engine


@pytest_asyncio.fixture(scope="session")
async def app_engine() -> AsyncIterator[AsyncEngine]:
    settings = _settings_or_skip()
    engine = await _engine_or_skip(str(settings.database_url))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def migrator_engine() -> AsyncIterator[AsyncEngine]:
    settings = _settings_or_skip()
    engine = await _engine_or_skip(str(settings.migration_database_url))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def single_connection_engine(app_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """App-role engine whose pool holds exactly one connection."""
    engine = create_async_engine(app_engine.url, pool_size=1, max_overflow=0)
    yield engine
    await engine.dispose()


@dataclass(frozen=True)
class TenantFixture:
    tenant_id: UUID
    user_id: UUID
    membership_id: UUID


@dataclass(frozen=True)
class TwoTenants:
    a: TenantFixture
    b: TenantFixture


async def _create_tenant(engine: AsyncEngine, label: str) -> TenantFixture:
    """Create a tenant, a user and a membership as the app role."""
    tenant_id = uuid4()
    suffix = uuid4().hex
    async with tenant_session(engine, tenant_id) as session:
        tenant = Tenant(id=tenant_id, name=f"test-{label}-{suffix}")
        user = User(clerk_user_id=f"user_test_{label}_{suffix}", email=f"{label}@example.test")
        session.add_all([tenant, user])
        await session.flush()
        membership = Membership(tenant_id=tenant_id, user_id=user.id, role="owner")
        session.add(membership)
        await session.flush()
        return TenantFixture(tenant_id=tenant_id, user_id=user.id, membership_id=membership.id)


async def _cleanup(migrator_engine: AsyncEngine, tenants: list[TenantFixture]) -> None:
    """Delete test rows as the migrator.

    RLS is FORCEd, so even the owning migrator must set each tenant to see
    (and delete) its rows.
    """
    for t in tenants:
        async with tenant_session(migrator_engine, t.tenant_id) as session:
            await session.execute(delete(AuditEvent).where(AuditEvent.tenant_id == t.tenant_id))
            await session.execute(delete(Membership).where(Membership.tenant_id == t.tenant_id))
            await session.execute(delete(Tenant).where(Tenant.id == t.tenant_id))
    async with migrator_engine.begin() as conn:
        await conn.execute(delete(User).where(User.id.in_([t.user_id for t in tenants])))


@pytest_asyncio.fixture
async def two_tenants(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine
) -> AsyncIterator[TwoTenants]:
    created: list[TenantFixture] = []
    try:
        created.append(await _create_tenant(app_engine, "a"))
        created.append(await _create_tenant(app_engine, "b"))
        yield TwoTenants(a=created[0], b=created[1])
    finally:
        await _cleanup(migrator_engine, created)
