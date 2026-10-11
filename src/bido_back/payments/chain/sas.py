"""The SAS side of a milestone release: the gate approval data, `CreateAttestation` and
the attestation account.

The escrow releases milestone i only against the Solana Attestation Service attestation
of its gate (checks 4-13 of `release`, `solana/programs/bido_escrow/src/approval.rs`).
SAS is not an Anchor program, so its instruction and its account are encoded here by
hand, at the offsets the program reads; twin of `solana/src/client/sas.ts`:

- the data of schema `bido-gate-approval` v1 (layout `[13, 0, 13]`), 73 bytes:
  `u32 LE 32 ‖ po_hash ‖ u8 gate ‖ u32 LE 32 ‖ evidence_hash`;
- `CreateAttestation` (tag 6): `0x06 ‖ nonce ‖ u32 LE data length ‖ data ‖ i64 LE expiry`,
  118 bytes with the gate data; accounts `[payer (w, s), signer (s), credential, schema,
  attestation (w), system_program]`;
- the attestation account, 246 bytes: `[2] ‖ nonce ‖ credential ‖ schema ‖ u32 LE 73 ‖
  data ‖ signer ‖ i64 LE expiry ‖ token_account`;
- the credential account, whose authorized signers are who may attest under it:
  `[0] ‖ authority ‖ u32 LE len ‖ name ‖ u32 LE count ‖ count × signer` (`sas.rs`).

In a release, the payer is the relayer (it pays the attestation's rent) and the signer
is the gate's attester; Bido always writes expiry 0 (never expires). The `chain.gates`
section of `solana/tests/fixtures/vectors.json` pins the data and the instruction.
"""

import struct
from dataclasses import dataclass
from typing import Final

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM_ID

from bido_back.payments.chain.pdas import SAS_PROGRAM_ID, attestation_pda

HASH_LEN: Final = 32
_ADDRESS_LEN: Final = 32
_U32_LEN: Final = 4
_I64_LEN: Final = 8
# `u32 LE 32 ‖ po_hash ‖ u8 gate ‖ u32 LE 32 ‖ evidence_hash`.
GATE_DATA_LEN: Final = _U32_LEN + HASH_LEN + 1 + _U32_LEN + HASH_LEN  # 73
# `tag ‖ nonce ‖ u32 LE data length ‖ data ‖ i64 LE expiry`, with the gate data.
CREATE_ATTESTATION_DATA_LEN: Final = 1 + _ADDRESS_LEN + _U32_LEN + GATE_DATA_LEN + _I64_LEN  # 118
# Discriminator, nonce, credential, schema, data (length-prefixed), signer, expiry, token account.
ATTESTATION_ACCOUNT_LEN: Final = (
    1 + 3 * _ADDRESS_LEN + _U32_LEN + GATE_DATA_LEN + _ADDRESS_LEN + _I64_LEN + _ADDRESS_LEN
)  # 246

_CREATE_ATTESTATION_TAG: Final = 6
_CREDENTIAL_DISCRIMINATOR: Final = 0
_ATTESTATION_DISCRIMINATOR: Final = 2
# Offsets in an attestation account (`approval.rs`).
_NONCE: Final = 1
_CREDENTIAL: Final = _NONCE + _ADDRESS_LEN  # 33
_SCHEMA: Final = _CREDENTIAL + _ADDRESS_LEN  # 65
_DATA_LEN: Final = _SCHEMA + _ADDRESS_LEN  # 97
_DATA: Final = _DATA_LEN + _U32_LEN  # 101
_SIGNER: Final = _DATA + GATE_DATA_LEN  # 174
_EXPIRY: Final = _SIGNER + _ADDRESS_LEN  # 206
_TOKEN_ACCOUNT: Final = _EXPIRY + _I64_LEN  # 214
# Offsets in the gate data.
_PO_HASH: Final = _U32_LEN  # 4
_GATE: Final = _PO_HASH + HASH_LEN  # 36
_EVIDENCE_HASH_LEN: Final = _GATE + 1  # 37
_EVIDENCE_HASH: Final = _EVIDENCE_HASH_LEN + _U32_LEN  # 41
_U8_MAX: Final = 255
_I64_MIN: Final = -(2**63)
_I64_MAX: Final = 2**63 - 1


@dataclass(frozen=True, slots=True)
class GateAttestationAccount:
    """An attestation account of the gate schema, decoded at the program's offsets."""

    nonce: Pubkey
    credential: Pubkey
    schema: Pubkey
    po_hash: bytes  # 32 bytes: the escrow's `po_hash` it approves
    gate: int
    evidence_hash: bytes  # 32 bytes: what the release records on the milestone
    # The authorized signer who created it: `release` requires the gate's pinned attester.
    signer: Pubkey
    expiry: int  # unix seconds; 0 never expires (Bido always writes 0)


