"""Privy client: wallet calls, the authorization signature, error mapping. MockTransport only.

The signature is checked with the public key over the exact payload Privy
verifies, and the canonical form against the golden vector the TypeScript twin
pins in `solana/tests/fixtures/vectors.json`.
"""

# ruff: noqa: S105, S106 - fake app secrets and keys, never real credentials

import base64
import hashlib
import json
from pathlib import Path
from typing import Final, cast

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from pydantic import JsonValue

from bido_back.payments.errors import ProviderError
from bido_back.payments.providers.privy import (
    PrivyClient,
    Wallet,
    authorization_payload,
    load_authorization_key,
)
from tests.payments.providers.support import (
    AUTHORIZATION_KEY,
    AUTHORIZATION_KEY_B64,
    PRIVY_APP_ID,
    PRIVY_APP_SECRET,
    PRIVY_URL,
    Answer,
    Recorder,
    always,
    json_body,
    p256_key,
    pkcs8_base64,
)

VECTORS: Final = Path(__file__).resolve().parents[3] / "solana/tests/fixtures/vectors.json"
NOW_MS: Final = 1_791_730_800_000
WALLET_ID: Final = "cm7wallet0000000000000001"
ADDRESS: Final = "7992GKVq51ChvsVJQ9GFto2aBozUGuMcuY93SUSxexJw"
EXTERNAL_ID: Final = "tenant-6f1c0b7e-2d1a-4c55-9a43-1f0e8f2b9c11"
WIRE: Final = bytes([1]) + bytes(64) + b"bido:unsigned-message"
SIGNED_WIRE: Final = bytes([1]) + bytes(range(64)) + b"bido:unsigned-message"
BASIC_AUTH: Final = (
    "Basic " + base64.b64encode(f"{PRIVY_APP_ID}:{PRIVY_APP_SECRET}".encode()).decode()
)
SIGNED_HEADERS: Final = ("privy-app-id", "privy-idempotency-key", "privy-request-expiry")


def client(recorder: Recorder, *, key: str = AUTHORIZATION_KEY_B64) -> PrivyClient:
    return PrivyClient(
        PRIVY_APP_ID,
        PRIVY_APP_SECRET,
        key,
        base_url=PRIVY_URL + "/",
        http=recorder.client(),
        clock_ms=lambda: NOW_MS,
    )


def wallet_body(**fields: JsonValue) -> dict[str, JsonValue]:
    body: dict[str, JsonValue] = {
        "id": WALLET_ID,
        "address": ADDRESS,
        "chain_type": "solana",
        "owner_id": "bido-test-owner-quorum",
        "external_id": EXTERNAL_ID,
        "policy_ids": ["bido-test-fund-policy-000"],
        "additional_signers": [],
        "created_at": 1_760_050_000_000,
    }
    return body | fields


def signed_body(signed: bytes = SIGNED_WIRE) -> JsonValue:
    return {
        "method": "signTransaction",
        "data": {"signed_transaction": base64.b64encode(signed).decode(), "encoding": "base64"},
    }


async def sign(recorder: Recorder, *, key: str = AUTHORIZATION_KEY_B64) -> bytes:
    return await client(recorder, key=key).sign_transaction(
        WALLET_ID, WIRE, idempotency_key="fund:payment-1:2"
    )


def verify(public_key: ec.EllipticCurvePublicKey, request: httpx.Request, payload: bytes) -> None:
    signature = base64.b64decode(request.headers["privy-authorization-signature"], validate=True)
    public_key.verify(signature, payload, ec.ECDSA(hashes.SHA256()))


# --- Wallets ------------------------------------------------------------------------------------


async def test_create_wallet_asks_for_a_solana_wallet_owned_by_our_quorum() -> None:
    recorder = Recorder(always(Answer(200, wallet_body())))

    wallet = await client(recorder).create_wallet(
        external_id=EXTERNAL_ID,
        owner_id="bido-test-owner-quorum",
        policy_ids=["bido-test-fund-policy-000"],
        idempotency_key="00000000-0000-4000-8000-000000000001",
    )

    request = recorder.only()
    assert (request.method, str(request.url)) == ("POST", f"{PRIVY_URL}/v1/wallets")
    assert request.headers["authorization"] == BASIC_AUTH
    assert request.headers["privy-app-id"] == PRIVY_APP_ID
    assert request.headers["privy-idempotency-key"] == "00000000-0000-4000-8000-000000000001"
    assert request.headers["content-type"] == "application/json"
    # Creating a wallet needs no authorization signature.
    assert "privy-authorization-signature" not in request.headers
    assert json_body(request) == {
        "chain_type": "solana",
        "owner_id": "bido-test-owner-quorum",
        "external_id": EXTERNAL_ID,
        "policy_ids": ["bido-test-fund-policy-000"],
    }
    assert wallet == Wallet(
        id=WALLET_ID,
        address=ADDRESS,
        chain_type="solana",
        owner_id="bido-test-owner-quorum",
        external_id=EXTERNAL_ID,
    )


