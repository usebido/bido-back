"""OAuth 2.0 for connecting a buyer's mailbox: Google (Gmail) and Microsoft 365.

Authorization code flow with PKCE (S256), for a confidential client (the
client secret is sent on the token calls too).

The `state` parameter
---------------------
The callback is a browser redirect from the provider, without our bearer
token. It only forwards the code and state to the frontend; the link is
finished by `POST /v1/mailbox/complete`, as the signed-in user, who must be
the tenant and user sealed in the state. The state is a Fernet token (AES-CBC +
HMAC-SHA256, with a timestamp) under a key derived with HKDF from
`TOKEN_ENCRYPTION_KEY`, so it is both signed and encrypted. It carries the
tenant id, the user id, the provider and the PKCE code verifier, and expires
after `STATE_TTL_S`. Encrypting matters: the verifier must stay secret, and
the state travels through the browser and the provider. A tampered, expired,
foreign or other-provider state is rejected.

Requested permissions
---------------------
Google: `gmail.send`, `gmail.readonly`, `openid`, `email`, with
`access_type=offline` and `prompt=consent` so a refresh token is always
issued. Microsoft (multi-tenant app, delegated): `offline_access Mail.Read
Mail.Send User.Read openid email`. `readonly`/`Mail.Read` grant the whole
mailbox; Bido's code limits itself to the RFQ threads it started (see
`bido_back.mailbox.base`). A grant missing the send or read permission
(Google lets the user untick scopes) is refused.

Tokens and codes never appear in logs or exception messages.
"""

import base64
import hashlib
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final, cast
from urllib.parse import urlencode
from uuid import UUID

import httpx
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from bido_back.mailbox.base import (
    ConsentRevoked,
    MailboxError,
    MailboxNotConfigured,
    Provider,
    ProviderError,
    TokenSet,
)
from bido_back.mailbox.http import DEFAULT_TIMEOUT_S, json_object, str_field
from bido_back.settings import Settings

STATE_TTL_S: Final = 600
CALLBACK_PATH: Final = "/v1/mailbox/callback/{provider}"

GOOGLE_AUTHORIZE_URL: Final = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL: Final = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL
GOOGLE_REVOKE_URL: Final = "https://oauth2.googleapis.com/revoke"
GOOGLE_PROFILE_URL: Final = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
GOOGLE_SCOPES: Final = (
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "openid",
    "email",
)
GOOGLE_REQUIRED: Final = frozenset(
    {
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
    }
)

MICROSOFT_LOGIN: Final = "https://login.microsoftonline.com"
MICROSOFT_ME_URL: Final = "https://graph.microsoft.com/v1.0/me"
MICROSOFT_SCOPES: Final = (
    "offline_access",
    "Mail.Read",
    "Mail.Send",
    "User.Read",
    "openid",
    "email",
)
MICROSOFT_REQUIRED: Final = frozenset({"mail.read", "mail.send"})
_GRAPH_SCOPE_PREFIX: Final = "https://graph.microsoft.com/"

_STATE_KDF_INFO: Final = b"bido/mailbox/oauth-state/v1"


class InvalidOAuthState(MailboxError):
    """The state is tampered, expired, malformed or for another provider."""


class OAuthExchangeFailed(MailboxError):
    """The provider refused the authorization code (or answered garbage)."""


class MissingPermissions(MailboxError):
    """The user did not grant every permission Bido needs."""


@dataclass(frozen=True, slots=True)
class OAuthClient:
    provider: Provider
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...]


def oauth_client(settings: Settings, provider: Provider) -> OAuthClient:
    """The provider's OAuth client from settings. Raises MailboxNotConfigured."""
    redirect_uri = settings.oauth_redirect_base_url.rstrip("/") + CALLBACK_PATH.format(
        provider=provider
    )
    if provider == "google":
        if not settings.google_client_id or not settings.google_client_secret:
            raise MailboxNotConfigured("Google mailbox connection is not configured")
        return OAuthClient(
            provider="google",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            redirect_uri=redirect_uri,
            authorize_url=GOOGLE_AUTHORIZE_URL,
            token_url=GOOGLE_TOKEN_URL,
            scopes=GOOGLE_SCOPES,
        )
    if not settings.microsoft_client_id or not settings.microsoft_client_secret:
        raise MailboxNotConfigured("Microsoft mailbox connection is not configured")
    tenant = settings.microsoft_tenant or "common"
    base = f"{MICROSOFT_LOGIN}/{tenant}/oauth2/v2.0"
    return OAuthClient(
        provider="microsoft",
        client_id=settings.microsoft_client_id,
        client_secret=settings.microsoft_client_secret,
        redirect_uri=redirect_uri,
        authorize_url=f"{base}/authorize",
        token_url=f"{base}/token",
        scopes=MICROSOFT_SCOPES,
    )