def encode_gate_approval_data(po_hash: bytes, gate: int, evidence_hash: bytes) -> bytes:
    """The 73-byte data `u32 LE 32 ‖ po_hash ‖ u8 gate ‖ u32 LE 32 ‖ evidence_hash`."""
    if len(po_hash) != HASH_LEN or len(evidence_hash) != HASH_LEN:
        raise ValueError(f"po_hash and evidence_hash must be {HASH_LEN} bytes")
    if not 0 <= gate <= _U8_MAX:
        raise ValueError("the gate must be a u8")
    length = struct.pack("<I", HASH_LEN)
    return length + po_hash + bytes((gate,)) + length + evidence_hash


def create_attestation_ix(
    *,
    payer: Pubkey,
    signer: Pubkey,
    credential: Pubkey,
    schema: Pubkey,
    nonce: Pubkey,
    data: bytes,
    expiry: int = 0,
) -> Instruction:
    """SAS `CreateAttestation` of `data` at `attestation_pda(credential, schema, nonce)`.

    `payer` pays the account's rent and `signer` must be an authorized signer of the
    credential. For a gate: `nonce` is `gate_nonce(escrow, gate)` and `data` is
    `encode_gate_approval_data(...)`, which makes 118 bytes of instruction data.
    """
    if not _I64_MIN <= expiry <= _I64_MAX:
        raise ValueError("the expiry must be an i64")
    instruction_data = b"".join(
        (
            bytes((_CREATE_ATTESTATION_TAG,)),
            bytes(nonce),
            struct.pack("<I", len(data)),
            data,
            struct.pack("<q", expiry),
        )
    )
    return Instruction(
        SAS_PROGRAM_ID,
        instruction_data,
        [
            AccountMeta(payer, is_signer=True, is_writable=True),
            AccountMeta(signer, is_signer=True, is_writable=False),
            AccountMeta(credential, is_signer=False, is_writable=False),
            AccountMeta(schema, is_signer=False, is_writable=False),
            AccountMeta(
                attestation_pda(credential, schema, nonce), is_signer=False, is_writable=True
            ),
            AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ],
    )


def decode_gate_attestation(data: bytes) -> GateAttestationAccount:
    """Decodes an attestation account of the gate schema.

    Raises `ValueError` on every shape `release` rejects as `AttestationMalformed`: a
    length other than 246, a discriminator other than 2, data that is not 73 bytes, a
    hash that is not a 32-byte vector, or a token account (a tokenized attestation).
    """
    if len(data) != ATTESTATION_ACCOUNT_LEN:
        raise ValueError(f"an attestation account is {ATTESTATION_ACCOUNT_LEN} bytes")
    if data[0] != _ATTESTATION_DISCRIMINATOR:
        raise ValueError("not an attestation account")
    if _u32(data, _DATA_LEN) != GATE_DATA_LEN:
        raise ValueError(f"the attestation data must be {GATE_DATA_LEN} bytes")
    gate_data = data[_DATA:_SIGNER]
    if _u32(gate_data, 0) != HASH_LEN or _u32(gate_data, _EVIDENCE_HASH_LEN) != HASH_LEN:
        raise ValueError(f"the attestation's hashes must be {HASH_LEN} bytes")
    if any(data[_TOKEN_ACCOUNT:]):
        raise ValueError("the attestation is tokenized")
    (expiry,) = struct.unpack_from("<q", data, _EXPIRY)
    return GateAttestationAccount(
        nonce=Pubkey.from_bytes(data[_NONCE:_CREDENTIAL]),
        credential=Pubkey.from_bytes(data[_CREDENTIAL:_SCHEMA]),
        schema=Pubkey.from_bytes(data[_SCHEMA:_DATA_LEN]),
        po_hash=gate_data[_PO_HASH:_GATE],
        gate=gate_data[_GATE],
        evidence_hash=gate_data[_EVIDENCE_HASH:],
        signer=Pubkey.from_bytes(data[_SIGNER:_EXPIRY]),
        expiry=expiry,
    )


def credential_signers(data: bytes) -> tuple[Pubkey, ...]:
    """The authorized signers of a SAS credential account, as the escrow program reads them
    (`sas.rs` `Credential::parse`: trailing bytes are ignored).

    Raises `ValueError` if `data` is not a credential or ends early.
    """
    if not data or data[0] != _CREDENTIAL_DISCRIMINATOR:
        raise ValueError("not a credential account")
    offset = 1 + _ADDRESS_LEN  # the authority
    offset += _U32_LEN + _u32(data, offset)  # the name
    count = _u32(data, offset)
    offset += _U32_LEN
    end = offset + count * _ADDRESS_LEN
    if end > len(data):
        raise ValueError("the credential's signer list ends early")
    return tuple(
        Pubkey.from_bytes(data[at : at + _ADDRESS_LEN]) for at in range(offset, end, _ADDRESS_LEN)
    )


def _u32(data: bytes, offset: int) -> int:
    """The u32 LE at `offset`; `ValueError` when `data` ends before it."""
    if offset + _U32_LEN > len(data):
        raise ValueError("the data ends early")
    (value,) = struct.unpack_from("<I", data, offset)
    return value
