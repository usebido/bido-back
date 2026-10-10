"""One provider call over httpx: an explicit timeout, transport failures as `ProviderError`.

A call uses the injected `httpx.AsyncClient` (tests, a shared client) or opens
a short-lived one, and always passes its own timeout: a shared client's
defaults never decide how long a payment call may wait. Nothing here retries
or logs. A timeout or a network failure becomes `ProviderError` with code
`timeout` or `network_error`, raised `from None` because httpx exceptions can
carry the URL. The caller states whether repeating the call is safe (a read, a
delete, or a write with an idempotency key); that is the error's `retryable`.
"""

import json
import re
from collections.abc import Mapping
from typing import Final

import httpx
from pydantic import BaseModel, JsonValue, ValidationError

from bido_back.payments.errors import ProviderError

TIMEOUT: Final = "timeout"
NETWORK_ERROR: Final = "network_error"
BAD_RESPONSE: Final = "bad_response"
INVALID_REFERENCE: Final = "invalid_reference"
INVALID_IDEMPOTENCY_KEY: Final = "invalid_idempotency_key"

# Provider ids (`pm_...`, `alw_...`, UUIDs, Privy ids): one path segment, nothing that
# could add a segment, a query or a fragment to the URL.
_PATH_SEGMENT: Final = re.compile(r"[A-Za-z0-9_-]{1,200}")
# Visible ASCII only: a header value, the same bytes on every retry.
_IDEMPOTENCY_KEY: Final = re.compile(r"[!-~]{1,255}")
# Ids we log from provider headers and bodies (trace ids, request ids, credential ids).
_LOGGABLE_ID: Final = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def canonical_json(value: JsonValue) -> bytes:
    """Sorted keys, no whitespace, UTF-8, no NaN.

    That is RFC 8785 (JCS) for the JSON we send (no floats, ASCII keys): Privy
    verifies its authorization signature over this form. Every provider gets
    the same bytes for the same request, so a retry under an idempotency key
    repeats its body exactly.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def path_segment(value: str, *, provider: str, operation: str) -> str:
    """`value` as one URL path segment; anything else is refused before a request is made."""
    if not _PATH_SEGMENT.fullmatch(value):
        raise ProviderError(provider, operation, INVALID_REFERENCE)
    return value


def checked_idempotency_key(
    value: str, *, provider: str, operation: str, max_length: int = 255
) -> str:
    """`value` as an idempotency key header: visible ASCII, at most `max_length` characters."""
    if len(value) > max_length or not _IDEMPOTENCY_KEY.fullmatch(value):
        raise ProviderError(provider, operation, INVALID_IDEMPOTENCY_KEY)
    return value


def loggable_id(value: str | None) -> str:
    """A provider id fit for a log line, or `-` (never a value we did not expect)."""
    return value if value is not None and _LOGGABLE_ID.fullmatch(value) else "-"


def parse[M: BaseModel](
    model: type[M], response: httpx.Response, *, provider: str, operation: str
) -> M:
    """The response body as `model`. Never chains the ValidationError: it quotes the input."""
    try:
        return model.model_validate_json(response.content)
    except ValidationError:
        raise ProviderError(
            provider, operation, BAD_RESPONSE, status=response.status_code
        ) from None


async def send(
    http: httpx.AsyncClient | None,
    method: str,
    url: str,
    *,
    provider: str,
    operation: str,
    timeouts: httpx.Timeout,
    retry_safe: bool,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, str] | None = None,
    content: bytes | None = None,
    data: Mapping[str, str] | None = None,
    auth: httpx.Auth | None = None,
) -> httpx.Response:
    """Send one request and return the response, whatever its status."""
    try:
        if http is not None:
            return await http.request(
                method,
                url,
                headers=headers,
                params=params,
                content=content,
                data=data,
                auth=auth,
                timeout=timeouts,
            )
        async with httpx.AsyncClient(timeout=timeouts) as client:
            return await client.request(
                method, url, headers=headers, params=params, content=content, data=data, auth=auth
            )
    except httpx.TimeoutException:
        raise ProviderError(provider, operation, TIMEOUT, retryable=retry_safe) from None
    except httpx.HTTPError:
        raise ProviderError(provider, operation, NETWORK_ERROR, retryable=retry_safe) from None
