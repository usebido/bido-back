"""Shared helpers for the mailbox tests. No network: every call goes to MockTransport."""

# ruff: noqa: S105, S106 - fake OAuth tokens and keys, never real credentials

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr

import httpx
from cryptography.fernet import Fernet

from bido_back.mailbox.base import ConsentRevoked, Provider, TokenSet
from bido_back.mailbox.http import AuthorizedHttp
from bido_back.settings import Settings

FERNET_KEY = Fernet.generate_key().decode()
BUYER = "buyer@acme.example"
SUPPLIER = "sales@supplier.example"

Handler = Callable[[httpx.Request], httpx.Response]


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "environment": "test",
        "database_url": "postgresql://app@localhost:5432/unused",
        "google_client_id": "google-client",
        "google_client_secret": "google-secret",
        "microsoft_client_id": "ms-client",
        "microsoft_client_secret": "ms-secret",
        "microsoft_tenant": "common",
        "oauth_redirect_base_url": "https://api.bido.example",
        "token_encryption_key": FERNET_KEY,
    }
    return Settings.model_validate({**base, **overrides})


@dataclass
class MemoryStore:
    saved: list[TokenSet] = field(default_factory=list[TokenSet])
    revoked: bool = False
    errored: bool = False

    async def save(self, tokens: TokenSet) -> None:
        self.saved.append(tokens)

    async def mark_revoked(self) -> None:
        self.revoked = True

    async def mark_error(self) -> None:
        self.errored = True


@dataclass
class Recorder:
    """A MockTransport handler that records requests and delegates to `route`."""

    route: Handler
    requests: list[httpx.Request] = field(default_factory=list[httpx.Request])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        return self.route(request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]


@dataclass
class Refresher:
    """A fake token refresh: hands out access-2, access-3, ... or raises ConsentRevoked."""

    revoked: bool = False
    calls: int = 0

    async def __call__(self, current: TokenSet) -> TokenSet:
        self.calls += 1
        if self.revoked:
            raise ConsentRevoked("refresh refused")
        return TokenSet(
            access_token=f"access-{self.calls + 1}",
            refresh_token=current.refresh_token,
            expires_at=None,
            scopes=current.scopes,
        )


async def no_sleep(_: float) -> None:
    return None


def authorized(
    provider: Provider,
    recorder: Recorder,
    *,
    refresher: Refresher | None = None,
    store: MemoryStore | None = None,
    tokens: TokenSet | None = None,
    sleep: Callable[[float], Awaitable[None]] = no_sleep,
) -> tuple[AuthorizedHttp, MemoryStore, Refresher]:
    used_store = store or MemoryStore()
    used_refresher = refresher or Refresher()
    http = AuthorizedHttp(
        provider=provider,
        tokens=tokens or TokenSet(access_token="access-1", refresh_token="refresh-1"),
        refresh=used_refresher,
        store=used_store,
        client=recorder.client(),
        sleep=sleep,
    )
    return http, used_store, used_refresher


def raw_email(
    *,
    from_address: str,
    to: str,
    subject: str = "Re: RFQ 42",
    message_id: str = "<m1@supplier.example>",
    in_reply_to: str | None = None,
    body: str = "Our quote is attached.",
) -> bytes:
    msg = EmailMessage()
    msg["From"] = formataddr(("Someone", from_address))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    msg.set_content(body)
    return msg.as_bytes()


def json_response(status: int, body: object) -> httpx.Response:
    return httpx.Response(
        status, content=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )


def bearer(request: httpx.Request) -> str:
    return request.headers.get("Authorization", "")
