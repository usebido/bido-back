"""The `bido_escrow` program's accounts and the instructions a payment sends.

The decoders read `PoEscrow` and `Config` account data. The builders return one
program instruction each, with the accounts in IDL order and the IDL's signer and
writable flags, and the data from the IDL-driven Borsh coder: `create_po` (the
operator signs, the relayer pays the escrow and both token accounts), `fund` (the
buyer moves exactly `total` into the vault) and `update_config` (the upgrade
authority changes `Config`; only the ops script sends it). The program id is a
setting, so callers pass it; nothing here reads settings, keys or the network.
The `codec` section of `solana/tests/fixtures/vectors.json` pins all of it.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.token import ID as TOKEN_PROGRAM_ID

from bido_back.payments.chain.borsh import (
    BorshCoder,
    BorshValue,
    as_bytes,
    as_int,
    as_list,
    as_pubkey,
    as_str,
    as_struct,
)
from bido_back.payments.chain.idl import load_idl
from bido_back.payments.chain.pdas import (
    config_pda,
    escrow_pda,
    event_authority_pda,
    program_data_address,
    token_account,
)

BPS_TOTAL: Final = 10_000
MAX_MILESTONES: Final = 4
MAX_GATES: Final = 4
MAX_MILESTONE_NAME_BYTES: Final = 24
# Every `PoEscrow` is allocated at its maximum size.
PO_ESCROW_ACCOUNT_LEN: Final = 1070

_CODER: Final = BorshCoder(load_idl())


class EscrowStatus(StrEnum):
    CREATED = "Created"
    FUNDED = "Funded"
    COMPLETED = "Completed"
    REFUNDED = "Refunded"


class MilestoneStatus(StrEnum):
    PENDING = "Pending"
    RELEASED = "Released"
    CANCELLED = "Cancelled"


@dataclass(frozen=True, slots=True)
class Gate:
    attester: Pubkey
    # The SAS attestation PDA a release of this gate must present.
    attestation: Pubkey


@dataclass(frozen=True, slots=True)
class Milestone:
    name: str
    bps: int
    amount: int
    gate: int
    status: MilestoneStatus
    released_slot: int
    approved_by: Pubkey
    evidence_hash: bytes


@dataclass(frozen=True, slots=True)
class PoEscrowAccount:
    """A decoded `PoEscrow` (PDA `["po", buyer, chain_po_id]`); amounts in base units."""

    chain_po_id: str
    buyer: Pubkey
    payout: Pubkey
    payout_token: Pubkey
    mint: Pubkey
    vault: Pubkey
    po_hash: bytes
    cad_hash: bytes
    total: int
    released: int
    status: EscrowStatus
    credential: Pubkey
    schema: Pubkey
    gates: tuple[Gate, ...]
    milestones: tuple[Milestone, ...]
    refund_after: int
    bump: int


@dataclass(frozen=True, slots=True)
class ConfigAccount:
    """A decoded `Config` (PDA `["config"]`); `Pubkey.default()` marks an empty mint slot."""

    admin: Pubkey
    operator: Pubkey
    allowed_mints: tuple[Pubkey, Pubkey]
    sas_credential: Pubkey
    sas_schema: Pubkey
    bump: int


@dataclass(frozen=True, slots=True)
class MilestoneInput:
    name: str
    bps: int
    gate: int


@dataclass(frozen=True, slots=True)
class CreatePoArgs:
    """`create_po`'s arguments; `refund_after` is unix seconds."""

    chain_po_id: str
    po_hash: bytes
    cad_hash: bytes
    total: int
    gate_attesters: tuple[Pubkey, ...]
    milestones: tuple[MilestoneInput, ...]
    refund_after: int


def decode_po_escrow(data: bytes) -> PoEscrowAccount:
    """Decodes `PoEscrow` account data; raises `ValueError` if it is not one."""
    fields = _CODER.decode_account("PoEscrow", data)
    return PoEscrowAccount(
        chain_po_id=as_str(fields["chain_po_id"]),
        buyer=as_pubkey(fields["buyer"]),
        payout=as_pubkey(fields["payout"]),
        payout_token=as_pubkey(fields["payout_token"]),
        mint=as_pubkey(fields["mint"]),
        vault=as_pubkey(fields["vault"]),
        po_hash=as_bytes(fields["po_hash"]),
        cad_hash=as_bytes(fields["cad_hash"]),
        total=as_int(fields["total"]),
        released=as_int(fields["released"]),
        status=EscrowStatus(as_str(fields["status"])),
        credential=as_pubkey(fields["credential"]),
        schema=as_pubkey(fields["schema"]),
        gates=tuple(_gate(gate) for gate in as_list(fields["gates"])),
        milestones=tuple(_milestone(milestone) for milestone in as_list(fields["milestones"])),
        refund_after=as_int(fields["refund_after"]),
        bump=as_int(fields["bump"]),
    )


def decode_config(data: bytes) -> ConfigAccount:
    """Decodes `Config` account data; raises `ValueError` if it is not one."""
    fields = _CODER.decode_account("Config", data)
    first_mint, second_mint = (as_pubkey(mint) for mint in as_list(fields["allowed_mints"]))
    return ConfigAccount(
        admin=as_pubkey(fields["admin"]),
        operator=as_pubkey(fields["operator"]),
        allowed_mints=(first_mint, second_mint),
        sas_credential=as_pubkey(fields["sas_credential"]),
        sas_schema=as_pubkey(fields["sas_schema"]),
        bump=as_int(fields["bump"]),
    )


