"""The wallet provider over Privy: one wallet per tenant, checked before use. MockTransport only."""

# ruff: noqa: S105, S106 - fake app secrets and keys, never real credentials

import base64
from typing import Final
from uuid import UUID

import pytest
from pydantic import JsonValue

from bido_back.payments.errors import PaymentsNotConfigured, ProviderError
from bido_back.payments.ports import CustodyWallet, WalletProvider
from bido_back.payments.wallets import (
    PrivyWalletProvider,
    build_wallet_provider,
    is_solana_address,
    tenant_external_id,
)
from tests.payments.providers.support import (
    WALLET_OWNER,
    WALLET_POLICY,
    Answer,
    Recorder,
    always,
    json_body,
    payment_settings,
    scripted,
)

TENANT: Final = UUID("6f1c0b7e-2d1a-4c55-9a43-1f0e8f2b9c11")
EXTERNAL_ID: Final = f"tenant-{TENANT}"
WALLET_ID: Final = "cm7wallet0000000000000001"
ADDRESS: Final = "7992GKVq51ChvsVJQ9GFto2aBozUGuMcuY93SUSxexJw"
PRIVY: Final = "api.privy.io"
LOOKUP: Final = f"GET {PRIVY}/v1/wallets/ext_wal_{EXTERNAL_ID}"
CREATE: Final = f"POST {PRIVY}/v1/wallets"
RPC: Final = f"POST {PRIVY}/v1/wallets/{WALLET_ID}/rpc"
NOT_FOUND: Final = Answer(404, {"error": "Wallet not found", "code": "not_found"})


def wallet(**fields: JsonValue) -> dict[str, JsonValue]:
    body: dict[str, JsonValue] = {
        "id": WALLET_ID,
        "address": ADDRESS,
        "chain_type": "solana",
        "owner_id": WALLET_OWNER,
        "external_id": EXTERNAL_ID,
        "policy_ids": [WALLET_POLICY],
    }
    return body | fields


def provider(recorder: Recorder, **settings: object) -> WalletProvider:
    return build_wallet_provider(payment_settings(**settings), http=recorder.client())


def uuid4_key(value: str) -> UUID:
    key = UUID(value)
    assert key.version == 4
    return key


async def test_an_existing_wallet_is_reused() -> None:
    recorder = Recorder(scripted({LOOKUP: [Answer(200, wallet())]}))

    custody = await provider(recorder).ensure_wallet(tenant_id=TENANT)

    assert custody == CustodyWallet(ref=WALLET_ID, address=ADDRESS)
    assert recorder.calls() == [LOOKUP]


async def test_a_missing_wallet_is_created_for_the_tenant() -> None:
    recorder = Recorder(scripted({LOOKUP: [NOT_FOUND], CREATE: [Answer(200, wallet())]}))

    custody = await provider(recorder).ensure_wallet(tenant_id=TENANT)

    assert custody == CustodyWallet(ref=WALLET_ID, address=ADDRESS)
    assert recorder.calls() == [LOOKUP, CREATE]
    created = recorder.requests[1]
    uuid4_key(created.headers["privy-idempotency-key"])
    assert json_body(created) == {
        "chain_type": "solana",
        "owner_id": WALLET_OWNER,
        "external_id": EXTERNAL_ID,
        "policy_ids": [WALLET_POLICY],
    }


async def test_without_a_policy_the_wallet_is_created_without_one() -> None:
    recorder = Recorder(scripted({LOOKUP: [NOT_FOUND], CREATE: [Answer(200, wallet())]}))

    await provider(recorder, privy_wallet_policy_id=None).ensure_wallet(tenant_id=TENANT)

    assert "policy_ids" not in json_body(recorder.requests[1])


async def test_every_create_attempt_has_a_new_idempotency_key() -> None:
    # Privy caches an error under its key: a retry with the same key would replay it.
    recorder = Recorder(
        scripted(
            {
                LOOKUP: [NOT_FOUND],
                CREATE: [Answer(500, {"error": "Internal"}), Answer(200, wallet())],
            }
        )
    )
    wallets = provider(recorder)

    with pytest.raises(ProviderError) as caught:
        await wallets.ensure_wallet(tenant_id=TENANT)
    assert (caught.value.operation, caught.value.retryable) == ("create_wallet", True)
    custody = await wallets.ensure_wallet(tenant_id=TENANT)

    assert custody.ref == WALLET_ID
    assert recorder.calls() == [LOOKUP, CREATE, LOOKUP, LOOKUP, CREATE]
    keys = [
        uuid4_key(r.headers["privy-idempotency-key"])
        for r in recorder.requests
        if r.method == "POST"
    ]
    assert len(set(keys)) == 2


async def test_a_wallet_created_concurrently_is_found_again() -> None:
    recorder = Recorder(
        scripted(
            {
                LOOKUP: [NOT_FOUND, Answer(200, wallet())],
                CREATE: [Answer(400, {"error": "Duplicate external id", "code": "invalid_data"})],
            }
        )
    )

    custody = await provider(recorder).ensure_wallet(tenant_id=TENANT)

    assert custody == CustodyWallet(ref=WALLET_ID, address=ADDRESS)
    assert recorder.calls() == [LOOKUP, CREATE, LOOKUP]


