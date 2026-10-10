"""Solana JSON-RPC over httpx.

Requests are JSON-RPC 2.0 bodies written here with every parameter explicit (for
example `"encoding": "base64"` on `sendTransaction`: solders 0.29's request builders
drop it without a config, and the node then reads the payload as base58). Responses
are parsed into our own pydantic models, not with `solders.rpc.responses`: its error
branch panics (a `BaseException`) on an error object without `data`, and its status
parser refuses transaction error variants newer than solders, which would leave a
landed transaction unreadable. A transaction error stays the node's JSON
(`transactions.describe_transaction_error` names it).

Every failure is `ProviderError("solana", <method>, <code>, status=, retryable=)`:
`timeout` and `network_error` (the request may or may not have reached the node),
`http_<status>`, a JSON-RPC error name (`node_unhealthy`, `invalid_params`, ...) or
`rpc_error_<code>`, and `invalid_response`. A failed `sendTransaction` preflight is
`PreflightFailed`, which keeps the transaction error.

The URL carries the RPC provider's API key: it never appears in an error, a log line
or an exception chain (errors are raised after the `except` blocks, so they chain to
nothing; a validation error, which quotes its input, is never chained either).
"""

import base64
import binascii
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from solders.hash import Hash, ParseHashError
from solders.pubkey import Pubkey
from solders.signature import Signature

from bido_back.payments.errors import ProviderError

type Commitment = Literal["processed", "confirmed", "finalized"]
type ConfirmationStatus = Literal["processed", "confirmed", "finalized"]

PROVIDER: Final = "solana"
DEFAULT_TIMEOUT: Final = httpx.Timeout(10.0, connect=5.0)
# `getSignatureStatuses` takes at most 256 signatures per call.
MAX_SIGNATURE_STATUSES: Final = 256
_HTTP_OK: Final = 200
# `sendTransaction`'s preflight simulation failed.
_PREFLIGHT_FAILURE: Final = -32002
# JSON-RPC errors worth a name in logs; any other code is `rpc_error_<code>`.
_RPC_ERRORS: Final[dict[int, str]] = {
    -32700: "parse_error",
    -32600: "invalid_request",
    -32601: "method_not_found",
    -32602: "invalid_params",
    -32603: "internal_error",
    -32003: "signature_verification_failed",
    -32004: "block_not_available",
    -32005: "node_unhealthy",
    -32013: "signature_length_mismatch",
    -32014: "block_status_not_available_yet",
    -32015: "unsupported_transaction_version",
    -32016: "min_context_slot_not_reached",
    429: "rate_limited",
}
# Errors of a node that is behind, busy or briefly unable to answer: asking again works.
_RETRYABLE_RPC_ERRORS: Final = frozenset({-32603, -32004, -32005, -32014, -32016, 429})


class PreflightFailed(ProviderError):
    """`sendTransaction`'s simulation failed, so the node did not forward the transaction.

    `transaction_error` is the simulation's error as the node sent it (JSON, e.g.
    `{"InstructionError": [2, {"Custom": 6019}]}`), or None if it sent none.
    """

    def __init__(self, transaction_error: JsonValue) -> None:
        super().__init__(PROVIDER, "sendTransaction", "preflight_failed")
        self.transaction_error = transaction_error


@dataclass(frozen=True, slots=True)
class LatestBlockhash:
    blockhash: Hash
    last_valid_block_height: int


@dataclass(frozen=True, slots=True)
class ChainPosition:
    """`getEpochInfo` at one commitment: the block height and the slot, read together."""

    block_height: int
    slot: int


@dataclass(frozen=True, slots=True)
class SignatureStatus:
    slot: int
    # Blocks since confirmation; None once the block is rooted (finalized).
    confirmations: int | None
    # The transaction error as the node sent it; None when it succeeded.
    err: JsonValue
    confirmation_status: ConfirmationStatus | None


@dataclass(frozen=True, slots=True)
class SignatureStatuses:
    """The statuses in request order (None: the node does not know the signature).

    `context_slot` is the slot the node answered at: an answer from a node behind a
    slot already seen finalized elsewhere proves nothing about a missing signature.
    """

    context_slot: int
    statuses: tuple[SignatureStatus | None, ...]


@dataclass(frozen=True, slots=True)
class AccountInfo:
    owner: Pubkey
    lamports: int
    data: bytes


