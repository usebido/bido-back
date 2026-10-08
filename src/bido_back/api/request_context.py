"""Request ids, the access log, and error bodies that name the request.

Every request gets an id: the caller's `X-Request-ID` when it is a plain token,
else a new one. It is bound to every log line written while the request runs
(`bido_back.logs.bind`), returned in the `X-Request-ID` response header (exposed
to the browser by CORS), and added to every error body as `request_id`, so an
error a buyer sees can be found in CloudWatch.

One access line per request, written when the response is finished (for the
Copilot's stream, when the stream ends): method, route template (never the raw
path or query string, which can carry file names), status and duration. Load
balancer health checks are logged at DEBUG.

An exception nothing handled is logged here, with the request's fields, and
answered with a 500 that still passes through CORS and carries the request id.
Once a response has started (a stream), it can only be logged and re-raised.
"""

import logging
import re
import time
from typing import Final
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from bido_back.logs import bind, bound, safe_reason

logger = logging.getLogger("bido_back.api.access")
errors_logger = logging.getLogger("bido_back.api.errors")

REQUEST_ID_HEADER: Final = "X-Request-ID"
_VALID_ID: Final = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
_QUIET_ROUTES: Final = frozenset({"/health"})


def request_id_of(scope: Scope) -> str | None:
    state = scope.get("state")
    value = state.get("request_id") if isinstance(state, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return value if isinstance(value, str) else None


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_id(scope) or uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        status: int | None = None

        async def send_with_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        with bind(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_id)
            except Exception:
                errors_logger.exception(
                    "unhandled error",
                    extra={"method": scope.get("method"), "route": _route(scope)},
                )
                if status is not None:
                    raise
                status = 500
                response = JSONResponse(
                    {"detail": "internal server error", "request_id": request_id},
                    status_code=500,
                    headers={REQUEST_ID_HEADER: request_id},
                )
                await response(scope, receive, send)
            finally:
                route = _route(scope)
                logger.log(
                    logging.DEBUG if route in _QUIET_ROUTES else logging.INFO,
                    "request",
                    extra={
                        "method": scope.get("method"),
                        "route": route,
                        "status": status,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    },
                )


def _incoming_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name == b"x-request-id":
            candidate = value.decode("latin-1").strip()
            return candidate if _VALID_ID.match(candidate) else None
    return None


def _route(scope: Scope) -> str:
    # FastAPI puts the matched route in the scope; its path is the template
    # (/v1/rfqs/{rfq_id}), never the ids or the query string.
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "(unmatched)"


# --- Error bodies --------------------------------------------------------------------


async def _http_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    request_id = request_id_of(request.scope) or bound("request_id")
    detail = exc.detail
    errors_logger.log(
        logging.WARNING if exc.status_code >= 500 else logging.INFO,
        "http error",
        extra={
            "status": exc.status_code,
            "route": _route(request.scope),
            "detail": safe_reason(str(detail)),
        },
    )
    headers = dict(exc.headers or {})
    if exc.status_code in (204, 304):
        return Response(status_code=exc.status_code, headers=headers)
    return JSONResponse(
        {"detail": detail, "request_id": request_id},
        status_code=exc.status_code,
        headers=headers,
    )


async def _validation_error(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)
    errors = exc.errors()
    # Where and what, never the input values.
    errors_logger.info(
        "request validation failed",
        extra={
            "status": 422,
            "route": _route(request.scope),
            "fields": [
                f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('type', '')}"
                for err in errors
            ][:20],
        },
    )
    return JSONResponse(
        {"detail": jsonable_encoder(errors), "request_id": request_id_of(request.scope)},
        status_code=422,
    )


def add_request_context(app: FastAPI) -> None:
    """Install the middleware and the error handlers. Call before CORS is added."""
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_middleware(RequestContextMiddleware)
