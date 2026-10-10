"""Privy's REST API: the company's server wallet and `signTransaction`.

Every call sends HTTP Basic `app_id:app_secret` and `privy-app-id`. Creating
and reading a wallet needs nothing else. Signing with a wallet owned by Bido's
key quorum also needs `privy-authorization-signature`: base64 of the DER
ECDSA P-256 / SHA-256 signature, made with Bido's authorization key, over the
RFC 8785 JSON of

    {"version": 1, "method": "POST", "url": <the exact URL>, "body": <the body>,
     "headers": {"privy-app-id", "privy-idempotency-key", "privy-request-expiry"}}

(`authorization_payload`). The request expires two minutes after it is signed
(`privy-request-expiry`, Unix milliseconds), so a leaked signed request is soon
useless. The key is base64 PKCS#8 DER; the dashboard's `wallet-auth:` prefix
is stripped.

`signTransaction` signs and never broadcasts: Privy fills only the wallet's
signature slot of the transaction it is given. Privy caches an error under its
idempotency key (4xx and 5xx alike, for `/rpc` and wallet creation), so a
retryable `ProviderError` from here must be retried with a NEW key. Nothing is
retried here, and nothing logs a body, a header, a key or the transaction.
"""

import base64
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Final

import httpx
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from bido_back.payments.errors import ProviderError
from bido_back.payments.providers.http import (
    BAD_RESPONSE,
    canonical_json,
    checked_idempotency_key,
    loggable_id,
    parse,
    path_segment,
    send,
)

logger = logging.getLogger(__name__)

PROVIDER: Final = "privy"
PRIVY_API_URL: Final = "https://api.privy.io"
SOLANA: Final = "solana"
REQUEST_TTL_MS: Final = 120_000
# Privy accepts idempotency keys of up to 256 characters.
MAX_IDEMPOTENCY_KEY: Final = 256

_TIMEOUT: Final = httpx.Timeout(20.0, connect=5.0)
_KEY_PREFIXES: Final = ("wallet-auth:", "wallet-api:")
_CODE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
# Privy error codes after which a new request (new expiry, new key) can succeed.
_RETRYABLE_CODES: Final = frozenset({"request_expired"})


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Wallet(_Model):
    """A Privy wallet: who owns it decides who may sign with it."""

    id: str
    address: str
    chain_type: str
    owner_id: str | None = None
    external_id: str | None = None


class _SignedTransaction(_Model):
    signed_transaction: str
    encoding: str | None = None


class _RpcResponse(_Model):
    data: _SignedTransaction


class _ErrorBody(_Model):
    """Privy's error body. `error` is prose and is never read."""

    code: str | None = None


