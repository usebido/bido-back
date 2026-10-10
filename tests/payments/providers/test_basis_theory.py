"""Basis Theory client: request shapes, parsing, and RFC 7807 error mapping. MockTransport only."""

# ruff: noqa: S105, S106 - fake keys and tokens, never real credentials

import logging
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Final
from uuid import UUID

import httpx
import pytest
from pydantic import JsonValue

from bido_back.payments.errors import ProviderError
from bido_back.payments.providers.basis_theory import (
    BasisTheoryClient,
    CardDetails,
    CardDisplay,
    CardIssuer,
    CredentialPayloadUnavailable,
    Merchant,
    MintedCredential,
    Rail,
    Token,
    find_rail,
    money_value,
)
from tests.payments.providers.support import (
    BT_KEY,
    BT_URL,
    Answer,
    Recorder,
    always,
    json_body,
    read_timeout,
)

CONSUMER: Final = UUID("6f1c0b7e-2d1a-4c55-9a43-1f0e8f2b9c11")
MERCHANT: Final = Merchant(name="Bido", url="https://usebido.com", country_code="US")
SPT: Final = "spt_mock_1RgaZcFPC5QUO6ZCDVZuVA8q"
TOKEN_BODY: Final[JsonValue] = {
    "id": "7d9f4a48-6c5b-4e0f-9a43-1f0e8f2b9c11",
    "type": "card",
    "fingerprint": "AKCmKQzHtdLeZJ5ZM1EZJ3iB7m7YBCQKhfdyddmKsZPX",
    "data": {"number": "XXXXXXXXXXXX4242", "expiration_month": 12, "expiration_year": 2030},
    "card": {
        "bin": "42424242",
        "last4": "4242",
        "brand": "visa",
        "funding": "credit",
        "expiration_month": 12,
        "expiration_year": 2030,
        "issuer_country": {"alpha2": "US"},
    },
}
PAYMENT_METHOD_BODY: Final[JsonValue] = {
    "id": "pm_h2Kd91mAqLwX",
    "status": "active",
    "card": {
        "brand": "visa",
        "last4": "4242",
        "expiration_month": 12,
        "expiration_year": 2030,
        "display": {
            "art_url": "https://cdn.basistheory.example/art/visa-signature.png",
            "background_color": "#1A1F71",
            "foreground_color": "#FFFFFF",
            "description": "Visa Signature",
            "issuer_name": None,
        },
        "issuer": {"name": "Example Bank", "country": "US"},
    },
    "rails": [
        {"rail": "agentic-token", "provider": "vic", "status": "enabled"},
        {"rail": "spt", "provider": "stripe", "status": "enabled"},
    ],
}
ALLOWANCE_BODY: Final[JsonValue] = {
    "id": "alw_Xw92mKvB3dQz",
    "status": "active",
    "amount": {"value": "12.30", "currency": "USD"},
    "expires_at": "2026-10-09T21:30:00.000Z",
    "rails": [
        {"rail": "agentic-token", "provider": "vic", "status": "pending_verification"},
        {"rail": "spt", "provider": "stripe", "status": "active"},
    ],
}


def client(recorder: Recorder) -> BasisTheoryClient:
    return BasisTheoryClient(BT_URL + "/", BT_KEY, http=recorder.client())


def mint_body(value: JsonValue) -> JsonValue:
    return {
        "id": "cred_pQ8vNx2LmRs4",
        "rail": "spt",
        "provider": "stripe",
        "amount": {"value": "12.30", "currency": "USD"},
        "credential": {"format": "identifier", "value": value},
    }


async def mint(recorder: Recorder) -> MintedCredential:
    return await client(recorder).mint_identifier(
        "alw_Xw92mKvB3dQz",
        rail="spt",
        provider="stripe",
        amount=Decimal("12.30"),
        currency="USD",
        network_business_profile="profile_test_bido",
        idempotency_key="payment:1:credential",
    )


# --- Vault tokens -------------------------------------------------------------------------------


