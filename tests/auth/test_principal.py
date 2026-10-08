"""`get_principal`: credentials -> our user, tenant and role, over the ASGI transport.

The db tests use the real `memberships_for_clerk_user` function (migration
0005) as the app role.
"""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.api.deps import ApiResources
from bido_back.api.me import me_router
from bido_back.auth import AuthConfigError, memberships_for, resolve_principal
from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import Membership, User
from bido_back.settings import Settings, get_settings
from tests.db.conftest import TwoTenants, app_engine, migrator_engine, two_tenants

__all__ = ["app_engine", "migrator_engine", "two_tenants"]

UNUSED_DB = "postgresql+asyncpg://app@localhost:5432/unused"


def _app(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.state.resources = ApiResources(settings)
    app.include_router(me_router())
    return app


async def _get(app: FastAPI, path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        return await http.get(path, headers=headers or {})


def test_dev_mode_outside_local_refuses_to_build_the_app() -> None:
    settings = Settings.model_validate(
        {"environment": "test", "auth_dev_mode": True, "database_url": UNUSED_DB}
    )
    with pytest.raises(AuthConfigError):
        _app(settings)


async def test_missing_token_is_401() -> None:
    app = _app(Settings.model_validate({"environment": "test", "database_url": UNUSED_DB}))
    response = await _get(app, "/v1/me")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_dev_header_is_ignored_unless_dev_mode() -> None:
    app = _app(Settings.model_validate({"environment": "local", "database_url": UNUSED_DB}))
    response = await _get(app, "/v1/me", {"X-Dev-User": "user_dev_buyer"})

    assert response.status_code == 401
    assert response.json()["detail"] == "dev authentication is disabled"


async def test_bearer_without_clerk_configuration_is_503() -> None:
    app = _app(Settings.model_validate({"environment": "test", "database_url": UNUSED_DB}))
    response = await _get(app, "/v1/me", {"Authorization": "Bearer abc.def.ghi"})

    assert response.status_code == 503


@pytest.fixture
async def second_membership(
    app_engine: AsyncEngine, two_tenants: TwoTenants
) -> AsyncIterator[UUID]:
    """Make tenant A's owner also a member of tenant B; yield their user id."""
    user_id = two_tenants.a.user_id
    async with tenant_session(app_engine, two_tenants.b.tenant_id) as session:
        session.add(Membership(tenant_id=two_tenants.b.tenant_id, user_id=user_id, role="member"))
    yield user_id  # two_tenants deletes memberships per tenant


async def _clerk_id(engine: AsyncEngine, user_id: UUID) -> str:
    async with engine.connect() as conn:
        clerk_id = await conn.scalar(select(User.clerk_user_id).where(User.id == user_id))
    assert clerk_id is not None
    return clerk_id


@pytest.mark.db
async def test_lookup_returns_only_that_users_memberships(
    app_engine: AsyncEngine, two_tenants: TwoTenants, second_membership: UUID
) -> None:
    db = create_sessionmaker(app_engine)
    clerk_id = await _clerk_id(app_engine, second_membership)

    rows = await memberships_for(db, clerk_id)
    assert {(r.tenant_id, r.role) for r in rows} == {
        (two_tenants.a.tenant_id, "owner"),
        (two_tenants.b.tenant_id, "member"),
    }
    assert {r.user_id for r in rows} == {second_membership}
    assert await memberships_for(db, f"user_nobody_{uuid4().hex}") == []

    # The app role still cannot read memberships without a tenant.
    async with app_engine.connect() as conn:
        assert (await conn.execute(select(Membership))).all() == []


@pytest.mark.db
async def test_several_memberships_need_x_tenant_id(
    app_engine: AsyncEngine, two_tenants: TwoTenants, second_membership: UUID
) -> None:
    db = create_sessionmaker(app_engine)
    clerk_id = await _clerk_id(app_engine, second_membership)

    picked = await resolve_principal(db, clerk_id, str(two_tenants.b.tenant_id))
    assert (picked.tenant_id, picked.user_id, picked.role) == (
        two_tenants.b.tenant_id,
        second_membership,
        "member",
    )


@pytest.mark.db
async def test_dev_mode_resolves_the_seeded_user_end_to_end(
    app_engine: AsyncEngine, two_tenants: TwoTenants, second_membership: UUID
) -> None:
    settings = Settings.model_validate(
        {
            "environment": "local",
            "auth_dev_mode": True,
            "database_url": str(get_settings().database_url),
        }
    )
    app = _app(settings)
    resources: ApiResources = app.state.resources
    clerk_id = await _clerk_id(app_engine, second_membership)
    try:
        ambiguous = await _get(app, "/v1/me", {"X-Dev-User": clerk_id})
        chosen = await _get(
            app, "/v1/me", {"X-Dev-User": clerk_id, "X-Tenant-Id": str(two_tenants.b.tenant_id)}
        )
        foreign = await _get(app, "/v1/me", {"X-Dev-User": clerk_id, "X-Tenant-Id": str(uuid4())})
        unknown = await _get(app, "/v1/me", {"X-Dev-User": f"user_nobody_{uuid4().hex}"})
    finally:
        await resources.aclose()

    assert ambiguous.status_code == 400
    assert chosen.status_code == 200
    assert chosen.json()["tenant_id"] == str(two_tenants.b.tenant_id)
    assert chosen.json()["user_id"] == str(second_membership)
    assert chosen.json()["role"] == "member"
    assert foreign.status_code == 403
    assert unknown.status_code == 403
