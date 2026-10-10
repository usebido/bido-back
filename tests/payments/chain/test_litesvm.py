"""Mint, `create_po` and `fund` through `SolanaPaymentChain`, executed by the real program.

LiteSVM (`solders.litesvm`) runs `bido_escrow.so` in process. A JSON-RPC bridge over
`httpx.MockTransport` answers the adapter's requests from it: every transaction the
adapter builds, signs and sends is executed by the real runtime, SPL programs and
escrow program, and every status and account it reads back is theirs. Nothing reaches
a network.

The program binary is built separately (`anchor build` in `solana/`): the test reads
it from `BIDO_ESCROW_SO`, else `solana/target/deploy/bido_escrow.so`, and is skipped
when neither exists. The world is the escrow harness's: public test keys, a funded
relayer, an SAS credential whose only signer is the gate attester (written directly:
`create_po` reads it, nothing here calls the SAS program), and a `Config` that allows
the mint created here with the ops script's instructions.
"""

import base64
import hashlib
import json
import os
import struct
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final, cast

import httpx
import pytest
from solders.account import Account
from solders.keypair import Keypair
from solders.litesvm import LiteSVM
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.token.state import TokenAccount
from solders.transaction import VersionedTransaction
from solders.transaction_metadata import FailedTransactionMetadata, TransactionMetadata
from solders.transaction_status import (
    InstructionErrorCustom,
    InstructionErrorFieldless,
    TransactionErrorFieldless,
    TransactionErrorInstructionError,
    TransactionErrorType,
)

from bido_back.payments.chain.borsh import BorshCoder
from bido_back.payments.chain.escrow import decode_po_escrow
from bido_back.payments.chain.hashes import bank_fingerprint, new_chain_po_id, payout_ref
from bido_back.payments.chain.idl import load_idl
from bido_back.payments.chain.keys import HotKey
from bido_back.payments.chain.pdas import token_account
from bido_back.payments.chain.solana import (
    CREATE_ESCROW_COMPUTE_UNITS,
    FUND_COMPUTE_UNITS,
    MINT_COMPUTE_UNITS,
    SolanaPaymentChain,
)
from bido_back.payments.chain.spl import (
    MINT_ACCOUNT_LEN,
    create_mint_account_ix,
    initialize_mint2_ix,
)
from bido_back.payments.chain.transactions import compile_message, sign_transaction
from bido_back.payments.errors import ProviderError
from bido_back.payments.ports import EscrowMilestone, EscrowSpec, EscrowState, TransactionStatus
from tests.payments.chain.support import (
    ATTESTER,
    BUYER,
    DEVNET_GENESIS,
    MINT_AUTHORITY,
    OPERATOR,
    PAYOUT,
    PROGRAM,
    RELAYER,
    settings,
    wallet_sign,
)
from tests.payments.chain.vectors import REPO_ROOT, harness_keypair

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]


def program_binary() -> Path | None:
    candidates = [REPO_ROOT / "solana" / "target" / "deploy" / "bido_escrow.so"]
    configured = os.environ.get("BIDO_ESCROW_SO")
    if configured:
        candidates.insert(0, Path(configured))
    return next((path for path in candidates if path.is_file()), None)


PROGRAM_SO: Final = program_binary()
pytestmark = pytest.mark.skipif(
    PROGRAM_SO is None, reason="bido_escrow.so not built (set BIDO_ESCROW_SO or anchor build)"
)

SAS_PROGRAM: Final = Pubkey.from_string("22zoJMtdu4tQc2PzL74ZUT7FrwgB1Udec8DdW4yw4BdG")
SOL: Final = 1_000_000_000
TOTAL: Final = 1_200_000_000  # 1,200.000000 of the stablecoin
DAY: Final = 86_400