async def test_get_token_reads_the_card_details_never_the_data() -> None:
    recorder = Recorder(always(Answer(200, TOKEN_BODY)))

    token = await client(recorder).get_token("7d9f4a48-6c5b-4e0f-9a43-1f0e8f2b9c11")

    request = recorder.only()
    assert request.method == "GET"
    assert str(request.url) == f"{BT_URL}/tokens/7d9f4a48-6c5b-4e0f-9a43-1f0e8f2b9c11"
    assert request.headers["BT-API-KEY"] == BT_KEY
    assert "BT-IDEMPOTENCY-KEY" not in request.headers
    assert token == Token(
        id="7d9f4a48-6c5b-4e0f-9a43-1f0e8f2b9c11",
        type="card",
        fingerprint="AKCmKQzHtdLeZJ5ZM1EZJ3iB7m7YBCQKhfdyddmKsZPX",
        card=CardDetails(
            brand="visa", last4="4242", expiration_month=12, expiration_year=2030, funding="credit"
        ),
    )
    # The masked card number is not even parsed.
    assert "XXXXXXXXXXXX4242" not in repr(token)


async def test_a_missing_token_is_none() -> None:
    recorder = Recorder(always(Answer(404, {"type": "TOKEN_NOT_FOUND", "status": 404})))

    assert await client(recorder).get_token("gone") is None


async def test_create_payment_method_sends_the_token_and_the_consumer() -> None:
    recorder = Recorder(always(Answer(201, PAYMENT_METHOD_BODY)))

    method = await client(recorder).create_payment_method(
        token_id="7d9f4a48-6c5b-4e0f-9a43-1f0e8f2b9c11",
        consumer_id=CONSUMER,
        email="buyer@acme.example",
        country_code="US",
        idempotency_key="enroll:1",
    )

    request = recorder.only()
    assert (request.method, str(request.url)) == ("POST", f"{BT_URL}/agentic/payment-methods")
    assert request.headers["BT-API-KEY"] == BT_KEY
    assert request.headers["BT-IDEMPOTENCY-KEY"] == "enroll:1"
    assert json_body(request) == {
        "source": {
            "type": "basis_theory_card_token",
            "token_id": "7d9f4a48-6c5b-4e0f-9a43-1f0e8f2b9c11",
        },
        "consumer": {
            "id": str(CONSUMER),
            "email": "buyer@acme.example",
            "country_code": "US",
            "language_code": "en",
        },
    }
    assert method.id == "pm_h2Kd91mAqLwX"
    assert find_rail(method.rails, rail="spt", provider="stripe") == Rail(
        rail="spt", provider="stripe", status="enabled"
    )


async def test_a_consumer_without_email_sends_none() -> None:
    recorder = Recorder(always(Answer(201, {"id": "pm_1", "rails": None})))

    method = await client(recorder).create_payment_method(
        token_id="tok-1", consumer_id=CONSUMER, email=None, country_code="BR", idempotency_key="k"
    )

    consumer = json_body(recorder.only())["consumer"]
    assert consumer == {"id": str(CONSUMER), "country_code": "BR", "language_code": "en"}
    assert method.rails == ()


async def test_get_payment_method_reads_it_or_none() -> None:
    found = Recorder(always(Answer(200, PAYMENT_METHOD_BODY)))
    gone = Recorder(always(Answer(404)))

    method = await client(found).get_payment_method("pm_h2Kd91mAqLwX")

    assert str(found.only().url) == f"{BT_URL}/agentic/payment-methods/pm_h2Kd91mAqLwX"
    assert method is not None and method.status == "active"
    assert method.card is not None
    assert method.card.display == CardDisplay(
        art_url="https://cdn.basistheory.example/art/visa-signature.png",
        background_color="#1A1F71",
        foreground_color="#FFFFFF",
        description="Visa Signature",
    )
    assert method.card.issuer == CardIssuer(name="Example Bank", country="US")
    assert await client(gone).get_payment_method("pm_h2Kd91mAqLwX") is None


