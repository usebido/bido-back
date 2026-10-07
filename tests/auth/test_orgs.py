"""Clerk organizations as companies: provisioning, linking, roles, races, isolation.

The db tests run as the app role against the real `tenant_for_clerk_org`
lookup (migration 0018) and clean up as the migrator.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.api.deps import ApiResources
from bido_back.api.me import me_router
from bido_back.auth import (
    ClerkJwtVerifier,
    ClerkOrg,
    OrganizationNotEnabled,
    VerifiedSession,
    memberships_for,
)
from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import AuditEvent, Membership, Tenant, User
from bido_back.orgs import (
    ClerkBackendApi,
    NoClerkDirectory,
    OrgPolicy,
    link_org,
    principal_for_org,
    tenant_for_org,
)
from bido_back.settings import Settings, get_settings
from tests.auth.test_jwt import ISSUER, PARTY, FakeJwks, Keys
from tests.db.conftest import app_engine, migrator_engine

__all__ = ["app_engine", "migrator_engine"]

AUTO = OrgPolicy(auto_provision=True)
CLOSED = OrgPolicy(auto_provision=False)


@dataclass
class FakeDirectory:
    names: dict[str, str] = field(default_factory=dict[str, str])
    emails: dict[str, str] = field(default_factory=dict[str, str])

    async def organization_name(self, org_id: str) -> str | None:
        return self.names.get(org_id)

    async def user_email(self, user_id: str) -> str | None:
        return self.emails.get(user_id)


@dataclass
class Created:
    """Everything a test provisions, deleted afterwards as the migrator."""

    org_ids: list[str] = field(default_factory=list[str])
    tenant_ids: list[UUID] = field(default_factory=list[UUID])
    clerk_user_ids: list[str] = field(default_factory=list[str])

    def org(self) -> str:
        org_id = f"org_test_{uuid4().hex}"
        self.org_ids.append(org_id)
        return org_id

    def user(self) -> str:
        user_id = f"user_test_{uuid4().hex}"
        self.clerk_user_ids.append(user_id)
        return user_id


@pytest_asyncio.fixture
async def created(app_engine: AsyncEngine, migrator_engine: AsyncEngine) -> AsyncIterator[Created]:
    record = Created()
    yield record
    db = create_sessionmaker(app_engine)
    tenant_ids = set(record.tenant_ids)
    for org_id in record.org_ids:
        found = await tenant_for_org(db, org_id)
        if found is not None:
            tenant_ids.add(found)
    for tenant_id in tenant_ids:
        async with tenant_session(migrator_engine, tenant_id) as session:
            await session.execute(delete(AuditEvent).where(AuditEvent.tenant_id == tenant_id))
            await session.execute(delete(Membership).where(Membership.tenant_id == tenant_id))
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
    async with migrator_engine.begin() as conn:
        await conn.execute(delete(User).where(User.clerk_user_id.in_(record.clerk_user_ids)))


def _session(
    user_id: str, org_id: str, role: str | None = "org:member", **kw: Any
) -> VerifiedSession:
    return VerifiedSession(user_id=user_id, org=ClerkOrg(id=org_id, role=role, slug="acme"), **kw)


async def _audit_actions(engine: AsyncEngine, tenant_id: UUID) -> list[str]:
    async with tenant_session(engine, tenant_id) as session:
        rows = await session.scalars(
            select(AuditEvent.action).where(AuditEvent.tenant_id == tenant_id)
        )
        return sorted(rows)


def _db(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(engine)


# --- Provisioning -------------------------------------------------------------------


@pytest.mark.db
async def test_first_login_creates_the_company_and_the_membership(
    app_engine: AsyncEngine, created: Created
) -> None:
    db = _db(app_engine)
    org_id, user_id = created.org(), created.user()
    directory = FakeDirectory(names={org_id: "Acme Motors S.p.A."})

    first = await principal_for_org(
        db, _session(user_id, org_id, "org:admin", email="a@acme.test"), AUTO, directory
    )
    again = await principal_for_org(
        db, _session(user_id, org_id, "org:admin"), AUTO, NoClerkDirectory()
    )

    assert first == again
    assert first.role == "owner"
    assert await tenant_for_org(db, org_id) == first.tenant_id
    async with tenant_session(app_engine, first.tenant_id) as session:
        tenant = await session.get(Tenant, first.tenant_id)
        user = await session.get(User, first.user_id)
    assert tenant is not None and tenant.name == "Acme Motors S.p.A."
    assert tenant.clerk_org_id == org_id
    # A provisioned company gets the default policy: the buyer approves their own.
    assert tenant.allow_self_approval is True
    assert user is not None and user.email == "a@acme.test"
    assert await _audit_actions(app_engine, first.tenant_id) == [
        "membership.provisioned",
        "tenant.provisioned",
    ]


@pytest.mark.db
async def test_company_name_falls_back_to_the_slug_and_email_to_empty(
    app_engine: AsyncEngine, created: Created
) -> None:
    db = _db(app_engine)
    principal = await principal_for_org(
        db, _session(created.user(), created.org()), AUTO, NoClerkDirectory()
    )
    async with tenant_session(app_engine, principal.tenant_id) as session:
        tenant = await session.get(Tenant, principal.tenant_id)
        user = await session.get(User, principal.user_id)
    assert tenant is not None and tenant.name == "acme"
    assert user is not None and user.email == ""
    assert principal.role == "member"


@pytest.mark.db
async def test_without_auto_provision_only_linked_organizations_get_in(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, created: Created
) -> None:
    db = _db(app_engine)
    org_id, user_id = created.org(), created.user()

    with pytest.raises(OrganizationNotEnabled):
        await principal_for_org(db, _session(user_id, org_id), CLOSED, NoClerkDirectory())
    assert await tenant_for_org(db, org_id) is None

    tenant_id = uuid4()
    created.tenant_ids.append(tenant_id)
    async with tenant_session(db, tenant_id) as session:
        session.add(Tenant(id=tenant_id, name="Linked S.p.A."))
    await link_org(_db(migrator_engine), tenant_id, org_id)

    principal = await principal_for_org(db, _session(user_id, org_id), CLOSED, NoClerkDirectory())
    assert principal.tenant_id == tenant_id
    assert principal.role == "member"


@pytest.mark.db
async def test_link_org_refuses_a_tenant_linked_elsewhere(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, created: Created
) -> None:
    db = _db(app_engine)
    operator = _db(migrator_engine)
    tenant_id = uuid4()
    created.tenant_ids.append(tenant_id)
    async with tenant_session(db, tenant_id) as session:
        session.add(Tenant(id=tenant_id, name="Once"))
    first = created.org()
    await link_org(operator, tenant_id, first)
    await link_org(operator, tenant_id, first)  # idempotent

    with pytest.raises(LookupError, match="another organization"):
        await link_org(operator, tenant_id, created.org())
    with pytest.raises(LookupError, match="not found"):
        await link_org(operator, uuid4(), created.org())
    # The app role can never write a link, even to an unlinked company.
    unlinked = uuid4()
    created.tenant_ids.append(unlinked)
    async with tenant_session(db, unlinked) as session:
        session.add(Tenant(id=unlinked, name="Unlinked"))
    with pytest.raises(ProgrammingError, match="permission denied"):
        await link_org(db, unlinked, created.org())


@pytest.mark.db
async def test_allowlist_restricts_even_with_auto_provision(
    app_engine: AsyncEngine, created: Created
) -> None:
    db = _db(app_engine)
    allowed, other = created.org(), created.org()
    policy = OrgPolicy(auto_provision=True, allowed_org_ids=frozenset({allowed}))

    with pytest.raises(OrganizationNotEnabled):
        await principal_for_org(db, _session(created.user(), other), policy, NoClerkDirectory())
    assert await tenant_for_org(db, other) is None

    ok = await principal_for_org(db, _session(created.user(), allowed), policy, NoClerkDirectory())
    assert await tenant_for_org(db, allowed) == ok.tenant_id


# --- Roles --------------------------------------------------------------------------


@pytest.mark.db
async def test_roles_are_raised_to_owner_never_lowered(
    app_engine: AsyncEngine, created: Created
) -> None:
    db = _db(app_engine)
    org_id, user_id = created.org(), created.user()

    as_member = await principal_for_org(
        db, _session(user_id, org_id, "member"), AUTO, NoClerkDirectory()
    )
    as_admin = await principal_for_org(
        db, _session(user_id, org_id, "admin"), AUTO, NoClerkDirectory()
    )
    demoted = await principal_for_org(
        db, _session(user_id, org_id, "org:member"), AUTO, NoClerkDirectory()
    )

    assert (as_member.role, as_admin.role, demoted.role) == ("member", "owner", "owner")


@pytest.mark.db
async def test_an_approver_set_in_bido_is_kept(app_engine: AsyncEngine, created: Created) -> None:
    db = _db(app_engine)
    org_id, user_id = created.org(), created.user()
    first = await principal_for_org(db, _session(user_id, org_id), AUTO, NoClerkDirectory())
    async with tenant_session(app_engine, first.tenant_id) as session:
        membership = await session.scalar(
            select(Membership).where(Membership.user_id == first.user_id)
        )
        assert membership is not None
        membership.role = "approver"

    again = await principal_for_org(
        db, _session(user_id, org_id, "org:admin"), AUTO, NoClerkDirectory()
    )
    assert again.role == "approver"


# --- Races and isolation ------------------------------------------------------------


@pytest.mark.db
async def test_concurrent_first_logins_create_one_company(
    app_engine: AsyncEngine, created: Created
) -> None:
    db = _db(app_engine)
    org_id = created.org()
    users = [created.user() for _ in range(3)]
    sessions = [_session(u, org_id) for u in users for _ in range(3)]

    principals = await asyncio.gather(
        *(principal_for_org(db, s, AUTO, NoClerkDirectory()) for s in sessions)
    )

    assert len({p.tenant_id for p in principals}) == 1
    assert len({p.user_id for p in principals}) == 3
    tenant_id = principals[0].tenant_id
    async with tenant_session(app_engine, tenant_id) as session:
        count = await session.scalar(
            text("SELECT count(*) FROM memberships WHERE tenant_id = :t"), {"t": tenant_id}
        )
    assert count == 3
    assert (await _audit_actions(app_engine, tenant_id)).count("tenant.provisioned") == 1


@pytest.mark.db
async def test_two_organizations_are_two_isolated_companies(
    app_engine: AsyncEngine, created: Created
) -> None:
    db = _db(app_engine)
    user_id = created.user()
    org_a, org_b = created.org(), created.org()

    in_a = await principal_for_org(db, _session(user_id, org_a), AUTO, NoClerkDirectory())
    in_b = await principal_for_org(db, _session(user_id, org_b), AUTO, NoClerkDirectory())

    assert in_a.tenant_id != in_b.tenant_id
    assert in_a.user_id == in_b.user_id
    assert {r.tenant_id for r in await memberships_for(db, user_id)} == {
        in_a.tenant_id,
        in_b.tenant_id,
    }
    # Inside company A, company B's tenant row is invisible.
    async with tenant_session(app_engine, in_a.tenant_id) as session:
        assert await session.get(Tenant, in_b.tenant_id) is None
    # Without a tenant the app role sees no tenants at all.
    async with app_engine.connect() as conn:
        assert (await conn.execute(select(Tenant.id))).all() == []


# --- Clerk Backend API --------------------------------------------------------------


async def test_backend_api_reads_names_and_primary_email_and_fails_soft() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sk_test_x"
        if request.url.path == "/v1/organizations/org_1":
            return httpx.Response(200, json={"id": "org_1", "name": " Acme S.p.A. "})
        if request.url.path == "/v1/users/user_1":
            return httpx.Response(
                200,
                json={
                    "primary_email_address_id": "idn_2",
                    "email_addresses": [
                        {"id": "idn_1", "email_address": "old@acme.test"},
                        {"id": "idn_2", "email_address": "giulia@acme.test"},
                    ],
                },
            )
        return httpx.Response(404, json={"errors": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = ClerkBackendApi("sk_test_x", base_url="https://clerk.test/v1", client=client)
        assert await api.organization_name("org_1") == "Acme S.p.A."
        assert await api.user_email("user_1") == "giulia@acme.test"
        assert await api.organization_name("org_missing") is None
        assert await api.user_email("user_missing") is None


# --- Over HTTP ----------------------------------------------------------------------


def _app(monkeypatch: pytest.MonkeyPatch, keys: Keys, **settings: Any) -> FastAPI:
    values: dict[str, Any] = {
        "environment": "test",
        "database_url": str(get_settings().database_url),
        "clerk_issuer": ISSUER,
        "clerk_jwks_url": "https://clerk.example.test/.well-known/jwks.json",
        "clerk_authorized_parties": [PARTY],
        **settings,
    }
    app = FastAPI()
    resources = ApiResources(Settings.model_validate(values))
    verifier = ClerkJwtVerifier(
        issuer=ISSUER, authorized_parties=[PARTY], fetch_jwks=FakeJwks(keys)
    )
    monkeypatch.setattr(resources, "verifier", lambda: verifier)
    app.state.resources = resources
    app.include_router(me_router())
    return app


async def _me(app: FastAPI, token: str, **headers: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        return await http.get("/v1/me", headers={"Authorization": f"Bearer {token}", **headers})


@pytest.mark.db
async def test_me_over_http_with_v1_and_v2_org_claims(
    app_engine: AsyncEngine, created: Created, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = Keys()
    app = _app(monkeypatch, keys, auth_auto_provision=True)
    resources: ApiResources = app.state.resources
    org_id, user_id = created.org(), created.user()
    try:
        no_org = await _me(app, keys.token(sub=user_id))
        v2 = await _me(
            app, keys.token(sub=user_id, v=2, o={"id": org_id, "rol": "admin", "slg": "acme"})
        )
        v1 = await _me(
            app,
            keys.token(sub=user_id, org_id=org_id, org_role="org:admin", org_slug="acme"),
            **{"X-Tenant-Id": str(uuid4())},  # ignored with an organization
        )
    finally:
        await resources.aclose()

    assert no_org.status_code == 403
    assert no_org.json() == {"detail": "select an organization"}
    assert v2.status_code == 200, v2.text
    assert v2.json()["clerk_org_id"] == org_id
    assert v2.json()["allow_self_approval"] is True
    assert v2.json()["role"] == "owner"
    assert v1.status_code == 200
    assert v1.json()["tenant_id"] == v2.json()["tenant_id"]


@pytest.mark.db
async def test_me_over_http_refuses_unlinked_orgs_without_auto_provision(
    app_engine: AsyncEngine, created: Created, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = Keys()
    app = _app(monkeypatch, keys)
    resources: ApiResources = app.state.resources
    try:
        response = await _me(app, keys.token(sub=created.user(), org_id=created.org()))
    finally:
        await resources.aclose()

    assert response.status_code == 403
    assert response.json() == {"detail": "organization not enabled"}
