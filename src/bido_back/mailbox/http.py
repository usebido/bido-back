"""Authorized calls to a mailbox provider: bearer token, refresh, bounded retries.

* An access token that is (about to be) expired is refreshed before the call.
* A 401 refreshes the token and retries the call once.
* A refresh refused with `invalid_grant` means consent was revoked: the
  connection is marked 'revoked' and `ConsentRevoked` is raised.
* 429 and 5xx are retried with bounded exponential backoff (Retry-After is
  honored up to a cap). A call that is not idempotent (sending) is retried on
  429 only: a 5xx after a send is ambiguous, and a retry could send twice.

Tokens appear only in the Authorization header; nothing here logs a token, a
URL query or a body.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import cast

import httpx

from bido_back.mailbox.base import ConsentRevoked, Provider, ProviderError, TokenSet, TokenStore

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 15.0
MAX_RETRIES = 3
BASE_BACKOFF_S = 0.5
MAX_BACKOFF_S = 20.0

Refresher = Callable[[TokenSet], Awaitable[TokenSet]]
Sleep = Callable[[float], Awaitable[None]]


class AuthorizedHttp:
    def __init__(
        self,
        *,
        provider: Provider,
        tokens: TokenSet,
        refresh: Refresher,
        store: TokenStore,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self.provider: Provider = provider
        self._tokens = tokens
        self._refresh = refresh
        self._store = store
        self._client = client
        self._sleep = sleep
        self._max_retries = max_retries

    @property
    def tokens(self) -> TokenSet:
        return self._tokens

    async def request(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        idempotent: bool = True,
        params: Mapping[str, str] | None = None,
        json: object = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        ok: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        """Send one call; return the response if its status is in `ok`."""
        if self._tokens.expired():
            await self._do_refresh()

        refreshed = False
        attempt = 0
        while True:
            response = await self._send(method, url, params, json, content, headers)
            status = response.status_code
            if status in ok:
                return response
            if status == 401 and not refreshed:
                refreshed = True
                await self._do_refresh()
                continue
            retryable = status == 429 or (idempotent and status >= 500)
            if retryable and attempt < self._max_retries:
                delay = _backoff(attempt, response.headers.get("Retry-After"))
                attempt += 1
                logger.info(
                    "%s %s throttled status=%s retry=%s", self.provider, operation, status, attempt
                )
                await self._sleep(delay)
                continue
            raise ProviderError(self.provider, status, operation)

    async def _send(
        self,
        method: str,
        url: str,
        params: Mapping[str, str] | None,
        json: object,
        content: bytes | None,
        headers: Mapping[str, str] | None,
    ) -> httpx.Response:
        all_headers = {**(headers or {}), "Authorization": f"Bearer {self._tokens.access_token}"}
        if self._client is not None:
            return await self._client.request(
                method, url, params=params, json=json, content=content, headers=all_headers
            )
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
            return await client.request(
                method, url, params=params, json=json, content=content, headers=all_headers
            )

    async def _do_refresh(self) -> None:
        try:
            new_tokens = await self._refresh(self._tokens)
        except ConsentRevoked:
            await self._store.mark_revoked()
            logger.warning("%s consent revoked; connection marked revoked", self.provider)
            raise
        self._tokens = new_tokens
        await self._store.save(new_tokens)


def _backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after is not None:
        try:
            return min(max(float(retry_after), 0.0), MAX_BACKOFF_S)
        except ValueError:
            pass
    return min(BASE_BACKOFF_S * (2**attempt), MAX_BACKOFF_S)


def json_object(response: httpx.Response) -> dict[str, object]:
    """The response body as a JSON object; ValueError otherwise."""
    body: object = response.json()
    if not isinstance(body, dict):
        raise ValueError("provider response is not a JSON object")
    return cast(dict[str, object], body)


def str_field(obj: Mapping[str, object], key: str) -> str | None:
    value = obj.get(key)
    return value if isinstance(value, str) and value else None


def list_field(obj: Mapping[str, object], key: str) -> list[dict[str, object]]:
    value = obj.get(key)
    if not isinstance(value, list):
        return []
    return [cast(dict[str, object], v) for v in cast(list[object], value) if isinstance(v, dict)]