class RpcClient:
    """JSON-RPC over one `httpx.AsyncClient`, injected (tests: `httpx.MockTransport`) or owned.

    An injected client must carry no default credentials of another provider: every
    header it holds is sent to the RPC provider.
    """

    def __init__(self, url: str, *, http: httpx.AsyncClient | None = None) -> None:
        self._url = url
        self._owns_http = http is None
        self._http = http if http is not None else httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        self._last_id = 0

    def __repr__(self) -> str:
        return "RpcClient()"

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def get_latest_blockhash(self, commitment: Commitment = "confirmed") -> LatestBlockhash:
        method = "getLatestBlockhash"
        value = _model(
            _BlockhashResult, method, await self._call(method, [{"commitment": commitment}])
        ).value
        return LatestBlockhash(
            blockhash=_parsed(Hash.from_string, value.blockhash, method),
            last_valid_block_height=value.last_valid_block_height,
        )

    async def get_block_height(self, commitment: Commitment = "finalized") -> int:
        method = "getBlockHeight"
        return _count(method, await self._call(method, [{"commitment": commitment}]))

    async def get_epoch_info(self, commitment: Commitment = "finalized") -> ChainPosition:
        method = "getEpochInfo"
        info = _model(_EpochInfo, method, await self._call(method, [{"commitment": commitment}]))
        return ChainPosition(block_height=info.block_height, slot=info.absolute_slot)

    async def get_signature_statuses(self, signatures: Sequence[Signature]) -> SignatureStatuses:
        """With `searchTransactionHistory`: a node also looks past its recent status cache."""
        if not 0 < len(signatures) <= MAX_SIGNATURE_STATUSES:
            raise ValueError(f"ask for 1 to {MAX_SIGNATURE_STATUSES} signatures")
        method = "getSignatureStatuses"
        params: list[JsonValue] = [
            [str(signature) for signature in signatures],
            {"searchTransactionHistory": True},
        ]
        found = _model(_StatusesResult, method, await self._call(method, params))
        if len(found.value) != len(signatures):
            raise ProviderError(PROVIDER, method, "invalid_response", retryable=True)
        return SignatureStatuses(
            context_slot=found.context.slot,
            statuses=tuple(
                None
                if status is None
                else SignatureStatus(
                    slot=status.slot,
                    confirmations=status.confirmations,
                    err=status.err,
                    confirmation_status=status.confirmation_status,
                )
                for status in found.value
            ),
        )

    async def get_account_info(
        self, address: Pubkey, commitment: Commitment = "confirmed"
    ) -> AccountInfo | None:
        """The account (data read as `base64`); None when it does not exist."""
        method = "getAccountInfo"
        params: list[JsonValue] = [str(address), {"encoding": "base64", "commitment": commitment}]
        account = _model(_AccountResult, method, await self._call(method, params)).value
        if account is None:
            return None
        encoded, encoding = account.data
        if encoding != "base64":
            raise ProviderError(PROVIDER, method, "invalid_response", retryable=True)
        return AccountInfo(
            owner=_parsed(Pubkey.from_string, account.owner, method),
            lamports=account.lamports,
            data=_parsed(_base64, encoded, method),
        )

    async def get_balance(self, address: Pubkey, commitment: Commitment = "confirmed") -> int:
        """Lamports."""
        method = "getBalance"
        params: list[JsonValue] = [str(address), {"commitment": commitment}]
        return _model(_BalanceResult, method, await self._call(method, params)).value

    async def get_minimum_balance_for_rent_exemption(self, size: int) -> int:
        method = "getMinimumBalanceForRentExemption"
        return _count(method, await self._call(method, [size, {"commitment": "confirmed"}]))

    async def get_genesis_hash(self) -> str:
        method = "getGenesisHash"
        genesis = await self._call(method, [])
        if not isinstance(genesis, str):
            raise ProviderError(PROVIDER, method, "invalid_response", retryable=True)
        return str(_parsed(Hash.from_string, genesis, method))

    async def send_transaction(self, wire: bytes, *, skip_preflight: bool = False) -> Signature:
        """Sends signed wire bytes once; the node does not retry them (`maxRetries: 0`).

        Preflight simulates at `confirmed`, the commitment of the blockhash; its failure
        raises `PreflightFailed`.
        """
        method = "sendTransaction"
        params: list[JsonValue] = [
            base64.b64encode(wire).decode("ascii"),
            {
                "encoding": "base64",
                "skipPreflight": skip_preflight,
                "preflightCommitment": "confirmed",
                "maxRetries": 0,
            },
        ]
        signature = await self._call(method, params)
        if not isinstance(signature, str):
            raise ProviderError(PROVIDER, method, "invalid_response", retryable=True)
        return _parsed(Signature.from_string, signature, method)

    async def _call(self, method: str, params: list[JsonValue]) -> object:
        """The `result` of one JSON-RPC call; every failure is a `ProviderError`."""
        self._last_id += 1
        body = json.dumps(
            {"jsonrpc": "2.0", "id": self._last_id, "method": method, "params": params}
        )
        response = await self._post(method, body)
        if response.status_code != _HTTP_OK:
            status = response.status_code
            raise ProviderError(
                PROVIDER,
                method,
                f"http_{status}",
                status=status,
                retryable=status in (408, 429) or status >= 500,
            )
        answer = _json_object(response.content)
        if answer is None:
            raise ProviderError(PROVIDER, method, "invalid_response", retryable=True)
        error = answer.get("error")
        if error is not None:
            raise _rpc_error(method, error)
        if "result" not in answer:
            raise ProviderError(PROVIDER, method, "invalid_response", retryable=True)
        return answer["result"]

    async def _post(self, method: str, body: str) -> httpx.Response:
        code = "network_error"
        try:
            return await self._http.post(
                self._url, content=body, headers={"Content-Type": "application/json"}
            )
        except httpx.TimeoutException:
            code = "timeout"
        except (httpx.HTTPError, httpx.InvalidURL):
            code = "network_error"
        # Raised here, not in the handler: httpx's exception names the URL.
        raise ProviderError(PROVIDER, method, code, retryable=True)


