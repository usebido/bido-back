"""OAuth: consent URLs, PKCE, the sealed state, code exchange, refresh, revoke."""

# ruff: noqa: S105, S106 - fake OAuth tokens and keys, never real credentials

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest

from bido_back.mailbox.base import ConsentRevoked, MailboxNotConfigured, ProviderError, TokenSet
from bido_back.mailbox.oauth import (
    InvalidOAuthState,
    MissingPermissions,
    OAuthExchangeFailed,
    OAuthState,
    StateSealer,
    account_email,
    authorization_url,
    code_challenge,
    exchange_code,
    new_code_verifier,
    oauth_client,
    refresh_tokens,
    revoke,
)

from .support import FERNET_KEY, Recorder, json_response, make_settings

GMAIL_SCOPES = (
    "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly "
    "openid https://www.googleapis.com/auth/userinfo.email"
)

GRAPH_SCOPES = (
    "https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Mail.Send "
    "https://graph.microsoft.com/User.Read openid email"
)


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def _state(provider: str = "google") -> OAuthState:
    return OAuthState(
        tenant_id=uuid4(),
        user_id=uuid4(),
        provider="google" if provider == "google" else "microsoft",
        code_verifier=new_code_verifier(),
    )


# --- Authorization URL ------------------------------------------------------------


def test_google_authorization_url() -> None:
    client = oauth_client(make_settings(), "google")
    verifier = new_code_verifier()
    url = authorization_url(client, state="sealed-state", code_verifier=verifier)
    parts = urlsplit(url)
    q = _query(url)

    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        "https://accounts.google.com/o/oauth2/v2/auth"
    )
    assert q["client_id"] == "google-client"
    assert q["redirect_uri"] == "https://api.bido.example/v1/mailbox/callback/google"
    assert q["response_type"] == "code"
    assert q["scope"].split() == [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
        "openid",
        "email",
    ]
    assert q["access_type"] == "offline"
    assert q["prompt"] == "consent"
    assert q["state"] == "sealed-state"
    assert q["code_challenge_method"] == "S256"
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert q["code_challenge"] == expected.decode()
    assert verifier not in url


def test_microsoft_authorization_url() -> None:
    client = oauth_client(make_settings(microsoft_tenant="organizations"), "microsoft")
    url = authorization_url(client, state="s", code_verifier=new_code_verifier())
    parts = urlsplit(url)
    q = _query(url)

    assert parts.netloc == "login.microsoftonline.com"
    assert parts.path == "/organizations/oauth2/v2.0/authorize"
    assert q["scope"].split() == [
        "offline_access",
        "Mail.Read",
        "Mail.Send",
        "User.Read",
        "openid",
        "email",
    ]
    assert q["redirect_uri"] == "https://api.bido.example/v1/mailbox/callback/microsoft"
    assert q["response_mode"] == "query"
    assert q["code_challenge_method"] == "S256"


def test_pkce_verifier_shape() -> None:
    verifier = new_code_verifier()
    assert 43 <= len(verifier) <= 128
    assert code_challenge(verifier) != verifier


def test_unconfigured_provider_is_refused() -> None:
    with pytest.raises(MailboxNotConfigured):
        oauth_client(make_settings(google_client_id=None), "google")


# --- State --------------------------------------------------------------------------


def test_state_round_trip_is_opaque() -> None:
    sealer = StateSealer(FERNET_KEY)
    state = _state()
    token = sealer.seal(state)

    assert str(state.tenant_id) not in token
    assert state.code_verifier not in token
    assert sealer.open(token, "google") == state


def test_state_tampering_is_rejected() -> None:
    sealer = StateSealer(FERNET_KEY)
    token = sealer.seal(_state())
    flipped = token[:-6] + ("A" if token[-6] != "A" else "B") + token[-5:]

    with pytest.raises(InvalidOAuthState):
        sealer.open(flipped, "google")


def test_state_from_another_key_is_rejected() -> None:
    token = StateSealer("another-secret").seal(_state())
    with pytest.raises(InvalidOAuthState):
        StateSealer(FERNET_KEY).open(token, "google")


def test_state_for_another_provider_is_rejected() -> None:
    sealer = StateSealer(FERNET_KEY)
    token = sealer.seal(_state("google"))
    with pytest.raises(InvalidOAuthState):
        sealer.open(token, "microsoft")


def test_expired_state_is_rejected() -> None:
    now = [1_800_000_000.0]
    sealer = StateSealer(FERNET_KEY, ttl_s=600, clock=lambda: now[0])
    token = sealer.seal(_state())
    now[0] += 599
    sealer.open(token, "google")
    now[0] += 2

    with pytest.raises(InvalidOAuthState):
        sealer.open(token, "google")


def test_garbage_state_is_rejected() -> None:
    sealer = StateSealer(FERNET_KEY)
    for value in ("", "not-a-token", "x" * 5000):
        with pytest.raises(InvalidOAuthState):
            sealer.open(value, "google")


def test_state_needs_a_key() -> None:
    with pytest.raises(MailboxNotConfigured):
        StateSealer.from_settings(make_settings(token_encryption_key=None))


# --- Token endpoint -------------------------------------------------------------------