def rpc_error_json(error: TransactionErrorType) -> Json:
    """A runtime error as the RPC writes it (`{"InstructionError": [2, {"Custom": 6019}]}`)."""
    if isinstance(error, TransactionErrorInstructionError):
        inner = error.err
        detail: Json
        if isinstance(inner, InstructionErrorCustom):
            detail = {"Custom": inner.code}
        elif isinstance(inner, InstructionErrorFieldless):
            detail = str(inner).rsplit(".", 1)[-1]
        else:
            detail = type(inner).__name__
        return {"InstructionError": [error.index, detail]}
    if isinstance(error, TransactionErrorFieldless):
        return str(error).rsplit(".", 1)[-1]
    return type(error).__name__


class LiteSvmRpc:
    """The JSON-RPC methods `SolanaPaymentChain` calls, answered by LiteSVM.

    A transaction LiteSVM refuses comes back as a preflight failure, as from a node.
    Every executed transaction is final at once.
    """

    def __init__(self, svm: LiteSVM) -> None:
        self.svm = svm
        self.compute_units: dict[str, int] = {}

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = cast(dict[str, Json], json.loads(request.content))
        method, params = cast(str, body["method"]), cast(list[Json], body["params"])
        handlers: dict[str, Callable[[list[Json]], Json]] = {
            "getGenesisHash": lambda _: DEVNET_GENESIS,
            "getLatestBlockhash": self._latest_blockhash,
            "getAccountInfo": self._account,
            "getSignatureStatuses": self._statuses,
            "getEpochInfo": lambda _: {"absoluteSlot": self._slot(), "blockHeight": self._slot()},
        }
        if method == "sendTransaction":
            error = self._send(cast(str, params[0]))
            if error is not None:
                return httpx.Response(
                    200, json={"jsonrpc": "2.0", "id": body["id"], "error": error}
                )
            tx = VersionedTransaction.from_bytes(base64.b64decode(cast(str, params[0])))
            result: Json = str(tx.signatures[0])
        else:
            result = handlers[method](params)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    def _slot(self) -> int:
        return self.svm.get_clock().slot

    def _latest_blockhash(self, _: list[Json]) -> Json:
        return {
            "context": {"slot": self._slot()},
            "value": {
                "blockhash": str(self.svm.latest_blockhash()),
                "lastValidBlockHeight": self._slot() + 150,
            },
        }

    def _account(self, params: list[Json]) -> Json:
        account = self.svm.get_account(Pubkey.from_string(cast(str, params[0])))
        value: Json = None
        if account is not None:
            value = {
                "data": [base64.b64encode(bytes(account.data)).decode(), "base64"],
                "executable": account.executable,
                "lamports": account.lamports,
                "owner": str(account.owner),
                "rentEpoch": 0,
                "space": len(account.data),
            }
        return {"context": {"slot": self._slot()}, "value": value}

    def _statuses(self, params: list[Json]) -> Json:
        values: list[Json] = []
        for signature in cast(list[str], params[0]):
            found = self.svm.get_transaction(Signature.from_string(signature))
            if found is None:
                values.append(None)
                continue
            err = (
                rpc_error_json(found.err())
                if isinstance(found, FailedTransactionMetadata)
                else None
            )
            values.append(
                {
                    "slot": self._slot(),
                    "confirmations": None,
                    "err": err,
                    "confirmationStatus": "finalized",
                }
            )
        return {"context": {"slot": self._slot()}, "value": values}

    def _send(self, encoded: str) -> Json:
        tx = VersionedTransaction.from_bytes(base64.b64decode(encoded))
        outcome = self.svm.send_transaction(tx)
        if isinstance(outcome, FailedTransactionMetadata):
            return {
                "code": -32002,
                "message": "Transaction simulation failed",
                "data": {"err": rpc_error_json(outcome.err()), "logs": []},
            }
        assert isinstance(outcome, TransactionMetadata)
        self.compute_units[str(tx.signatures[0])] = outcome.compute_units_consumed()
        return None


