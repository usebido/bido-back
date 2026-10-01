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


async def test_me_is_not_implemented_yet(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/me")

    assert response.status_code == 501
    assert response.json() == {"detail": "not implemented"}
