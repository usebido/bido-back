"""Basis Theory's REST API: vault tokens and Agentic Payments.

One base URL serves the vault (`/tokens`) and the agentic surface
(`/agentic/...`); the key decides the tenant (test tenants live on
`api.test.basistheory.com`). Every call sends `BT-API-KEY`; writes send a JSON
body and `BT-IDEMPOTENCY-KEY`.

* Agentic idempotency keys are durable and keep failed results: the same key
  replays the same outcome, and the same key with another body is refused
  (`409 IDEMPOTENCY_CONFLICT`). A replayed mint answers
  `409 CREDENTIAL_PAYLOAD_UNAVAILABLE` (the one-time value is gone) and a mint
  whose outcome is unknown `409 CREDENTIAL_OUTCOME_UNKNOWN` (the capacity is
  spent); both raise `CredentialPayloadUnavailable`, so the card gateway can
  look for the charge it may already have made.
* `409 *_IN_PROGRESS` means "repeat the same request with the same key, later":
  retryable. So are 429 and 5xx when repeating is safe (a read, a delete, or a
  write with a key). Anything else is final for that key.

Errors are RFC 7807 problems: only `type`, the machine code, is read, never
`title` or `detail`. Each failure logs the operation, the status, the code,
the `BT-TRACE-ID` response header and `debug.provider_correlation` (what Basis
Theory support asks for), never a body. A token's `data` is never parsed, and
a minted credential's value is only ever held as a `SecretStr`.
"""

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Final, cast
from uuid import UUID

import httpx
from pydantic import BaseModel, BeforeValidator, ConfigDict, JsonValue, SecretStr, ValidationError

from bido_back.payments.errors import ProviderError
from bido_back.payments.providers.http import (
    canonical_json,
    checked_idempotency_key,
    loggable_id,
    parse,
    path_segment,
    send,
)

logger = logging.getLogger(__name__)

PROVIDER: Final = "basis_theory"
# Agentic error types after which the payload of a mint is gone for good.
CREDENTIAL_UNAVAILABLE_CODES: Final = frozenset(
    {"CREDENTIAL_PAYLOAD_UNAVAILABLE", "CREDENTIAL_OUTCOME_UNKNOWN"}
)
IN_PROGRESS_SUFFIX: Final = "_IN_PROGRESS"
MISSING_CREDENTIAL: Final = "missing_credential"
UNEXPECTED_CREDENTIAL_FORMAT: Final = "unexpected_credential_format"

_TIMEOUT: Final = httpx.Timeout(20.0, connect=5.0)
# The mint's response carries the one-time token: a client timeout after Basis
# Theory minted loses it (the capacity stays spent), so wait longer than usual.
_MINT_TIMEOUT: Final = httpx.Timeout(45.0, connect=5.0)
_SAFE_METHODS: Final = frozenset({"GET", "DELETE"})
_CODE: Final = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
_CODE_SEPARATORS: Final = re.compile(r"[/#]")
_CENT: Final = Decimal("0.01")


class CredentialPayloadUnavailable(ProviderError):
    """A mint was replayed, or its outcome is unknown: its one-time value cannot be read again."""


# --- Response models (only what we use; `extra="ignore"` drops the rest) -----------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


def _empty_if_none(value: object) -> object:
    return () if value is None else value


class Rail(_Model):
    """One rail of a payment method or an allowance (`spt`/`stripe`, `agentic-token`/`vic`)."""

    rail: str | None = None
    provider: str | None = None
    # Payment method rails: enabled | pending | error. Allowance rails: active |
    # pending_verification | error.
    status: str | None = None


Rails = Annotated[tuple[Rail, ...], BeforeValidator(_empty_if_none)]


def _string_or_none(value: object) -> object:
    return value if isinstance(value, str) else None


def _object_or_none(value: object) -> object:
    if isinstance(value, dict | BaseModel):
        return cast(object, value)
    return None


