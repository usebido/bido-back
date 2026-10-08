"""`GET /v1/mailbox/providers`: which mailbox providers this server can connect."""

from uuid import UUID

import httpx
from fastapi import FastAPI

from bido_back.api.deps import ApiResources, get_principal
from bido_back.api.mailbox import mailbox_router, provider_available
from bido_back.auth import Principal

from .support import make_settings

PRINCIPAL = Principal(tenant_id=UUID(int=1), user_id=UUID(int=2))


async def test_lists_each_provider_with_its_availability_only() -> None:
    app = FastAPI()
    app.state.resources = ApiResources(make_settings(google_client_id="", google_client_secret=""))
    app.include_router(mailbox_router())
    app.dependency_overrides[get_principal] = lambda: PRINCIPAL
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        response = await http.get("/v1/mailbox/providers")

    assert response.status_code == 200
    assert response.json() == {
        "providers": [
            {"provider": "google", "available": False},
            {"provider": "microsoft", "available": True},
        ]
    }
    # Availability only: no client ids, secrets or URLs.
    assert "ms-client" not in response.text
    assert "secret" not in response.text


def test_configured_providers_are_available() -> None:
    settings = make_settings()
    assert provider_available(settings, "google") is True
    assert provider_available(settings, "microsoft") is True


def test_no_token_protection_makes_every_provider_unavailable() -> None:
    settings = make_settings(token_encryption_key=None)
    assert provider_available(settings, "google") is False
    assert provider_available(settings, "microsoft") is False


def test_kms_alone_cannot_sign_the_oauth_state() -> None:
    # Token encryption under KMS is not enough: the OAuth `state` is sealed with
    # TOKEN_ENCRYPTION_KEY, so connect would answer 503.
    kms_alias = "alias/bido-dev-oauth-tokens"
    settings = make_settings(token_encryption_key=None, oauth_token_kms_key_id=kms_alias)
    assert provider_available(settings, "google") is False
