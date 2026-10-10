"""Addresses derived from seeds: the escrow program's PDAs and associated token accounts.

Every token account here is the associated token account of classic SPL Token, as
the program and its clients use. The `chain` section of
`solana/tests/fixtures/vectors.json` pins these derivations.
"""

from typing import Final

from solders.pubkey import Pubkey
from solders.token.associated import get_associated_token_address

# The upgradeable BPF loader: a program's ProgramData is its PDA `[program_id]` here.
BPF_LOADER_UPGRADEABLE_ID: Final = Pubkey.from_string("BPFLoaderUpgradeab1e11111111111111111111111")

_CONFIG_SEED: Final = b"config"
_PO_SEED: Final = b"po"
# Anchor's `#[event_cpi]` event authority.
_EVENT_AUTHORITY_SEED: Final = b"__event_authority"
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


def _find(seeds: list[bytes], program_id: Pubkey) -> tuple[Pubkey, int]:
    if any(len(seed) > MAX_SEED_LEN for seed in seeds):
        raise ValueError(f"a PDA seed is longer than {MAX_SEED_LEN} bytes")
    return Pubkey.find_program_address(seeds, program_id)
