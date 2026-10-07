"""Clerk organizations are companies: find, or create, the tenant and membership.

Every request the frontend makes carries a Clerk session with an active
organization, and that organization is the buyer's company. A tenant is
linked to its organization through `tenants.clerk_org_id` (migration 0018).

`principal_for_org`, given a verified session with an organization:

1. Refuses organizations outside `CLERK_ALLOWED_ORG_IDS` when that list is set.
2. Finds the tenant linked to the organization (`tenant_for_clerk_org`, a
   SECURITY DEFINER lookup: the app role cannot search `tenants` under RLS).
   None linked: with `AUTH_AUTO_PROVISION` it creates one, named after the
   organization; without, the organization is not enabled (403). Tenants are
   linked ahead of time with `scripts/link_demo_org.py`.
3. Finds the caller's membership there, or creates it: the verified token
   proves the caller belongs to the organization. Clerk admins become owners,
   everyone else members. An existing role is never lowered; a member whose
   Clerk role became admin is raised to owner.

Creation is idempotent and safe under concurrent first logins: the unique
keys on `tenants.clerk_org_id`, `users.clerk_user_id` and
`memberships (tenant_id, user_id)` decide, with ON CONFLICT DO NOTHING and a
re-read. Everything runs as the app role inside tenant-scoped transactions;
the only extra privilege is the lookup in step 2.

The Clerk Backend API (`CLERK_SECRET_KEY`) is used only while provisioning,
to read the organization's display name and the user's e-mail. It is
optional, time-bounded and fail-soft: without it the tenant is named after
the organization slug and the e-mail comes from the session token's `email`
claim, or stays empty.

Logs and audit events carry ids only.
"""

import logging
from dataclasses import dataclass
from typing import Final, Protocol, cast
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.auth import (
    OrganizationNotEnabled,
    Principal,
    Role,
    VerifiedSession,
    membership_role_for,
    memberships_for,
)
from bido_back.db import tenant_session
from bido_back.db.models import AuditEvent, Membership, Tenant, User
from bido_back.settings import Settings

logger = logging.getLogger(__name__)

CLERK_API_URL: Final = "https://api.clerk.com/v1"
_TENANT_FOR_ORG = text("SELECT tenant_for_clerk_org(:org_id)")


# --- Clerk Backend API (optional, provisioning only) ------------------------------


class ClerkDirectory(Protocol):
    async def organization_name(self, org_id: str) -> str | None: ...

    async def user_email(self, user_id: str) -> str | None: ...


class NoClerkDirectory:
    """Used when CLERK_SECRET_KEY is not set."""

    async def organization_name(self, org_id: str) -> str | None:
        return None

    async def user_email(self, user_id: str) -> str | None:
        return None


