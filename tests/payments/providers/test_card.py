"""The card gateway over Basis Theory and Stripe. MockTransport only.

Covers enrollment and its status mapping, the allowance, the charge (mint, then
one Stripe PaymentIntent), declines, the replayed mint (Stripe search hit, or
`CardCredentialLost`), retries of a transient Stripe failure under the same
key, the simulated mode that never calls Stripe, and the builders.
"""

# ruff: noqa: S105, S106 - fake keys and tokens, never real credentials

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID

import httpx
import pytest
from pydantic import JsonValue

from bido_back.payments.card import (
    BasisTheoryCardGateway,
    build_card_gateway,
)
from bido_back.payments.errors import (
    CardCredentialLost,
    PaymentRefused,
    PaymentsNotConfigured,
    ProviderError,
)
from bido_back.payments.ports import (
    CardAuthorization,
    CardCharge,
    CardConsumer,
    CardGateway,
    EnrolledCard,
)
from bido_back.payments.providers.basis_theory import BasisTheoryClient, Merchant
from bido_back.payments.providers.stripe import StripeClient
from tests.payments.providers.support import (
    BT_KEY,
    BT_URL,
    NETWORK_PROFILE,
    STRIPE_KEY,
    Answer,
    Recorder,
    always,
    form_body,
    json_body,
    payment_settings,
    scripted,
)

PAYMENT: Final = UUID("0b6f6c55-91c4-4f4e-8d39-7d5a0c2f1e01")
TENANT: Final = UUID("6f1c0b7e-2d1a-4c55-9a43-1f0e8f2b9c11")
SUPPLIER: Final = UUID("2a3b4c5d-0000-4000-8000-00000000abcd")
BUYER: Final = CardConsumer(
    id=UUID("11111111-2222-4333-8444-555555555555"),
    email="buyer@acme.example",
    country_code="US",
)
SPT: Final = "spt_mock_1RgaZcFPC5QUO6ZCDVZuVA8q"
EXPIRES: Final = datetime(2026, 10, 9, 21, 30, tzinfo=UTC)

BT: Final = "api.test.basistheory.com"
TOKEN: Final = f"GET {BT}/tokens/tok-1"
PAYMENT_METHODS: Final = f"POST {BT}/agentic/payment-methods"
ALLOWANCES: Final = f"POST {BT}/agentic/allowances"
MINT: Final = f"POST {BT}/agentic/allowances/alw_1/credentials"
STRIPE: Final = "api.stripe.com"
CREATE: Final = f"POST {STRIPE}/v1/payment_intents"
SEARCH: Final = f"GET {STRIPE}/v1/payment_intents/search"

TOKEN_BODY: Final[JsonValue] = {
    "id": "tok-1",
    "type": "card",
    "fingerprint": "fp-card-1",
    "data": {"number": "XXXXXXXXXXXX4242"},
    "card": {
        "brand": "visa",
        "last4": "4242",
        "expiration_month": 12,
        "expiration_year": 2030,
        "funding": "credit",
    },
}


def rail(name: str, provider: str, status: str) -> JsonValue:
    return {"rail": name, "provider": provider, "status": status}


def payment_method(*rails: JsonValue, card: JsonValue = None) -> JsonValue:
    return {"id": "pm_1", "status": "active", "card": card, "rails": list(rails)}


def allowance(*rails: JsonValue, status: str = "active") -> JsonValue:
    return {
        "id": "alw_1",
        "status": status,
        "amount": {"value": "12.30", "currency": "USD"},
        "expires_at": "2026-10-09T21:30:00.000Z",
        "rails": list(rails),
    }


def minted(value: str = SPT) -> JsonValue:
    return {"id": "cred_1", "credential": {"format": "identifier", "value": value}}


def replayed(code: str = "CREDENTIAL_PAYLOAD_UNAVAILABLE") -> Answer:
    return Answer(409, {"type": code, "status": 409})