async def test_create_wallet_without_a_policy_sends_none() -> None:
    recorder = Recorder(always(Answer(200, wallet_body(policy_ids=[]))))

    await client(recorder).create_wallet(
        external_id=EXTERNAL_ID, owner_id="owner", policy_ids=(), idempotency_key="k-1"
    )

    assert "policy_ids" not in json_body(recorder.only())


async def test_get_wallet_by_external_id_uses_the_ext_wal_reference() -> None:
    recorder = Recorder(always(Answer(200, wallet_body())))

    wallet = await client(recorder).get_wallet_by_external_id(EXTERNAL_ID)

    request = recorder.only()
    assert (request.method, str(request.url)) == (
        "GET",
        f"{PRIVY_URL}/v1/wallets/ext_wal_{EXTERNAL_ID}",
    )
    assert request.headers["authorization"] == BASIC_AUTH
    assert request.headers["privy-app-id"] == PRIVY_APP_ID
    assert "privy-authorization-signature" not in request.headers
    assert wallet is not None and wallet.id == WALLET_ID


async def test_a_missing_wallet_is_none() -> None:
    recorder = Recorder(always(Answer(404, {"error": "Wallet not found", "code": "not_found"})))

    assert await client(recorder).get_wallet_by_external_id(EXTERNAL_ID) is None


# --- signTransaction ----------------------------------------------------------------------------


async def test_sign_transaction_sends_the_signed_rpc_request() -> None:
    recorder = Recorder(always(Answer(200, signed_body())))

    signed = await sign(recorder)

    assert signed == SIGNED_WIRE
    request = recorder.only()
    assert (request.method, str(request.url)) == ("POST", f"{PRIVY_URL}/v1/wallets/{WALLET_ID}/rpc")
    assert request.headers["authorization"] == BASIC_AUTH
    assert request.headers["privy-app-id"] == PRIVY_APP_ID
    assert request.headers["privy-idempotency-key"] == "fund:payment-1:2"
    # Two minutes after signing, in Unix milliseconds.
    assert request.headers["privy-request-expiry"] == str(NOW_MS + 120_000)
    assert json_body(request) == {
        "method": "signTransaction",
        "chain_type": "solana",
        "params": {"transaction": base64.b64encode(WIRE).decode(), "encoding": "base64"},
    }


async def test_the_signature_verifies_over_the_exact_payload_privy_rebuilds() -> None:
    recorder = Recorder(always(Answer(200, signed_body())))

    await sign(recorder)

    request = recorder.only()
    transaction = base64.b64encode(WIRE).decode()
    # Written out by hand: sorted keys, no whitespace, the URL exactly as sent.
    expected = (
        '{"body":{"chain_type":"solana","method":"signTransaction","params":'
        f'{{"encoding":"base64","transaction":"{transaction}"}}}},'
        f'"headers":{{"privy-app-id":"{PRIVY_APP_ID}","privy-idempotency-key":"fund:payment-1:2",'
        f'"privy-request-expiry":"{NOW_MS + 120_000}"}},'
        f'"method":"POST","url":"{PRIVY_URL}/v1/wallets/{WALLET_ID}/rpc","version":1}}'
    ).encode()
    verify(AUTHORIZATION_KEY.public_key(), request, expected)
    # The same bytes from what Privy receives: the URL, the body and the privy- headers.
    body = cast(JsonValue, json.loads(request.content))
    rebuilt = authorization_payload(
        method=request.method,
        url=str(request.url),
        body=body,
        headers={name: request.headers[name] for name in SIGNED_HEADERS},
    )
    assert rebuilt == expected
    with pytest.raises(InvalidSignature):
        verify(AUTHORIZATION_KEY.public_key(), request, expected.replace(b"POST", b"PUT"))


@pytest.mark.parametrize("prefix", ["wallet-auth:", "wallet-api:", "  "])
async def test_the_dashboard_prefix_of_the_key_is_stripped(prefix: str) -> None:
    key = p256_key()
    recorder = Recorder(always(Answer(200, signed_body())))

    await sign(recorder, key=prefix + pkcs8_base64(key))

    request = recorder.only()
    body = cast(JsonValue, json.loads(request.content))
    payload = authorization_payload(
        method="POST",
        url=str(request.url),
        body=body,
        headers={name: request.headers[name] for name in SIGNED_HEADERS},
    )
    verify(key.public_key(), request, payload)