@pytest.mark.parametrize(
    "card",
    [
        {"brand": "visa", "display": None, "issuer": None},
        {"brand": "visa", "display": "visa-signature", "issuer": ["Example Bank"]},
        {"brand": "visa", "display": {"art_url": 42, "background_color": {"hex": "#000000"}}},
    ],
)
async def test_branding_of_another_shape_reads_as_absent(card: JsonValue) -> None:
    recorder = Recorder(always(Answer(200, {"id": "pm_1", "card": card})))

    method = await client(recorder).get_payment_method("pm_1")

    assert method is not None and method.card is not None
    assert method.card.brand == "visa"
    assert method.card.issuer is None
    assert method.card.display in (None, CardDisplay())


@pytest.mark.parametrize("status", [204, 404])
async def test_delete_payment_method_is_done_when_deleted_or_already_gone(status: int) -> None:
    recorder = Recorder(always(Answer(status)))

    await client(recorder).delete_payment_method("pm_h2Kd91mAqLwX")

    request = recorder.only()
    assert (request.method, str(request.url)) == (
        "DELETE",
        f"{BT_URL}/agentic/payment-methods/pm_h2Kd91mAqLwX",
    )


# --- Allowances and credentials -------------------------------------------------------------------


async def test_create_allowance_scopes_it_to_the_merchant() -> None:
    recorder = Recorder(always(Answer(201, ALLOWANCE_BODY)))
    brasilia = timezone(timedelta(hours=-3))

    allowance = await client(recorder).create_allowance(
        payment_method_id="pm_h2Kd91mAqLwX",
        amount=Decimal("12.3"),
        currency="USD",
        merchant=MERCHANT,
        description="Bido purchase order PO-12",
        expires_at=datetime(2026, 10, 9, 18, 30, tzinfo=brasilia),
        idempotency_key="payment:1:allowance",
        metadata={"bido_payment_id": "p-1"},
    )

    request = recorder.only()
    assert (request.method, str(request.url)) == ("POST", f"{BT_URL}/agentic/allowances")
    assert request.headers["BT-IDEMPOTENCY-KEY"] == "payment:1:allowance"
    assert json_body(request) == {
        "payment_method_id": "pm_h2Kd91mAqLwX",
        "amount": {"value": "12.30", "currency": "USD"},
        "merchant": {"name": "Bido", "url": "https://usebido.com", "country_code": "US"},
        "description": "Bido purchase order PO-12",
        "expires_at": "2026-10-09T21:30:00.000Z",
        "metadata": {"bido_payment_id": "p-1"},
    }
    assert allowance.id == "alw_Xw92mKvB3dQz"
    assert allowance.expires_at == datetime(2026, 10, 9, 21, 30, tzinfo=UTC)
    assert find_rail(allowance.rails, rail="spt", provider="stripe") == Rail(
        rail="spt", provider="stripe", status="active"
    )


async def test_an_allowance_needs_an_aware_expiry_and_whole_cents() -> None:
    recorder = Recorder(always(Answer(201, ALLOWANCE_BODY)))
    bt = client(recorder)

    async def allow(amount: Decimal, expires_at: datetime) -> None:
        await bt.create_allowance(
            payment_method_id="pm_1",
            amount=amount,
            currency="USD",
            merchant=MERCHANT,
            description="d",
            expires_at=expires_at,
            idempotency_key="k",
        )

    with pytest.raises(ValueError, match="timezone-aware"):
        await allow(Decimal("1.00"), datetime(2026, 10, 9, 21, 30))  # naive: refused
    with pytest.raises(ValueError, match="whole cents"):
        await allow(Decimal("1.005"), datetime(2026, 10, 9, 21, 30, tzinfo=UTC))
    assert recorder.requests == []


def test_money_values_are_whole_positive_cents() -> None:
    assert money_value(Decimal("12.3")) == "12.30"
    assert money_value(Decimal("1000000")) == "1000000.00"
    for bad in (Decimal("0"), Decimal("-1.00"), Decimal("0.001")):
        with pytest.raises(ValueError, match="whole cents"):
            money_value(bad)


async def test_get_allowance_reads_it_or_none() -> None:
    found = Recorder(always(Answer(200, ALLOWANCE_BODY)))
    gone = Recorder(always(Answer(404)))

    allowance = await client(found).get_allowance("alw_Xw92mKvB3dQz")

    assert str(found.only().url) == f"{BT_URL}/agentic/allowances/alw_Xw92mKvB3dQz"
    assert allowance is not None and allowance.status == "active"
    assert await client(gone).get_allowance("alw_Xw92mKvB3dQz") is None