class World:
    """The escrow program with its `Config`, an SAS credential and our mint."""

    def __init__(self, program_so: Path) -> None:
        self.svm = LiteSVM()
        self.svm.add_program_from_file(PROGRAM, program_so)
        self.svm.airdrop(RELAYER.pubkey(), 10 * SOL)
        self.credential = harness_keypair("sas-credential").pubkey()
        self._set(self.credential, self._credential_data(), SAS_PROGRAM)
        self.mint = self._create_mint()
        self._write_config()

    def _set(self, address: Pubkey, data: bytes, owner: Pubkey) -> None:
        lamports = self.svm.minimum_balance_for_rent_exemption(len(data))
        self.svm.set_account(address, Account(lamports, data, owner))

    @staticmethod
    def _credential_data() -> bytes:
        """SAS `Credential`: `[0] ‖ authority ‖ u32 len ‖ name ‖ u32 count ‖ signers`."""
        name = b"bido-approvals"
        authority = harness_keypair("sas-issuer").pubkey()
        return b"".join(
            [b"\x00", bytes(authority), struct.pack("<I", len(name)), name]
            + [struct.pack("<I", 1), bytes(ATTESTER)]
        )

    def _create_mint(self) -> Pubkey:
        """The ops script's `create-mint` instructions, run on the real token program."""
        new_mint = HotKey(Keypair.from_seed(hashlib.sha256(b"bido-litesvm:mint").digest()), "mint")
        relayer = HotKey(RELAYER, "relayer")
        compiled = compile_message(
            payer=RELAYER.pubkey(),
            instructions=[
                create_mint_account_ix(
                    payer=RELAYER.pubkey(),
                    mint=new_mint.pubkey(),
                    lamports=self.svm.minimum_balance_for_rent_exemption(MINT_ACCOUNT_LEN),
                ),
                initialize_mint2_ix(
                    mint=new_mint.pubkey(),
                    decimals=6,
                    mint_authority=MINT_AUTHORITY.pubkey(),
                    freeze_authority=None,
                ),
            ],
            signers={RELAYER.pubkey(), new_mint.pubkey()},
            blockhash=self.svm.latest_blockhash(),
            last_valid_block_height=0,
            compute_unit_limit=200_000,
            compute_unit_price=1_000,
        )
        signed = sign_transaction(compiled, (relayer, new_mint))
        outcome = self.svm.send_transaction(VersionedTransaction.from_bytes(signed.wire))
        assert isinstance(outcome, TransactionMetadata), outcome
        return new_mint.pubkey()

    def _write_config(self) -> None:
        address, bump = Pubkey.find_program_address([b"config"], PROGRAM)
        data = BorshCoder(load_idl()).encode_account(
            "Config",
            {
                "admin": harness_keypair("admin").pubkey(),
                "operator": OPERATOR.pubkey(),
                "allowed_mints": [self.mint, Pubkey.default()],
                "sas_credential": self.credential,
                "sas_schema": harness_keypair("sas-schema").pubkey(),
                "bump": bump,
            },
        )
        self._set(address, data, PROGRAM)

    def token_balance(self, address: Pubkey) -> int:
        account = self.svm.get_account(address)
        return 0 if account is None else TokenAccount.from_bytes(bytes(account.data)).amount

    def lamports(self, address: Pubkey) -> int:
        account = self.svm.get_account(address)
        return 0 if account is None else account.lamports


