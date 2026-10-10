"""API smoke tests over the ASGI transport. No server, no database."""

from collections.abc import AsyncIterator

import httpx
import pytest

from bido_back.api.app import create_app
from bido_back.settings import Settings


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings.model_validate(
        {"environment": "test", "database_url": "postgresql://app@localhost:5432/unused"}
    )
    transport = httpx.ASGITransport(app=create_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def test_health(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "environment": "test"}


async def test_v1_requires_authentication(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/me")

    assert response.status_code == 401


async def _status(settings: Settings, method: str, path: str) -> int:
    transport = httpx.ASGITransport(app=create_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        return (await http.request(method, path)).status_code


async def test_dev_routes_are_mounted_only_locally() -> None:
    base = {"database_url": "postgresql://app@localhost:5432/unused"}
    local = Settings.model_validate({**base, "environment": "local"})
    dev = Settings.model_validate({**base, "environment": "dev"})

    # POST-only route: a GET is 405 when it is mounted, 404 when it is not.
    assert await _status(local, "GET", "/dev/inbound") == 405
    assert await _status(dev, "GET", "/dev/inbound") == 404
    assert await _status(dev, "GET", "/v1/mailbox/complete") == 405
    # The payment debug route is the one dev endpoint mounted outside local.
    assert await _status(local, "GET", "/dev/payments/purchase-order") == 405
    assert await _status(dev, "GET", "/dev/payments/purchase-order") == 405
