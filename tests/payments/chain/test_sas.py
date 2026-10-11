"""The SAS side of a release against the golden vectors: gate nonces, attestation PDAs, the
gate approval data, `CreateAttestation`, and the attestation and credential accounts.

`vectors.json` holds what the program itself derived and accepted (`chain.gates`, and the
approval templates in `codec.templates`); here each value is rebuilt with
`bido_back.payments.chain` and compared byte for byte. The fixture escrow is PO-1042's,
whose gate 0 is attested by the buyer and gate 1 by the SQE (`codec.inputs`).
"""

import struct
from collections.abc import Callable
from typing import Final

import pytest
from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM_ID

from bido_back.payments.chain.escrow import decode_po_escrow, release_ix
from bido_back.payments.chain.pdas import SAS_PROGRAM_ID, attestation_pda, gate_nonce
from bido_back.payments.chain.sas import (
    ATTESTATION_ACCOUNT_LEN,
    CREATE_ATTESTATION_DATA_LEN,
    GATE_DATA_LEN,
    GateAttestationAccount,
    create_attestation_ix,
    credential_signers,
    decode_gate_attestation,
    encode_gate_approval_data,
)
from tests.payments.chain.support import attestation_account, credential_account
from tests.payments.chain.vectors import (
    FUNDED_ESCROW_FILE,
    GateVector,
    chain_vectors,
    codec_vectors,
    harness_keypair,
    instruction_vector,
)


def pk(base58: str) -> Pubkey:
    return Pubkey.from_string(base58)


INPUTS: Final = chain_vectors()["inputs"]
PROGRAM: Final = pk(INPUTS["program_id"])
ESCROW: Final = pk(chain_vectors()["escrow"])
CREDENTIAL: Final = pk(INPUTS["credential"])
SCHEMA: Final = pk(INPUTS["schema"])
PO_HASH: Final = bytes.fromhex(INPUTS["po_hash"])
RELAYER: Final = pk(codec_vectors()["inputs"]["relayer"])
# The fixture's attester of each gate: the buyer approver, then the SQE.
ATTESTERS: Final = (pk(codec_vectors()["inputs"]["buyer"]), pk(codec_vectors()["inputs"]["sqe"]))
GATES: Final = chain_vectors()["gates"]


def gate_id(vector: GateVector) -> str:
    return f"gate-{vector['gate']}"


def attest_gate(vector: GateVector) -> Instruction:
    gate = vector["gate"]
    return create_attestation_ix(
        payer=RELAYER,
        signer=ATTESTERS[gate],
        credential=CREDENTIAL,
        schema=SCHEMA,
        nonce=gate_nonce(ESCROW, gate),
        data=encode_gate_approval_data(PO_HASH, gate, bytes.fromhex(vector["evidence_hash"])),
    )


def release(milestone: int) -> Instruction:
    state = decode_po_escrow(FUNDED_ESCROW_FILE.read_bytes())
    return release_ix(
        program_id=PROGRAM,
        escrow=ESCROW,
        vault=state.vault,
        payout_token=state.payout_token,
        mint=state.mint,
        attestation=state.gates[state.milestones[milestone].gate].attestation,
        milestone=milestone,
    )


def test_the_vectors_cover_gates_0_and_1() -> None:
    assert [vector["gate"] for vector in GATES] == [0, 1]
    assert pk(INPUTS["sas_program_id"]) == SAS_PROGRAM_ID
    assert INPUTS["attestation_expiry"] == 0


# ---- addresses ----


@pytest.mark.parametrize("vector", GATES, ids=gate_id)
def test_the_gate_nonce_and_attestation_address_equal_the_vectors(vector: GateVector) -> None:
    nonce = gate_nonce(ESCROW, vector["gate"])
    assert nonce == pk(vector["nonce"])
    assert attestation_pda(CREDENTIAL, SCHEMA, nonce) == pk(vector["attestation"])