class ClerkBackendApi:
    """Reads an organization's name and a user's primary e-mail. Never raises."""

    def __init__(
        self,
        secret_key: str,
        *,
        base_url: str = CLERK_API_URL,
        timeout_s: float = 3.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._secret_key = secret_key
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._client = client

    async def _get(self, path: str) -> dict[str, object] | None:
        headers = {"Authorization": f"Bearer {self._secret_key}"}
        url = f"{self._base_url}{path}"
        try:
            if self._client is not None:
                response = await self._client.get(url, headers=headers, timeout=self._timeout_s)
            else:
                async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                    response = await client.get(url, headers=headers)
            response.raise_for_status()
            body: object = response.json()
        except (httpx.HTTPError, ValueError):
            logger.warning("clerk api: lookup failed path=%s", path.split("/")[1])
            return None
        return cast(dict[str, object], body) if isinstance(body, dict) else None

    async def organization_name(self, org_id: str) -> str | None:
        body = await self._get(f"/organizations/{org_id}")
        name = body.get("name") if body else None
        if not isinstance(name, str):
            return None
        return name.strip() or None

    async def user_email(self, user_id: str) -> str | None:
        body = await self._get(f"/users/{user_id}")
        if not body:
            return None
        primary = body.get("primary_email_address_id")
        addresses = body.get("email_addresses")
        if not isinstance(addresses, list):
            return None
        for item in cast(list[object], addresses):
            if not isinstance(item, dict):
                continue
            entry = cast(dict[str, object], item)
            address = entry.get("email_address")
            if isinstance(address, str) and (primary is None or entry.get("id") == primary):
                return address
        return None


def clerk_directory_for(settings: Settings) -> ClerkDirectory:
    if settings.clerk_secret_key:
        return ClerkBackendApi(settings.clerk_secret_key)
    return NoClerkDirectory()


# --- Policy -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrgPolicy:
    auto_provision: bool = False
    # Empty: every organization may sign in (subject to auto_provision).
    allowed_org_ids: frozenset[str] = frozenset()

    @classmethod
    def from_settings(cls, settings: Settings) -> "OrgPolicy":
        return cls(
            auto_provision=settings.auth_auto_provision,
            allowed_org_ids=frozenset(settings.clerk_allowed_org_ids),
        )


# --- Provisioning -----------------------------------------------------------------


async def tenant_for_org(db: async_sessionmaker[AsyncSession], org_id: str) -> UUID | None:
    """The tenant linked to this Clerk organization, if any (no tenant context needed)."""
    async with db() as session, session.begin():
        found = await session.scalar(_TENANT_FOR_ORG, {"org_id": org_id})
    if found is not None and not isinstance(found, UUID):
        raise TypeError("tenant_for_clerk_org returned a non-uuid")
    return found


async def _create_tenant(db: async_sessionmaker[AsyncSession], org_id: str, name: str) -> UUID:
    tenant_id = uuid4()
    async with tenant_session(db, tenant_id) as session:
        created = await session.scalar(
            insert(Tenant)
            .values(id=tenant_id, name=name, clerk_org_id=org_id)
            .on_conflict_do_nothing(index_elements=[Tenant.clerk_org_id])
            .returning(Tenant.id)
        )
        if created is not None:
            session.add(
                AuditEvent(
                    tenant_id=tenant_id,
                    action="tenant.provisioned",
                    payload={"source": "clerk_org"},
                )
            )
            logger.info("orgs: tenant provisioned tenant=%s", tenant_id)
            return tenant_id
    # Another first login of the same organization won the race.
    existing = await tenant_for_org(db, org_id)
    if existing is None:
        raise RuntimeError("tenant for organization vanished during provisioning")
    return existing


async def _user_id(db: async_sessionmaker[AsyncSession], clerk_user_id: str, email: str) -> UUID:
    """Find or create the global user row; fills a missing e-mail, never overwrites one."""
    async with db() as session, session.begin():
        await session.execute(
            insert(User)
            .values(clerk_user_id=clerk_user_id, email=email)
            .on_conflict_do_nothing(index_elements=[User.clerk_user_id])
        )
        if email:
            await session.execute(
                update(User)
                .where(User.clerk_user_id == clerk_user_id, User.email == "")
                .values(email=email)
            )
        user_id = await session.scalar(select(User.id).where(User.clerk_user_id == clerk_user_id))
    if user_id is None:
        raise RuntimeError("user row missing after upsert")
    return user_id


async def _ensure_membership(
    db: async_sessionmaker[AsyncSession], tenant_id: UUID, user_id: UUID, role: Role
) -> Role:
    async with tenant_session(db, tenant_id) as session:
        created = await session.scalar(
            insert(Membership)
            .values(tenant_id=tenant_id, user_id=user_id, role=role)
            .on_conflict_do_nothing(index_elements=[Membership.tenant_id, Membership.user_id])
            .returning(Membership.id)
        )
        current = await session.scalar(
            select(Membership.role).where(
                Membership.tenant_id == tenant_id, Membership.user_id == user_id
            )
        )
        if current is None:
            raise RuntimeError("membership missing after upsert")
        if current == "member" and role == "owner":
            await session.execute(
                update(Membership)
                .where(Membership.tenant_id == tenant_id, Membership.user_id == user_id)
                .values(role="owner")
            )
            current = "owner"
        if created is not None:
            session.add(
                AuditEvent(
                    tenant_id=tenant_id,
                    actor_user_id=user_id,
                    action="membership.provisioned",
                    payload={"role": current, "source": "clerk_org"},
                )
            )
            logger.info("orgs: membership provisioned tenant=%s user=%s", tenant_id, user_id)
    return cast(Role, current)


async def principal_for_org(
    db: async_sessionmaker[AsyncSession],
    verified: VerifiedSession,
    policy: OrgPolicy,
    directory: ClerkDirectory,
) -> Principal:
    """The principal for a Clerk session with an active organization (see module doc)."""
    org = verified.org
    if org is None:
        raise ValueError("principal_for_org needs a session with an organization")
    if policy.allowed_org_ids and org.id not in policy.allowed_org_ids:
        raise OrganizationNotEnabled("organization not enabled")

    tenant_id = await tenant_for_org(db, org.id)
    if tenant_id is None:
        if not policy.auto_provision:
            raise OrganizationNotEnabled("organization not enabled")
        name = await directory.organization_name(org.id) or org.slug or org.id
        tenant_id = await _create_tenant(db, org.id, name)

    wanted = membership_role_for(org.role)
    # Fast path: an existing membership that needs no change is a single read.
    for row in await memberships_for(db, verified.user_id):
        if row.tenant_id == tenant_id and not (row.role == "member" and wanted == "owner"):
            return Principal(tenant_id=tenant_id, user_id=row.user_id, role=row.role)

    email = verified.email or await directory.user_email(verified.user_id) or ""
    user_id = await _user_id(db, verified.user_id, email)
    role = await _ensure_membership(db, tenant_id, user_id, wanted)
    return Principal(tenant_id=tenant_id, user_id=user_id, role=role)


async def link_org(db: async_sessionmaker[AsyncSession], tenant_id: UUID, org_id: str) -> None:
    """Link an existing tenant to a Clerk organization (operator scripts)."""
    async with tenant_session(db, tenant_id) as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            raise LookupError(f"tenant {tenant_id} not found")
        if tenant.clerk_org_id not in (None, org_id):
            raise LookupError(f"tenant {tenant_id} is already linked to another organization")
        tenant.clerk_org_id = org_id
