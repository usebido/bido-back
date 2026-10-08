"""Request ids, the access log, and error bodies that name the request."""

import json
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from bido_back.api.cors import add_cors
from bido_back.api.request_context import add_request_context
from bido_back.logs import JsonFormatter
from bido_back.settings import Settings

CONTENT = "customer-content-7f3a"
ORIGIN = "https://app.bido.test"


class Body(BaseModel):
    quantity: int


class JsonLines(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(JsonFormatter(service="api", release=None))
        self.lines: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(self.format(record)))


@pytest.fixture
def lines() -> Iterator[list[dict[str, Any]]]:
    handler = JsonLines()
    logger = logging.getLogger("bido_back.api")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    yield handler.lines
    logger.removeHandler(handler)
    logger.setLevel(previous)


def build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    @app.get("/v1/things/{thing_id}")
    async def thing(thing_id: str) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        if thing_id == "missing":
            raise HTTPException(404, f"no thing {CONTENT!r}")
        if thing_id == "boom":
            raise RuntimeError(f"failed on {CONTENT!r}")
        return {"id": thing_id}

    @app.post("/v1/things")
    async def create(body: Body) -> Body:  # pyright: ignore[reportUnusedFunction]
        return body

    add_request_context(app)
    add_cors(
        app,
        Settings.model_validate(
            {
                "environment": "test",
                "database_url": "postgresql+asyncpg://app@localhost:5432/unused",
                "cors_allowed_origins": [ORIGIN],
            }
        ),
    )
    return app


@pytest_asyncio.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_every_response_names_its_request(
    http: httpx.AsyncClient, lines: list[dict[str, Any]]
) -> None:
    response = await http.get(f"/v1/things/abc?file={CONTENT}.xlsx")

    request_id = response.headers["x-request-id"]
    assert len(request_id) == 32
    (access,) = [line for line in lines if line["msg"] == "request"]
    assert access["request_id"] == request_id
    assert access["method"] == "GET"
    # The route template, never the raw path or the query string.
    assert access["route"] == "/v1/things/{thing_id}"
    assert access["status"] == 200
    assert isinstance(access["duration_ms"], float)
    assert CONTENT not in json.dumps(lines)


async def test_a_plain_incoming_id_is_kept_and_anything_else_replaced(
    http: httpx.AsyncClient,
) -> None:
    kept = await http.get("/v1/things/a", headers={"X-Request-ID": "front-1234abcd"})
    assert kept.headers["x-request-id"] == "front-1234abcd"

    replaced = await http.get("/v1/things/a", headers={"X-Request-ID": "bad id\twith spaces"})
    assert replaced.headers["x-request-id"] != "bad id\twith spaces"


async def test_error_bodies_carry_the_request_id(
    http: httpx.AsyncClient, lines: list[dict[str, Any]]
) -> None:
    missing = await http.get("/v1/things/missing")
    invalid = await http.post("/v1/things", json={"quantity": CONTENT})

    assert missing.status_code == 404
    assert missing.json()["request_id"] == missing.headers["x-request-id"]
    assert invalid.status_code == 422
    assert invalid.json()["request_id"] == invalid.headers["x-request-id"]
    logged = json.dumps(lines)
    # The 404 detail is masked; the 422 is logged as field paths only.
    assert CONTENT not in logged
    validation = next(line for line in lines if line["msg"] == "request validation failed")
    assert validation["fields"] == ["body.quantity: int_parsing"]


async def test_an_unhandled_error_is_logged_and_answered_through_cors(
    http: httpx.AsyncClient, lines: list[dict[str, Any]]
) -> None:
    response = await http.get("/v1/things/boom", headers={"Origin": ORIGIN})

    assert response.status_code == 500
    body = response.json()
    assert body == {"detail": "internal server error", "request_id": body["request_id"]}
    assert response.headers["x-request-id"] == body["request_id"]
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "x-request-id" in response.headers["access-control-expose-headers"].lower()
    error = next(line for line in lines if line["msg"] == "unhandled error")
    assert error["level"] == "ERROR"
    assert error["request_id"] == body["request_id"]
    assert error["route"] == "/v1/things/{thing_id}"
    assert error["exc_type"] == "RuntimeError"
    assert "RuntimeError: failed on '…'" in error["exc"]
    assert CONTENT not in json.dumps(lines)


async def test_health_checks_are_logged_at_debug(
    http: httpx.AsyncClient, lines: list[dict[str, Any]]
) -> None:
    await http.get("/health")
    (access,) = [line for line in lines if line["msg"] == "request"]
    assert access["level"] == "DEBUG"


async def test_the_real_app_answers_a_crash_with_cors_headers_and_a_request_id() -> None:
    from bido_back.api.app import create_app

    app = create_app(
        Settings.model_validate(
            {
                "environment": "test",
                "database_url": "postgresql+asyncpg://app@localhost:5432/unused",
                "cors_allowed_origins": [ORIGIN],
            }
        )
    )

    async def crash() -> None:
        raise RuntimeError("boom")

    # The middleware stack is built on the first request, so a late route is wired too.
    app.add_api_route("/crash", crash)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/crash", headers={"Origin": ORIGIN})

    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == ORIGIN
    exposed = response.headers["access-control-expose-headers"].lower()
    assert "x-request-id" in exposed
    assert response.json()["request_id"] == response.headers["x-request-id"]
