"""Who is making a request, in our own terms, and how we find out.

A `Principal` is produced only by authentication (the API's `get_principal`
dependency). It carries the tenant the request acts in, the user making it,
and that user's role in the tenant AS READ AT AUTHENTICATION TIME. The role is
there for display and coarse routing (`/v1/me`, the approval queue); every
authorization decision that commits something (the approval guard) reads the
membership from our database again at the moment of the decision, never from
the principal or a token claim.

Authentication has two steps:

1. `ClerkJwtVerifier` verifies a Clerk session JWT: RS256 only, signature
   against Clerk's JWKS (cached, refetched on an unknown `kid` at most once
   per cooldown), `exp`/`nbf` with a small leeway, `iss` equal to the
   configured issuer, and `azp` in the configured authorized parties. From
   the verified token it takes the Clerk user id (`sub`), the active
   organization (session token v2 `o.id/rol/slg`, or v1
   `org_id/org_role/org_slug`) and, if the session token was customized to
   carry it, `email`.
2. With an organization (every Clerk-authenticated request: the frontend
   always has one active), the organization IS the company:
   `bido_back.orgs.principal_for_org` finds the tenant linked to it and the
   caller's membership there, creating the membership on first sight (the
   token proves the caller belongs to the organization) and, with
   `AUTH_AUTO_PROVISION`, the tenant too. `X-Tenant-Id` is ignored.
   Without an organization (dev mode only), `resolve_principal` maps the
   Clerk user id to our memberships through the `memberships_for_clerk_user`
   SECURITY DEFINER function (migration 0005): one membership is that
   tenant, several need `X-Tenant-Id` naming one of them, none is refused.
   A Clerk token without an organization is refused.

Dev mode (`AUTH_DEV_MODE=true`) lets a local run act as a seeded user through
the `X-Dev-User: <clerk_user_id>` header, skipping step 1 only. It is refused
at startup unless `ENVIRONMENT=local` (`check_auth_settings`).

Error messages never contain the token or its claims.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, cast
from uuid import UUID

import httpx
import jwt
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.settings import Settings

logger = logging.getLogger(__name__)

Role = Literal["member", "approver", "owner"]
_ROLES: Final[frozenset[str]] = frozenset({"member", "approver", "owner"})

_ALGORITHM: Final = "RS256"


@dataclass(frozen=True, slots=True)
class Principal:
    tenant_id: UUID
    user_id: UUID
    # The membership role read when the request was authenticated. Display and
    # routing only: decisions re-read `memberships` (see the module docstring).
    role: Role | None = None


# --- Errors -------------------------------------------------------------------


class AuthError(Exception):
    """Base class. Messages are safe for HTTP responses and logs (no token content)."""


class AuthenticationFailed(AuthError):
    """No valid credentials: missing, malformed, expired or untrusted token (401)."""


class NotAMember(AuthError):
    """Authenticated, but not a member of any tenant or of the requested one (403)."""


class TenantSelectionRequired(AuthError):
    """Member of several tenants and no valid `X-Tenant-Id` was sent (400)."""


class OrganizationRequired(AuthError):
    """A Clerk token without an active organization (403)."""


class OrganizationNotEnabled(AuthError):
    """The organization is not linked to a company and may not create one (403)."""


class AuthConfigError(RuntimeError):
    """The process is configured in a way auth refuses to run with (startup)."""


# --- Startup checks -----------------------------------------------------------


def check_auth_settings(settings: Settings) -> None:
    """Refuse unsafe auth configuration at startup: dev mode only where
    `environment == "local"`.

    Missing Clerk settings are not a startup error (the OpenAPI export and
    tests build the app without them): a bearer token is then answered with
    503, and with no CLERK_AUTHORIZED_PARTIES every token fails the `azp`
    check (fail closed).
    """
    if settings.auth_dev_mode and settings.environment != "local":
        raise AuthConfigError(
            f"AUTH_DEV_MODE is only allowed when ENVIRONMENT=local (got {settings.environment!r})"
        )


def dev_mode_enabled(settings: Settings) -> bool:
    return settings.auth_dev_mode and settings.environment == "local"


# --- JWT verification ---------------------------------------------------------

_MAX_ID_LENGTH: Final = 255


@dataclass(frozen=True, slots=True)
class ClerkOrg:
    """The session's active Clerk organization, as the verified token states it."""

    id: str
    # Clerk's role key: "org:admin" / "org:member" (v1) or "admin" / "member" (v2).
    role: str | None = None
    slug: str | None = None


