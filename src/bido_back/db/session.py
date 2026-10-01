"""Engine construction and the one way to get a tenant-scoped session.

Tenant isolation is enforced by Postgres RLS. Every tenant-owned table has a
policy comparing `tenant_id` to the `app.tenant_id` setting, so the database
only needs to know which tenant the current unit of work belongs to. This
module is the single place that tells it.

Why transaction-local (`set_config(..., true)`) and never plain `SET`:
    Connections are pooled and reused across requests, tenants and worker
    jobs. A session-level `SET app.tenant_id` survives on the connection after
    it goes back to the pool, so the next borrower would silently run as the
    previous tenant. A transaction-local setting is discarded by Postgres at
    COMMIT or ROLLBACK, so there is nothing to forget to reset: once the
    transaction ends, the connection has no tenant and every RLS-protected
    query returns zero rows (fail closed).

Why there is no global/default session:
    A module-level session or an ambient "current tenant" makes it possible to
    query without having decided which tenant the work is for. Requiring the
    tenant as an argument on every `tenant_session` call keeps that decision
    explicit and reviewable at each call site, for the API and the worker
    alike.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bido_back.db.tenant import TenantContextMissing
from bido_back.settings import Settings

_SET_TENANT = text("SELECT set_config('app.tenant_id', :tenant_id, true)")


def create_engine(
    settings: Settings,
    *,
    pool_size: int = 5,
    max_overflow: int = 5,
) -> AsyncEngine:
    """Create the app-role engine (asyncpg) from `settings.database_url`.

    The pool is small on purpose: each API task and worker gets its own, and
    the database's connection budget is shared across all of them.
    """
    return create_async_engine(
        str(settings.database_url),
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def tenant_session(
    bind: AsyncEngine | async_sessionmaker[AsyncSession],
    tenant_id: UUID | None,
) -> AsyncGenerator[AsyncSession]:
    """Open a transaction scoped to `tenant_id` and yield its session.

    The tenant is set with `set_config('app.tenant_id', ..., true)` as the
    first statement of the transaction. The block commits on normal exit and
    rolls back on any exception; either way the setting disappears with the
    transaction.

    Use `session.flush()` inside the block, not `session.commit()`: a commit
    ends the tenant-scoped transaction, and anything after it would run in a
    new transaction with no tenant (and therefore see no rows).

    Raises:
        TenantContextMissing: if `tenant_id` is None.
    """
    if tenant_id is None:
        raise TenantContextMissing("tenant_session requires a tenant_id")
    if not isinstance(tenant_id, UUID):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(f"tenant_id must be a UUID, got {type(tenant_id).__name__}")

    sessionmaker = bind if isinstance(bind, async_sessionmaker) else create_sessionmaker(bind)
    async with sessionmaker() as session, session.begin():
        await session.execute(_SET_TENANT, {"tenant_id": str(tenant_id)})
        yield session