# --- PKCE -----------------------------------------------------------------------


def new_code_verifier() -> str:
    """43-128 chars from the unreserved set (RFC 7636 section 4.1): 64 here."""
    return secrets.token_urlsafe(48)


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


# --- State ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OAuthState:
    tenant_id: UUID
    user_id: UUID
    provider: Provider
    code_verifier: str = field(repr=False)


class StateSealer:
    """Seals and opens the OAuth `state` (see the module docstring)."""

    def __init__(
        self,
        secret: str,
        *,
        ttl_s: int = STATE_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not secret:
            raise MailboxNotConfigured("TOKEN_ENCRYPTION_KEY is required to sign OAuth state")
        derived = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=_STATE_KDF_INFO
        ).derive(secret.encode())
        self._fernet = Fernet(base64.urlsafe_b64encode(derived))
        self._ttl_s = ttl_s
        self._clock = clock

    @classmethod
    def from_settings(cls, settings: Settings) -> "StateSealer":
        return cls(settings.token_encryption_key or "")

    def seal(self, state: OAuthState) -> str:
        payload = json.dumps(
            {
                "v": 1,
                "t": str(state.tenant_id),
                "u": str(state.user_id),
                "p": state.provider,
                "cv": state.code_verifier,
                # Makes two states for the same user distinct even within a second.
                "n": secrets.token_urlsafe(8),
            },
            separators=(",", ":"),
        ).encode()
        return self._fernet.encrypt_at_time(payload, int(self._clock())).decode()

    def open(self, token: str, provider: Provider) -> OAuthState:
        if not token or len(token) > 2048:
            raise InvalidOAuthState("invalid OAuth state")
        try:
            payload = self._fernet.decrypt_at_time(
                token.encode(), ttl=self._ttl_s, current_time=int(self._clock())
            )
            raw: object = json.loads(payload)
        except (InvalidToken, ValueError):
            raise InvalidOAuthState("invalid or expired OAuth state") from None
        if not isinstance(raw, dict):
            raise InvalidOAuthState("invalid OAuth state")
        obj = cast(dict[str, object], raw)
        verifier = obj.get("cv")
        if obj.get("v") != 1 or obj.get("p") != provider or not isinstance(verifier, str):
            raise InvalidOAuthState("OAuth state does not match this provider")
        try:
            tenant_id = UUID(str(obj.get("t")))
            user_id = UUID(str(obj.get("u")))
        except ValueError:
            raise InvalidOAuthState("invalid OAuth state") from None
        return OAuthState(
            tenant_id=tenant_id, user_id=user_id, provider=provider, code_verifier=verifier
        )


# --- Authorization URL ------------------------------------------------------------


def authorization_url(client: OAuthClient, *, state: str, code_verifier: str) -> str:
    params: dict[str, str] = {
        "client_id": client.client_id,
        "redirect_uri": client.redirect_uri,
        "response_type": "code",
        "scope": " ".join(client.scopes),
        "state": state,
        "code_challenge": code_challenge(code_verifier),
        "code_challenge_method": "S256",
    }
    if client.provider == "google":
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    else:
        params["response_mode"] = "query"
        params["prompt"] = "select_account"
    return f"{client.authorize_url}?{urlencode(params)}"


# --- Token endpoint ---------------------------------------------------------------