async def test_mint_asks_for_an_identifier_credential_and_keeps_it_secret() -> None:
    recorder = Recorder(always(Answer(201, mint_body(SPT))))

    credential = await client(recorder).mint_identifier(
        "alw_Xw92mKvB3dQz",
        rail="spt",
        provider="stripe",
        amount=Decimal("12.30"),
        currency="USD",
        network_business_profile="profile_test_bido",
        idempotency_key="payment:1:credential",
    )

    request = recorder.only()
    assert (request.method, str(request.url)) == (
        "POST",
        f"{BT_URL}/agentic/allowances/alw_Xw92mKvB3dQz/credentials",
    )
    assert request.headers["BT-IDEMPOTENCY-KEY"] == "payment:1:credential"
    assert json_body(request) == {
        "rail": "spt",
        "provider": "stripe",
        "amount": {"value": "12.30", "currency": "USD"},
        "credential": {
            "format": "identifier",
            "payload": {"network_business_profile": "profile_test_bido"},
        },
    }
    # A client timeout after the mint would lose the one-time token: wait longer.
    assert read_timeout(request) == 45.0
    assert credential.id == "cred_pQ8vNx2LmRs4"
    assert credential.token.get_secret_value() == SPT
    assert SPT not in repr(credential)
    assert SPT not in str(credential)


@pytest.mark.parametrize("value", [{"id": SPT}, {"token": SPT}, {"identifier": SPT}])
async def test_mint_reads_the_token_from_an_object_value(value: JsonValue) -> None:
    recorder = Recorder(always(Answer(201, mint_body(value))))

    credential = await mint(recorder)

    assert credential.token.get_secret_value() == SPT


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (mint_body(None), "missing_credential"),
        (mint_body(""), "missing_credential"),
        ({"id": "cred_1"}, "missing_credential"),
        (
            {
                "id": "cred_1",
                "credential": {"format": "card", "value": {"number": "4000001000004242"}},
            },
            "unexpected_credential_format",
        ),
    ],
)
async def test_a_mint_without_an_identifier_is_refused(body: JsonValue, code: str) -> None:
    recorder = Recorder(always(Answer(201, body)))

    with pytest.raises(ProviderError) as caught:
        await mint(recorder)

    assert (caught.value.provider, caught.value.operation) == ("basis_theory", "mint_credential")
    assert caught.value.code == code
    assert "4000001000004242" not in str(caught.value)


# --- Errors -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "code", "retryable"),
    [
        # Repeat the same request with the same key, later.
        (
            Answer(409, {"type": "ALLOWANCE_CREDENTIAL_IN_PROGRESS", "status": 409}),
            "ALLOWANCE_CREDENTIAL_IN_PROGRESS",
            True,
        ),
        # Conclusive: the key keeps this outcome.
        (
            Answer(422, {"type": "PROVIDER_CREDENTIALS_FAILED", "status": 422}),
            "PROVIDER_CREDENTIALS_FAILED",
            False,
        ),
        (Answer(400, {"type": "ALLOWANCE_AMOUNT_EXCEEDED"}), "ALLOWANCE_AMOUNT_EXCEEDED", False),
        (Answer(409, {"type": "IDEMPOTENCY_CONFLICT"}), "IDEMPOTENCY_CONFLICT", False),
        (
            Answer(409, {"type": "https://docs.basistheory.com/errors#RAIL_NOT_ACTIVE"}),
            "RAIL_NOT_ACTIVE",
            False,
        ),
        (
            Answer(400, {"type": "https://tools.ietf.org/html/rfc7231#section-6.5.1"}),
            "http_400",
            False,
        ),
        (Answer(400, {"type": "not a code, prose"}), "http_400", False),
        # Safe to repeat: the request carries an idempotency key.
        (Answer(429, {}), "http_429", True),
        (Answer(500, {"type": "INTERNAL_ERROR"}), "INTERNAL_ERROR", True),
        (Answer(502, raw=b"<html>Bad gateway</html>"), "http_502", True),
    ],
)
async def test_problems_become_provider_errors_with_their_code(
    answer: Answer, code: str, retryable: bool
) -> None:
    recorder = Recorder(always(answer))

    with pytest.raises(ProviderError) as caught:
        await mint(recorder)

    assert caught.value.code == code
    assert caught.value.status == answer.status
    assert caught.value.retryable is retryable
    assert not isinstance(caught.value, CredentialPayloadUnavailable)
    assert len(recorder.requests) == 1  # never retried here