async def test_when_the_wallet_is_still_missing_the_create_error_is_raised() -> None:
    recorder = Recorder(
        scripted(
            {
                LOOKUP: [NOT_FOUND, Answer(503, {"error": "Unavailable"})],
                CREATE: [Answer(401, {"error": "Invalid app secret", "code": "invalid_data"})],
            }
        )
    )

    with pytest.raises(ProviderError) as caught:
        await provider(recorder).ensure_wallet(tenant_id=TENANT)

    assert (caught.value.operation, caught.value.code) == ("create_wallet", "invalid_data")


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (wallet(chain_type="ethereum"), "wallet_chain_mismatch"),
        (wallet(owner_id="someone-else"), "wallet_owner_mismatch"),
        (wallet(owner_id=None), "wallet_owner_mismatch"),
        (wallet(external_id="tenant-another"), "wallet_external_id_mismatch"),
        (wallet(address="0xAbC0000000000000000000000000000000000000"), "bad_response"),
        (wallet(address="4uQeVj5tqViQh7yWWGStvkEG1Zmhx6uasJtWCJziofL"), "bad_response"),
        (wallet(id="wallet/../other"), "invalid_reference"),
    ],
)
async def test_a_wallet_that_is_not_ours_is_refused(body: JsonValue, code: str) -> None:
    recorder = Recorder(scripted({LOOKUP: [Answer(200, body)]}))

    with pytest.raises(ProviderError) as caught:
        await provider(recorder).ensure_wallet(tenant_id=TENANT)

    assert (caught.value.provider, caught.value.code) == ("privy", code)


async def test_sign_transaction_signs_with_the_tenant_wallet() -> None:
    signed = bytes([1]) + bytes(range(64)) + b"message"
    answer: JsonValue = {
        "method": "signTransaction",
        "data": {"signed_transaction": base64.b64encode(signed).decode(), "encoding": "base64"},
    }
    recorder = Recorder(scripted({RPC: [Answer(200, answer)]}))
    wire = bytes([1]) + bytes(64) + b"message"

    result = await provider(recorder).sign_transaction(
        wallet_ref=WALLET_ID, transaction=wire, idempotency_key="fund:payment-1:1"
    )

    assert result == signed
    request = recorder.only()
    assert request.headers["privy-idempotency-key"] == "fund:payment-1:1"
    assert "privy-authorization-signature" in request.headers
    assert json_body(request)["params"] == {
        "transaction": base64.b64encode(wire).decode(),
        "encoding": "base64",
    }


# --- Builder and helpers --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["PRIVY_APP_ID", "PRIVY_APP_SECRET", "PRIVY_AUTHORIZATION_KEY", "PRIVY_WALLET_OWNER_ID"],
)
def test_the_provider_needs_its_settings(name: str) -> None:
    with pytest.raises(PaymentsNotConfigured, match=name):
        build_wallet_provider(payment_settings(**{name.lower(): None}))


def test_an_invalid_authorization_key_is_a_configuration_error() -> None:
    key = "wallet-auth:bm90IGEga2V5"

    with pytest.raises(PaymentsNotConfigured, match="PRIVY_AUTHORIZATION_KEY") as caught:
        build_wallet_provider(payment_settings(privy_authorization_key=key))

    assert key not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_the_builder_returns_the_privy_provider() -> None:
    assert isinstance(build_wallet_provider(payment_settings()), PrivyWalletProvider)


def test_the_external_id_is_the_tenant_id() -> None:
    assert tenant_external_id(TENANT) == "tenant-6f1c0b7e-2d1a-4c55-9a43-1f0e8f2b9c11"


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        (ADDRESS, True),
        ("11111111111111111111111111111111", True),  # 32 zero bytes (the system program)
        ("JEKNVnkbo3jma5nREBBJCDoXFVeKkD56V3xKrvRmWxFG", True),  # 32 bytes of 0xff
        ("4uQeVj5tqViQh7yWWGStvkEG1Zmhx6uasJtWCJziofL", False),  # 31 bytes
        ("JJEfe6DcPM2ziB2vfUWDV6aHVerXRGkv3TcyvJUNGHZz", False),  # 33 bytes
        ("0OIl" + "1" * 40, False),  # not base58
        ("", False),
    ],
)
def test_solana_addresses_are_base58_for_32_bytes(value: str, valid: bool) -> None:
    assert is_solana_address(value) is valid


async def test_a_tenant_wallet_lookup_uses_only_its_own_external_id() -> None:
    other = UUID("00000000-0000-4000-8000-000000000002")
    recorder = Recorder(always(Answer(200, wallet(external_id=f"tenant-{other}"))))

    await provider(recorder).ensure_wallet(tenant_id=other)

    assert recorder.only().url.path == f"/v1/wallets/ext_wal_tenant-{other}"
