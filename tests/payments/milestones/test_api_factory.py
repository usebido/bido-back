"""The factory's routes over the ASGI transport: the token only in `X-Factory-Token`, one 401
for every bad token, nothing cached, no chain words, and a milestone sent end to end."""

from datetime import UTC, datetime, timedelta
from typing import Final, cast
from uuid import uuid4

import httpx
import pytest

from bido_back.api.app import create_app
from bido_back.payments.milestones.links import issue_factory_link, new_factory_token
from bido_back.settings import Settings
from tests.awards.test_awards_db import buyer
from tests.payments.milestones.conftest import Funded
from tests.payments.milestones.test_api_milestones import MilestoneApi, api, build_app
from tests.payments.milestones.test_documents_db import (
    SETTINGS,
    definition_at,
    factory_link,
    jpeg,
    sample,
)
from tests.payments.milestones.test_service_db import CHAIN_WORDS, strings_of

__all__ = ["api"]

ORDER: Final = "/v1/factory/order"
HEADER: Final = "X-Factory-Token"


def routes(document_id: str) -> list[tuple[str, str]]:
    return [
        ("GET", ORDER),
        ("POST", f"{ORDER}/accept"),
        ("POST", f"{ORDER}/milestones/0/documents?doc_type=tool_start_photo&filename=a.jpg"),
        ("DELETE", f"{ORDER}/milestones/0/documents/{document_id}"),
        ("GET", f"{ORDER}/milestones/0/documents/{document_id}/link"),
        ("POST", f"{ORDER}/milestones/0/submit"),
    ]


async def call(http: httpx.AsyncClient, method: str, url: str, token: str | None) -> httpx.Response:
    headers = {HEADER: token} if token is not None else {}
    if method == "POST":
        content = jpeg() if "/documents" in url else b"{}"
        kind = "image/jpeg" if "/documents" in url else "application/json"
        return await http.post(url, headers={**headers, "Content-Type": kind}, content=content)
    return await http.request(method, url, headers=headers)


async def test_a_missing_or_malformed_token_answers_401_before_any_lookup() -> None:
    # No database behind this app: a malformed token never reaches one.
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        answers = [
            await call(http, method, url, token)
            for token in (None, "", "short", "x" * 44, "*" * 43, "a b" * 14 + "c")
            for method, url in routes(str(uuid4()))
        ]
    assert {r.status_code for r in answers} == {401}
    assert len({r.text for r in answers}) == 1


@pytest.mark.db
async def test_unknown_revoked_and_expired_tokens_answer_the_same(
    funded: Funded, api: MilestoneApi
) -> None:
    http = api.client()
    revoked, _ = await factory_link(funded)
    await factory_link(funded)  # stops the first link
    expired = await issue_factory_link(
        funded.engine,
        buyer(funded.people),
        funded.po_id,
        settings=SETTINGS,
        now=datetime.now(UTC) - timedelta(days=SETTINGS.factory_link_ttl_days + 1),
    )
    answers = [
        await call(http, method, url, token)
        for token in (new_factory_token(), revoked.token, expired.token)
        for method, url in routes(str(uuid4()))
    ]
    assert {r.status_code for r in answers} == {401}
    assert len({r.text for r in answers}) == 1


