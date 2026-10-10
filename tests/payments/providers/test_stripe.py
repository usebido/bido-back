"""Stripe client: the PaymentIntent request, outcome mapping, search, errors. MockTransport only."""

# ruff: noqa: S105, S106 - fake keys and tokens, never real credentials

import logging
from typing import Final
from uuid import UUID

import httpx
import pytest
from pydantic import JsonValue, SecretStr

from bido_back.payments.errors import PaymentRefused, ProviderError
from bido_back.payments.providers.stripe import StripeCharge, StripeClient
from tests.payments.providers.support import (
    STRIPE_KEY,
    Answer,
    Recorder,
    always,
    form_body,
    read_timeout,
)

PAYMENT: Final = UUID("0b6f6c55-91c4-4f4e-8d39-7d5a0c2f1e01")
SPT: Final = "spt_mock_1RgaZcFPC5QUO6ZCDVZuVA8q"
STRIPE: Final = "https://api.stripe.com"


def client(recorder: Recorder, *, api_version: str | None = None) -> StripeClient:
    return StripeClient(STRIPE_KEY, api_version=api_version, http=recorder.client())


def intent(status: str, **fields: JsonValue) -> dict[str, JsonValue]:
    body: dict[str, JsonValue] = {
        "id": "pi_3Abc",
        "object": "payment_intent",
        "status": status,
        "amount": 1230,
        "currency": "usd",
        "metadata": {"bido_payment_id": str(PAYMENT)},
        "client_secret": "pi_3Abc_secret_never_read",
    }
    return body | fields


async def create(
    recorder: Recorder, *, amount_cents: int = 1230, api_version: str | None = None
) -> StripeCharge:
    return await client(recorder, api_version=api_version).create_payment_intent(
        shared_payment_token=SecretStr(SPT),
        amount_cents=amount_cents,
        description="Bido purchase order PO-12",
        payment_id=PAYMENT,
        idempotency_key="payment:1:charge",
    )


# --- Create -------------------------------------------------------------------------------------


async def test_create_confirms_one_payment_intent_with_the_token() -> None:
    recorder = Recorder(always(Answer(200, intent("succeeded"))))

    charge = await create(recorder)

    request = recorder.only()
    assert (request.method, str(request.url)) == ("POST", f"{STRIPE}/v1/payment_intents")
    assert request.headers["Authorization"] == f"Bearer {STRIPE_KEY}"
    assert request.headers["Idempotency-Key"] == "payment:1:charge"
    assert "Stripe-Version" not in request.headers
    assert form_body(request) == {
        "amount": "1230",
        "currency": "usd",
        "confirm": "true",
        "description": "Bido purchase order PO-12",
        "payment_method_data[shared_payment_granted_token]": SPT,
        "metadata[bido_payment_id]": str(PAYMENT),
    }
    assert read_timeout(request) == 30.0
    assert charge == StripeCharge(
        outcome="succeeded",
        payment_intent_id="pi_3Abc",
        decline_code=None,
        amount_cents=1230,
        currency="usd",
    )


async def test_create_pins_the_api_version_when_set() -> None:
    recorder = Recorder(always(Answer(200, intent("succeeded"))))

    await create(recorder, api_version="2026-04-22.preview")

    assert recorder.only().headers["Stripe-Version"] == "2026-04-22.preview"


@pytest.mark.parametrize(
    ("body", "outcome", "decline_code"),
    [
        (intent("succeeded"), "succeeded", None),
        (intent("processing"), "pending", None),
        (intent("requires_action"), "declined", "card_requires_action"),
        (
            intent(
                "requires_payment_method",
                last_payment_error={"code": "card_declined", "decline_code": "insufficient_funds"},
            ),
            "declined",
            "insufficient_funds",
        ),
        (
            intent("requires_payment_method", last_payment_error={"code": "expired_card"}),
            "declined",
            "expired_card",
        ),
        (intent("requires_payment_method"), "declined", "card_declined"),
        (intent("canceled"), "declined", "payment_canceled"),
    ],
)
async def test_payment_intent_statuses_map_to_our_outcomes(
    body: JsonValue, outcome: str, decline_code: str | None
) -> None:
    recorder = Recorder(always(Answer(200, body)))

    charge = await create(recorder)

    assert (charge.outcome, charge.decline_code, charge.payment_intent_id) == (
        outcome,
        decline_code,
        "pi_3Abc",
    )