def load_authorization_key(value: str) -> ec.EllipticCurvePrivateKey:
    """A Privy authorization key: base64 PKCS#8 DER P-256, with or without its prefix."""
    text = value.strip()
    for prefix in _KEY_PREFIXES:
        text = text.removeprefix(prefix)
    try:
        key = serialization.load_der_private_key(base64.b64decode(text, validate=True), None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise ValueError("the Privy authorization key is not a base64 PKCS#8 private key") from None
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("the Privy authorization key is not a P-256 key")
    return key


def authorization_payload(
    *, method: str, url: str, body: JsonValue, headers: Mapping[str, str]
) -> bytes:
    """The bytes `privy-authorization-signature` signs.

    `headers` are only the `privy-` headers the request sends (the app id, and the
    idempotency key and expiry when present): never authentication, content type
    or trace headers.
    """
    signed_headers: dict[str, JsonValue] = {name: value for name, value in headers.items()}
    return canonical_json(
        {"version": 1, "method": method, "url": url, "body": body, "headers": signed_headers}
    )


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class PrivyClient:
    """Privy's wallet API for one app, signing with Bido's authorization key."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        authorization_key: str,
        *,
        base_url: str = PRIVY_API_URL,
        http: httpx.AsyncClient | None = None,
        clock_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self._app_id = app_id
        self._auth = httpx.BasicAuth(app_id, app_secret)
        self._key = load_authorization_key(authorization_key)
        self._base_url = base_url.rstrip("/")
        self._http = http
        self._clock_ms = clock_ms

    def __repr__(self) -> str:
        return f"PrivyClient(app_id={self._app_id!r})"

    async def create_wallet(
        self,
        *,
        external_id: str,
        owner_id: str,
        policy_ids: Sequence[str],
        idempotency_key: str,
    ) -> Wallet:
        """`POST /v1/wallets`: a Solana wallet owned by the key quorum `owner_id`."""
        op = "create_wallet"
        body: dict[str, JsonValue] = {
            "chain_type": SOLANA,
            "owner_id": owner_id,
            "external_id": external_id,
        }
        if policy_ids:
            body["policy_ids"] = [policy_id for policy_id in policy_ids]
        headers = {
            "privy-app-id": self._app_id,
            "privy-idempotency-key": checked_idempotency_key(
                idempotency_key, provider=PROVIDER, operation=op, max_length=MAX_IDEMPOTENCY_KEY
            ),
            "Content-Type": "application/json",
        }
        response = await self._send(
            "POST",
            f"{self._base_url}/v1/wallets",
            op,
            headers=headers,
            content=canonical_json(body),
        )
        if not response.is_success:
            raise _failure(response, op)
        wallet = parse(Wallet, response, provider=PROVIDER, operation=op)
        logger.info("privy wallet created wallet=%s", loggable_id(wallet.id))
        return wallet

    async def get_wallet_by_external_id(self, external_id: str) -> Wallet | None:
        """`GET /v1/wallets/ext_wal_<external_id>`: the wallet, or None when there is none."""
        op = "get_wallet"
        segment = path_segment(f"ext_wal_{external_id}", provider=PROVIDER, operation=op)
        response = await self._send(
            "GET",
            f"{self._base_url}/v1/wallets/{segment}",
            op,
            headers={"privy-app-id": self._app_id},
        )
        if response.status_code == 404:
            return None
        if not response.is_success:
            raise _failure(response, op)
        return parse(Wallet, response, provider=PROVIDER, operation=op)

    async def sign_transaction(
        self, wallet_id: str, transaction: bytes, *, idempotency_key: str
    ) -> bytes:
        """`POST /v1/wallets/{id}/rpc` `signTransaction`: `transaction` signed by the wallet."""
        op = "sign_transaction"
        segment = path_segment(wallet_id, provider=PROVIDER, operation=op)
        url = f"{self._base_url}/v1/wallets/{segment}/rpc"
        body: dict[str, JsonValue] = {
            "method": "signTransaction",
            "chain_type": SOLANA,
            "params": {
                "transaction": base64.b64encode(transaction).decode("ascii"),
                "encoding": "base64",
            },
        }
        privy_headers = {
            "privy-app-id": self._app_id,
            "privy-idempotency-key": checked_idempotency_key(
                idempotency_key, provider=PROVIDER, operation=op, max_length=MAX_IDEMPOTENCY_KEY
            ),
            "privy-request-expiry": str(self._clock_ms() + REQUEST_TTL_MS),
        }
        payload = authorization_payload(method="POST", url=url, body=body, headers=privy_headers)
        signature = base64.b64encode(self._key.sign(payload, ec.ECDSA(hashes.SHA256())))
        headers = {
            **privy_headers,
            "privy-authorization-signature": signature.decode("ascii"),
            "Content-Type": "application/json",
        }
        response = await self._send("POST", url, op, headers=headers, content=canonical_json(body))
        if not response.is_success:
            raise _failure(response, op)
        signed = parse(_RpcResponse, response, provider=PROVIDER, operation=op).data
        if signed.encoding not in (None, "base64"):
            raise ProviderError(PROVIDER, op, BAD_RESPONSE, status=response.status_code)
        try:
            return base64.b64decode(signed.signed_transaction, validate=True)
        except ValueError:
            raise ProviderError(PROVIDER, op, BAD_RESPONSE, status=response.status_code) from None

    async def _send(
        self,
        method: str,
        url: str,
        operation: str,
        *,
        headers: Mapping[str, str],
        content: bytes | None = None,
    ) -> httpx.Response:
        return await send(
            self._http,
            method,
            url,
            provider=PROVIDER,
            operation=operation,
            timeouts=_TIMEOUT,
            # Reads are safe to repeat; a create or a signature with a new key too
            # (signing broadcasts nothing; the external id makes a wallet unique).
            retry_safe=True,
            headers=headers,
            content=content,
            auth=self._auth,
        )


def _privy_code(response: httpx.Response) -> str | None:
    try:
        code = _ErrorBody.model_validate_json(response.content).code
    except ValidationError:
        return None
    return code if code is not None and _CODE.fullmatch(code) else None


def _failure(response: httpx.Response, operation: str) -> ProviderError:
    status = response.status_code
    code = _privy_code(response) or f"http_{status}"
    retryable = status == 429 or status >= 500 or code in _RETRYABLE_CODES
    logger.warning("privy %s failed status=%s code=%s", operation, status, code)
    return ProviderError(PROVIDER, operation, code, status=status, retryable=retryable)