@dataclass(frozen=True, slots=True)
class VerifiedSession:
    """What a verified Clerk session token tells us."""

    user_id: str  # the Clerk user id (`sub`)
    org: ClerkOrg | None = None
    # Only present when the Clerk session token is customized to carry it.
    email: str | None = None


def _short_str(value: object) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= _MAX_ID_LENGTH:
        return value
    return None


def org_from_claims(claims: Mapping[str, object]) -> ClerkOrg | None:
    """The active organization from session token v2 (`o`) or v1 (`org_*`) claims."""
    compact: object = claims.get("o")
    if isinstance(compact, dict):
        o = cast(dict[str, object], compact)
        org_id = _short_str(o.get("id"))
        if org_id is not None:
            return ClerkOrg(id=org_id, role=_short_str(o.get("rol")), slug=_short_str(o.get("slg")))
    org_id = _short_str(claims.get("org_id"))
    if org_id is None:
        return None
    return ClerkOrg(
        id=org_id, role=_short_str(claims.get("org_role")), slug=_short_str(claims.get("org_slug"))
    )


def membership_role_for(org_role: str | None) -> Role:
    """Our role for a Clerk organization role: admins own the company, others are members."""
    key = (org_role or "").removeprefix("org:")
    return "owner" if key == "admin" else "member"


class JwksFetcher:
    """Fetches a JWKS document over HTTPS with a timeout."""

    def __init__(
        self, url: str, *, timeout_s: float = 5.0, client: httpx.AsyncClient | None = None
    ):
        self._url = url
        self._timeout_s = timeout_s
        self._client = client

    async def __call__(self) -> Mapping[str, object]:
        if self._client is not None:
            response = await self._client.get(self._url, timeout=self._timeout_s)
        else:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                response = await client.get(self._url)
        response.raise_for_status()
        body: object = response.json()
        if not isinstance(body, dict):
            raise ValueError("JWKS document is not a JSON object")
        return cast(dict[str, object], body)


FetchJwks = Callable[[], Awaitable[Mapping[str, object]]]