@pytest.mark.parametrize("code", ["CREDENTIAL_PAYLOAD_UNAVAILABLE", "CREDENTIAL_OUTCOME_UNKNOWN"])
async def test_a_replayed_or_unknown_mint_has_its_own_error(code: str) -> None:
    recorder = Recorder(always(Answer(409, {"type": code, "status": 409})))

    with pytest.raises(CredentialPayloadUnavailable) as caught:
        await mint(recorder)

    assert caught.value.code == code
    assert caught.value.retryable is False


async def test_errors_keep_provider_prose_out_and_log_trace_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    problem: JsonValue = {
        "type": "VALIDATION_ERROR",
        "title": "One or more validation errors occurred.",
        "detail": "card 4242424242424242 of Jane Doe is invalid",
        "debug": {"provider_correlation": "corr-123"},
    }
    recorder = Recorder(always(Answer(400, problem, headers={"BT-TRACE-ID": "trace-abc"})))
    caplog.set_level(logging.DEBUG)

    with pytest.raises(ProviderError) as caught:
        await mint(recorder)

    assert str(caught.value) == "basis_theory mint_credential failed: VALIDATION_ERROR"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "trace=trace-abc" in caplog.text
    assert "correlation=corr-123" in caplog.text
    for secret in ("4242424242424242", "Jane Doe", "validation errors occurred", BT_KEY):
        assert secret not in caplog.text
        assert secret not in str(caught.value)


async def test_a_read_that_fails_on_the_server_is_retryable() -> None:
    recorder = Recorder(always(Answer(503, {})))

    with pytest.raises(ProviderError) as caught:
        await client(recorder).get_token("tok-1")

    assert (caught.value.code, caught.value.retryable) == ("http_503", True)


@pytest.mark.parametrize(
    ("failure", "code"),
    [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "network_error")],
)
async def test_transport_failures_are_retryable_when_the_request_has_a_key(
    failure: type[httpx.TransportError], code: str
) -> None:
    recorder = Recorder(always(Answer(fail=failure)))

    with pytest.raises(ProviderError) as caught:
        await mint(recorder)

    assert (caught.value.code, caught.value.retryable) == (code, True)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert BT_URL not in str(caught.value)


async def test_an_unreadable_success_is_a_bad_response() -> None:
    recorder = Recorder(always(Answer(200, raw=b"<html>not json</html>")))

    with pytest.raises(ProviderError) as caught:
        await client(recorder).get_payment_method("pm_1")

    assert (caught.value.code, caught.value.retryable) == ("bad_response", False)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


@pytest.mark.parametrize("reference", ["../admin", "pm_1/rails", "pm_1?x=1", "", "a" * 201])
async def test_references_that_could_change_the_url_are_refused(reference: str) -> None:
    recorder = Recorder(always(Answer(200, PAYMENT_METHOD_BODY)))

    with pytest.raises(ProviderError) as caught:
        await client(recorder).get_payment_method(reference)

    assert caught.value.code == "invalid_reference"
    assert recorder.requests == []


@pytest.mark.parametrize("key", ["", "two words", "line\nbreak", "k" * 256])
async def test_idempotency_keys_must_be_plain_header_values(key: str) -> None:
    recorder = Recorder(always(Answer(201, PAYMENT_METHOD_BODY)))

    with pytest.raises(ProviderError) as caught:
        await client(recorder).create_payment_method(
            token_id="tok-1",
            consumer_id=CONSUMER,
            email=None,
            country_code="US",
            idempotency_key=key,
        )

    assert caught.value.code == "invalid_idempotency_key"
    assert recorder.requests == []


def test_the_client_repr_has_no_key() -> None:
    assert BT_KEY not in repr(BasisTheoryClient(BT_URL, BT_KEY))
