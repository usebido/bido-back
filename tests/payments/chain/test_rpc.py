"""`RpcClient`: the JSON-RPC requests it sends and how failures become `ProviderError`.

Every exchange runs over `httpx.MockTransport`; nothing reaches a network. The URL
carries a provider API key, so no error, chain or traceback may show it.
"""

import base64
import json
import traceback
from collections.abc import Callable
from typing import Final, cast

import httpx
import pytest
from solders.pubkey import Pubkey
from solders.signature import Signature

from bido_back.payments.chain.rpc import PreflightFailed, RpcClient, SignatureStatus
from bido_back.payments.errors import ProviderError

# A provider URL with its API key: it must never show up in an error.
API_KEY: Final = "sk-not-a-real-key-4f2a"
RPC_URL: Final = f"https://devnet.rpc.invalid/?api-key={API_KEY}"
SIGNATURE: Final = Signature.from_bytes(bytes(range(64)))
ADDRESS: Final = Pubkey.from_string("7992GKVq51ChvsVJQ9GFto2aBozUGuMcuY93SUSxexJw")

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]
type Handler = Callable[[dict[str, Json]], httpx.Response]


def result(request: dict[str, Json], value: Json) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": request["id"], "result": value})


def rpc_error(request: dict[str, Json], error: Json) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": request["id"], "error": error})


