"""Addresses derived from seeds: the escrow program's PDAs, associated token accounts and
the SAS attestation of each gate.

Every token account here is the associated token account of classic SPL Token, as
the program and its clients use. A gate's attestation lives at a fixed SAS address:
`gate_nonce(escrow, gate)` is its nonce and `attestation_pda` its address, which
`create_po` stores in `gates[gate].attestation` (`create_po.rs`). The `chain` section
of `solana/tests/fixtures/vectors.json` pins these derivations.
"""

import hashlib
from typing import Final

from solders.pubkey import Pubkey
from solders.token.associated import get_associated_token_address

# The upgradeable BPF loader: a program's ProgramData is its PDA `[program_id]` here.
BPF_LOADER_UPGRADEABLE_ID: Final = Pubkey.from_string("BPFLoaderUpgradeab1e11111111111111111111111")
# The Solana Attestation Service (`constants.rs` `SAS_PROGRAM_ID`), the same on every cluster.
SAS_PROGRAM_ID: Final = Pubkey.from_string("22zoJMtdu4tQc2PzL74ZUT7FrwgB1Udec8DdW4yw4BdG")

_CONFIG_SEED: Final = b"config"
_PO_SEED: Final = b"po"
# Anchor's `#[event_cpi]` event authority.
_EVENT_AUTHORITY_SEED: Final = b"__event_authority"
_ATTESTATION_SEED: Final = b"attestation"
# `constants.rs` `GATE_NONCE_DOMAIN`.
_GATE_NONCE_DOMAIN: Final = b"bido-gate-v1"
_U8_MAX: Final = 255
# Solana's limit on one seed. solders panics beyond it with a `PanicException`, which
# `except Exception` does not catch, so `_find` raises `ValueError` first.
MAX_SEED_LEN: Final = 32


def config_pda(program_id: Pubkey) -> Pubkey:
    """`Config` PDA `["config"]`."""
    return _find([_CONFIG_SEED], program_id)[0]


def escrow_pda(program_id: Pubkey, buyer: Pubkey, chain_po_id: str) -> tuple[Pubkey, int]:
    """`PoEscrow` PDA `["po", buyer, chain_po_id]` and its bump."""
    return _find([_PO_SEED, bytes(buyer), chain_po_id.encode()], program_id)


def event_authority_pda(program_id: Pubkey) -> Pubkey:
    """The program's event authority PDA `["__event_authority"]` (`emit_cpi!`)."""
    return _find([_EVENT_AUTHORITY_SEED], program_id)[0]


def program_data_address(program_id: Pubkey) -> Pubkey:
    """The program's ProgramData account (holds the upgrade authority `update_config` checks)."""
    return _find([bytes(program_id)], BPF_LOADER_UPGRADEABLE_ID)[0]


def token_account(owner: Pubkey, mint: Pubkey) -> Pubkey:
    """`owner`'s associated token account for `mint` (classic SPL Token).

    The escrow's vault is `token_account(escrow, mint)` and the payout's
    `payout_token` is `token_account(payout, mint)`.
    """
    return get_associated_token_address(owner, mint)


def gate_nonce(escrow: Pubkey, gate: int) -> Pubkey:
    """The SAS nonce of `gate`'s attestation: `sha256("bido-gate-v1" ‖ escrow ‖ u8 gate)`."""
    if not 0 <= gate <= _U8_MAX:
        raise ValueError("the gate must be a u8")
    digest = hashlib.sha256(_GATE_NONCE_DOMAIN + bytes(escrow) + bytes((gate,))).digest()
    return Pubkey.from_bytes(digest)


def attestation_pda(credential: Pubkey, schema: Pubkey, nonce: Pubkey) -> Pubkey:
    """SAS attestation PDA `["attestation", credential, schema, nonce]`.

    One address per nonce, so a gate's attestation can be created only once.
    """
    seeds = [_ATTESTATION_SEED, bytes(credential), bytes(schema), bytes(nonce)]
    return _find(seeds, SAS_PROGRAM_ID)[0]


def _find(seeds: list[bytes], program_id: Pubkey) -> tuple[Pubkey, int]:
    if any(len(seed) > MAX_SEED_LEN for seed in seeds):
        raise ValueError(f"a PDA seed is longer than {MAX_SEED_LEN} bytes")
    return Pubkey.find_program_address(seeds, program_id)