@pytest.mark.parametrize("status", ["requires_capture", "requires_confirmation", "mystery"])
async def test_a_status_a_confirmed_charge_never_has_is_an_error(status: str) -> None:
    recorder = Recorder(always(Answer(200, intent(status))))

    with pytest.raises(ProviderError) as caught:
        await create(recorder)

    assert (caught.value.code, caught.value.retryable) == ("unexpected_status", False)


@pytest.mark.parametrize(
    ("error", "decline_code"),
    [
        (
            {"type": "card_error", "code": "card_declined", "decline_code": "do_not_honor"},
            "do_not_honor",
        ),
        ({"type": "card_error", "code": "expired_card"}, "expired_card"),
        # Not a plain machine code: ours instead, never Stripe's text.
        (
            {"type": "card_error", "code": "Card Declined!", "decline_code": "x" * 41},
            "card_declined",
        ),
        ({"type": "card_error"}, "card_declined"),
    ],
)
async def test_a_card_error_is_a_decline_with_a_safe_code(
    error: dict[str, JsonValue], decline_code: str
) -> None:
    detail: dict[str, JsonValue] = {
        **error,
        "message": "Your card was declined.",
        "payment_intent": {"id": "pi_declined", "status": "requires_payment_method"},
    }
    body: JsonValue = {"error": detail}
    recorder = Recorder(always(Answer(402, body)))

    charge = await create(recorder)

    assert charge == StripeCharge(
        outcome="declined",
        payment_intent_id="pi_declined",
        decline_code=decline_code,
        amount_cents=1230,
        currency="usd",
    )


@pytest.mark.parametrize(
    ("amount_cents", "limit"), [(49, "at least 0.50"), (100_000_000, "at most")]
)
async def test_amounts_stripe_cannot_charge_are_refused_before_calling(
    amount_cents: int, limit: str
) -> None:
    recorder = Recorder(always(Answer(200, intent("succeeded"))))

    with pytest.raises(PaymentRefused, match=limit):
        await create(recorder, amount_cents=amount_cents)

    assert recorder.requests == []


@pytest.mark.parametrize(
    ("answer", "code", "retryable"),
    [
        (
            Answer(400, {"error": {"type": "invalid_request_error", "code": "amount_too_small"}}),
            "amount_too_small",
            False,
        ),
        (
            Answer(
                401,
                {
                    "error": {
                        "type": "invalid_request_error",
                        "message": f"Invalid API Key provided: {STRIPE_KEY}",
                    }
                },
            ),
            "invalid_request_error",
            False,
        ),
        (
            Answer(400, {"error": {"type": "idempotency_error", "message": "Keys for idempotent"}}),
            "idempotency_error",
            False,
        ),
        (
            Answer(
                409, {"error": {"type": "invalid_request_error", "code": "idempotency_key_in_use"}}
            ),
            "idempotency_key_in_use",
            True,
        ),
        (
            Answer(429, {"error": {"type": "invalid_request_error", "code": "rate_limit"}}),
            "rate_limit",
            True,
        ),
        (Answer(500, {"error": {"type": "api_error"}}), "api_error", True),
        (Answer(503, raw=b"<html>unavailable</html>"), "http_503", True),
        # Stripe's own advice wins.
        (
            Answer(500, {"error": {"type": "api_error"}}, headers={"Stripe-Should-Retry": "false"}),
            "api_error",
            False,
        ),
        (
            Answer(
                400,
                {"error": {"type": "invalid_request_error"}},
                headers={"Stripe-Should-Retry": "true"},
            ),
            "invalid_request_error",
            True,
        ),
    ],
)
async def test_other_failures_are_provider_errors(
    answer: Answer, code: str, retryable: bool
) -> None:
    recorder = Recorder(always(answer))

    with pytest.raises(ProviderError) as caught:
        await create(recorder)

    assert (caught.value.provider, caught.value.operation) == ("stripe", "create_payment_intent")
    assert (caught.value.code, caught.value.status, caught.value.retryable) == (
        code,
        answer.status,
        retryable,
    )
    assert STRIPE_KEY not in str(caught.value)
    assert len(recorder.requests) == 1  # never retried here


