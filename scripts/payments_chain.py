"""Set up Bido's stablecoin for payments on Solana devnet. Run by a person, never by tests.

From the repository root, with the SOLANA_* settings in `.env`:

    uv run python scripts/payments_chain.py status
    uv run python scripts/payments_chain.py create-mint
    uv run python scripts/payments_chain.py allow-mint --admin-keypair <path> [--slot 0]

- `status` reads the escrow program's `Config` and the mint, and says whether payments
  can run: is SOLANA_OPERATOR_KEY the program's operator, does the program allow
  SOLANA_STABLECOIN_MINT, is that mint ours (SOLANA_MINT_AUTHORITY_KEY mints,
  SOLANA_STABLECOIN_DECIMALS decimals, no freeze authority), and how much SOL the
  relayer has left to pay fees and rent. It sends nothing; exit code 0 means ready.
- `create-mint` creates a new mint of the classic SPL Token program with
  SOLANA_STABLECOIN_DECIMALS decimals, SOLANA_MINT_AUTHORITY_KEY as mint authority and
  no freeze authority; the relayer pays. It prints the address to set as
  SOLANA_STABLECOIN_MINT. The mint account's own key is thrown away: nothing needs it.
- `allow-mint` writes SOLANA_STABLECOIN_MINT into slot `--slot` (default 0: today the
  fixed-supply tUSD, which can no longer mint) of `Config.allowed_mints` with
  `update_config`, keeping the other slot, Circle's devnet USDC: the payout provider
  (Tazapay) takes USDC, not our token. Escrows already created keep their own mint.
  The escrow program's upgrade authority signs it: `--admin-keypair` is
  its keypair file (the `solana-keygen` JSON array). The relayer pays the fee.

It refuses to run unless SOLANA_CLUSTER is devnet and SOLANA_RPC_URL answers with
devnet's genesis hash. It prints public values only (addresses, signatures, balances),
never a key, a keypair file's content or the RPC URL.
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Final

from pydantic import ValidationError
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.token import ID as TOKEN_PROGRAM_ID
from solders.token.state import Mint

from bido_back.payments.chain.escrow import ConfigAccount, decode_config, update_config_ix
from bido_back.payments.chain.keys import HotKey, load_hot_key
from bido_back.payments.chain.pdas import config_pda
from bido_back.payments.chain.rpc import PreflightFailed, RpcClient
from bido_back.payments.chain.solana import require_pubkey, require_rpc_url
from bido_back.payments.chain.spl import (
    MINT_ACCOUNT_LEN,
    create_mint_account_ix,
    initialize_mint2_ix,
)
from bido_back.payments.chain.transactions import (
    CompiledMessage,
    compile_message,
    describe_transaction_error,
    sign_transaction,
)
from bido_back.payments.config import explorer_tx_url
from bido_back.payments.errors import PaymentError
from bido_back.settings import Settings, get_settings

DEVNET: Final = "devnet"
DEVNET_GENESIS_HASH: Final = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
LAMPORTS_PER_SOL: Final = 1_000_000_000
# Generous for one-off setup transactions; the fee it adds is a few hundred lamports.
SETUP_COMPUTE_UNITS: Final = 200_000
CONFIRM_TIMEOUT_S: Final = 90.0
POLL_INTERVAL_S: Final = 2.0


class SetupRefused(Exception):
    """A setup step that cannot run as asked; the message is safe to print."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="payments_chain.py",
        description="Set up Bido's stablecoin for payments on Solana devnet.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="check the program config, the mint and the relayer")
    commands.add_parser("create-mint", help="create the stablecoin mint (prints its address)")
    allow = commands.add_parser("allow-mint", help="let the escrow program take our mint")
    allow.add_argument("--admin-keypair", type=Path, required=True, help="upgrade authority")
    allow.add_argument("--slot", type=int, choices=(0, 1), default=0, help="slot to replace")
    args = parser.parse_args(argv)
    command: str = args.command

    try:
        settings = get_settings()
    except ValidationError as error:
        # The input values may hold keys: name the settings, never show them.
        names = sorted({".".join(str(part) for part in item["loc"]) for item in error.errors()})
        print(f"error: invalid settings: {', '.join(names).upper()}", file=sys.stderr)
        return 1
    try:
        admin = read_keypair(args.admin_keypair) if command == "allow-mint" else None
        slot: int = args.slot if command == "allow-mint" else 0
        return asyncio.run(run(command, settings, admin=admin, slot=slot))
    except (PaymentError, SetupRefused) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def read_keypair(path: Path) -> HotKey:
    """A `solana-keygen` keypair file (a JSON array of the 64 secret key bytes)."""
    text: str | None = None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        text = None
    if text is None:
        raise SetupRefused(f"cannot read {path}")
    keypair: Keypair | None = None
    try:
        keypair = Keypair.from_json(text.strip())
    except ValueError:
        keypair = None
    if keypair is None:
        # Raised outside the handler: the parse error is not chained to it.
        raise SetupRefused(f"{path} is not a solana-keygen keypair file")
    return HotKey(keypair, "upgrade_authority")


