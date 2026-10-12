"""CORS for a frontend served from another origin. No server, no database."""

from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from bido_back.api.app import create_app
from bido_back.settings import Settings

FRONTEND = "https://app.dev.usebido.com"
PRODUCTION_FRONTEND = "https://app.usebido.com"
PREVIEW_REGEX = r"^https://bido-front-[a-z0-9-]+\.vercel\.app$"
PREVIEW = "https://bido-front-git-feat-x-usebido.vercel.app"
UNUSED_DB = "postgresql://app@localhost:5432/unused"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "dev",
        "database_url": UNUSED_DB,
        "cors_allowed_origins": [FRONTEND],
        "cors_allowed_origin_regex": PREVIEW_REGEX,
    }
    values.update(overrides)
    return Settings.model_validate(values)


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(_settings()))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


def _preflight(origin: str, method: str = "POST", headers: str = "authorization,content-type"):
    return {
        "Origin": origin,
        "Access-Control-Request-Method": method,
        "Access-Control-Request-Headers": headers,
    }


def _allowed_headers(response: httpx.Response) -> set[str]:
    raw = response.headers.get("access-control-allow-headers", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


async def test_an_allowed_origin_gets_cors_headers(client: httpx.AsyncClient) -> None:
    response = await client.get("/health", headers={"Origin": FRONTEND})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == FRONTEND
    assert "access-control-allow-credentials" not in response.headers
    assert "content-disposition" in response.headers["access-control-expose-headers"].lower()


async def test_a_preview_deployment_matches_the_regex(client: httpx.AsyncClient) -> None:
    response = await client.get("/health", headers={"Origin": PREVIEW})

    assert response.headers["access-control-allow-origin"] == PREVIEW


async def test_dev_api_accepts_the_canonical_app_origin(client: httpx.AsyncClient) -> None:
    response = await client.options(
        "/v1/purchase-orders/00000000-0000-0000-0000-000000000000/payment/milestones/0/documents",
        headers=_preflight(PRODUCTION_FRONTEND),
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == PRODUCTION_FRONTEND


async def test_another_origin_gets_no_cors_headers(client: httpx.AsyncClient) -> None:
    response = await client.get("/health", headers={"Origin": "https://evil.example"})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("path", ["/v1/rfqs", "/v1/copilot/messages", "/v1/mailbox/complete"])
async def test_preflight_is_answered_before_auth(client: httpx.AsyncClient, path: str) -> None:
    response = await client.options(path, headers=_preflight(FRONTEND))

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == FRONTEND
    assert {"authorization", "content-type"} <= _allowed_headers(response)
    assert "POST" in response.headers["access-control-allow-methods"]


async def test_preflight_from_another_origin_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.options("/v1/rfqs", headers=_preflight("https://evil.example"))

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


async def _preflight_headers(settings: Settings, headers: Mapping[str, str]) -> set[str]:
    transport = httpx.ASGITransport(app=create_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        return _allowed_headers(await http.options("/v1/me", headers=dict(headers)))


async def test_the_dev_user_header_is_allowed_only_locally() -> None:
    local = _settings(environment="local", cors_allowed_origins=["http://localhost:3000"])
    request = _preflight("http://localhost:3000", "GET", "x-dev-user")

    assert "x-dev-user" in await _preflight_headers(local, request)
    assert "x-dev-user" not in await _preflight_headers(
        _settings(), _preflight(FRONTEND, "GET", "authorization")
    )


async def test_no_configured_origin_means_no_cors() -> None:
    settings = _settings(cors_allowed_origins=[], cors_allowed_origin_regex=None)
    transport = httpx.ASGITransport(app=create_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/health", headers={"Origin": FRONTEND})

    assert "access-control-allow-origin" not in response.headers


def test_a_wildcard_origin_is_refused_outside_local() -> None:
    with pytest.raises(ValidationError, match="only when ENVIRONMENT=local"):
        _settings(cors_allowed_origins=["*"])

    assert _settings(environment="local", cors_allowed_origins=["*"]).cors_allowed_origins == ["*"]


def test_an_invalid_regex_is_refused() -> None:
    with pytest.raises(ValidationError, match="not a valid regex"):
        _settings(cors_allowed_origin_regex="^(unclosed")