def intent(status: str, **fields: JsonValue) -> dict[str, JsonValue]:
    body: dict[str, JsonValue] = {
        "id": "pi_paid",
        "status": status,
        "amount": 1230,
        "currency": "usd",
        "metadata": {"bido_payment_id": str(PAYMENT)},
    }
    return body | fields


def search_result(*intents: JsonValue) -> JsonValue:
    return {"object": "search_result", "data": list(intents), "has_more": False}


def gateway(
    recorder: Recorder, *, simulated: bool = False, sleeps: list[float] | None = None
) -> BasisTheoryCardGateway:
    http = recorder.client()
    slept = sleeps if sleeps is not None else []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    return BasisTheoryCardGateway(
        BasisTheoryClient(BT_URL, BT_KEY, http=http),
        None if simulated else StripeClient(STRIPE_KEY, http=http),
        merchant=Merchant(name="Bido", url="https://usebido.com", country_code="US"),
        network_business_profile=NETWORK_PROFILE,
        sleep=sleep,
    )


async def charge(card: CardGateway, *, amount: Decimal = Decimal("12.30")) -> CardCharge:
    return await card.charge(
        authorization_ref="alw_1",
        payment_id=PAYMENT,
        amount=amount,
        currency="USD",
        description="Bido purchase order PO-12",
        idempotency_key="payment:1",
    )


# --- Enroll and remove --------------------------------------------------------------------------


async def test_enroll_reads_the_token_then_creates_the_payment_method() -> None:
    recorder = Recorder(
        scripted(
            {
                TOKEN: [Answer(200, TOKEN_BODY)],
                PAYMENT_METHODS: [
                    Answer(
                        201,
                        payment_method(rail("spt", "stripe", "enabled"), card={"brand": "visa"}),
                    )
                ],
            }
        )
    )

    card = await gateway(recorder).enroll(
        token_id="tok-1", consumer=BUYER, idempotency_key="enroll:1"
    )

    assert recorder.calls() == [TOKEN, PAYMENT_METHODS]
    created = recorder.requests[1]
    assert created.headers["BT-IDEMPOTENCY-KEY"] == "enroll:1"
    assert json_body(created) == {
        "source": {"type": "basis_theory_card_token", "token_id": "tok-1"},
        "consumer": {
            "id": str(BUYER.id),
            "email": "buyer@acme.example",
            "country_code": "US",
            "language_code": "en",
        },
    }
    assert card == EnrolledCard(
        payment_method_ref="pm_1",
        fingerprint="fp-card-1",
        brand="visa",
        last4="4242",
        exp_month=12,
        exp_year=2030,
        funding="credit",
        status="active",
    )


@pytest.mark.parametrize(
    ("rails", "status"),
    [
        ([rail("spt", "stripe", "enabled")], "active"),
        ([rail("spt", "stripe", "pending")], "pending"),
        ([rail("spt", "stripe", "error")], "failed"),
        # Only the rail Bido charges on counts.
        ([rail("agentic-token", "vic", "enabled")], "failed"),
        ([rail("agentic-token", "vic", "enabled"), rail("spt", "stripe", "pending")], "pending"),
        ([], "failed"),
        ([rail("spt", "stripe", "provisioning")], "pending"),
    ],
)
async def test_the_card_status_is_the_spt_rail_status(rails: list[JsonValue], status: str) -> None:
    recorder = Recorder(
        scripted(
            {
                TOKEN: [Answer(200, TOKEN_BODY)],
                PAYMENT_METHODS: [Answer(201, payment_method(*rails))],
            }
        )
    )

    card = await gateway(recorder).enroll(token_id="tok-1", consumer=BUYER, idempotency_key="k")

    assert card.status == status