class ClerkJwtVerifier:
    """Verifies Clerk session tokens; returns the user id, organization and e-mail."""

    def __init__(
        self,
        *,
        issuer: str,
        authorized_parties: Sequence[str],
        fetch_jwks: FetchJwks,
        cache_ttl_s: float = 3600.0,
        refetch_cooldown_s: float = 30.0,
        leeway_s: float = 5.0,
    ) -> None:
        if not issuer:
            raise AuthConfigError("CLERK_ISSUER is required")
        self._issuer = issuer
        self._authorized_parties = frozenset(authorized_parties)
        self._fetch_jwks = fetch_jwks
        self._cache_ttl_s = cache_ttl_s
        self._refetch_cooldown_s = refetch_cooldown_s
        self._leeway_s = leeway_s
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> "ClerkJwtVerifier":
        if not settings.clerk_issuer or not settings.clerk_jwks_url:
            raise AuthConfigError("CLERK_ISSUER and CLERK_JWKS_URL are required")
        return cls(
            issuer=settings.clerk_issuer,
            authorized_parties=settings.clerk_authorized_parties,
            fetch_jwks=JwksFetcher(settings.clerk_jwks_url),
        )

    async def verify(self, token: str) -> VerifiedSession:
        """Return what the verified token says. Raises AuthenticationFailed."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise AuthenticationFailed("malformed token") from None
        if header.get("alg") != _ALGORITHM:
            raise AuthenticationFailed("unsupported token algorithm")
        kid: object = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise AuthenticationFailed("token has no key id")

        key = await self._key(kid)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[_ALGORITHM],
                issuer=self._issuer,
                leeway=self._leeway_s,
                options={"require": ["exp", "nbf", "iss", "sub"], "verify_aud": False},
            )
        except jwt.ExpiredSignatureError:
            raise AuthenticationFailed("token expired") from None
        except jwt.ImmatureSignatureError:
            raise AuthenticationFailed("token not yet valid") from None
        except jwt.InvalidIssuerError:
            raise AuthenticationFailed("untrusted token issuer") from None
        except jwt.PyJWTError:
            raise AuthenticationFailed("invalid token") from None

        azp: object = claims.get("azp")
        # Fail closed: with no authorized parties configured, no token passes.
        if not isinstance(azp, str) or azp not in self._authorized_parties:
            raise AuthenticationFailed("token was issued for an unauthorized party")
        sub = _short_str(claims.get("sub"))
        if sub is None:
            raise AuthenticationFailed("token has no subject")
        email = _short_str(claims.get("email"))
        return VerifiedSession(user_id=sub, org=org_from_claims(claims), email=email)

    async def _key(self, kid: str) -> jwt.PyJWK:
        now = time.monotonic()
        key = self._keys.get(kid)
        fresh = self._fetched_at is not None and now - self._fetched_at < self._cache_ttl_s
        if key is not None and fresh:
            return key
        async with self._lock:
            now = time.monotonic()
            key = self._keys.get(kid)
            fresh = self._fetched_at is not None and now - self._fetched_at < self._cache_ttl_s
            if key is not None and fresh:
                return key
            # An unknown kid may mean a key rotation; refetch, but not more than
            # once per cooldown, so garbage kids cannot hammer the JWKS endpoint.
            cooling = (
                self._fetched_at is not None and now - self._fetched_at < self._refetch_cooldown_s
            )
            if not cooling:
                await self._refresh(now)
            key = self._keys.get(kid)
        if key is None:
            raise AuthenticationFailed("unknown signing key")
        return key

    async def _refresh(self, now: float) -> None:
        try:
            document = await self._fetch_jwks()
            jwks = jwt.PyJWKSet.from_dict(dict(document))
        except (httpx.HTTPError, ValueError, jwt.PyJWKSetError):
            logger.exception("could not refresh the Clerk JWKS")
            # Keep serving cached keys; an unknown kid then fails as 401.
            self._fetched_at = now
            return
        keys: dict[str, jwt.PyJWK] = {}
        for key in jwks.keys:
            if key.key_id and key.algorithm_name == _ALGORITHM:
                keys[key.key_id] = key
        self._keys = keys
        self._fetched_at = now


# --- Principal resolution -----------------------------------------------------

_MEMBERSHIPS = text(
    "SELECT tenant_id, user_id, role FROM memberships_for_clerk_user(:clerk_user_id)"
)


@dataclass(frozen=True, slots=True)
class MembershipRow:
    tenant_id: UUID
    user_id: UUID
    role: Role


async def memberships_for(
    db: async_sessionmaker[AsyncSession], clerk_user_id: str
) -> list[MembershipRow]:
    """All memberships of the user with this Clerk id, across tenants.

    Runs outside any tenant (no tenant is known yet) through the
    SECURITY DEFINER function, which returns only this user's rows.
    """
    async with db() as session, session.begin():
        result = await session.execute(_MEMBERSHIPS, {"clerk_user_id": clerk_user_id})
        rows: list[MembershipRow] = []
        for tenant_id, user_id, role in result:
            if not isinstance(tenant_id, UUID) or not isinstance(user_id, UUID):
                raise TypeError("memberships_for_clerk_user returned non-uuid ids")
            if role not in _ROLES:
                continue
            rows.append(MembershipRow(tenant_id, user_id, cast(Role, role)))
        return rows


def choose_membership(rows: Sequence[MembershipRow], requested_tenant: str | None) -> MembershipRow:
    """Pick the membership the request acts in. Pure; raises AuthError subclasses."""
    if not rows:
        raise NotAMember("no membership for this user")
    if requested_tenant is not None:
        try:
            tenant_id = UUID(requested_tenant)
        except ValueError:
            raise TenantSelectionRequired("X-Tenant-Id is not a UUID") from None
        for row in rows:
            if row.tenant_id == tenant_id:
                return row
        # Same answer whether the tenant exists or not.
        raise NotAMember("not a member of the requested tenant")
    if len(rows) > 1:
        raise TenantSelectionRequired("member of several tenants: send X-Tenant-Id")
    return rows[0]


async def resolve_principal(
    db: async_sessionmaker[AsyncSession], clerk_user_id: str, requested_tenant: str | None
) -> Principal:
    row = choose_membership(await memberships_for(db, clerk_user_id), requested_tenant)
    return Principal(tenant_id=row.tenant_id, user_id=row.user_id, role=row.role)
