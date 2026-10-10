"""Stripe: charge a Shared Payment Token on Bido's own account, and find that charge again.

Bido is the merchant of record. A card is charged with one PaymentIntent,
created and confirmed in one form-encoded call under an `Idempotency-Key`,
tagged `metadata[bido_payment_id]` so the charge can be found by payment id
when its response was lost (`find_payment_intent`, Stripe search). The token
comes from Basis Theory and is only ever held as a `SecretStr`. Charges are in
USD; Stripe refuses less than 0.50, so a smaller amount is refused before any
call.

Outcomes are ours: `succeeded`; `pending` while Stripe is `processing`;
`declined` with a code we chose (`card_declined`, `card_requires_action`,
`payment_canceled`) or Stripe's own decline code when it is a plain machine
code (`^[a-z_]{1,40}$`). Any other failure raises
`ProviderError("stripe", operation, code)`, retryable on timeouts, network
errors, 409, 429 and 5xx unless Stripe answers `Stripe-Should-Retry: false`:
every write carries an idempotency key, so repeating it cannot charge twice.
Nothing here logs or raises with a body, a header or the token.
"""

import logging
import re
from dataclasses import dataclass
from typing import Annotated, Final, Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, BeforeValidator, ConfigDict, SecretStr, ValidationError

from bido_back.payments.errors import PaymentRefused, ProviderError
from bido_back.payments.providers.http import (
    checked_idempotency_key,
    loggable_id,
    parse,
    send,
)

logger = logging.getLogger(__name__)

PROVIDER: Final = "stripe"
STRIPE_API_URL: Final = "https://api.stripe.com"
# Stripe's limits for a USD charge, in cents.
MIN_AMOUNT_CENTS: Final = 50
MAX_AMOUNT_CENTS: Final = 99_999_999
PAYMENT_ID_METADATA: Final = "bido_payment_id"
UNEXPECTED_STATUS: Final = "unexpected_status"

CARD_DECLINED: Final = "card_declined"
CARD_REQUIRES_ACTION: Final = "card_requires_action"
PAYMENT_CANCELED: Final = "payment_canceled"