async def test_enroll_keeps_only_well_formed_card_details() -> None:
    token: dict[str, JsonValue] = {
        "id": "tok-1",
        "type": "card",
        "fingerprint": "fp-card-1",
        "card": {"last4": "XXXX", "expiration_month": 13, "expiration_year": 30},
    }
    recorder = Recorder(
        scripted(
            {
                TOKEN: [Answer(200, token)],
                PAYMENT_METHODS: [Answer(201, payment_method(rail("spt", "stripe", "enabled")))],
            }
        )
    )

    card = await gateway(recorder).enroll(token_id="tok-1", consumer=BUYER, idempotency_key="k")

    assert (card.last4, card.exp_month, card.exp_year, card.brand) == (None, None, None, None)


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (Answer(404, {"type": "TOKEN_NOT_FOUND"}), "not found"),
        (Answer(200, {"id": "tok-1", "type": "bank", "fingerprint": "fp"}), "not a card"),
    ],
)
async def test_enroll_refuses_a_missing_or_non_card_token(answer: Answer, message: str) -> None:
    recorder = Recorder(scripted({TOKEN: [answer]}))

    with pytest.raises(PaymentRefused, match=message):
        await gateway(recorder).enroll(token_id="tok-1", consumer=BUYER, idempotency_key="k")

    assert recorder.calls() == [TOKEN]


async def test_a_token_without_a_fingerprint_cannot_be_enrolled() -> None:
    recorder = Recorder(scripted({TOKEN: [Answer(200, {"id": "tok-1", "type": "card"})]}))

    with pytest.raises(ProviderError) as caught:
        await gateway(recorder).enroll(token_id="tok-1", consumer=BUYER, idempotency_key="k")

    assert caught.value.code == "missing_fingerprint"
    assert recorder.calls() == [TOKEN]


@pytest.mark.parametrize("status", [204, 404])
async def test_remove_deletes_the_payment_method(status: int) -> None:
    recorder = Recorder(always(Answer(status)))

    await gateway(recorder).remove("pm_1")

    assert recorder.calls() == [f"DELETE {BT}/agentic/payment-methods/pm_1"]


# --- Authorize ----------------------------------------------------------------------------------


async def test_authorize_creates_an_allowance_scoped_to_bido() -> None:
    recorder = Recorder(
        scripted({ALLOWANCES: [Answer(201, allowance(rail("spt", "stripe", "active")))]})
    )

    authorization = await gateway(recorder).authorize(
        payment_method_ref="pm_1",
        amount=Decimal("12.30"),
        currency="USD",
        description="Bido purchase order PO-12",
        expires_at=EXPIRES,
        idempotency_key="payment:1:allowance",
    )

    request = recorder.only()
    assert request.headers["BT-IDEMPOTENCY-KEY"] == "payment:1:allowance"
    assert json_body(request) == {
        "payment_method_id": "pm_1",
        "amount": {"value": "12.30", "currency": "USD"},
        "merchant": {"name": "Bido", "url": "https://usebido.com", "country_code": "US"},
        "description": "Bido purchase order PO-12",
        "expires_at": "2026-10-09T21:30:00.000Z",
    }
    assert authorization == CardAuthorization(ref="alw_1", usable=True, expires_at=EXPIRES)


@pytest.mark.parametrize(
    "body",
    [
        allowance(rail("spt", "stripe", "pending_verification")),
        allowance(rail("spt", "stripe", "error")),
        allowance(rail("agentic-token", "vic", "active")),
        allowance(rail("spt", "stripe", "active"), status="cancelled"),
        allowance(rail("spt", "stripe", "active"), status="expired"),
    ],
)
async def test_an_allowance_without_an_active_spt_rail_is_not_usable(body: JsonValue) -> None:
    recorder = Recorder(scripted({ALLOWANCES: [Answer(201, body)]}))

    authorization = await gateway(recorder).authorize(
        payment_method_ref="pm_1",
        amount=Decimal("12.30"),
        currency="USD",
        description="d",
        expires_at=EXPIRES,
        idempotency_key="k",
    )

    assert authorization.usable is False


