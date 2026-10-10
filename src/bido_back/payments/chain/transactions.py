"""v0 transactions: compile, sign, merge a remote wallet's signature, name an error. Pure.

Every transaction is `[SetComputeUnitLimit, SetComputeUnitPrice, *instructions]` in one
v0 message without lookup tables, whose fee payer (slot 0) is the relayer. Every
signer signs the same bytes, `to_bytes_versioned(message)` (the `0x80` prefix and the
message; `bytes(message)` would give signatures the runtime rejects).

A transaction a remote wallet signs leaves this process with every slot zeroed
(`unsigned_transaction`), so nothing that could land exists until the wallet's
signature comes back and ours go on last (`add_remote_signature`): the message the
wallet returns must be byte-identical to ours and its signature must verify.
"""

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Final, cast

from pydantic import JsonValue
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.errors import BincodeError
from solders.hash import Hash
from solders.instruction import Instruction
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from bido_back.payments.chain.idl import program_error_names
from bido_back.payments.chain.keys import HotKey
from bido_back.payments.errors import ProviderError
from bido_back.payments.ports import SignedTransaction, UnsignedTransaction

# The largest serialized transaction a cluster accepts.
MAX_TRANSACTION_BYTES: Final = 1232
# Who signs a transaction on behalf of the buyer, for errors about its answer.
_WALLET: Final = "wallet"
_WALLET_SIGN: Final = "sign_transaction"
# A Solana error variant name, as the node writes it (`BlockhashNotFound`, ...).
_VARIANT: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_UNKNOWN_ERROR: Final = "Unknown"


@dataclass(frozen=True, slots=True)
class CompiledMessage:
    """A compiled v0 message, the bytes every signer signs and the blockhash's expiry."""

    message: MessageV0
    message_bytes: bytes
    last_valid_block_height: int

    @property
    def signers(self) -> list[Pubkey]:
        """The required signers, in signature-slot order (slot 0 pays the fee)."""
        return self.message.account_keys[: self.message.header.num_required_signatures]


def compile_message(
    *,
    payer: Pubkey,
    instructions: Sequence[Instruction],
    signers: Collection[Pubkey],
    blockhash: Hash,
    last_valid_block_height: int,
    compute_unit_limit: int,
    compute_unit_price: int,
) -> CompiledMessage:
    """`[SetComputeUnitLimit, SetComputeUnitPrice, *instructions]` as a v0 message.

    Raises `ValueError` unless the required signers are exactly `signers` with `payer`
    in slot 0, and when the transaction would exceed `MAX_TRANSACTION_BYTES`.
    """
    message = MessageV0.try_compile(
        payer,
        [
            set_compute_unit_limit(compute_unit_limit),
            set_compute_unit_price(compute_unit_price),
            *instructions,
        ],
        [],
        blockhash,
    )
    compiled = CompiledMessage(message, to_bytes_versioned(message), last_valid_block_height)
    required = compiled.signers
    if required[0] != payer or set(required) != set(signers) or len(required) != len(set(signers)):
        raise ValueError("the transaction's signers are not the expected ones")
    size = len(_populate(compiled, [Signature.default()] * len(required)))
    if size > MAX_TRANSACTION_BYTES:
        raise ValueError(f"the transaction is {size} bytes, over {MAX_TRANSACTION_BYTES}")
    return compiled


def compiled_from_wire(wire: bytes, last_valid_block_height: int) -> CompiledMessage:
    """The v0 message of serialized transaction bytes; `ValueError` if there is none."""
    message = _parse_transaction(wire).message
    if not isinstance(message, MessageV0):
        raise ValueError("not a v0 transaction")
    return CompiledMessage(message, to_bytes_versioned(message), last_valid_block_height)


def sign_transaction(compiled: CompiledMessage, keys: Sequence[HotKey]) -> SignedTransaction:
    """Every required signer signs with its key in `keys`; `ValueError` if one has none."""
    by_key = {key.pubkey(): key for key in keys}
    if any(signer not in by_key for signer in compiled.signers):
        raise ValueError("no key for a required signer")
    signatures = [
        by_key[signer].sign_message(compiled.message_bytes) for signer in compiled.signers
    ]
    return _signed(compiled, signatures)


def unsigned_transaction(compiled: CompiledMessage) -> UnsignedTransaction:
    """The wire bytes with every signature slot zeroed, for a remote wallet to sign."""
    wire = _populate(compiled, [Signature.default()] * len(compiled.signers))
    return UnsignedTransaction(wire=wire, last_valid_block_height=compiled.last_valid_block_height)