_CREATE_TIMEOUT: Final = httpx.Timeout(30.0, connect=5.0)
_READ_TIMEOUT: Final = httpx.Timeout(20.0, connect=5.0)
_MACHINE_CODE: Final = re.compile(r"[a-z_]{1,40}")
# Which payment intent wins when a search finds several for one payment (lower is better).
_STATUS_RANK: Final = {
    "succeeded": 0,
    "processing": 1,
    "requires_action": 2,
    "requires_payment_method": 3,
    "canceled": 4,
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


def _empty_if_none(value: object) -> object:
    return {} if value is None else value


class _PaymentIntentRef(_Model):
    id: str | None = None


class _ErrorDetail(_Model):
    """Stripe's `error` object. `message` is prose and is never read."""

    type: str | None = None
    code: str | None = None
    decline_code: str | None = None
    payment_intent: _PaymentIntentRef | None = None


class _ErrorBody(_Model):
    error: _ErrorDetail | None = None


class _LastPaymentError(_Model):
    code: str | None = None
    decline_code: str | None = None


class PaymentIntent(_Model):
    id: str
    status: str
    amount: int | None = None
    currency: str | None = None
    metadata: Annotated[dict[str, str], BeforeValidator(_empty_if_none)] = {}
    last_payment_error: _LastPaymentError | None = None


class _SearchResult(_Model):
    data: tuple[PaymentIntent, ...] = ()


StripeOutcome = Literal["succeeded", "pending", "declined"]


@dataclass(frozen=True, slots=True)
class StripeCharge:
    """A PaymentIntent, as the card gateway needs it."""

    outcome: StripeOutcome
    payment_intent_id: str | None
    decline_code: str | None
    amount_cents: int | None
    currency: str | None


def machine_code(value: str | None) -> str | None:
    """`value` when it is a plain machine code (`insufficient_funds`), else None."""
    return value if value is not None and _MACHINE_CODE.fullmatch(value) else None


def charge_from_intent(intent: PaymentIntent) -> StripeCharge | None:
    """Our outcome for a PaymentIntent's status; None for a status a confirmed charge never has."""
    decline: str | None = None
    match intent.status:
        case "succeeded":
            outcome: StripeOutcome = "succeeded"
        case "processing":
            outcome = "pending"
        case "requires_action":
            # 3D Secure or another step only the cardholder can take: nothing was charged.
            outcome, decline = "declined", CARD_REQUIRES_ACTION
        case "requires_payment_method":
            error = intent.last_payment_error
            outcome = "declined"
            decline = (
                (machine_code(error.decline_code) or machine_code(error.code)) if error else None
            ) or CARD_DECLINED
        case "canceled":
            outcome, decline = "declined", PAYMENT_CANCELED
        case _:
            return None
    return StripeCharge(
        outcome=outcome,
        payment_intent_id=intent.id,
        decline_code=decline,
        amount_cents=intent.amount,
        currency=intent.currency,
    )


def _error_detail(response: httpx.Response) -> _ErrorDetail | None:
    try:
        return _ErrorBody.model_validate_json(response.content).error
    except ValidationError:
        return None


class StripeClient:
    """The two calls Bido makes to Stripe with its secret key."""

    def __init__(
        self,
        secret_key: str,
        *,
        api_version: str | None = None,
        http: httpx.AsyncClient | None = None,
        base_url: str = STRIPE_API_URL,
    ) -> None:
        self._secret_key = secret_key
        self._api_version = api_version
        self._http = http
        self._base_url = base_url.rstrip("/")

    def __repr__(self) -> str:
        return f"StripeClient({self._base_url!r})"

    async def create_payment_intent(
        self,
        *,
        shared_payment_token: SecretStr,
        amount_cents: int,
        description: str,
        payment_id: UUID,
        idempotency_key: str,
    ) -> StripeCharge:
        """`POST /v1/payment_intents` with `confirm=true`: charge the token now, in USD."""
        op = "create_payment_intent"
        if amount_cents < MIN_AMOUNT_CENTS:
            raise PaymentRefused("a card charge is at least 0.50 USD")
        if amount_cents > MAX_AMOUNT_CENTS:
            raise PaymentRefused("a card charge is at most 999999.99 USD")
        form = {
            "amount": str(amount_cents),
            "currency": "usd",
            "confirm": "true",
            "description": description,
            "payment_method_data[shared_payment_granted_token]": (
                shared_payment_token.get_secret_value()
            ),
            f"metadata[{PAYMENT_ID_METADATA}]": str(payment_id),
        }
        response = await self._send(
            "POST",
            "/v1/payment_intents",
            op,
            data=form,
            key=idempotency_key,
            timeouts=_CREATE_TIMEOUT,
        )
        if response.is_success:
            intent = parse(PaymentIntent, response, provider=PROVIDER, operation=op)
            charge = charge_from_intent(intent)
            if charge is None:
                raise ProviderError(PROVIDER, op, UNEXPECTED_STATUS, status=response.status_code)
            return charge
        error = _error_detail(response)
        if response.status_code == 402 or (error is not None and error.type == "card_error"):
            # The card said no: a PaymentIntent may exist (requires_payment_method),
            # nothing was charged.
            decline = (
                (machine_code(error.decline_code) or machine_code(error.code)) if error else None
            )
            intent_id = error.payment_intent.id if error and error.payment_intent else None
            return StripeCharge(
                outcome="declined",
                payment_intent_id=intent_id,
                decline_code=decline or CARD_DECLINED,
                amount_cents=amount_cents,
                currency="usd",
            )
        raise _failure(response, op, error)

    async def find_payment_intent(self, payment_id: UUID) -> StripeCharge | None:
        """The charge made for `payment_id` (by its metadata), or None when there is none.

        Stripe search is eventually consistent: a PaymentIntent is usually
        searchable within a minute, later during a Stripe incident.
        """
        op = "find_payment_intent"
        wanted = str(payment_id)
        response = await self._send(
            "GET",
            "/v1/payment_intents/search",
            op,
            params={"query": f"metadata['{PAYMENT_ID_METADATA}']:'{wanted}'", "limit": "10"},
            timeouts=_READ_TIMEOUT,
        )
        if not response.is_success:
            raise _failure(response, op, _error_detail(response))
        found = parse(_SearchResult, response, provider=PROVIDER, operation=op)
        matches = [i for i in found.data if i.metadata.get(PAYMENT_ID_METADATA) == wanted]
        if not matches:
            return None
        if len(matches) > 1:
            logger.warning("stripe %s matches=%d payment=%s", op, len(matches), payment_id)
        best = min(matches, key=lambda intent: _STATUS_RANK.get(intent.status, len(_STATUS_RANK)))
        charge = charge_from_intent(best)
        if charge is None:
            raise ProviderError(PROVIDER, op, UNEXPECTED_STATUS)
        return charge

    async def _send(
        self,
        method: str,
        path: str,
        operation: str,
        *,
        timeouts: httpx.Timeout,
        data: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        key: str | None = None,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._secret_key}"}
        if self._api_version:
            headers["Stripe-Version"] = self._api_version
        if key is not None:
            headers["Idempotency-Key"] = checked_idempotency_key(
                key, provider=PROVIDER, operation=operation
            )
        return await send(
            self._http,
            method,
            f"{self._base_url}{path}",
            provider=PROVIDER,
            operation=operation,
            timeouts=timeouts,
            # Reads are safe to repeat; every write carries an idempotency key.
            retry_safe=True,
            headers=headers,
            params=params,
            data=data,
        )


def _failure(response: httpx.Response, operation: str, error: _ErrorDetail | None) -> ProviderError:
    status = response.status_code
    code = (
        (machine_code(error.code) or machine_code(error.type)) if error else None
    ) or f"http_{status}"
    should_retry = response.headers.get("Stripe-Should-Retry")
    if should_retry is not None:
        retryable = should_retry.strip().lower() == "true"
    else:
        retryable = status in (409, 429) or status >= 500
    logger.warning(
        "stripe %s failed status=%s code=%s request=%s",
        operation,
        status,
        code,
        loggable_id(response.headers.get("Request-Id")),
    )
    return ProviderError(PROVIDER, operation, code, status=status, retryable=retryable)
