"""The other programs' instructions a payment sends, encoded by hand.

Classic SPL Token only (as the escrow program's clients use): `MintToChecked` to
issue the stablecoin, `InitializeMint2` (with the System `CreateAccount` before it)
to create the mint once, the Associated Token Account `CreateIdempotent`, and SPL
Memo v2 to tag a transaction with the payment it belongs to.
"""

import struct
from typing import Final

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.system_program import CreateAccountParams, create_account
from solders.token import ID as TOKEN_PROGRAM_ID

from bido_back.payments.chain.pdas import token_account

ASSOCIATED_TOKEN_PROGRAM_ID: Final = Pubkey.from_string(
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
)
# SPL Memo v2.
MEMO_PROGRAM_ID: Final = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
# The size of an SPL Token mint account.
MINT_ACCOUNT_LEN: Final = 82

# SPL Token instruction tags and layouts.
_MINT_TO_CHECKED: Final = struct.Struct("<BQB")  # tag 14, u64 LE amount, u8 decimals
_MINT_TO_CHECKED_TAG: Final = 14
_INITIALIZE_MINT2_TAG: Final = 20
# Associated Token Account instruction tag of `CreateIdempotent`.
_CREATE_IDEMPOTENT_TAG: Final = 1
_U64_MAX: Final = 2**64 - 1


def create_ata_idempotent_ix(*, payer: Pubkey, owner: Pubkey, mint: Pubkey) -> Instruction:
    """`CreateIdempotent` of `owner`'s associated token account for `mint`; `payer` pays rent.

    Accounts `[payer (w, s), ata (w), owner, mint, system_program, token_program]`;
    data `0x01`. It succeeds without change when the account already exists.
    """
    accounts = [
        AccountMeta(payer, is_signer=True, is_writable=True),
        AccountMeta(token_account(owner, mint), is_signer=False, is_writable=True),
        AccountMeta(owner, is_signer=False, is_writable=False),
        AccountMeta(mint, is_signer=False, is_writable=False),
        AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
    ]
    return Instruction(ASSOCIATED_TOKEN_PROGRAM_ID, bytes([_CREATE_IDEMPOTENT_TAG]), accounts)


def mint_to_checked_ix(
    *, mint: Pubkey, destination: Pubkey, authority: Pubkey, amount: int, decimals: int
) -> Instruction:
    """SPL Token `MintToChecked` of `amount` base units, signed by the mint authority.

    Accounts `[mint (w), destination (w), authority (s)]`; data `0x0e ‖ u64 LE amount ‖
    u8 decimals`. The token program refuses it unless `decimals` is the mint's.
    """
    _check_u64(amount, "amount")
    _check_u8(decimals, "decimals")
    accounts = [
        AccountMeta(mint, is_signer=False, is_writable=True),
        AccountMeta(destination, is_signer=False, is_writable=True),
        AccountMeta(authority, is_signer=True, is_writable=False),
    ]
    data = _MINT_TO_CHECKED.pack(_MINT_TO_CHECKED_TAG, amount, decimals)
    return Instruction(TOKEN_PROGRAM_ID, data, accounts)


def initialize_mint2_ix(
    *, mint: Pubkey, decimals: int, mint_authority: Pubkey, freeze_authority: Pubkey | None
) -> Instruction:
    """SPL Token `InitializeMint2` of an allocated, Token-owned 82-byte account.

    Accounts `[mint (w)]`; data `0x14 ‖ u8 decimals ‖ mint_authority ‖ COption<Pubkey>
    freeze_authority` (`0x00`, or `0x01` and the key).
    """
    _check_u8(decimals, "decimals")
    freeze = b"\x00" if freeze_authority is None else b"\x01" + bytes(freeze_authority)
    data = bytes([_INITIALIZE_MINT2_TAG, decimals]) + bytes(mint_authority) + freeze
    return Instruction(
        TOKEN_PROGRAM_ID, data, [AccountMeta(mint, is_signer=False, is_writable=True)]
    )


def create_mint_account_ix(*, payer: Pubkey, mint: Pubkey, lamports: int) -> Instruction:
    """System `CreateAccount` of a rent-exempt 82-byte account owned by SPL Token."""
    return create_account(
        CreateAccountParams(
            from_pubkey=payer,
            to_pubkey=mint,
            lamports=lamports,
            space=MINT_ACCOUNT_LEN,
            owner=TOKEN_PROGRAM_ID,
        )
    )


def memo_ix(text: str) -> Instruction:
    """SPL Memo v2 with no signer accounts: the program only checks UTF-8 and logs it."""
    if not text:
        raise ValueError("a memo needs text")
    return Instruction(MEMO_PROGRAM_ID, text.encode("utf-8"), [])


def _check_u64(value: int, what: str) -> None:
    if not 0 <= value <= _U64_MAX:
        raise ValueError(f"{what} must be a u64")


def _check_u8(value: int, what: str) -> None:
    if not 0 <= value <= 255:
        raise ValueError(f"{what} must be a u8")