# Branding is optional: a value of another type reads as absent, never as a bad response.
_OptionalText = Annotated[str | None, BeforeValidator(_string_or_none)]


class CardDisplay(_Model):
    """The issuer's branding of a card (`card.display`). Cleaned by `payments.card_display`."""

    art_url: _OptionalText = None
    background_color: _OptionalText = None
    foreground_color: _OptionalText = None
    description: _OptionalText = None
    issuer_name: _OptionalText = None


class CardIssuer(_Model):
    name: _OptionalText = None
    country: _OptionalText = None


class CardDetails(_Model):
    """What Basis Theory tells about a card: never its number."""

    brand: str | None = None
    last4: str | None = None
    expiration_month: int | None = None
    expiration_year: int | None = None
    funding: str | None = None
    display: Annotated[CardDisplay | None, BeforeValidator(_object_or_none)] = None
    issuer: Annotated[CardIssuer | None, BeforeValidator(_object_or_none)] = None


class Token(_Model):
    """A vault token's metadata. Its `data` is not a field: it is never read."""

    id: str
    type: str | None = None
    fingerprint: str | None = None
    card: CardDetails | None = None


class PaymentMethod(_Model):
    """An agentic payment method (`pm_...`): whether each rail can be used for this card."""

    id: str
    status: str | None = None
    card: CardDetails | None = None
    rails: Rails = ()


class Allowance(_Model):
    """An allowance (`alw_...`): the ceiling, merchant and expiry one payment may use."""

    id: str
    status: str | None = None  # active | cancelled | expired
    expires_at: datetime | None = None
    rails: Rails = ()


class _CredentialIdentifier(_Model):
    id: SecretStr | None = None
    token: SecretStr | None = None
    identifier: SecretStr | None = None


class _CredentialBody(_Model):
    format: str | None = None
    value: SecretStr | _CredentialIdentifier | None = None


class _MintResponse(_Model):
    id: str | None = None
    credential: _CredentialBody | None = None


class _ProblemDebug(_Model):
    provider_correlation: str | None = None


class _Problem(_Model):
    type: str | None = None
    debug: _ProblemDebug | None = None


# --- Our side of the calls ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Merchant:
    """The merchant an allowance is scoped to (Bido, which collects the payment)."""

    name: str
    url: str
    country_code: str


@dataclass(frozen=True, slots=True)
class MintedCredential:
    """An `identifier` credential: `token` is the one-time Stripe Shared Payment Token."""

    id: str | None  # `cred_...`
    token: SecretStr


def find_rail(rails: tuple[Rail, ...], *, rail: str, provider: str) -> Rail | None:
    return next((r for r in rails if r.rail == rail and r.provider == provider), None)


def money_value(amount: Decimal) -> str:
    """Basis Theory's major-unit decimal string (`12.30`). The amount must be whole cents."""
    quantized = amount.quantize(_CENT)
    if quantized != amount or quantized <= 0:
        raise ValueError("an amount is a positive number of whole cents")
    return f"{quantized:f}"


def _money(amount: Decimal, currency: str) -> dict[str, JsonValue]:
    return {"value": money_value(amount), "currency": currency}