async def test_a_payment_mints_creates_and_funds_its_escrow_on_the_program() -> None:
    assert PROGRAM_SO is not None
    world = World(PROGRAM_SO)
    rpc = LiteSvmRpc(world.svm)
    chain = SolanaPaymentChain.from_settings(
        settings(solana_stablecoin_mint=str(world.mint)), http=rpc.http()
    )
    buyer = str(BUYER.pubkey())
    buyer_token = token_account(BUYER.pubkey(), world.mint)

    # 1. Mint the charged amount into the company's wallet.
    minted = await chain.build_mint(owner=buyer, amount=TOTAL, memo="bido:payment:p-1")
    await chain.send(minted)
    assert await chain.status(minted.signature) == TransactionStatus("confirmed")
    assert world.token_balance(buyer_token) == TOTAL

    # 2. Create the PO's escrow: operator signs, relayer pays, payout reference memo.
    chain_po_id = new_chain_po_id("PO-12")
    fingerprint = bank_fingerprint("CN", "102100099996", "6222020200000000042")
    terms = EscrowSpec(
        chain_po_id=chain_po_id,
        buyer=buyer,
        po_hash=hashlib.sha256(b"PO-12 document").digest(),
        cad_hash=bytes(32),
        total=TOTAL,
        milestones=(EscrowMilestone("Tooling", 3_000), EscrowMilestone("Delivery", 7_000)),
        refund_after=int(time.time()) + 90 * DAY,
        payout_ref=payout_ref("payout-account-1", fingerprint, chain_po_id),
    )
    created = await chain.build_create_escrow(terms)
    await chain.send(created)
    assert await chain.status(created.signature) == TransactionStatus("confirmed")
    escrow = chain.escrow_address(buyer=buyer, chain_po_id=chain_po_id)
    vault = chain.vault_address(escrow=escrow)
    expected = EscrowState(escrow, "created", chain_po_id, buyer, str(world.mint), TOTAL, vault)
    assert await chain.escrow(escrow) == expected
    account = world.svm.get_account(Pubkey.from_string(escrow))
    assert account is not None
    on_chain = decode_po_escrow(bytes(account.data))
    assert [gate.attester for gate in on_chain.gates] == [ATTESTER, ATTESTER]
    assert [(m.name, m.bps, m.gate, m.amount) for m in on_chain.milestones] == [
        ("Tooling", 3_000, 0, 360_000_000),
        ("Delivery", 7_000, 1, 840_000_000),
    ]
    assert (on_chain.payout, on_chain.po_hash, on_chain.refund_after) == (
        PAYOUT,
        terms.po_hash,
        terms.refund_after,
    )

    # 3. Fund it: the company's wallet signs remotely, the relayer co-signs and pays.
    unsigned = await chain.build_fund(buyer=buyer, escrow=escrow)
    funded = chain.complete_fund(
        unsigned, buyer=buyer, signed_by_buyer=wallet_sign(unsigned.wire, BUYER)
    )
    await chain.send(funded)
    assert await chain.status(funded.signature) == TransactionStatus("confirmed")
    assert await chain.escrow(escrow) == EscrowState(
        escrow, "funded", chain_po_id, buyer, str(world.mint), TOTAL, vault
    )
    assert world.token_balance(Pubkey.from_string(vault)) == TOTAL
    assert world.token_balance(buyer_token) == 0
    assert world.lamports(BUYER.pubkey()) == 0  # the wallet never pays a fee

    # Each step fits its compute budget.
    assert rpc.compute_units[minted.signature] < MINT_COMPUTE_UNITS
    assert rpc.compute_units[created.signature] < CREATE_ESCROW_COMPUTE_UNITS
    assert rpc.compute_units[funded.signature] < FUND_COMPUTE_UNITS

    # Sending the same bytes again is not a second fund.
    await chain.send(funded)
    assert world.token_balance(Pubkey.from_string(vault)) == TOTAL

    # A new fund of the same escrow is refused by the program, by name.
    world.svm.expire_blockhash()
    again = await chain.build_fund(buyer=buyer, escrow=escrow)
    again_signed = chain.complete_fund(
        again, buyer=buyer, signed_by_buyer=wallet_sign(again.wire, BUYER)
    )
    with pytest.raises(ProviderError) as refused:
        await chain.send(again_signed)
    assert (refused.value.code, refused.value.retryable) == ("AlreadyFunded", False)

    # The escrow id is spent: a second `create_po` fails in the System program.
    with pytest.raises(ProviderError) as duplicate:
        await chain.send(await chain.build_create_escrow(terms))
    assert duplicate.value.code == "InstructionError(2, Custom(0))"