def add_remote_signature(
    compiled: CompiledMessage,
    *,
    remote: Pubkey,
    remote_signed: bytes,
    local: Sequence[HotKey],
) -> SignedTransaction:
    """`compiled` with `remote`'s signature taken from `remote_signed`, then ours.

    `remote_signed` is the transaction the remote wallet returned. Its message must be
    byte-identical to `compiled`'s and `remote`'s signature must verify over it;
    otherwise `ProviderError("wallet", "sign_transaction", "message_changed" |
    "invalid_signature" | "unreadable_transaction")`. Every other required signer signs
    with its key in `local`.
    """
    required = compiled.signers
    if remote not in required:
        raise ValueError("the remote wallet is not a signer of this transaction")
    returned = _remote_transaction(remote_signed)
    returned_message = returned.message
    if (
        not isinstance(returned_message, MessageV0)
        or to_bytes_versioned(returned_message) != compiled.message_bytes
        or len(returned.signatures) != len(required)
    ):
        raise ProviderError(_WALLET, _WALLET_SIGN, "message_changed")
    remote_signature = returned.signatures[required.index(remote)]
    if not remote_signature.verify(remote, compiled.message_bytes):
        raise ProviderError(_WALLET, _WALLET_SIGN, "invalid_signature")
    by_key = {key.pubkey(): key for key in local}
    if any(signer != remote and signer not in by_key for signer in required):
        raise ValueError("no key for a required signer")
    signatures = [
        remote_signature
        if signer == remote
        else by_key[signer].sign_message(compiled.message_bytes)
        for signer in required
    ]
    return _signed(compiled, signatures)


def describe_transaction_error(
    error: JsonValue, *, program_id: Pubkey, message: MessageV0 | None = None
) -> str:
    """A code for a transaction error as the node sent it (JSON): never logs, never data.

    A custom error of the escrow program is its IDL name (`AlreadyFunded`, ...). Without
    the message, a custom code is attributed to the program when the IDL defines it: the
    other programs our transactions call (Compute Budget, System, SPL Token, Associated
    Token Account, Memo) define no custom code from 6000 up. Anything else is Solana's
    own name: `BlockhashNotFound`, `InstructionError(2, Custom(1))`,
    `InstructionError(0, InvalidArgument)`, and `Unknown` for a shape it does not know.
    """
    if isinstance(error, str):
        return _variant(error)
    if not isinstance(error, dict) or len(error) != 1:
        return _UNKNOWN_ERROR
    [(kind, detail)] = error.items()
    if kind != "InstructionError":
        return _variant(kind)
    if not isinstance(detail, list) or len(detail) != 2 or not _is_index(detail[0]):
        return _UNKNOWN_ERROR
    index, inner = cast(int, detail[0]), detail[1]
    if isinstance(inner, dict) and len(inner) == 1:
        [(inner_kind, inner_detail)] = inner.items()
        if inner_kind == "Custom" and _is_index(inner_detail):
            code = cast(int, inner_detail)
            name = program_error_names().get(code)
            if name is not None and (message is None or _program_at(message, index) == program_id):
                return name
            return f"InstructionError({index}, Custom({code}))"
        return f"InstructionError({index}, {_variant(inner_kind)})"
    if isinstance(inner, str):
        return f"InstructionError({index}, {_variant(inner)})"
    return _UNKNOWN_ERROR


def _variant(name: str) -> str:
    """The node's variant name if it looks like one; it is logged, so nothing else is."""
    return name if _VARIANT.fullmatch(name) else _UNKNOWN_ERROR


def _is_index(value: JsonValue) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _program_at(message: MessageV0, index: int) -> Pubkey | None:
    if not 0 <= index < len(message.instructions):
        return None
    program_index = message.instructions[index].program_id_index
    keys = message.account_keys
    return keys[program_index] if program_index < len(keys) else None


def _signed(compiled: CompiledMessage, signatures: list[Signature]) -> SignedTransaction:
    return SignedTransaction(
        signature=str(signatures[0]),
        wire=_populate(compiled, signatures),
        last_valid_block_height=compiled.last_valid_block_height,
    )


def _populate(compiled: CompiledMessage, signatures: list[Signature]) -> bytes:
    return bytes(VersionedTransaction.populate(compiled.message, signatures))


def _parse_transaction(wire: bytes) -> VersionedTransaction:
    transaction: VersionedTransaction | None
    try:
        transaction = VersionedTransaction.from_bytes(wire)
    except (ValueError, BincodeError):
        transaction = None
    if transaction is None:
        raise ValueError("not a serialized transaction")
    return transaction


def _remote_transaction(wire: bytes) -> VersionedTransaction:
    transaction: VersionedTransaction | None
    try:
        transaction = _parse_transaction(wire)
    except ValueError:
        transaction = None
    if transaction is None:
        raise ProviderError(_WALLET, _WALLET_SIGN, "unreadable_transaction")
    return transaction