def test_the_canonical_payload_matches_the_golden_vector() -> None:
    vectors = cast(dict[str, dict[str, JsonValue]], json.loads(VECTORS.read_text()))
    signing = vectors["signing"]
    privy = cast(dict[str, JsonValue], signing["privy"])
    tx_base64 = cast(str, signing["tx_base64"])

    payload = authorization_payload(
        method="POST",
        url=f"https://api.privy.io/v1/wallets/{privy['walletId']}/rpc",
        # The vector predates `chain_type` in the body; the canonical form is what is pinned.
        body={
            "method": "signTransaction",
            "params": {"transaction": tx_base64, "encoding": "base64"},
        },
        headers={
            "privy-app-id": cast(str, privy["appId"]),
            "privy-idempotency-key": cast(str, privy["idempotencyKey"]),
            "privy-request-expiry": str(privy["requestExpiryMs"]),
        },
    )

    assert payload == cast(str, signing["canonical_privy_payload"]).encode("utf-8")
    assert hashlib.sha256(payload).hexdigest() == signing["canonical_privy_payload_sha256"]


def test_non_ascii_is_kept_as_utf8_in_the_payload() -> None:
    payload = authorization_payload(
        method="POST", url="https://x", body={"name": "Fábrica 深圳"}, headers={"privy-app-id": "a"}
    )

    assert '"name":"Fábrica 深圳"'.encode() in payload


@pytest.mark.parametrize(
    "key",
    [
        "not base64!",
        base64.b64encode(b"not a der key").decode(),
        # A key of the wrong kind or curve.
        base64.b64encode(
            rsa.generate_private_key(65537, 2048).private_bytes(
                Encoding.DER, PrivateFormat.PKCS8, NoEncryption()
            )
        ).decode(),
        pkcs8_base64(ec.generate_private_key(ec.SECP384R1())),
    ],
)
def test_keys_that_are_not_p256_pkcs8_are_refused(key: str) -> None:
    with pytest.raises(ValueError) as caught:
        load_authorization_key(key)

    assert key not in str(caught.value)
    assert caught.value.__cause__ is None


# --- Errors -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "code", "retryable"),
    [
        (
            Answer(400, {"error": "Policy violation", "code": "policy_violation"}),
            "policy_violation",
            False,
        ),
        (
            Answer(
                401,
                {"error": "No valid signature", "code": "zero_correct_authorization_signatures"},
            ),
            "zero_correct_authorization_signatures",
            False,
        ),
        # A new request (new expiry, new key) can succeed.
        (Answer(400, {"error": "Expired", "code": "request_expired"}), "request_expired", True),
        (Answer(429, {"error": "Slow down"}), "http_429", True),
        (Answer(500, {"error": "Internal", "code": "internal_error"}), "internal_error", True),
        (Answer(503, raw=b"<html>unavailable</html>"), "http_503", True),
        (Answer(400, {"error": "x", "code": "Not A Code"}), "http_400", False),
        (Answer(200, raw=b"not json"), "bad_response", False),
        (Answer(200, {"data": {"signed_transaction": "not base64!"}}), "bad_response", False),
        (
            Answer(200, {"data": {"signed_transaction": "AA==", "encoding": "hex"}}),
            "bad_response",
            False,
        ),
        (Answer(fail=httpx.ReadTimeout), "timeout", True),
        (Answer(fail=httpx.ConnectError), "network_error", True),
    ],
)
async def test_sign_failures_map_to_provider_errors_after_one_request(
    answer: Answer, code: str, retryable: bool
) -> None:
    recorder = Recorder(always(answer))

    with pytest.raises(ProviderError) as caught:
        await sign(recorder)

    assert (caught.value.provider, caught.value.operation) == ("privy", "sign_transaction")
    assert (caught.value.code, caught.value.retryable) == (code, retryable)
    assert len(recorder.requests) == 1
    assert PRIVY_APP_SECRET not in str(caught.value)


@pytest.mark.parametrize("wallet_id", ["../wallets", "w/rpc", ""])
async def test_wallet_ids_that_could_change_the_url_are_refused(wallet_id: str) -> None:
    recorder = Recorder(always(Answer(200, signed_body())))

    with pytest.raises(ProviderError) as caught:
        await client(recorder).sign_transaction(wallet_id, WIRE, idempotency_key="k")

    assert caught.value.code == "invalid_reference"
    assert recorder.requests == []


async def test_idempotency_keys_are_at_most_256_characters() -> None:
    recorder = Recorder(always(Answer(200, signed_body())))

    with pytest.raises(ProviderError) as caught:
        await client(recorder).sign_transaction(WALLET_ID, WIRE, idempotency_key="k" * 257)

    assert caught.value.code == "invalid_idempotency_key"
    assert recorder.requests == []


def test_the_client_repr_has_no_secret() -> None:
    text = repr(client(Recorder(always(Answer()))))

    assert PRIVY_APP_SECRET not in text
    assert AUTHORIZATION_KEY_B64 not in text