@pytest.mark.parametrize(
    ("failure", "code"), [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "network_error")]
)
async def test_transport_failures_are_retryable(
    failure: type[httpx.TransportError], code: str
) -> None:
    recorder = Recorder(always(Answer(fail=failure)))

    with pytest.raises(ProviderError) as caught:
        await create(recorder)

    assert (caught.value.code, caught.value.retryable) == (code, True)


async def test_neither_the_token_nor_the_key_reaches_logs_or_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    body: JsonValue = {
        "error": {
            "type": "invalid_request_error",
            "code": "resource_missing",
            "message": f"No such shared payment granted token: '{SPT}'",
        }
    }
    recorder = Recorder(always(Answer(400, body, headers={"Request-Id": "req_123"})))

    with pytest.raises(ProviderError) as caught:
        await create(recorder)

    assert "request=req_123" in caplog.text
    for secret in (SPT, STRIPE_KEY):
        assert secret not in caplog.text
        assert secret not in str(caught.value)
        assert secret not in repr(caught.value)
    assert STRIPE_KEY not in repr(client(recorder))


# --- Search -------------------------------------------------------------------------------------


def search_result(*intents: JsonValue) -> JsonValue:
    return {"object": "search_result", "data": list(intents), "has_more": False}


async def test_find_searches_by_payment_id_metadata() -> None:
    recorder = Recorder(always(Answer(200, search_result(intent("succeeded")))))

    charge = await client(recorder).find_payment_intent(PAYMENT)

    request = recorder.only()
    assert request.method == "GET"
    assert request.url.path == "/v1/payment_intents/search"
    assert request.url.params["query"] == f"metadata['bido_payment_id']:'{PAYMENT}'"
    assert request.headers["Authorization"] == f"Bearer {STRIPE_KEY}"
    assert "Idempotency-Key" not in request.headers
    assert charge is not None
    assert (charge.outcome, charge.payment_intent_id, charge.amount_cents) == (
        "succeeded",
        "pi_3Abc",
        1230,
    )


async def test_find_returns_none_when_nothing_matches() -> None:
    other = intent("succeeded", metadata={"bido_payment_id": "another-payment"})
    recorder = Recorder(always(Answer(200, search_result(other))))

    assert await client(recorder).find_payment_intent(PAYMENT) is None


async def test_find_prefers_the_charge_that_went_furthest() -> None:
    found = search_result(
        intent("requires_payment_method", id="pi_declined"),
        intent("succeeded", id="pi_paid"),
        intent("processing", id="pi_processing"),
    )
    recorder = Recorder(always(Answer(200, found)))

    charge = await client(recorder).find_payment_intent(PAYMENT)

    assert charge is not None
    assert (charge.outcome, charge.payment_intent_id) == ("succeeded", "pi_paid")


async def test_a_failed_search_is_retryable() -> None:
    recorder = Recorder(always(Answer(500, {"error": {"type": "api_error"}})))

    with pytest.raises(ProviderError) as caught:
        await client(recorder).find_payment_intent(PAYMENT)

    assert (caught.value.operation, caught.value.retryable) == ("find_payment_intent", True)