@pytest.mark.parametrize(
    ("amount", "currency", "message"),
    [
        (Decimal("0.49"), "USD", "at least 0.50"),
        (Decimal("-5.00"), "USD", "at least 0.50"),
        (Decimal("NaN"), "USD", "at least 0.50"),
        (Decimal("1000000.00"), "USD", "at most"),
        (Decimal("10.005"), "USD", "whole number of cents"),
        (Decimal("10.00"), "EUR", "in USD"),
    ],
)
async def test_amounts_stripe_cannot_charge_are_refused_before_any_call(
    amount: Decimal, currency: str, message: str
) -> None:
    recorder = Recorder(always(Answer(500)))
    card = gateway(recorder)

    with pytest.raises(PaymentRefused, match=message):
        await card.authorize(
            payment_method_ref="pm_1",
            amount=amount,
            currency=currency,
            description="d",
            expires_at=EXPIRES,
            idempotency_key="k",
        )
    with pytest.raises(PaymentRefused, match=message):
        await card.charge(
            authorization_ref="alw_1",
            payment_id=PAYMENT,
            amount=amount,
            currency=currency,
            description="d",
            idempotency_key="k",
        )

    assert recorder.requests == []


# --- Charge -------------------------------------------------------------------------------------


async def test_charge_mints_the_token_then_charges_it_on_stripe() -> None:
    recorder = Recorder(
        scripted({MINT: [Answer(201, minted())], CREATE: [Answer(200, intent("succeeded"))]})
    )

    result = await charge(gateway(recorder))

    assert result == CardCharge("succeeded", "pi_paid", None, simulated=False)
    assert recorder.calls() == [MINT, CREATE]
    mint, create = recorder.requests
    assert mint.headers["BT-IDEMPOTENCY-KEY"] == "payment:1:credential"
    assert json_body(mint) == {
        "rail": "spt",
        "provider": "stripe",
        "amount": {"value": "12.30", "currency": "USD"},
        "credential": {
            "format": "identifier",
            "payload": {"network_business_profile": NETWORK_PROFILE},
        },
    }
    assert create.headers["Idempotency-Key"] == "payment:1:charge"
    assert form_body(create) == {
        "amount": "1230",
        "currency": "usd",
        "confirm": "true",
        "description": "Bido purchase order PO-12",
        "payment_method_data[shared_payment_granted_token]": SPT,
        "metadata[bido_payment_id]": str(PAYMENT),
    }


async def test_a_declined_card_is_a_declined_charge() -> None:
    declined: JsonValue = {
        "error": {
            "type": "card_error",
            "code": "card_declined",
            "decline_code": "insufficient_funds",
            "message": "Your card has insufficient funds.",
            "payment_intent": {"id": "pi_declined", "status": "requires_payment_method"},
        }
    }
    recorder = Recorder(scripted({MINT: [Answer(201, minted())], CREATE: [Answer(402, declined)]}))

    result = await charge(gateway(recorder))

    assert result == CardCharge("declined", "pi_declined", "insufficient_funds", simulated=False)
    assert recorder.calls() == [MINT, CREATE]


async def test_a_card_needing_3ds_is_declined_not_left_pending() -> None:
    recorder = Recorder(
        scripted({MINT: [Answer(201, minted())], CREATE: [Answer(200, intent("requires_action"))]})
    )

    result = await charge(gateway(recorder))

    assert result == CardCharge("declined", "pi_paid", "card_requires_action", simulated=False)


async def test_a_processing_charge_is_pending() -> None:
    recorder = Recorder(
        scripted({MINT: [Answer(201, minted())], CREATE: [Answer(200, intent("processing"))]})
    )

    result = await charge(gateway(recorder))

    assert result == CardCharge("pending", "pi_paid", None, simulated=False)


@pytest.mark.parametrize("code", ["CREDENTIAL_PAYLOAD_UNAVAILABLE", "CREDENTIAL_OUTCOME_UNKNOWN"])
async def test_a_replayed_charge_returns_the_charge_already_made(code: str) -> None:
    recorder = Recorder(
        scripted(
            {
                MINT: [replayed(code)],
                SEARCH: [Answer(200, search_result(intent("succeeded")))],
            }
        )
    )

    result = await charge(gateway(recorder))

    assert result == CardCharge("succeeded", "pi_paid", None, simulated=False)
    assert recorder.calls() == [MINT, SEARCH]
    assert recorder.requests[1].url.params["query"] == f"metadata['bido_payment_id']:'{PAYMENT}'"