@pytest.mark.db
async def test_the_factory_sends_a_milestone_over_http(funded: Funded, api: MilestoneApi) -> None:
    link, _ = await factory_link(funded, accept=False)
    http = api.client()
    token = {HEADER: link.token}
    definition = await definition_at(funded, 0)
    photos = next(d for d in definition.documents if d.required and "jpg" in d.accepts)
    upload_url = f"{ORDER}/milestones/0/documents"

    order = await http.get(ORDER, headers=token)
    assert order.status_code == 200
    assert order.headers["cache-control"] == "no-store"
    body = order.json()
    assert (body["purchase_order_ref"], body["accepted_at"]) == ("PO-1", None)
    assert [m["status"] for m in body["milestones"]] == ["awaiting_documents", "locked", "locked"]
    words = [s.lower() for s in strings_of(body)]
    assert not [w for w in CHAIN_WORDS if any(w in s for s in words)]

    not_yet = await http.post(
        upload_url,
        headers=token,
        params={"doc_type": photos.code, "filename": "a.jpg"},
        content=jpeg(),
    )
    assert not_yet.status_code == 409
    accepted = await http.post(f"{ORDER}/accept", headers=token)
    again = await http.post(f"{ORDER}/accept", headers=token)
    assert accepted.status_code == again.status_code == 200
    assert accepted.json() == again.json()

    added = await http.post(
        upload_url,
        headers={**token, "Content-Type": "image/jpeg"},
        params={"doc_type": photos.code, "filename": "steel.jpg"},
        content=jpeg(),
    )
    assert added.status_code == 201
    document = added.json()
    # The factory's view of a file: no hash.
    assert set(document) == {"id", "doc_type", "filename", "size_bytes", "uploaded_at"}
    link_answer = await http.get(
        f"{ORDER}/milestones/0/documents/{document['id']}/link",
        headers=token,
        params={"disposition": "inline"},
    )
    assert link_answer.status_code == 200 and link_answer.json()["content_type"] == "image/jpeg"
    removed = await http.delete(f"{ORDER}/milestones/0/documents/{document['id']}", headers=token)
    assert removed.status_code == 204
    assert removed.headers["cache-control"] == "no-store"

    missing = await http.post(f"{ORDER}/milestones/0/submit", headers=token, json={})
    assert missing.status_code == 422
    for code in sorted(definition.required_codes):
        document_type = definition.document(code)
        assert document_type is not None
        name, content = sample(document_type)
        response = await http.post(
            upload_url, headers=token, params={"doc_type": code, "filename": name}, content=content
        )
        assert response.status_code == 201, response.text
    out_of_order = await http.post(f"{ORDER}/milestones/1/submit", headers=token, json={})
    assert out_of_order.status_code == 409

    submitted = await http.post(
        f"{ORDER}/milestones/0/submit", headers=token, json={"note": "Steel arrived."}
    )
    assert submitted.status_code == 202
    milestone = submitted.json()
    assert (milestone["index"], milestone["status"]) == (0, "in_review")
    assert milestone["submitted_at"] is not None
    assert (
        await http.post(f"{ORDER}/milestones/0/submit", headers=token, json={})
    ).status_code == (409)
    after = (await http.get(ORDER, headers=token)).json()
    words = [s.lower() for s in strings_of(after)]
    assert not [w for w in CHAIN_WORDS if any(w in s for s in words)]


def test_the_token_never_travels_in_a_path_or_a_query() -> None:
    schema = create_app(
        Settings.model_validate(
            {"environment": "test", "database_url": "postgresql://app@localhost:5432/unused"}
        )
    ).openapi()
    paths = cast(dict[str, dict[str, dict[str, object]]], schema["paths"])
    factory = {path: item for path, item in paths.items() if path.startswith("/v1/factory/")}
    assert set(factory) == {
        "/v1/factory/order",
        "/v1/factory/order/accept",
        "/v1/factory/order/milestones/{index}/documents",
        "/v1/factory/order/milestones/{index}/documents/{document_id}",
        "/v1/factory/order/milestones/{index}/documents/{document_id}/link",
        "/v1/factory/order/milestones/{index}/submit",
    }
    for path, item in factory.items():
        assert "token" not in path
        for operation in item.values():
            parameters = cast(list[dict[str, str]], operation.get("parameters", []))
            assert all("token" not in p["name"].lower() for p in parameters), path
            assert operation.get("security") == [{"FactoryToken": []}], path
    schemes = cast(dict[str, dict[str, object]], schema["components"])["securitySchemes"]
    assert schemes["FactoryToken"] == {
        "type": "apiKey",
        "in": "header",
        "name": HEADER,
        "description": "The factory link's token: the part of the link after `#`",
    }


async def test_the_factory_header_passes_the_cors_preflight() -> None:
    settings = Settings.model_validate(
        {
            "environment": "dev",
            "database_url": "postgresql://app@localhost:5432/unused",
            "cors_allowed_origins": ["https://factory.example.test"],
        }
    )
    transport = httpx.ASGITransport(app=create_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.options(
            ORDER,
            headers={
                "Origin": "https://factory.example.test",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "x-factory-token",
            },
        )
    assert response.status_code == 200
    allowed = {
        h.strip().lower() for h in response.headers["access-control-allow-headers"].split(",")
    }
    assert "x-factory-token" in allowed