async def run(command: str, settings: Settings, *, admin: HotKey | None, slot: int) -> int:
    cluster: str = settings.solana_cluster
    if cluster != DEVNET:
        raise SetupRefused("SOLANA_CLUSTER is not devnet")
    rpc = RpcClient(require_rpc_url(settings))
    try:
        if await rpc.get_genesis_hash() != DEVNET_GENESIS_HASH:
            raise SetupRefused("SOLANA_RPC_URL is not a devnet node")
        if command == "status":
            return await status(rpc, settings)
        if command == "create-mint":
            return await create_mint(rpc, settings)
        if admin is None:
            raise SetupRefused("allow-mint needs --admin-keypair")
        return await allow_mint(rpc, settings, admin=admin, slot=slot)
    finally:
        await rpc.aclose()


async def status(rpc: RpcClient, settings: Settings) -> int:
    program_id = require_pubkey(settings.solana_escrow_program_id, "SOLANA_ESCROW_PROGRAM_ID")
    relayer = _key(settings.solana_relayer_key, "SOLANA_RELAYER_KEY", "relayer")
    operator = _key(settings.solana_operator_key, "SOLANA_OPERATOR_KEY", "operator")
    authority = _key(settings.solana_mint_authority_key, "SOLANA_MINT_AUTHORITY_KEY", "mint")
    mint = require_pubkey(settings.solana_stablecoin_mint, "SOLANA_STABLECOIN_MINT")
    ready = True

    print(f"program: {program_id}")
    config = await read_config(rpc, program_id)
    print(f"config: {config_pda(program_id)}")
    print(f"  operator: {config.operator}")
    print(f"  allowed mints: {config.allowed_mints[0]}, {config.allowed_mints[1]}")
    print(f"  sas credential: {config.sas_credential}")
    if config.operator != operator.pubkey():
        print(f"NOT READY: SOLANA_OPERATOR_KEY is {operator.pubkey()}, not the operator")
        ready = False
    if mint not in config.allowed_mints:
        print("NOT READY: the program does not allow SOLANA_STABLECOIN_MINT (run allow-mint)")
        ready = False

    print(f"mint: {mint}")
    account = await rpc.get_account_info(mint)
    state: Mint | None = None
    if account is not None and account.owner == TOKEN_PROGRAM_ID:
        try:
            state = Mint.from_bytes(account.data)
        except ValueError:
            state = None
    if state is None:
        print("NOT READY: SOLANA_STABLECOIN_MINT is not a mint of the SPL Token program")
        ready = False
    else:
        print(f"  decimals: {state.decimals}, supply: {state.supply} base units")
        print(f"  mint authority: {state.mint_authority}")
        print(f"  freeze authority: {state.freeze_authority}")
        if state.mint_authority != authority.pubkey():
            print("NOT READY: SOLANA_MINT_AUTHORITY_KEY is not the mint's authority")
            ready = False
        if state.decimals != settings.solana_stablecoin_decimals:
            print("NOT READY: the mint's decimals are not SOLANA_STABLECOIN_DECIMALS")
            ready = False

    lamports = await rpc.get_balance(relayer.pubkey())
    print(f"relayer: {relayer.pubkey()} holds {lamports / LAMPORTS_PER_SOL:.6f} SOL")
    print("ready" if ready else "not ready")
    return 0 if ready else 1


async def create_mint(rpc: RpcClient, settings: Settings) -> int:
    relayer = _key(settings.solana_relayer_key, "SOLANA_RELAYER_KEY", "relayer")
    authority = _key(settings.solana_mint_authority_key, "SOLANA_MINT_AUTHORITY_KEY", "mint")
    if settings.solana_stablecoin_mint:
        print("note: SOLANA_STABLECOIN_MINT is already set; this creates another mint")
    new_mint = HotKey(Keypair(), "new_mint")
    lamports = await rpc.get_minimum_balance_for_rent_exemption(MINT_ACCOUNT_LEN)
    latest = await rpc.get_latest_blockhash("confirmed")
    compiled = compile_message(
        payer=relayer.pubkey(),
        instructions=[
            create_mint_account_ix(
                payer=relayer.pubkey(), mint=new_mint.pubkey(), lamports=lamports
            ),
            initialize_mint2_ix(
                mint=new_mint.pubkey(),
                decimals=settings.solana_stablecoin_decimals,
                mint_authority=authority.pubkey(),
                freeze_authority=None,
            ),
        ],
        signers={relayer.pubkey(), new_mint.pubkey()},
        blockhash=latest.blockhash,
        last_valid_block_height=latest.last_valid_block_height,
        compute_unit_limit=SETUP_COMPUTE_UNITS,
        compute_unit_price=settings.solana_priority_fee_micro_lamports,
    )
    print(f"creating mint {new_mint.pubkey()} ({settings.solana_stablecoin_decimals} decimals)")
    program_id = require_pubkey(settings.solana_escrow_program_id, "SOLANA_ESCROW_PROGRAM_ID")
    if not await send_and_confirm(rpc, compiled, (relayer, new_mint), program_id=program_id):
        return 1
    print(f"set SOLANA_STABLECOIN_MINT={new_mint.pubkey()}, then run allow-mint")
    return 0