async def exchange_code(
    http: httpx.AsyncClient, client: OAuthClient, *, code: str, code_verifier: str
) -> TokenSet:
    """Trade the authorization code for tokens. Requires a refresh token and the scopes."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": client.redirect_uri,
        "client_id": client.client_id,
        "client_secret": client.client_secret,
        "code_verifier": code_verifier,
    }
    if client.provider == "microsoft":
        form["scope"] = " ".join(client.scopes)
    response = await _post_form(http, client.token_url, form)
    if response.status_code != 200:
        raise OAuthExchangeFailed(
            f"{client.provider} refused the authorization code (HTTP {response.status_code})"
        )
    tokens = _token_set(response, client.provider, previous_refresh=None)
    if tokens.refresh_token is None:
        raise OAuthExchangeFailed(f"{client.provider} issued no refresh token")
    check_scopes(client.provider, tokens.scopes)
    return tokens


async def refresh_tokens(
    http: httpx.AsyncClient, client: OAuthClient, tokens: TokenSet
) -> TokenSet:
    """A fresh access token. Raises ConsentRevoked on `invalid_grant`."""
    if tokens.refresh_token is None:
        raise ConsentRevoked(f"{client.provider} connection has no refresh token")
    form = {
        "grant_type": "refresh_token",
        "refresh_token": tokens.refresh_token,
        "client_id": client.client_id,
        "client_secret": client.client_secret,
    }
    if client.provider == "microsoft":
        form["scope"] = " ".join(client.scopes)
    response = await _post_form(http, client.token_url, form)
    if response.status_code == 200:
        refreshed = _token_set(response, client.provider, previous_refresh=tokens.refresh_token)
        if not refreshed.scopes:
            refreshed = TokenSet(
                access_token=refreshed.access_token,
                refresh_token=refreshed.refresh_token,
                expires_at=refreshed.expires_at,
                scopes=tokens.scopes,
            )
        return refreshed
    if response.status_code in (400, 401) and _oauth_error(response) == "invalid_grant":
        raise ConsentRevoked(f"{client.provider} refused the refresh token")
    raise ProviderError(client.provider, response.status_code, "token refresh")


async def revoke(http: httpx.AsyncClient, client: OAuthClient, tokens: TokenSet) -> bool:
    """Best-effort provider-side revocation. True if the provider confirmed it.

    Google revokes the grant (refresh and access tokens). Microsoft has no
    per-grant revocation endpoint for delegated tokens; the user removes the
    app at https://myapps.microsoft.com (or the admin in Entra ID), so this
    returns False and the tokens are only erased on our side.
    """
    if client.provider != "google":
        return False
    token = tokens.refresh_token or tokens.access_token
    try:
        response = await _post_form(http, GOOGLE_REVOKE_URL, {"token": token})
    except httpx.HTTPError:
        return False
    return response.status_code == 200


async def account_email(http: httpx.AsyncClient, provider: Provider, access_token: str) -> str:
    """The mailbox's own address: Gmail profile, or Graph /me."""
    headers = {"Authorization": f"Bearer {access_token}"}
    if provider == "google":
        response = await http.get(GOOGLE_PROFILE_URL, headers=headers, timeout=DEFAULT_TIMEOUT_S)
        if response.status_code != 200:
            raise ProviderError(provider, response.status_code, "profile")
        email = str_field(json_object(response), "emailAddress")
    else:
        response = await http.get(
            MICROSOFT_ME_URL,
            params={"$select": "mail,userPrincipalName"},
            headers=headers,
            timeout=DEFAULT_TIMEOUT_S,
        )
        if response.status_code != 200:
            raise ProviderError(provider, response.status_code, "profile")
        body = json_object(response)
        email = str_field(body, "mail") or str_field(body, "userPrincipalName")
    if email is None or "@" not in email:
        raise OAuthExchangeFailed(f"{provider} returned no mailbox address")
    return email.strip().lower()


def check_scopes(provider: Provider, granted: tuple[str, ...]) -> None:
    if provider == "google":
        missing = GOOGLE_REQUIRED - set(granted)
    else:
        normalized = {s.removeprefix(_GRAPH_SCOPE_PREFIX).lower() for s in granted}
        missing = MICROSOFT_REQUIRED - normalized
    if missing:
        raise MissingPermissions(
            f"{provider} connection is missing permissions: {', '.join(sorted(missing))}"
        )


async def _post_form(http: httpx.AsyncClient, url: str, form: dict[str, str]) -> httpx.Response:
    return await http.post(
        url,
        data=form,
        headers={"Accept": "application/json"},
        timeout=DEFAULT_TIMEOUT_S,
    )


def _token_set(
    response: httpx.Response, provider: Provider, *, previous_refresh: str | None
) -> TokenSet:
    try:
        body = json_object(response)
    except ValueError:
        raise OAuthExchangeFailed(f"{provider} token response is not JSON") from None
    access = str_field(body, "access_token")
    if access is None:
        raise OAuthExchangeFailed(f"{provider} token response has no access token")
    expires_in = body.get("expires_in")
    expires_at = (
        int(time.time()) + int(expires_in)
        if isinstance(expires_in, int | float | str) and str(expires_in).isdigit()
        else None
    )
    scope = str_field(body, "scope") or ""
    return TokenSet(
        access_token=access,
        # Microsoft rotates refresh tokens; Google usually returns none on refresh.
        refresh_token=str_field(body, "refresh_token") or previous_refresh,
        expires_at=expires_at,
        scopes=tuple(s for s in scope.split() if s),
    )


def _oauth_error(response: httpx.Response) -> str | None:
    try:
        return str_field(json_object(response), "error")
    except ValueError:
        return None
