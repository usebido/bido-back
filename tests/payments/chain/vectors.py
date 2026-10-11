"""The golden vectors of `solana/tests/fixtures/vectors.json` and the harness's test keys.

The program's Rust tests write the `chain` and `codec` sections from what the program
itself derived, wrote and accepted (`UPDATE_VECTORS=1 cargo test -p bido_escrow --test
vectors`); `signing` holds the bank fingerprint and payout reference values. A missing
file or section fails the test that reads it, never skips it.
"""

import hashlib
import json
from functools import cache
from pathlib import Path
from typing import Final, TypedDict, cast

from solders.instruction import Instruction
from solders.keypair import Keypair

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
FIXTURES_DIR: Final = REPO_ROOT / "solana" / "tests" / "fixtures"
VECTORS_FILE: Final = FIXTURES_DIR / "vectors.json"
FUNDED_ESCROW_FILE: Final = FIXTURES_DIR / "po_escrow_funded.bin"
IDL_SOURCE: Final = REPO_ROOT / "solana" / "idl" / "bido_escrow.json"
# The harness's public test-key seeds (`programs/bido_escrow/tests/common/mod.rs`).
_TEST_KEY_DOMAIN: Final = b"bido-escrow-test:"

type JsonValue = None | bool | int | str | list[JsonValue] | dict[str, JsonValue]


class ChainInputs(TypedDict):
    program_id: str
    sas_program_id: str
    buyer: str
    payout: str
    mint: str
    credential: str
    schema: str
    chain_po_id: str
    po_hash: str
    attestation_expiry: int


class EvidenceHashVector(TypedDict):
    chain_po_id: str
    gate: int
    file_hashes: list[str]
    evidence_hash: str


class GateVector(TypedDict):
    gate: int
    nonce: str
    attestation: str
    evidence_hash: str
    # The 73-byte `bido-gate-approval` data and the 118-byte SAS `CreateAttestation` data.
    attestation_data: str
    create_attestation_data: str


class ChainVectors(TypedDict):
    inputs: ChainInputs
    config: str
    config_bump: int
    escrow: str
    escrow_bump: int
    vault: str
    payout_token: str
    event_authority: str
    evidence_hashes: list[EvidenceHashVector]
    gates: list[GateVector]


class AccountMetaVector(TypedDict):
    pubkey: str
    is_signer: bool
    is_writable: bool


class InstructionVector(TypedDict):
    program_id: str
    accounts: list[AccountMetaVector]
    data: str


class AccountVector(TypedDict):
    data: str
    fields: dict[str, JsonValue]


class CodecAccounts(TypedDict):
    config: AccountVector
    # PO-1042's escrow by `EscrowStatus` name.
    po_escrow: dict[str, AccountVector]


class MilestoneInputVector(TypedDict):
    name: str
    bps: int
    gate: int


class CreatePoArgsVector(TypedDict):
    chain_po_id: str
    po_hash: str
    cad_hash: str
    total: int
    gate_attesters: list[str]
    milestones: list[MilestoneInputVector]
    refund_after: int


class CodecInputs(TypedDict):
    program_id: str
    admin: str
    operator: str
    relayer: str
    buyer: str
    sqe: str
    payout: str
    mint: str
    credential: str
    schema: str
    buyer_token: str
    now: int
    create_po: CreatePoArgsVector
    payout_ref: str


class CodecInstructions(TypedDict):
    create_po: InstructionVector
    fund: InstructionVector
    # `release(milestone)` of PO-1042's funded escrow, by milestone index.
    release: list[InstructionVector]


class CodecVectors(TypedDict):
    inputs: CodecInputs
    accounts: CodecAccounts
    instructions: CodecInstructions
    # By transaction kind: the program instructions after the two compute-budget ones.
    templates: dict[str, list[InstructionVector]]


class BankFingerprintVector(TypedDict):
    country: str
    bank_code: str
    account_number: str
    bank_fingerprint: str


class PayoutRefVector(TypedDict):
    beneficiary_ref: str
    bank_fingerprint: str
    chain_po_id: str
    payout_ref: str
    memo: str


class SigningVectors(TypedDict):
    bank_fingerprint: list[BankFingerprintVector]
    payout_ref: list[PayoutRefVector]


def read_section(path: Path, key: str) -> object:
    """The top-level `key` of the vectors file at `path`; asserts that both exist."""
    assert path.is_file(), f"{path} is missing"
    sections = json.loads(path.read_text(encoding="utf-8"))
    assert key in sections, f"{path} has no `{key}` section"
    section: object = sections[key]
    return section


@cache
def chain_vectors() -> ChainVectors:
    return cast(ChainVectors, read_section(VECTORS_FILE, "chain"))


@cache
def codec_vectors() -> CodecVectors:
    return cast(CodecVectors, read_section(VECTORS_FILE, "codec"))


@cache
def signing_vectors() -> SigningVectors:
    return cast(SigningVectors, read_section(VECTORS_FILE, "signing"))


def harness_keypair(label: str) -> Keypair:
    """The harness's throwaway `test_keypair(label)`, from its public seed: never a real key."""
    return Keypair.from_seed(hashlib.sha256(_TEST_KEY_DOMAIN + label.encode()).digest())


def instruction_vector(ix: Instruction) -> InstructionVector:
    """An instruction as `vectors.rs` writes it: base58 keys, flags and hex data."""
    return {
        "program_id": str(ix.program_id),
        "accounts": [
            {
                "pubkey": str(account.pubkey),
                "is_signer": account.is_signer,
                "is_writable": account.is_writable,
            }
            for account in ix.accounts
        ],
        "data": bytes(ix.data).hex(),
    }
