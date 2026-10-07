"""Cross-origin access for the frontend.

The frontend is served from another origin than the api (Vercel in dev:
app.dev.usebido.com calling api.dev.usebido.com). Browsers then send a CORS
preflight before any request with an Authorization header, which this
middleware answers before routing, so preflights never reach auth.

No credentials: the frontend sends the Clerk session token as a bearer
token, never a cookie, so `Access-Control-Allow-Credentials` stays off.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bido_back.settings import Settings

ALLOWED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
# Authorization: the Clerk bearer token. Accept/Cache-Control/Last-Event-ID:
# the Copilot's Server-Sent Events stream.
ALLOWED_HEADERS = ["Authorization", "Content-Type", "Accept", "Cache-Control", "Last-Event-ID"]
# Exports name their file in Content-Disposition.
EXPOSED_HEADERS = ["Content-Disposition"]
# How long a browser may cache a preflight answer.
PREFLIGHT_MAX_AGE_S = 600


def add_cors(app: FastAPI, settings: Settings) -> None:
    """Allow the configured frontend origins. Without any, nothing is added."""
    if not settings.cors_allowed_origins and settings.cors_allowed_origin_regex is None:
        return
    headers = list(ALLOWED_HEADERS)
    if settings.environment == "local":
        headers.append("X-Dev-User")  # dev-mode auth; refused anywhere else
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_origin_regex=settings.cors_allowed_origin_regex,
        allow_credentials=False,
        allow_methods=ALLOWED_METHODS,
        allow_headers=headers,
        expose_headers=EXPOSED_HEADERS,
        max_age=PREFLIGHT_MAX_AGE_S,
    )
