"""Fixtures for the intake tests that need a real Postgres with migrations applied.

Same rule as tests/db/conftest.py: without DATABASE_URL / MIGRATION_DATABASE_URL
or a reachable, migrated database, `db` tests are skipped on a laptop and
FAIL in CI (the `CI` env var), so a skipped tenant-isolation test never passes
a build.

`TenantWorld` is one tenant with a user, a supplier (with `email_domain`), the
supplier's contact, an RFQ, a sent invite (reply token + thread Message-ID) and
one outbound follow-up message. Everything is created as the app role through
`tenant_session` and removed by the migrator afterwards.
"""

import os
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from bido_back.db import tenant_session
from bido_back.db.models import (
    Attachment,
    Contact,
    Invite,
    Membership,
    Message,
    Rfq,
    Supplier,
    SupplierContact,
    Tenant,
    User,
)
from bido_back.email.addresses import make_message_id, new_reply_token
from bido_back.settings import Settings, get_settings

EMAIL_DOMAIN = "quotes.example.test"
_IN_CI = os.environ.get("CI", "").lower() in ("1", "true", "yes")


def _unavailable(reason: str) -> NoReturn:
    if _IN_CI:
        pytest.fail(f"db tests cannot run in CI: {reason}")
    pytest.skip(f"skipping db tests: {reason}")


def _settings() -> Settings:
    try:
        settings = get_settings()
    except ValidationError:
        _unavailable("DATABASE_URL not set")
    if settings.migration_database_url is None:
        _unavailable("MIGRATION_DATABASE_URL not set")
    return settings


async def _engine(url: str) -> AsyncEngine:
    engine = create_async_engine(url, connect_args={"timeout": 3})
    try:
        async with engine.connect() as conn:
            # 0004 applied: the lookup functions exist.
            await conn.execute(text("SELECT 1 FROM resolve_invite_by_token('x') LIMIT 0"))
    except Exception as exc:  # noqa: BLE001 - any failure means "no usable db"
        await engine.dispose()
        _unavailable(f"database not reachable or not migrated: {exc!r}")
    return engine


@pytest_asyncio.fixture(scope="session")
async def app_engine() -> AsyncIterator[AsyncEngine]:
    engine = await _engine(str(_settings().database_url))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="session")
async def migrator_engine() -> AsyncIterator[AsyncEngine]:
    engine = await _engine(str(_settings().migration_database_url))
    yield engine
    await engine.dispose()


@dataclass(frozen=True)
class TenantWorld:
    tenant_id: UUID
    user_id: UUID
    supplier_id: UUID
    contact_id: UUID
    contact_email: str
    supplier_domain: str
    rfq_id: UUID
    rfq_ref: str
    invite_id: UUID
    reply_token: str
    thread_message_id: str
    followup_message_id: str


WorldFactory = Callable[..., Awaitable[TenantWorld]]


async def _create_world(
    engine: AsyncEngine,
    *,
    rfq_ref: str,
    supplier_domain: str | None,
    contact_email: str,
) -> TenantWorld:
    tenant_id, suffix = uuid4(), uuid4().hex
    ids = {name: uuid4() for name in ("user", "supplier", "contact", "rfq", "invite")}
    token = new_reply_token()
    thread_id, followup_id = make_message_id(EMAIL_DOMAIN), make_message_id(EMAIL_DOMAIN)
    async with tenant_session(engine, tenant_id) as session:
        session.add(Tenant(id=tenant_id, name=f"intake-test-{suffix}"))
        session.add(User(id=ids["user"], clerk_user_id=f"user_intake_{suffix}", email="b@x.test"))
        await session.flush()
        session.add(Membership(tenant_id=tenant_id, user_id=ids["user"], role="owner"))
        session.add(
            Supplier(
                id=ids["supplier"],
                tenant_id=tenant_id,
                name="Supplier",
                email_domain=supplier_domain,
            )
        )
        session.add(
            Contact(id=ids["contact"], tenant_id=tenant_id, kind="supplier", email=contact_email)
        )
        session.add(
            Rfq(
                id=ids["rfq"],
                tenant_id=tenant_id,
                ref=rfq_ref,
                title="M8 bolts",
                status="open",
                created_by=ids["user"],
            )
        )
        await session.flush()
        session.add(
            Invite(
                id=ids["invite"],
                tenant_id=tenant_id,
                rfq_id=ids["rfq"],
                supplier_id=ids["supplier"],
                contact_id=ids["contact"],
                reply_token=token,
                status="sent",
                thread_message_id=thread_id,
                sent_at=datetime.now(UTC),
            )
        )
        await session.flush()
        session.add(
            Message(
                tenant_id=tenant_id,
                invite_id=ids["invite"],
                direction="out",
                message_id_header=followup_id,
                subject="Reminder",
                occurred_at=datetime.now(UTC),
            )
        )
        await session.flush()
    return TenantWorld(
        tenant_id=tenant_id,
        user_id=ids["user"],
        supplier_id=ids["supplier"],
        contact_id=ids["contact"],
        contact_email=contact_email,
        supplier_domain=supplier_domain or "",
        rfq_id=ids["rfq"],
        rfq_ref=rfq_ref,
        invite_id=ids["invite"],
        reply_token=token,
        thread_message_id=thread_id,
        followup_message_id=followup_id,
    )


async def _cleanup(migrator: AsyncEngine, worlds: list[TenantWorld]) -> None:
    """Delete test rows as the migrator (RLS is FORCEd, so per tenant)."""
    for w in worlds:
        async with tenant_session(migrator, w.tenant_id) as session:
            for model in (Attachment, Message, Invite, Rfq, SupplierContact, Supplier, Contact):
                await session.execute(delete(model).where(model.tenant_id == w.tenant_id))
            await session.execute(delete(Membership).where(Membership.tenant_id == w.tenant_id))
            await session.execute(delete(Tenant).where(Tenant.id == w.tenant_id))
    async with migrator.begin() as conn:
        await conn.execute(delete(User).where(User.id.in_([w.user_id for w in worlds])))


def unique_ref() -> str:
    # Refs are unique per tenant only; a random number keeps tests independent
    # of other suites' RFQs in a shared database.
    return f"RFQ-{secrets.randbelow(10**12)}"


@pytest_asyncio.fixture
async def make_world(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine
) -> AsyncIterator[WorldFactory]:
    created: list[TenantWorld] = []

    async def factory(
        *,
        rfq_ref: str | None = None,
        supplier_domain: str | None = "supplier.test",
        contact_email: str | None = None,
    ) -> TenantWorld:
        world = await _create_world(
            app_engine,
            rfq_ref=rfq_ref or unique_ref(),
            supplier_domain=supplier_domain,
            contact_email=contact_email or f"sales-{uuid4().hex[:8]}@supplier.test",
        )
        created.append(world)
        return world

    try:
        yield factory
    finally:
        await _cleanup(migrator_engine, created)