async def test_google_code_exchange() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        return json_response(
            200,
            {
                "access_token": "at",
                "refresh_token": "rt",
                "expires_in": 3599,
                "scope": GMAIL_SCOPES,
                "token_type": "Bearer",
            },
        )

    recorder = Recorder(route)
    client = oauth_client(make_settings(), "google")
    async with recorder.client() as http:
        tokens = await exchange_code(http, client, code="the-code", code_verifier="v" * 64)

    request = recorder.requests[0]
    assert str(request.url) == "https://oauth2.googleapis.com/token"
    form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
    assert form == {
        "grant_type": "authorization_code",
        "code": "the-code",
        "redirect_uri": "https://api.bido.example/v1/mailbox/callback/google",
        "client_id": "google-client",
        "client_secret": "google-secret",
        "code_verifier": "v" * 64,
    }
    assert tokens.access_token == "at"
    assert tokens.refresh_token == "rt"
    assert tokens.expires_at is not None


async def test_microsoft_code_exchange_sends_scope() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        return json_response(
            200,
            {
                "access_token": "at",
                "refresh_token": "rt",
                "expires_in": 3600,
                "scope": GRAPH_SCOPES,
            },
        )

    recorder = Recorder(route)
    client = oauth_client(make_settings(), "microsoft")
    async with recorder.client() as http:
        tokens = await exchange_code(http, client, code="c", code_verifier="v" * 64)

    request = recorder.requests[0]
    assert request.url.path == "/common/oauth2/v2.0/token"
    form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
    assert form["scope"] == "offline_access Mail.Read Mail.Send User.Read openid email"
    assert form["code_verifier"] == "v" * 64
    assert tokens.refresh_token == "rt"


async def test_exchange_refuses_missing_permissions() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        scope = "https://www.googleapis.com/auth/gmail.send openid"
        return json_response(200, {"access_token": "at", "refresh_token": "rt", "scope": scope})

    async with Recorder(route).client() as http:
        with pytest.raises(MissingPermissions):
            await exchange_code(
                http, oauth_client(make_settings(), "google"), code="c", code_verifier="v" * 64
            )


async def test_exchange_refuses_a_grant_without_refresh_token() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        return json_response(200, {"access_token": "at", "scope": GMAIL_SCOPES})

    async with Recorder(route).client() as http:
        with pytest.raises(OAuthExchangeFailed):
            await exchange_code(
                http, oauth_client(make_settings(), "google"), code="c", code_verifier="v" * 64
            )


async def test_exchange_refused_code() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        return json_response(400, {"error": "invalid_grant"})

    async with Recorder(route).client() as http:
        with pytest.raises(OAuthExchangeFailed) as info:
            await exchange_code(
                http, oauth_client(make_settings(), "google"), code="c", code_verifier="v" * 64
            )
    assert "c" * 3 not in str(info.value)


async def test_google_refresh_keeps_refresh_token_and_scopes() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        return json_response(200, {"access_token": "new-at", "expires_in": 3599})

    recorder = Recorder(route)
    old = TokenSet(access_token="old", refresh_token="rt", scopes=("s1",))
    async with recorder.client() as http:
        tokens = await refresh_tokens(http, oauth_client(make_settings(), "google"), old)

    form = {k: v[0] for k, v in parse_qs(recorder.requests[0].content.decode()).items()}
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "rt"
    assert tokens == TokenSet(
        access_token="new-at", refresh_token="rt", expires_at=tokens.expires_at, scopes=("s1",)
    )


async def test_microsoft_refresh_rotates_refresh_token() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        return json_response(
            200, {"access_token": "new-at", "refresh_token": "rt-2", "scope": "Mail.Read"}
        )

    recorder = Recorder(route)
    old = TokenSet(access_token="old", refresh_token="rt-1")
    async with recorder.client() as http:
        tokens = await refresh_tokens(http, oauth_client(make_settings(), "microsoft"), old)

    form = {k: v[0] for k, v in parse_qs(recorder.requests[0].content.decode()).items()}
    assert "scope" in form
    assert tokens.refresh_token == "rt-2"


@pytest.mark.parametrize("provider", ["google", "microsoft"])
async def test_refresh_invalid_grant_means_revoked(provider: str) -> None:
    def route(request: httpx.Request) -> httpx.Response:
        return json_response(400, {"error": "invalid_grant", "error_description": "revoked"})

    client = oauth_client(make_settings(), "google" if provider == "google" else "microsoft")
    async with Recorder(route).client() as http:
        with pytest.raises(ConsentRevoked):
            await refresh_tokens(http, client, TokenSet(access_token="a", refresh_token="r"))


async def test_refresh_server_error_is_not_revocation() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        return json_response(503, {"error": "temporarily_unavailable"})

    async with Recorder(route).client() as http:
        with pytest.raises(ProviderError):
            await refresh_tokens(
                http,
                oauth_client(make_settings(), "google"),
                TokenSet(access_token="a", refresh_token="r"),
            )


async def test_google_revoke_and_microsoft_noop() -> None:
    recorder = Recorder(lambda request: httpx.Response(200))
    tokens = TokenSet(access_token="a", refresh_token="r")
    async with recorder.client() as http:
        assert await revoke(http, oauth_client(make_settings(), "google"), tokens) is True
        assert await revoke(http, oauth_client(make_settings(), "microsoft"), tokens) is False

    assert [str(r.url) for r in recorder.requests] == ["https://oauth2.googleapis.com/revoke"]


async def test_account_email_per_provider() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "gmail.googleapis.com":
            return json_response(200, {"emailAddress": "Buyer@Acme.example"})
        return json_response(200, {"mail": None, "userPrincipalName": "buyer@contoso.example"})

    recorder = Recorder(route)
    async with recorder.client() as http:
        assert await account_email(http, "google", "at") == "buyer@acme.example"
        assert await account_email(http, "microsoft", "at") == "buyer@contoso.example"

    assert recorder.paths() == ["GET /gmail/v1/users/me/profile", "GET /v1.0/me"]
    assert all(r.headers["Authorization"] == "Bearer at" for r in recorder.requests)