def test_the_escrow_stores_the_attestation_address_of_each_gate() -> None:
    state = decode_po_escrow(FUNDED_ESCROW_FILE.read_bytes())
    assert (state.credential, state.schema) == (CREDENTIAL, SCHEMA)
    assert [gate.attestation for gate in state.gates] == [
        attestation_pda(state.credential, state.schema, gate_nonce(ESCROW, index))
        for index in range(len(state.gates))
    ]
    assert [str(gate.attestation) for gate in state.gates] == [v["attestation"] for v in GATES]


def test_gate_2_has_the_address_the_program_derived() -> None:
    """No vector has gate 2 yet: these are the program's own values for PO-1042's escrow,
    from a LiteSVM run of a three-gate escrow (research 04, section 10)."""
    nonce = gate_nonce(ESCROW, 2)
    assert nonce == pk("EbvkU9BZ43KqmURsAmNQhSY7wKSevERgcUUAthY1b9MM")
    assert attestation_pda(CREDENTIAL, SCHEMA, nonce) == pk(
        "7gTqfSqzCnbMUCo9mTXuJ4vN7WKsoKvymbfrp1S5dads"
    )


@pytest.mark.parametrize("gate", [-1, 256])
def test_a_gate_is_a_u8(gate: int) -> None:
    with pytest.raises(ValueError, match="u8"):
        gate_nonce(ESCROW, gate)
    with pytest.raises(ValueError, match="u8"):
        encode_gate_approval_data(PO_HASH, gate, bytes(32))


# ---- the attestation's data and `CreateAttestation` ----


@pytest.mark.parametrize("vector", GATES, ids=gate_id)
def test_the_gate_approval_data_equals_the_vectors(vector: GateVector) -> None:
    gate, evidence = vector["gate"], bytes.fromhex(vector["evidence_hash"])
    data = encode_gate_approval_data(PO_HASH, gate, evidence)
    assert data.hex() == vector["attestation_data"]
    assert len(data) == GATE_DATA_LEN == 73


@pytest.mark.parametrize(
    ("po_hash", "evidence_hash"), [(PO_HASH[:31], bytes(32)), (PO_HASH, bytes(33)), (b"", b"")]
)
def test_the_gate_approval_data_needs_two_32_byte_hashes(
    po_hash: bytes, evidence_hash: bytes
) -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        encode_gate_approval_data(po_hash, 0, evidence_hash)


@pytest.mark.parametrize("vector", GATES, ids=gate_id)
def test_create_attestation_equals_the_vectors(vector: GateVector) -> None:
    ix = attest_gate(vector)
    assert bytes(ix.data).hex() == vector["create_attestation_data"]
    assert len(ix.data) == CREATE_ATTESTATION_DATA_LEN == 118
    assert ix.program_id == SAS_PROGRAM_ID
    assert ix.accounts == [
        AccountMeta(RELAYER, is_signer=True, is_writable=True),
        AccountMeta(ATTESTERS[vector["gate"]], is_signer=True, is_writable=False),
        AccountMeta(CREDENTIAL, is_signer=False, is_writable=False),
        AccountMeta(SCHEMA, is_signer=False, is_writable=False),
        AccountMeta(pk(vector["attestation"]), is_signer=False, is_writable=True),
        AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
    ]


def test_create_attestation_lays_out_tag_nonce_data_and_expiry() -> None:
    nonce, data = gate_nonce(ESCROW, 1), b"\x07" * GATE_DATA_LEN
    ix = create_attestation_ix(
        payer=RELAYER,
        signer=ATTESTERS[1],
        credential=CREDENTIAL,
        schema=SCHEMA,
        nonce=nonce,
        data=data,
        expiry=1_791_464_400,
    )
    assert bytes(ix.data) == (
        b"\x06" + bytes(nonce) + struct.pack("<I", 73) + data + struct.pack("<q", 1_791_464_400)
    )


@pytest.mark.parametrize("expiry", [2**63, -(2**63) - 1])
def test_the_expiry_is_an_i64(expiry: int) -> None:
    with pytest.raises(ValueError, match="i64"):
        create_attestation_ix(
            payer=RELAYER,
            signer=ATTESTERS[0],
            credential=CREDENTIAL,
            schema=SCHEMA,
            nonce=gate_nonce(ESCROW, 0),
            data=b"",
            expiry=expiry,
        )