def milestone_amounts(total: int, bps: Sequence[int]) -> list[int]:
    """The program's split of `total`: `floor(total × bps / 10_000)`, the rest to the last."""
    amounts = [total * share // BPS_TOTAL for share in bps[:-1]]
    return [*amounts, total - sum(amounts)]


def create_po_ix(
    *,
    program_id: Pubkey,
    operator: Pubkey,
    payer: Pubkey,
    buyer: Pubkey,
    payout: Pubkey,
    mint: Pubkey,
    credential: Pubkey,
    args: CreatePoArgs,
) -> Instruction:
    """`create_po`; the escrow, vault, payout token account and event authority are derived.

    `credential` is `Config.sas_credential`; `payer` (the relayer) pays the escrow's and
    both token accounts' rent.
    """
    escrow, _ = escrow_pda(program_id, buyer, args.chain_po_id)
    accounts = {
        "config": config_pda(program_id),
        "operator": operator,
        "payer": payer,
        "buyer": buyer,
        "payout": payout,
        "mint": mint,
        "escrow": escrow,
        "vault": token_account(escrow, mint),
        "payout_token": token_account(payout, mint),
        "sas_credential": credential,
        "token_program": TOKEN_PROGRAM_ID,
        "event_authority": event_authority_pda(program_id),
        "program": program_id,
    }
    value: dict[str, BorshValue] = {
        "chain_po_id": args.chain_po_id,
        "po_hash": args.po_hash,
        "cad_hash": args.cad_hash,
        "total": args.total,
        "gate_attesters": list[BorshValue](args.gate_attesters),
        "milestones": [
            {"name": milestone.name, "bps": milestone.bps, "gate": milestone.gate}
            for milestone in args.milestones
        ],
        "refund_after": args.refund_after,
    }
    return _instruction(program_id, "create_po", accounts, {"args": value})


def fund_ix(
    *,
    program_id: Pubkey,
    buyer: Pubkey,
    escrow: Pubkey,
    mint: Pubkey,
    buyer_token: Pubkey,
    vault: Pubkey,
) -> Instruction:
    """`fund`, signed by the escrow's buyer: `total` from `buyer_token` into the vault."""
    accounts = {
        "buyer": buyer,
        "escrow": escrow,
        "buyer_token": buyer_token,
        "vault": vault,
        "mint": mint,
        "token_program": TOKEN_PROGRAM_ID,
        "event_authority": event_authority_pda(program_id),
        "program": program_id,
    }
    return _instruction(program_id, "fund", accounts, {})


def update_config_ix(
    *,
    program_id: Pubkey,
    admin: Pubkey,
    current: ConfigAccount,
    operator: Pubkey | None = None,
    allowed_mints: tuple[Pubkey, Pubkey] | None = None,
    sas_credential: Pubkey | None = None,
    sas_schema: Pubkey | None = None,
) -> Instruction:
    """`update_config`, signed by the program's upgrade authority `admin`.

    A `None` field keeps its stored value. The SAS accounts passed are the ones the
    config holds after the update, as the program requires.
    """
    accounts = {
        "admin": admin,
        "config": config_pda(program_id),
        "program": program_id,
        "program_data": program_data_address(program_id),
        "sas_credential": current.sas_credential if sas_credential is None else sas_credential,
        "sas_schema": current.sas_schema if sas_schema is None else sas_schema,
    }
    value: dict[str, BorshValue] = {
        "operator": operator,
        "allowed_mints": None if allowed_mints is None else list[BorshValue](allowed_mints),
        "sas_credential": sas_credential,
        "sas_schema": sas_schema,
    }
    return _instruction(program_id, "update_config", accounts, {"args": value})


def _instruction(
    program_id: Pubkey,
    name: str,
    accounts: Mapping[str, Pubkey],
    args: Mapping[str, BorshValue],
) -> Instruction:
    """Instruction `name`: each IDL account in order, from `accounts` or its pinned address.

    An account the IDL pins to the program's own address takes `program_id`, so a
    deployment at another address (a test validator) builds the same instructions.
    """
    idl = load_idl()
    idl_accounts = idl.instructions[name].accounts
    unknown = set(accounts) - {account.name for account in idl_accounts}
    if unknown:
        raise ValueError(f"{name}: accounts {sorted(unknown)} are not in the IDL")
    metas: list[AccountMeta] = []
    for account in idl_accounts:
        pinned: Pubkey | None = None
        if account.address == idl.address:
            pinned = program_id
        elif account.address is not None:
            pinned = Pubkey.from_string(account.address)
        pubkey = accounts.get(account.name, pinned)
        if pubkey is None:
            raise ValueError(f"{name}: no address for account {account.name!r}")
        if pinned is not None and pubkey != pinned:
            raise ValueError(f"{name}: account {account.name!r} must be {pinned}")
        metas.append(AccountMeta(pubkey, account.signer, account.writable))
    return Instruction(program_id, _CODER.encode_instruction(name, args), metas)


def _gate(value: BorshValue) -> Gate:
    fields = as_struct(value)
    return Gate(
        attester=as_pubkey(fields["attester"]), attestation=as_pubkey(fields["attestation"])
    )


def _milestone(value: BorshValue) -> Milestone:
    fields = as_struct(value)
    return Milestone(
        name=as_str(fields["name"]),
        bps=as_int(fields["bps"]),
        amount=as_int(fields["amount"]),
        gate=as_int(fields["gate"]),
        status=MilestoneStatus(as_str(fields["status"])),
        released_slot=as_int(fields["released_slot"]),
        approved_by=as_pubkey(fields["approved_by"]),
        evidence_hash=as_bytes(fields["evidence_hash"]),
    )