async def test_a_replayed_decline_stays_a_decline() -> None:
    declined = intent(
        "requires_payment_method", last_payment_error={"decline_code": "do_not_honor"}
    )
    recorder = Recorder(
        scripted({MINT: [replayed()], SEARCH: [Answer(200, search_result(declined))]})
    )

    result = await charge(gateway(recorder))

    assert result == CardCharge("declined", "pi_paid", "do_not_honor", simulated=False)


@pytest.mark.parametrize("code", ["CREDENTIAL_PAYLOAD_UNAVAILABLE", "CREDENTIAL_OUTCOME_UNKNOWN"])
async def test_a_minted_token_with_no_charge_at_stripe_is_lost(code: str) -> None:
    recorder = Recorder(scripted({MINT: [replayed(code)], SEARCH: [Answer(200, search_result())]}))

    with pytest.raises(CardCredentialLost) as caught:
        await charge(gateway(recorder))

    assert str(PAYMENT) in str(caught.value)
    assert recorder.calls() == [MINT, SEARCH]


async def test_a_found_charge_for_another_amount_is_refused() -> None:
    other = intent("succeeded", amount=999)
    recorder = Recorder(scripted({MINT: [replayed()], SEARCH: [Answer(200, search_result(other))]}))

    with pytest.raises(ProviderError) as caught:
        await charge(gateway(recorder))

    assert (caught.value.provider, caught.value.code) == ("stripe", "charge_mismatch")


async def test_a_failed_search_is_retried_later_not_a_lost_credential() -> None:
    recorder = Recorder(
        scripted({MINT: [replayed()], SEARCH: [Answer(503, {"error": {"type": "api_error"}})]})
    )

    with pytest.raises(ProviderError) as caught:
        await charge(gateway(recorder))

    assert (caught.value.code, caught.value.retryable) == ("api_error", True)


async def test_a_transient_stripe_failure_is_retried_with_the_same_key_and_token() -> None:
    sleeps: list[float] = []
    recorder = Recorder(
        scripted(
            {
                MINT: [Answer(201, minted())],
                CREATE: [
                    Answer(500, {"error": {"type": "api_error"}}),
                    Answer(fail=httpx.ReadTimeout),
                    Answer(200, intent("succeeded")),
                ],
            }
        )
    )

    result = await charge(gateway(recorder, sleeps=sleeps))

    assert result.outcome == "succeeded"
    assert recorder.calls() == [MINT, CREATE, CREATE, CREATE]
    creates = recorder.to(STRIPE)
    assert {r.headers["Idempotency-Key"] for r in creates} == {"payment:1:charge"}
    assert len({r.content for r in creates}) == 1  # the same token and parameters each time
    assert sleeps == [1.0, 2.0]


async def test_a_stripe_failure_that_cannot_pass_is_not_retried() -> None:
    refused: JsonValue = {"error": {"type": "invalid_request_error", "code": "amount_too_small"}}
    recorder = Recorder(scripted({MINT: [Answer(201, minted())], CREATE: [Answer(400, refused)]}))

    with pytest.raises(ProviderError) as caught:
        await charge(gateway(recorder))

    assert (caught.value.code, caught.value.retryable) == ("amount_too_small", False)
    assert recorder.calls() == [MINT, CREATE]


async def test_after_three_tries_the_stripe_failure_is_raised_as_retryable() -> None:
    recorder = Recorder(
        scripted(
            {
                MINT: [Answer(201, minted())],
                CREATE: [Answer(503, {"error": {"type": "api_error"}})],
            }
        )
    )

    with pytest.raises(ProviderError) as caught:
        await charge(gateway(recorder))

    assert caught.value.retryable is True
    assert recorder.calls() == [MINT, CREATE, CREATE, CREATE]