class Node:
    """Records each JSON-RPC request and answers it with `handler`."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[dict[str, Json]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == RPC_URL
        assert request.method == "POST"
        assert request.headers["content-type"] == "application/json"
        body = cast(dict[str, Json], json.loads(request.content))
        assert body["jsonrpc"] == "2.0"
        self.requests.append(body)
        return self.handler(body)

    def client(self) -> RpcClient:
        return RpcClient(RPC_URL, http=httpx.AsyncClient(transport=httpx.MockTransport(self)))

    @property
    def last(self) -> tuple[Json, Json]:
        request = self.requests[-1]
        return request["method"], request["params"]


def answering(value: Json) -> Node:
    return Node(lambda request: result(request, value))


def failing(error: Json) -> Node:
    return Node(lambda request: rpc_error(request, error))


def preflight_error(err: Json) -> Json:
    return {
        "code": -32002,
        "message": "Transaction simulation failed: Error processing Instruction 2",
        "data": {
            "err": err,
            "logs": ["Program log: AnchorError"],
            "accounts": None,
            "unitsConsumed": 12_000,
            "returnData": None,
        },
    }


def assert_no_url(error: BaseException) -> None:
    shown = [str(error), repr(error), *(str(arg) for arg in error.args)]
    shown.append("".join(traceback.format_exception(error)))
    assert all(API_KEY not in text and "rpc.invalid" not in text for text in shown)
    assert error.__cause__ is None
    assert error.__context__ is None


# ---- requests and results ----


async def test_send_transaction_sends_base64_with_every_parameter_explicit() -> None:
    node = answering(str(SIGNATURE))
    wire = b"\x01" + bytes(64) + b"message bytes"

    sent = await node.client().send_transaction(wire)

    assert sent == SIGNATURE
    assert node.last == (
        "sendTransaction",
        [
            base64.b64encode(wire).decode(),
            {
                "encoding": "base64",
                "skipPreflight": False,
                "preflightCommitment": "confirmed",
                "maxRetries": 0,
            },
        ],
    )


async def test_signature_statuses_search_the_history_and_keep_the_context_slot() -> None:
    status: Json = {
        "slot": 410,
        "confirmations": None,
        "err": {"InstructionError": [2, {"Custom": 6019}]},
        "status": {"Err": {"InstructionError": [2, {"Custom": 6019}]}},
        "confirmationStatus": "finalized",
    }
    node = answering({"context": {"slot": 420}, "value": [status, None]})
    other = Signature.from_bytes(bytes(64))

    statuses = await node.client().get_signature_statuses([SIGNATURE, other])

    assert node.last == (
        "getSignatureStatuses",
        [[str(SIGNATURE), str(other)], {"searchTransactionHistory": True}],
    )
    assert statuses.context_slot == 420
    assert statuses.statuses == (
        SignatureStatus(
            slot=410,
            confirmations=None,
            err={"InstructionError": [2, {"Custom": 6019}]},
            confirmation_status="finalized",
        ),
        None,
    )


async def test_a_status_keeps_an_error_variant_newer_than_solders() -> None:
    status: Json = {
        "slot": 7,
        "confirmations": 3,
        "err": {"SomeFutureError": {"detail": 1}},
        "confirmationStatus": "confirmed",
    }
    node = answering({"context": {"slot": 9}, "value": [status]})

    [found] = (await node.client().get_signature_statuses([SIGNATURE])).statuses

    assert found == SignatureStatus(7, 3, {"SomeFutureError": {"detail": 1}}, "confirmed")


async def test_epoch_info_reads_the_height_and_slot_at_the_commitment() -> None:
    node = answering(
        {
            "absoluteSlot": 1_234,
            "blockHeight": 1_200,
            "epoch": 3,
            "slotIndex": 34,
            "slotsInEpoch": 432_000,
        }
    )

    position = await node.client().get_epoch_info("finalized")

    assert node.last == ("getEpochInfo", [{"commitment": "finalized"}])
    assert (position.block_height, position.slot) == (1_200, 1_234)


async def test_account_info_asks_for_base64_and_returns_none_when_absent() -> None:
    data = b"escrow-bytes"
    account: Json = {
        "data": [base64.b64encode(data).decode(), "base64"],
        "executable": False,
        "lamports": 8_338_080,
        "owner": str(ADDRESS),
        "rentEpoch": 18_446_744_073_709_551_615,
        "space": len(data),
    }
    node = answering({"context": {"slot": 5}, "value": account})
    client = node.client()

    found = await client.get_account_info(ADDRESS)

    assert node.last == (
        "getAccountInfo",
        [str(ADDRESS), {"encoding": "base64", "commitment": "confirmed"}],
    )
    assert found is not None
    assert (found.data, found.owner, found.lamports) == (data, ADDRESS, 8_338_080)
    assert (
        await answering({"context": {"slot": 5}, "value": None}).client().get_account_info(ADDRESS)
        is None
    )


async def test_the_other_reads() -> None:
    blockhash = "EkSnNWid2cvwEVnVx9aBqawnmiCNiDgp3gUdkDPTKN1N"
    latest_node = answering(
        {"context": {"slot": 9}, "value": {"blockhash": blockhash, "lastValidBlockHeight": 3_090}}
    )
    latest = await latest_node.client().get_latest_blockhash()
    assert latest_node.last == ("getLatestBlockhash", [{"commitment": "confirmed"}])
    assert (str(latest.blockhash), latest.last_valid_block_height) == (blockhash, 3_090)

    height_node = answering(1_233)
    assert await height_node.client().get_block_height() == 1_233
    assert height_node.last == ("getBlockHeight", [{"commitment": "finalized"}])

    balance_node = answering({"context": {"slot": 9}, "value": 5_000_000})
    assert await balance_node.client().get_balance(ADDRESS) == 5_000_000
    assert balance_node.last == ("getBalance", [str(ADDRESS), {"commitment": "confirmed"}])

    rent_node = answering(1_461_600)
    assert await rent_node.client().get_minimum_balance_for_rent_exemption(82) == 1_461_600
    assert rent_node.last == (
        "getMinimumBalanceForRentExemption",
        [82, {"commitment": "confirmed"}],
    )

    genesis = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
    genesis_node = answering(genesis)
    assert await genesis_node.client().get_genesis_hash() == genesis
    assert genesis_node.last == ("getGenesisHash", [])


async def test_request_ids_increase() -> None:
    node = answering(1)
    client = node.client()
    await client.get_block_height()
    await client.get_block_height()
    assert [request["id"] for request in node.requests] == [1, 2]


# ---- failures ----


async def test_a_preflight_failure_keeps_the_transaction_error() -> None:
    node = failing(preflight_error({"InstructionError": [2, {"Custom": 6019}]}))

    with pytest.raises(PreflightFailed) as caught:
        await node.client().send_transaction(b"wire")

    failure = caught.value
    assert (failure.provider, failure.operation, failure.code) == (
        "solana",
        "sendTransaction",
        "preflight_failed",
    )
    assert failure.retryable is False
    assert failure.transaction_error == {"InstructionError": [2, {"Custom": 6019}]}
    assert_no_url(failure)


async def test_a_preflight_failure_without_details_has_no_transaction_error() -> None:
    node = failing({"code": -32002, "message": "Transaction simulation failed"})

    with pytest.raises(PreflightFailed) as caught:
        await node.client().send_transaction(b"wire")

    assert caught.value.transaction_error is None


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (
            {"code": -32005, "message": "Node is unhealthy", "data": {"numSlotsBehind": 10}},
            "node_unhealthy",
            True,
        ),
        ({"code": -32602, "message": "invalid params"}, "invalid_params", False),
        # Without `data`: solders' own parser panics on these two.
        ({"code": -32005, "message": "Node is unhealthy"}, "node_unhealthy", True),
        (
            {"code": -32016, "message": "Minimum context slot has not been reached"},
            "min_context_slot_not_reached",
            True,
        ),
        ({"code": 429, "message": "Too many requests"}, "rate_limited", True),
        ({"code": -32099, "message": "something new"}, "rpc_error_-32099", False),
    ],
)
async def test_a_json_rpc_error_is_a_named_provider_error(
    error: Json, code: str, retryable: bool
) -> None:
    with pytest.raises(ProviderError) as caught:
        await failing(error).client().get_block_height()

    assert (caught.value.provider, caught.value.operation) == ("solana", "getBlockHeight")
    assert (caught.value.code, caught.value.retryable) == (code, retryable)
    assert_no_url(caught.value)


@pytest.mark.parametrize(
    ("status", "retryable"), [(503, True), (429, True), (500, True), (401, False), (404, False)]
)
async def test_an_http_error_status_is_a_provider_error(status: int, retryable: bool) -> None:
    node = Node(lambda request: httpx.Response(status, text=f"denied for {RPC_URL}"))

    with pytest.raises(ProviderError) as caught:
        await node.client().get_balance(ADDRESS)

    assert (caught.value.code, caught.value.status, caught.value.retryable) == (
        f"http_{status}",
        status,
        retryable,
    )
    assert_no_url(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "{}",
        '{"jsonrpc":"2.0","id":1,"result":"x"}',
        '{"jsonrpc":"2.0","id":1,"result":-1}',
        '{"jsonrpc":"2.0","id":1,"error":"broken"}',
        '{"jsonrpc":"2.0","id":1,"error":{"message":"no code"}}',
        "[1, 2]",
    ],
)
async def test_an_unreadable_answer_is_invalid_response(body: str) -> None:
    node = Node(lambda request: httpx.Response(200, text=body))

    with pytest.raises(ProviderError) as caught:
        await node.client().get_block_height()

    assert (caught.value.code, caught.value.retryable) == ("invalid_response", True)
    assert_no_url(caught.value)


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (httpx.ConnectTimeout(f"timed out connecting to {RPC_URL}"), "timeout"),
        (httpx.ReadTimeout(f"timed out reading {RPC_URL}"), "timeout"),
        (httpx.ConnectError(f"cannot reach {RPC_URL}"), "network_error"),
        (httpx.RemoteProtocolError(f"bad answer from {RPC_URL}"), "network_error"),
    ],
)
async def test_a_transport_failure_never_carries_the_url(
    raised: httpx.HTTPError, code: str
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise raised

    client = RpcClient(RPC_URL, http=httpx.AsyncClient(transport=httpx.MockTransport(fail)))

    with pytest.raises(ProviderError) as caught:
        await client.send_transaction(b"wire")

    assert (caught.value.operation, caught.value.code, caught.value.retryable) == (
        "sendTransaction",
        code,
        True,
    )
    assert_no_url(caught.value)


def test_the_client_never_shows_its_url() -> None:
    client = RpcClient(RPC_URL, http=httpx.AsyncClient())
    assert API_KEY not in repr(client)
    assert API_KEY not in str(client)


async def test_an_owned_client_is_closed_and_an_injected_one_is_not() -> None:
    injected = httpx.AsyncClient(transport=httpx.MockTransport(answering(1)))
    await RpcClient(RPC_URL, http=injected).aclose()
    assert not injected.is_closed
    await injected.aclose()

    owned = RpcClient(RPC_URL)
    await owned.aclose()


@pytest.mark.parametrize(
    ("method", "value"),
    [
        (
            "account",
            {
                "context": {"slot": 1},
                "value": {"data": ["!!", "base64"], "owner": str(ADDRESS), "lamports": 1},
            },
        ),
        (
            "account",
            {
                "context": {"slot": 1},
                "value": {"data": ["AA==", "base58"], "owner": str(ADDRESS), "lamports": 1},
            },
        ),
        (
            "account",
            {
                "context": {"slot": 1},
                "value": {"data": ["AA==", "base64"], "owner": "not-a-key", "lamports": 1},
            },
        ),
        ("statuses", {"context": {"slot": 1}, "value": []}),
        (
            "statuses",
            {"context": {"slot": 1}, "value": [{"slot": 1, "confirmationStatus": "rooted"}]},
        ),
        (
            "blockhash",
            {"context": {"slot": 1}, "value": {"blockhash": "nope", "lastValidBlockHeight": 1}},
        ),
        ("signature", "not-a-signature"),
    ],
)
async def test_a_malformed_result_is_invalid_response(method: str, value: Json) -> None:
    client = answering(value).client()

    with pytest.raises(ProviderError) as caught:
        if method == "account":
            await client.get_account_info(ADDRESS)
        elif method == "statuses":
            await client.get_signature_statuses([SIGNATURE])
        elif method == "blockhash":
            await client.get_latest_blockhash()
        else:
            await client.send_transaction(b"wire")

    assert (caught.value.code, caught.value.retryable) == ("invalid_response", True)
    assert_no_url(caught.value)
