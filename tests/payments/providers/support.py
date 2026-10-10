"""Shared helpers for the provider tests. No network: every call goes to httpx.MockTransport."""

# ruff: noqa: S105, S106 - fake keys and secrets, never real credentials

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final, cast
from urllib.parse import parse_qsl

import httpx
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from pydantic import JsonValue

from bido_back.settings import Settings

Handler = Callable[[httpx.Request], httpx.Response]

BT_URL: Final = "https://api.test.basistheory.com"
BT_KEY: Final = "key_test_pvt_fake-backend-key"
STRIPE_KEY: Final = "sk_test_fake_stripe_secret_key"
NETWORK_PROFILE: Final = "profile_test_bido"
PRIVY_URL: Final = "https://api.privy.io"
PRIVY_APP_ID: Final = "bido-test-privy-app"
PRIVY_APP_SECRET: Final = "privy-test-app-secret-value"
WALLET_OWNER: Final = "bido-test-owner-quorum"
WALLET_POLICY: Final = "bido-test-fund-policy-000"


def p256_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def pkcs8_base64(key: ec.EllipticCurvePrivateKey) -> str:
    """The key as Privy's dashboard shows it, without the `wallet-auth:` prefix."""
    der = key.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())
    return base64.b64encode(der).decode("ascii")


AUTHORIZATION_KEY: Final = p256_key()
AUTHORIZATION_KEY_B64: Final = pkcs8_base64(AUTHORIZATION_KEY)


@dataclass(frozen=True)
class Answer:
    """One canned response (built fresh for every request), or a transport failure."""

    status: int = 200
    body: JsonValue = None
    raw: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict[str, str])
    fail: type[httpx.TransportError] | None = None

    def respond(self, request: httpx.Request) -> httpx.Response:
        if self.fail is not None:
            raise self.fail("simulated", request=request)
        if self.raw is not None:
            return httpx.Response(self.status, content=self.raw, headers=self.headers)
        if self.body is None:
            return httpx.Response(self.status, headers=self.headers)
        return httpx.Response(self.status, json=self.body, headers=self.headers)


def always(answer: Answer) -> Handler:
    return answer.respond


def scripted(answers: dict[str, list[Answer]]) -> Handler:
    """Answer `"METHOD host/path"` with its queued answers in order; the last one repeats."""

    def route(request: httpx.Request) -> httpx.Response:
        key = f"{request.method} {request.url.host}{request.url.path}"
        queue = answers.get(key)
        if not queue:
            raise AssertionError(f"unexpected request {key}")
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        return answer.respond(request)

    return route


@dataclass
class Recorder:
    """A MockTransport handler that records every request and delegates to `route`."""

    route: Handler
    requests: list[httpx.Request] = field(default_factory=list[httpx.Request])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        return self.route(request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def only(self) -> httpx.Request:
        assert len(self.requests) == 1, self.calls()
        return self.requests[0]

    def calls(self) -> list[str]:
        return [f"{r.method} {r.url.host}{r.url.path}" for r in self.requests]

    def to(self, host: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == host]


def json_body(request: httpx.Request) -> dict[str, object]:
    body: object = json.loads(request.content)
    assert isinstance(body, dict)
    return cast(dict[str, object], body)


def form_body(request: httpx.Request) -> dict[str, str]:
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    pairs = parse_qsl(request.content.decode("ascii"), keep_blank_values=True, strict_parsing=True)
    assert len({name for name, _ in pairs}) == len(pairs), "a form field is repeated"
    return dict(pairs)


def read_timeout(request: httpx.Request) -> float:
    timeout = cast(dict[str, float], request.extensions["timeout"])
    return timeout["read"]


def payment_settings(**overrides: object) -> Settings:
    """Settings with every payment provider set up (fake values); `overrides` win."""
    base: dict[str, object] = {
        "environment": "test",
        "database_url": "postgresql://app@localhost:5432/unused",
        "bt_api_base": BT_URL,
        "bt_api_key": BT_KEY,
        "payments_card_charge": "stripe",
        "stripe_secret_key": STRIPE_KEY,
        "stripe_network_business_profile": NETWORK_PROFILE,
        "stripe_api_version": None,
        "privy_api_url": PRIVY_URL,
        "privy_app_id": PRIVY_APP_ID,
        "privy_app_secret": PRIVY_APP_SECRET,
        "privy_authorization_key": AUTHORIZATION_KEY_B64,
        "privy_wallet_owner_id": WALLET_OWNER,
        "privy_wallet_policy_id": WALLET_POLICY,
    }
    return Settings.model_validate({**base, **overrides})