async def test_a_mint_failure_is_raised_without_calling_stripe() -> None:
    recorder = Recorder(
        scripted({MINT: [Answer(422, {"type": "PROVIDER_CREDENTIALS_FAILED", "status": 422})]})
    )

    with pytest.raises(ProviderError) as caught:
        await charge(gateway(recorder))

    assert (caught.value.code, caught.value.retryable) == ("PROVIDER_CREDENTIALS_FAILED", False)
    assert recorder.to(STRIPE) == []


async def test_simulated_charges_mint_the_token_and_never_call_stripe() -> None:
    recorder = Recorder(scripted({MINT: [Answer(201, minted("spt_mock_test_tenant"))]}))
    card = gateway(recorder, simulated=True)

    result = await charge(card)

    assert card.simulated is True
    assert result == CardCharge("succeeded", f"sim_{PAYMENT}", None, simulated=True)
    assert recorder.calls() == [MINT]


async def test_a_simulated_replay_is_the_simulated_success() -> None:
    recorder = Recorder(scripted({MINT: [replayed()]}))

    result = await charge(gateway(recorder, simulated=True))

    assert result == CardCharge("succeeded", f"sim_{PAYMENT}", None, simulated=True)
    assert recorder.to(STRIPE) == []


async def test_the_token_never_reaches_a_log_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    recorder = Recorder(
        scripted(
            {
                MINT: [Answer(201, minted())],
                CREATE: [
                    Answer(400, {"error": {"type": "invalid_request_error", "message": SPT}}),
                ],
            }
        )
    )

    with pytest.raises(ProviderError) as caught:
        await charge(gateway(recorder))

    assert "stripe create_payment_intent failed" in caplog.text
    assert SPT not in caplog.text
    assert SPT not in str(caught.value)


# --- Builders -----------------------------------------------------------------------------------


async def test_the_gateway_charges_on_stripe_unless_simulated() -> None:
    recorder = Recorder(
        scripted({MINT: [Answer(201, minted())], CREATE: [Answer(200, intent("succeeded"))]})
    )

    card = build_card_gateway(payment_settings(), http=recorder.client())
    result = await charge(card)

    assert card.simulated is False
    assert result.reference == "pi_paid"
    assert recorder.requests[0].headers["BT-API-KEY"] == BT_KEY
    assert recorder.requests[1].headers["Authorization"] == f"Bearer {STRIPE_KEY}"


async def test_the_simulated_gateway_needs_no_stripe_settings() -> None:
    recorder = Recorder(scripted({MINT: [Answer(201, minted())]}))
    settings = payment_settings(
        payments_card_charge="simulated",
        stripe_secret_key=None,
        stripe_network_business_profile=None,
    )

    card = build_card_gateway(settings, http=recorder.client())
    result = await charge(card)

    assert card.simulated is True
    assert result.simulated is True
    profile = json_body(recorder.only())["credential"]
    assert profile == {
        "format": "identifier",
        "payload": {"network_business_profile": "profile_bido_simulated"},
    }


@pytest.mark.parametrize(
    ("overrides", "missing"),
    [
        ({"basis_theory_api_key": None}, "BASIS_THEORY_API_KEY"),
        ({"stripe_secret_key": None}, "STRIPE_SECRET_KEY"),
        ({"stripe_network_business_profile": None}, "STRIPE_NETWORK_BUSINESS_PROFILE"),
    ],
)
def test_the_gateway_needs_its_settings(overrides: dict[str, object], missing: str) -> None:
    with pytest.raises(PaymentsNotConfigured, match=missing):
        build_card_gateway(payment_settings(**overrides))


@pytest.mark.parametrize(
    "update",
    [
        {"basis_theory_api_url": "https://api.basistheory.com"},
        {"environment": "prd"},
    ],
)
def test_a_simulated_gateway_is_refused_outside_the_test_tenant(update: dict[str, object]) -> None:
    # Settings refuse this combination; the builder refuses it again (a copy skips validation).
    settings = payment_settings(payments_card_charge="simulated").model_copy(update=update)

    with pytest.raises(PaymentsNotConfigured, match="test tenant"):
        build_card_gateway(settings)
