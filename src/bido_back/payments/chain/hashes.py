"""The escrow id and the payout reference a payment writes on chain. Standard library only.

The service imports this module to fix a payment's terms before anything reaches
Solana, so it must stay free of solders and of I/O.

- `new_chain_po_id(po_ref)`: the escrow's id on chain, `<PO ref>-<4 random Crockford
  base32 characters>` (e.g. `PO-12-7KQ2`). The escrow PDA is `["po", buyer,
  chain_po_id]` and `create_po` can create it once, so each escrow gets a fresh id,
  stored with the payment and reused by every retry of that payment.
- `bank_fingerprint(country, bank_code, account_number)`: binds a payment to one
  supplier bank account without storing the account.
- `payout_ref(beneficiary_ref, fingerprint_hex, chain_po_id)`: the reference
  `create_po` carries as the memo `bido:payout_ref:<hex>`, so the payout of a
  released milestone can be matched to the account the buyer approved.
- `compute_evidence_hash(chain_po_id, gate, file_hashes)`: what a milestone's
  approval attests on chain (`evidence_hash` of the gate's SAS attestation): the
  files the person approved, bound to the escrow and the gate. Twin of the TS
  client's `computeEvidenceHash`.

Golden values: `signing.bank_fingerprint` and `signing.payout_ref` of
`solana/tests/fixtures/vectors.json`, and its `chain.evidence_hashes`.
"""

import hashlib
import re
import secrets
from collections.abc import Iterable
from typing import Final

# `<1-27 of [A-Z0-9-]>-<4 Crockford base32>`: 6 to 32 bytes, the program's limits for a
# PDA seed of `[A-Z0-9-]` (`create_po` check 3).
CHAIN_PO_ID_RE: Final = re.compile(r"[A-Z0-9-]{1,27}-[0-9A-HJKMNP-TV-Z]{4}")
PAYOUT_REF_MEMO_PREFIX: Final = "bido:payout_ref:"

# Crockford base32: digits and capitals without I, L, O and U.
_CROCKFORD_BASE32: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_SUFFIX_LEN: Final = 4
_BANK_DOMAIN: Final = b"bido-bank-v1"
_EVIDENCE_DOMAIN: Final = b"bido-evidence-v1"
_PAYOUT_REF_DOMAIN: Final = b"bido-payout-ref-v1"
_SEPARATOR: Final = b"\x00"
# The u8 length prefix of each string in the payout reference.
_MAX_FIELD_BYTES: Final = 255
_U8_MAX: Final = 255
_HEX64: Final = re.compile(r"[0-9a-f]{64}")


def new_chain_po_id(po_ref: str) -> str:
    """`<po_ref>-<4 random Crockford base32 characters>`, e.g. `PO-12-7KQ2`.

    Raises `ValueError` unless the result matches `CHAIN_PO_ID_RE`: `po_ref` must be 1
    to 27 of `[A-Z0-9-]`, which keeps the id within the 32-byte PDA seed limit.
    """
    suffix = "".join(secrets.choice(_CROCKFORD_BASE32) for _ in range(_SUFFIX_LEN))
    chain_po_id = f"{po_ref}-{suffix}"
    if not is_chain_po_id(chain_po_id):
        raise ValueError("the PO reference does not make a valid chain_po_id")
    return chain_po_id


def is_chain_po_id(value: str) -> bool:
    return CHAIN_PO_ID_RE.fullmatch(value) is not None


def bank_fingerprint(country: str, bank_code: str, account_number: str) -> str:
    """`sha256("bido-bank-v1" ‖ country ‖ 0x00 ‖ bank_code ‖ 0x00 ‖ account_number)`, hex.

    The strings are hashed exactly as given (UTF-8): normalising them is the caller's
    job, and the same account must always be given the same way.
    """
    data = _SEPARATOR.join(
        (country.encode("utf-8"), bank_code.encode("utf-8"), account_number.encode("utf-8"))
    )
    return hashlib.sha256(_BANK_DOMAIN + data).hexdigest()


def payout_ref(beneficiary_ref: str, fingerprint_hex: str, chain_po_id: str) -> str:
    """The payout reference, lowercase hex.

    `sha256("bido-payout-ref-v1" ‖ u8 len ‖ beneficiary_ref ‖ fingerprint (32 bytes) ‖
    u8 len ‖ chain_po_id)`, strings in UTF-8. Raises `ValueError` when a string is longer
    than 255 UTF-8 bytes or the fingerprint is not 64 lowercase hex digits.
    """
    if _HEX64.fullmatch(fingerprint_hex) is None:
        raise ValueError("the bank fingerprint must be 64 lowercase hex digits")
    data = b"".join(
        (
            _PAYOUT_REF_DOMAIN,
            _length_prefixed(beneficiary_ref, "beneficiary_ref"),
            bytes.fromhex(fingerprint_hex),
            _length_prefixed(chain_po_id, "chain_po_id"),
        )
    )
    return hashlib.sha256(data).hexdigest()


def compute_evidence_hash(chain_po_id: str, gate: int, file_hashes: Iterable[str]) -> str:
    """`sha256("bido-evidence-v1" ‖ chain_po_id ‖ u8 gate ‖ sorted file hashes)`, hex.

    `file_hashes` are the sha256 of each evidence file (64 lowercase hex digits), in any
    order; equal-length lowercase hex sorts in the order of its bytes. Raises
    `ValueError` for a gate outside u8, a hash that is not 64 lowercase hex digits, or
    an id that is not a chain PO id.
    """
    if not 0 <= gate <= _U8_MAX:
        raise ValueError("the gate must be a u8")
    if not is_chain_po_id(chain_po_id):
        raise ValueError("not a chain PO id")
    hashes = list(file_hashes)
    if any(_HEX64.fullmatch(value) is None for value in hashes):
        raise ValueError("a file hash must be 64 lowercase hex digits")
    data = b"".join(
        (
            _EVIDENCE_DOMAIN,
            chain_po_id.encode("utf-8"),
            bytes((gate,)),
            *(bytes.fromhex(value) for value in sorted(hashes)),
        )
    )
    return hashlib.sha256(data).hexdigest()


def payout_ref_memo(payout_ref_hex: str) -> str:
    """The `create_po` memo: `bido:payout_ref:<64 lowercase hex>` (80 bytes)."""
    if _HEX64.fullmatch(payout_ref_hex) is None:
        raise ValueError("the payout reference must be 64 lowercase hex digits")
    return f"{PAYOUT_REF_MEMO_PREFIX}{payout_ref_hex}"


def _length_prefixed(value: str, name: str) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) > _MAX_FIELD_BYTES:
        raise ValueError(f"{name} is longer than {_MAX_FIELD_BYTES} bytes")
    return bytes((len(encoded),)) + encoded