async def allow_mint(rpc: RpcClient, settings: Settings, *, admin: HotKey, slot: int) -> int:
    program_id = require_pubkey(settings.solana_escrow_program_id, "SOLANA_ESCROW_PROGRAM_ID")
    mint = require_pubkey(settings.solana_stablecoin_mint, "SOLANA_STABLECOIN_MINT")
    relayer = _key(settings.solana_relayer_key, "SOLANA_RELAYER_KEY", "relayer")
    config = await read_config(rpc, program_id)
    if mint in config.allowed_mints:
        print(f"the program already allows {mint} (slot {config.allowed_mints.index(mint)})")
        return 0
    mints = list(config.allowed_mints)
    print(f"slot {slot}: {mints[slot]} -> {mint}; slot {1 - slot} stays {mints[1 - slot]}")
    mints[slot] = mint
    instruction = update_config_ix(
        program_id=program_id,
        admin=admin.pubkey(),
        current=config,
        allowed_mints=(mints[0], mints[1]),
    )
    latest = await rpc.get_latest_blockhash("confirmed")
    compiled = compile_message(
        payer=relayer.pubkey(),
        instructions=[instruction],
        signers={relayer.pubkey(), admin.pubkey()},
        blockhash=latest.blockhash,
        last_valid_block_height=latest.last_valid_block_height,
        compute_unit_limit=SETUP_COMPUTE_UNITS,
        compute_unit_price=settings.solana_priority_fee_micro_lamports,
    )
    if not await send_and_confirm(rpc, compiled, (relayer, admin), program_id=program_id):
        return 1
    updated = await read_config(rpc, program_id)
    print(f"allowed mints: {updated.allowed_mints[0]}, {updated.allowed_mints[1]}")
    return 0 if mint in updated.allowed_mints else 1


async def read_config(rpc: RpcClient, program_id: Pubkey) -> ConfigAccount:
    account = await rpc.get_account_info(config_pda(program_id))
    if account is None or account.owner != program_id:
        raise SetupRefused("the escrow program has no Config account (init_config first)")
    return decode_config(account.data)


async def send_and_confirm(
    rpc: RpcClient,
    compiled: CompiledMessage,
    keys: tuple[HotKey, ...],
    *,
    program_id: Pubkey,
) -> bool:
    """Signs, sends once and waits for `confirmed`, the expiry or the timeout."""
    signed = sign_transaction(compiled, keys)
    print(f"signature: {signed.signature}")
    print(f"explorer: {explorer_tx_url(DEVNET, signed.signature)}")
    refused: str | None = None
    try:
        await rpc.send_transaction(signed.wire)
    except PreflightFailed as failure:
        refused = describe_transaction_error(
            failure.transaction_error, program_id=program_id, message=compiled.message
        )
    if refused is not None:
        print(f"refused by the node: {refused}", file=sys.stderr)
        return False
    signature = Signature.from_string(signed.signature)
    deadline = time.monotonic() + CONFIRM_TIMEOUT_S
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_INTERVAL_S)
        [found] = (await rpc.get_signature_statuses([signature])).statuses
        if found is not None and found.confirmation_status in ("confirmed", "finalized"):
            if found.err is None:
                print("confirmed")
                return True
            error = describe_transaction_error(
                found.err, program_id=program_id, message=compiled.message
            )
            print(f"failed on chain: {error}", file=sys.stderr)
            return False
        if await rpc.get_block_height("confirmed") > signed.last_valid_block_height:
            print("expired before it landed; run the command again", file=sys.stderr)
            return False
    print("not confirmed yet; look the signature up before running again", file=sys.stderr)
    return False


def _key(secret: str | None, setting: str, role: str) -> HotKey:
    return load_hot_key(secret, setting=setting, role=role)


if __name__ == "__main__":
    sys.exit(main())