# --- response models: what we read of each result -------------------------------------


class _Result(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _Context(_Result):
    slot: int = Field(ge=0)


class _Blockhash(_Result):
    blockhash: str
    last_valid_block_height: int = Field(alias="lastValidBlockHeight", ge=0)


class _BlockhashResult(_Result):
    value: _Blockhash


class _EpochInfo(_Result):
    absolute_slot: int = Field(alias="absoluteSlot", ge=0)
    block_height: int = Field(alias="blockHeight", ge=0)


class _Status(_Result):
    slot: int = Field(ge=0)
    confirmations: int | None = None
    err: JsonValue = None
    confirmation_status: ConfirmationStatus | None = Field(default=None, alias="confirmationStatus")


class _StatusesResult(_Result):
    context: _Context
    value: list[_Status | None]


class _Account(_Result):
    data: tuple[str, str]
    owner: str
    lamports: int = Field(ge=0)


class _AccountResult(_Result):
    value: _Account | None


class _BalanceResult(_Result):
    value: int = Field(ge=0)


def _model[M: BaseModel](model: type[M], method: str, value: object) -> M:
    parsed: M | None = None
    try:
        parsed = model.model_validate(value)
    except ValidationError:
        parsed = None
    if parsed is None:
        # Raised outside the handler: a validation error quotes the data it refused.
        raise ProviderError(PROVIDER, method, "invalid_response", retryable=True)
    return parsed


def _parsed[T](parse: Callable[[str], T], value: str, method: str) -> T:
    parsed: T | None = None
    try:
        parsed = parse(value)
    except (ValueError, ParseHashError):  # `Hash.from_string` raises the latter
        parsed = None
    if parsed is None:
        raise ProviderError(PROVIDER, method, "invalid_response", retryable=True)
    return parsed


def _count(method: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProviderError(PROVIDER, method, "invalid_response", retryable=True)
    return value


def _base64(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except binascii.Error:
        raise ValueError("not base64") from None


def _json_object(content: bytes) -> dict[str, object] | None:
    try:
        body: object = json.loads(content)
    except ValueError:
        return None
    return cast(dict[str, object], body) if isinstance(body, dict) else None


def _rpc_error(method: str, error: object) -> ProviderError:
    if not isinstance(error, dict):
        return ProviderError(PROVIDER, method, "invalid_response", retryable=True)
    fields = cast(dict[str, object], error)
    code = fields.get("code")
    if isinstance(code, bool) or not isinstance(code, int):
        return ProviderError(PROVIDER, method, "invalid_response", retryable=True)
    if method == "sendTransaction" and code == _PREFLIGHT_FAILURE:
        data = fields.get("data")
        details = cast(dict[str, object], data) if isinstance(data, dict) else {}
        return PreflightFailed(_json_value(details.get("err")))
    return ProviderError(
        PROVIDER,
        method,
        _RPC_ERRORS.get(code, f"rpc_error_{code}"),
        retryable=code in _RETRYABLE_RPC_ERRORS,
    )


def _json_value(value: object) -> JsonValue:
    """`value` (from `json.loads`) typed as JSON."""
    return cast(JsonValue, value)