def _utc_text(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("a timestamp sent to Basis Theory must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _problem_code(problem: _Problem | None, status: int) -> str:
    """The problem's `type` when it is (or ends with) a machine code, else `http_<status>`."""
    raw = problem.type.strip() if problem is not None and problem.type else ""
    tail = _CODE_SEPARATORS.split(raw)[-1] if raw else ""
    return tail if _CODE.fullmatch(tail) else f"http_{status}"


def _read_problem(response: httpx.Response) -> _Problem | None:
    try:
        return _Problem.model_validate_json(response.content)
    except ValidationError:
        return None


def _identifier(minted: _MintResponse) -> SecretStr | None:
    value = minted.credential.value if minted.credential is not None else None
    if isinstance(value, _CredentialIdentifier):
        value = value.id or value.token or value.identifier
    if value is None or not value.get_secret_value().strip():
        return None
    return value


class BasisTheoryClient:
    """The calls Bido makes to Basis Theory with its private (backend) key."""

    def __init__(
        self, base_url: str, api_key: str, *, http: httpx.AsyncClient | None = None
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._http = http

    def __repr__(self) -> str:
        return f"BasisTheoryClient({self._base_url!r})"

    # --- Vault tokens ---------------------------------------------------------------------------

    async def get_token(self, token_id: str) -> Token | None:
        """`GET /tokens/{id}`: the card's fingerprint and details. None when it does not exist."""
        op = "get_token"
        segment = path_segment(token_id, provider=PROVIDER, operation=op)
        response = await self._request("GET", f"/tokens/{segment}", op, absent_ok=True)
        if response.status_code == 404:
            return None
        return parse(Token, response, provider=PROVIDER, operation=op)

    # --- Agentic payment methods ----------------------------------------------------------------

    async def create_payment_method(
        self,
        *,
        token_id: str,
        consumer_id: UUID,
        email: str | None,
        country_code: str,
        idempotency_key: str,
    ) -> PaymentMethod:
        """`POST /agentic/payment-methods` from a card token. No ceremony; read the rails."""
        op = "create_payment_method"
        consumer: dict[str, JsonValue] = {
            "id": str(consumer_id),
            "country_code": country_code,
            "language_code": "en",
        }
        if email:
            consumer["email"] = email
        body: dict[str, JsonValue] = {
            "source": {"type": "basis_theory_card_token", "token_id": token_id},
            "consumer": consumer,
        }
        response = await self._request(
            "POST", "/agentic/payment-methods", op, body=body, key=idempotency_key
        )
        return parse(PaymentMethod, response, provider=PROVIDER, operation=op)

    async def get_payment_method(self, payment_method_id: str) -> PaymentMethod | None:
        op = "get_payment_method"
        segment = path_segment(payment_method_id, provider=PROVIDER, operation=op)
        response = await self._request(
            "GET", f"/agentic/payment-methods/{segment}", op, absent_ok=True
        )
        if response.status_code == 404:
            return None
        return parse(PaymentMethod, response, provider=PROVIDER, operation=op)

    async def delete_payment_method(self, payment_method_id: str) -> None:
        """`DELETE /agentic/payment-methods/{id}`; cancels its allowances. Gone already is fine."""
        op = "delete_payment_method"
        segment = path_segment(payment_method_id, provider=PROVIDER, operation=op)
        await self._request("DELETE", f"/agentic/payment-methods/{segment}", op, absent_ok=True)

    # --- Allowances and credentials -------------------------------------------------------------

    async def create_allowance(
        self,
        *,
        payment_method_id: str,
        amount: Decimal,
        currency: str,
        merchant: Merchant,
        description: str,
        expires_at: datetime,
        idempotency_key: str,
        metadata: Mapping[str, str] | None = None,
    ) -> Allowance:
        """`POST /agentic/allowances`, scoped to `merchant` (so a mint names no merchant)."""
        op = "create_allowance"
        body: dict[str, JsonValue] = {
            "payment_method_id": payment_method_id,
            "amount": _money(amount, currency),
            "merchant": {
                "name": merchant.name,
                "url": merchant.url,
                "country_code": merchant.country_code,
            },
            "description": description,
            "expires_at": _utc_text(expires_at),
        }
        if metadata:
            tags: dict[str, JsonValue] = {name: value for name, value in metadata.items()}
            body["metadata"] = tags
        response = await self._request(
            "POST", "/agentic/allowances", op, body=body, key=idempotency_key
        )
        return parse(Allowance, response, provider=PROVIDER, operation=op)

    async def get_allowance(self, allowance_id: str) -> Allowance | None:
        op = "get_allowance"
        segment = path_segment(allowance_id, provider=PROVIDER, operation=op)
        response = await self._request("GET", f"/agentic/allowances/{segment}", op, absent_ok=True)
        if response.status_code == 404:
            return None
        return parse(Allowance, response, provider=PROVIDER, operation=op)

    async def mint_identifier(
        self,
        allowance_id: str,
        *,
        rail: str,
        provider: str,
        amount: Decimal,
        currency: str,
        network_business_profile: str,
        idempotency_key: str,
    ) -> MintedCredential:
        """`POST /agentic/allowances/{id}/credentials`, format `identifier`: a processor token.

        Spends `amount` of the allowance for good (there is no void). The value is
        returned once: replaying `idempotency_key` raises
        `CredentialPayloadUnavailable`.
        """
        op = "mint_credential"
        segment = path_segment(allowance_id, provider=PROVIDER, operation=op)
        body: dict[str, JsonValue] = {
            "rail": rail,
            "provider": provider,
            "amount": _money(amount, currency),
            "credential": {
                "format": "identifier",
                # The token's grantee: Bido's Stripe Business Network profile.
                "payload": {"network_business_profile": network_business_profile},
            },
        }
        response = await self._request(
            "POST",
            f"/agentic/allowances/{segment}/credentials",
            op,
            body=body,
            key=idempotency_key,
            timeouts=_MINT_TIMEOUT,
        )
        minted = parse(_MintResponse, response, provider=PROVIDER, operation=op)
        credential_format = minted.credential.format if minted.credential is not None else None
        if credential_format not in (None, "identifier"):
            raise ProviderError(PROVIDER, op, UNEXPECTED_CREDENTIAL_FORMAT)
        token = _identifier(minted)
        if token is None:
            raise ProviderError(PROVIDER, op, MISSING_CREDENTIAL, status=response.status_code)
        logger.info(
            "basis_theory credential minted credential=%s trace=%s",
            loggable_id(minted.id),
            loggable_id(response.headers.get("BT-TRACE-ID")),
        )
        return MintedCredential(id=minted.id, token=token)

    # --- Transport ------------------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        operation: str,
        *,
        body: JsonValue = None,
        key: str | None = None,
        timeouts: httpx.Timeout = _TIMEOUT,
        absent_ok: bool = False,
    ) -> httpx.Response:
        """One call; the response when it is a success (or a 404 with `absent_ok`)."""
        headers = {"BT-API-KEY": self._api_key, "Accept": "application/json"}
        content: bytes | None = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            content = canonical_json(body)
        if key is not None:
            headers["BT-IDEMPOTENCY-KEY"] = checked_idempotency_key(
                key, provider=PROVIDER, operation=operation
            )
        retry_safe = method in _SAFE_METHODS or key is not None
        response = await send(
            self._http,
            method,
            f"{self._base_url}{path}",
            provider=PROVIDER,
            operation=operation,
            timeouts=timeouts,
            retry_safe=retry_safe,
            headers=headers,
            content=content,
        )
        if response.is_success or (absent_ok and response.status_code == 404):
            return response
        raise _failure(response, operation, retry_safe=retry_safe)


def _failure(response: httpx.Response, operation: str, *, retry_safe: bool) -> ProviderError:
    status = response.status_code
    problem = _read_problem(response)
    code = _problem_code(problem, status)
    correlation = problem.debug.provider_correlation if problem and problem.debug else None
    logger.warning(
        "basis_theory %s failed status=%s code=%s trace=%s correlation=%s",
        operation,
        status,
        code,
        loggable_id(response.headers.get("BT-TRACE-ID")),
        loggable_id(correlation),
    )
    if code in CREDENTIAL_UNAVAILABLE_CODES:
        return CredentialPayloadUnavailable(PROVIDER, operation, code, status=status)
    retryable = code.endswith(IN_PROGRESS_SUFFIX) or (
        retry_safe and (status == 429 or status >= 500)
    )
    return ProviderError(PROVIDER, operation, code, status=status, retryable=retryable)