def test_each_approval_equals_its_template() -> None:
    """Attestation then release(s), as the program accepted them (`codec.templates`)."""
    templates = codec_vectors()["templates"]
    tool_start = [attest_gate(GATES[0]), release(0)]
    assert [instruction_vector(ix) for ix in tool_start] == templates["release_tool_start"]
    # The fixture's old plan releases T1 and PPAP through one gate; Bido gives each its own.
    ppap = [attest_gate(GATES[1]), release(1), release(2)]
    assert [instruction_vector(ix) for ix in ppap] == templates["approve_ppap"]


# ---- the attestation account ----


def attestation(vector: GateVector, *, expiry: int = 0) -> bytes:
    """The account SAS writes for the vector's gate, signed by the fixture's attester."""
    gate = vector["gate"]
    return attestation_account(
        nonce=gate_nonce(ESCROW, gate),
        credential=CREDENTIAL,
        schema=SCHEMA,
        po_hash=PO_HASH,
        gate=gate,
        evidence_hash=bytes.fromhex(vector["evidence_hash"]),
        signer=ATTESTERS[gate],
        expiry=expiry,
    )


@pytest.mark.parametrize("vector", GATES, ids=gate_id)
def test_an_attestation_decodes_at_the_programs_offsets(vector: GateVector) -> None:
    data = attestation(vector)
    assert len(data) == ATTESTATION_ACCOUNT_LEN == 246
    # The data the program reads at 101..174 is the gate data of the vectors.
    assert data[101:174].hex() == vector["attestation_data"]

    gate = vector["gate"]
    assert decode_gate_attestation(data) == GateAttestationAccount(
        nonce=pk(vector["nonce"]),
        credential=CREDENTIAL,
        schema=SCHEMA,
        po_hash=PO_HASH,
        gate=gate,
        evidence_hash=bytes.fromhex(vector["evidence_hash"]),
        signer=ATTESTERS[gate],
        expiry=0,
    )


def test_an_attestation_keeps_its_expiry() -> None:
    assert decode_gate_attestation(attestation(GATES[0], expiry=1_791_464_400)).expiry == (
        1_791_464_400
    )


def _patched(offset: int, value: bytes) -> Callable[[bytes], bytes]:
    return lambda data: data[:offset] + value + data[offset + len(value) :]


MALFORMED: Final[dict[str, Callable[[bytes], bytes]]] = {
    "one byte short": lambda data: data[:-1],
    "one byte long": lambda data: data + b"\x00",
    "a schema, not an attestation": _patched(0, b"\x01"),
    "data of 72 bytes": _patched(97, struct.pack("<I", 72)),
    "a 31-byte po_hash": _patched(101, struct.pack("<I", 31)),
    "a 33-byte evidence hash": _patched(138, struct.pack("<I", 33)),
    "tokenized": _patched(245, b"\x01"),
}


@pytest.mark.parametrize("change", MALFORMED.values(), ids=MALFORMED.keys())
def test_every_shape_the_program_calls_malformed_is_refused(
    change: Callable[[bytes], bytes],
) -> None:
    data = attestation(GATES[0])
    decode_gate_attestation(data)
    with pytest.raises(ValueError):
        decode_gate_attestation(change(data))


# ---- the credential account ----


def test_a_credentials_signers_are_read_as_the_program_reads_them() -> None:
    signers = (ATTESTERS[0], ATTESTERS[1], harness_keypair("gate-attester").pubkey())
    assert credential_signers(credential_account(signers)) == signers
    assert credential_signers(credential_account(())) == ()
    # Trailing bytes are ignored, as `Credential::parse` does.
    assert credential_signers(credential_account(signers[:1]) + b"\x00" * 7) == signers[:1]


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x01" + credential_account(ATTESTERS)[1:],
        credential_account(ATTESTERS)[:-1],
        credential_account(ATTESTERS)[:40],
    ],
    ids=["empty", "a schema", "a signer cut short", "the name cut short"],
)
def test_data_that_is_not_a_credential_is_refused(data: bytes) -> None:
    with pytest.raises(ValueError):
        credential_signers(data)
