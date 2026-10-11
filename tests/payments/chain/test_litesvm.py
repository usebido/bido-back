"""Mint, `create_po`, `fund` and each milestone's release through `SolanaPaymentChain`,
executed by the real programs.

LiteSVM (`solders.litesvm`) runs `bido_escrow.so` and the Solana Attestation Service
(`solana/tests/fixtures/sas.so`, dumped from devnet) in process. A JSON-RPC bridge over
`httpx.MockTransport` answers the adapter's requests from it: every transaction the
adapter builds, signs and sends is executed by the real runtime, SPL programs, SAS and
escrow program, and every status and account it reads back is theirs. Nothing reaches
a network.

The program binary is built separately (`anchor build` in `solana/`): the test reads
it from `BIDO_ESCROW_SO`, else `solana/target/deploy/bido_escrow.so`, and is skipped
when neither exists. The world is the escrow harness's: public test keys, a funded
relayer, the SAS credential `bido-approvals` (its only signer the gate attester) and
the schema `bido-gate-approval` v1, both created through SAS as the devnet setup does,
and a `Config` that allows the mint created here with the ops script's instructions.
"""

import base64
import dataclasses
import hashlib
import json
import os
import struct
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final, cast

import httpx
import pytest
from solders.account import Account
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.litesvm import LiteSVM
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import ID as SYSTEM_PROGRAM_ID
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
from bido_back.payments.chain.escrow import PoEscrowAccount, decode_po_escrow, release_ix
from bido_back.payments.chain.hashes import (
    bank_fingerprint,
    compute_evidence_hash,
    new_chain_po_id,
    payout_ref,
)
from bido_back.payments.chain.idl import load_idl
from bido_back.payments.chain.keys import HotKey
from bido_back.payments.chain.pdas import (
    SAS_PROGRAM_ID,
    attestation_pda,
    gate_nonce,
    token_account,
)
from bido_back.payments.chain.sas import create_attestation_ix, encode_gate_approval_data
from bido_back.payments.chain.solana import (
    CREATE_ESCROW_COMPUTE_UNITS,
    FUND_COMPUTE_UNITS,
    MINT_COMPUTE_UNITS,
    RELEASE_ATTEST_COMPUTE_UNITS,
    RELEASE_COMPUTE_UNITS,
    SolanaPaymentChain,
)
from bido_back.payments.chain.spl import (
    MINT_ACCOUNT_LEN,
    create_mint_account_ix,
    initialize_mint2_ix,
)
from bido_back.payments.chain.transactions import (
    MAX_TRANSACTION_BYTES,
    compile_message,
    sign_transaction,
)
from bido_back.payments.errors import PaymentRefused, PaymentsNotConfigured, ProviderError
from bido_back.payments.ports import (
    EscrowMilestone,
    EscrowMilestoneState,
    EscrowSpec,
    EscrowState,
    GateAttestation,
    ReleaseSpec,
    SignedTransaction,
    TransactionStatus,
)
from tests.payments.chain.support import (
    ATTESTER,
    BUYER,
    DEVNET_GENESIS,
    GATE_ATTESTER,
    MINT_AUTHORITY,
    OPERATOR,
    PAYOUT,
    PROGRAM,
    RELAYER,
    SAS_CREDENTIAL_NAME,
    settings,
    wallet_sign,
)
from tests.payments.chain.vectors import FIXTURES_DIR, REPO_ROOT, harness_keypair

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

SAS_SO: Final = FIXTURES_DIR / "sas.so"
# The SAS credential's authority (the offline issuer on devnet).
ISSUER: Final = harness_keypair("sas-issuer")
# An authorized signer of nothing Bido pins.
SOMEONE: Final = harness_keypair("someone")
SAS_SCHEMA_NAME: Final = b"bido-gate-approval"
SAS_SCHEMA_DESCRIPTION: Final = (
    b"Approval of a PO gate; releases escrowed funds to the fixed payout"
)
# `[VecU8 po_hash, U8 gate, VecU8 evidence_hash]` (`constants.rs` `GATE_SCHEMA_LAYOUT`).
SAS_SCHEMA_LAYOUT: Final = bytes([13, 0, 13])
SAS_SCHEMA_FIELDS: Final = (b"po_hash", b"gate", b"evidence_hash")
SOL: Final = 1_000_000_000
TOTAL: Final = 1_200_000_000  # 1,200.000000 of the stablecoin
DAY: Final = 86_400
# The tooling plan: milestone i is released through gate i.
TOOLING_PLAN: Final = (
    EscrowMilestone("Tool start", 3_000),
    EscrowMilestone("T1", 3_000),
    EscrowMilestone("PPAP", 4_000),
)
TOOLING_AMOUNTS: Final = (360_000_000, 360_000_000, 480_000_000)


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


# ---- SAS instructions the devnet setup sends (the Rust harness's encoders) ----


def _sized(data: bytes) -> bytes:
    return struct.pack("<I", len(data)) + data


def _keys(keys: Sequence[Pubkey]) -> bytes:
    return struct.pack("<I", len(keys)) + b"".join(bytes(key) for key in keys)


def sas_credential_address(issuer: Pubkey) -> Pubkey:
    seeds = [b"credential", bytes(issuer), SAS_CREDENTIAL_NAME]
    return Pubkey.find_program_address(seeds, SAS_PROGRAM_ID)[0]


def sas_create_credential_ix(signers: Sequence[Pubkey]) -> Instruction:
    """`CreateCredential` (tag 0): `[payer (w, s), credential (w), authority (s), system]`."""
    return Instruction(
        SAS_PROGRAM_ID,
        b"\x00" + _sized(SAS_CREDENTIAL_NAME) + _keys(signers),
        [
            AccountMeta(RELAYER.pubkey(), is_signer=True, is_writable=True),
            AccountMeta(sas_credential_address(ISSUER.pubkey()), is_signer=False, is_writable=True),
            AccountMeta(ISSUER.pubkey(), is_signer=True, is_writable=False),
            AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ],
    )


def sas_create_schema_ix(credential: Pubkey) -> tuple[Instruction, Pubkey]:
    """`CreateSchema` (tag 1), version 1: `[payer (w, s), authority (s), credential, schema
    (w), system]`; the field names are a u32 count, then each length-prefixed."""
    seeds = [b"schema", bytes(credential), SAS_SCHEMA_NAME, bytes([1])]
    schema = Pubkey.find_program_address(seeds, SAS_PROGRAM_ID)[0]
    data = b"".join(
        [b"\x01", _sized(SAS_SCHEMA_NAME), _sized(SAS_SCHEMA_DESCRIPTION)]
        + [_sized(SAS_SCHEMA_LAYOUT), struct.pack("<I", len(SAS_SCHEMA_FIELDS))]
        + [_sized(field) for field in SAS_SCHEMA_FIELDS]
    )
    accounts = [
        AccountMeta(RELAYER.pubkey(), is_signer=True, is_writable=True),
        AccountMeta(ISSUER.pubkey(), is_signer=True, is_writable=False),
        AccountMeta(credential, is_signer=False, is_writable=False),
        AccountMeta(schema, is_signer=False, is_writable=True),
        AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
    ]
    return Instruction(SAS_PROGRAM_ID, data, accounts), schema


def sas_change_authorized_signers_ix(credential: Pubkey, signers: Sequence[Pubkey]) -> Instruction:
    """`ChangeAuthorizedSigners` (tag 3), the whole list: `[payer (w, s), authority (s),
    credential (w), system]`."""
    return Instruction(
        SAS_PROGRAM_ID,
        b"\x03" + _keys(signers),
        [
            AccountMeta(RELAYER.pubkey(), is_signer=True, is_writable=True),
            AccountMeta(ISSUER.pubkey(), is_signer=True, is_writable=False),
            AccountMeta(credential, is_signer=False, is_writable=True),
            AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        ],
    )


class World:
    """The escrow and SAS programs, a credential and schema made by SAS, and our mint."""

    def __init__(self, program_so: Path) -> None:
        self.svm = LiteSVM()
        self.svm.add_program_from_file(PROGRAM, program_so)
        self.svm.add_program_from_file(SAS_PROGRAM_ID, SAS_SO)
        self.svm.airdrop(RELAYER.pubkey(), 10 * SOL)
        self.credential = sas_credential_address(ISSUER.pubkey())
        self.run([sas_create_credential_ix([ATTESTER])], ISSUER)
        create_schema, self.schema = sas_create_schema_ix(self.credential)
        self.run([create_schema], ISSUER)
        self.mint = self._create_mint()
        self._write_config()

    def signed(self, instructions: Sequence[Instruction], *signers: Keypair) -> SignedTransaction:
        """`[CU, CU, *instructions]` paid by the relayer, on a fresh blockhash."""
        self.svm.expire_blockhash()
        keys = (HotKey(RELAYER, "relayer"), *(HotKey(signer, "signer") for signer in signers))
        compiled = compile_message(
            payer=RELAYER.pubkey(),
            instructions=instructions,
            signers={key.pubkey() for key in keys},
            blockhash=self.svm.latest_blockhash(),
            last_valid_block_height=self.svm.get_clock().slot + 150,
            compute_unit_limit=200_000,
            compute_unit_price=1_000,
        )
        return sign_transaction(compiled, keys)

    def run(self, instructions: Sequence[Instruction], *signers: Keypair) -> None:
        """Executes `instructions` (see `signed`); they must succeed."""
        signed = self.signed(instructions, *signers)
        outcome = self.svm.send_transaction(VersionedTransaction.from_bytes(signed.wire))
        assert isinstance(outcome, TransactionMetadata), outcome

    def _create_mint(self) -> Pubkey:
        """The ops script's `create-mint` instructions, run on the real token program."""
        new_mint = Keypair.from_seed(hashlib.sha256(b"bido-litesvm:mint").digest())
        self.run(
            [
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
            new_mint,
        )
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
                "sas_schema": self.schema,
                "bump": bump,
            },
        )
        lamports = self.svm.minimum_balance_for_rent_exemption(len(data))
        self.svm.set_account(address, Account(lamports, data, PROGRAM))

    def escrow(self, address: str) -> PoEscrowAccount:
        account = self.svm.get_account(Pubkey.from_string(address))
        assert account is not None
        return decode_po_escrow(bytes(account.data))

    def token_balance(self, address: Pubkey) -> int:
        account = self.svm.get_account(address)
        return 0 if account is None else TokenAccount.from_bytes(bytes(account.data)).amount

    def lamports(self, address: Pubkey) -> int:
        account = self.svm.get_account(address)
        return 0 if account is None else account.lamports


def chain_of(world: World) -> tuple[SolanaPaymentChain, LiteSvmRpc]:
    rpc = LiteSvmRpc(world.svm)
    chain = SolanaPaymentChain.from_settings(
        settings(solana_stablecoin_mint=str(world.mint)), http=rpc.http()
    )
    return chain, rpc


def escrow_terms(milestones: tuple[EscrowMilestone, ...]) -> EscrowSpec:
    chain_po_id = new_chain_po_id("PO-12")
    fingerprint = bank_fingerprint("CN", "102100099996", "6222020200000000042")
    return EscrowSpec(
        chain_po_id=chain_po_id,
        buyer=str(BUYER.pubkey()),
        po_hash=hashlib.sha256(b"PO-12 document").digest(),
        cad_hash=bytes(32),
        total=TOTAL,
        milestones=milestones,
        refund_after=int(time.time()) + 90 * DAY,
        payout_ref=payout_ref("payout-account-1", fingerprint, chain_po_id),
    )


async def created_escrow(chain: SolanaPaymentChain, terms: EscrowSpec) -> str:
    created = await chain.build_create_escrow(terms)
    await chain.send(created)
    assert await chain.status(created.signature) == TransactionStatus("confirmed")
    return chain.escrow_address(buyer=terms.buyer, chain_po_id=terms.chain_po_id)


async def funded_escrow(chain: SolanaPaymentChain, terms: EscrowSpec) -> str:
    """Mints the total into the company's wallet, creates the escrow and funds it."""
    minted = await chain.build_mint(owner=terms.buyer, amount=terms.total, memo="bido:payment:p")
    await chain.send(minted)
    escrow = await created_escrow(chain, terms)
    unsigned = await chain.build_fund(buyer=terms.buyer, escrow=escrow)
    funded = chain.complete_fund(
        unsigned, buyer=terms.buyer, signed_by_buyer=wallet_sign(unsigned.wire, BUYER)
    )
    await chain.send(funded)
    assert await chain.status(funded.signature) == TransactionStatus("confirmed")
    return escrow


def evidence_for(chain_po_id: str, gate: int) -> str:
    """What a person approves for a milestone: its documents' hashes, bound to the gate."""
    files = [hashlib.sha256(f"gate {gate} document {n}".encode()).hexdigest() for n in range(2)]
    return compute_evidence_hash(chain_po_id, gate, files)


async def refused_code(chain: SolanaPaymentChain, transaction: SignedTransaction) -> str:
    """The code `send` names a preflight refusal by (never retryable here)."""
    with pytest.raises(ProviderError) as caught:
        await chain.send(transaction)
    assert (caught.value.operation, caught.value.retryable) == ("sendTransaction", False)
    return caught.value.code


# ---- a payment ----


async def test_a_payment_mints_creates_and_funds_its_escrow_on_the_program() -> None:
    assert PROGRAM_SO is not None
    world = World(PROGRAM_SO)
    chain, rpc = chain_of(world)
    buyer = str(BUYER.pubkey())
    buyer_token = token_account(BUYER.pubkey(), world.mint)

    # 1. Mint the charged amount into the company's wallet.
    minted = await chain.build_mint(owner=buyer, amount=TOTAL, memo="bido:payment:p-1")
    await chain.send(minted)
    assert await chain.status(minted.signature) == TransactionStatus("confirmed")
    assert world.token_balance(buyer_token) == TOTAL

    # 2. Create the PO's escrow: operator signs, relayer pays, payout reference memo.
    terms = escrow_terms((EscrowMilestone("Tooling", 3_000), EscrowMilestone("Delivery", 7_000)))
    created = await chain.build_create_escrow(terms)
    await chain.send(created)
    assert await chain.status(created.signature) == TransactionStatus("confirmed")
    escrow = chain.escrow_address(buyer=buyer, chain_po_id=terms.chain_po_id)
    vault = chain.vault_address(escrow=escrow)
    expected = EscrowState(
        address=escrow,
        status="created",
        chain_po_id=terms.chain_po_id,
        buyer=buyer,
        mint=str(world.mint),
        total=TOTAL,
        vault=vault,
        released=0,
        po_hash=terms.po_hash.hex(),
        milestones=(
            EscrowMilestoneState(0, "Tooling", 3_000, 360_000_000, 0, "pending", "00" * 32, None),
            EscrowMilestoneState(1, "Delivery", 7_000, 840_000_000, 1, "pending", "00" * 32, None),
        ),
    )
    assert await chain.escrow(escrow) == expected
    on_chain = world.escrow(escrow)
    assert [gate.attester for gate in on_chain.gates] == [ATTESTER, ATTESTER]
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
    assert await chain.escrow(escrow) == dataclasses.replace(expected, status="funded")
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


async def test_an_escrow_is_never_pinned_to_an_attester_sas_refuses() -> None:
    """The devnet build of `create_po` does not check it: such an escrow could be funded,
    then never released."""
    assert PROGRAM_SO is not None
    world = World(PROGRAM_SO)
    world.run([sas_change_authorized_signers_ix(world.credential, [SOMEONE.pubkey()])], ISSUER)
    chain, _ = chain_of(world)

    with pytest.raises(PaymentsNotConfigured, match="not an authorized signer"):
        await chain.build_create_escrow(escrow_terms(TOOLING_PLAN))


# ---- releases ----


async def test_each_milestone_is_released_by_its_own_approval() -> None:
    assert PROGRAM_SO is not None
    world = World(PROGRAM_SO)
    chain, rpc = chain_of(world)
    terms = escrow_terms(TOOLING_PLAN)
    escrow = await funded_escrow(chain, terms)
    escrow_key = Pubkey.from_string(escrow)
    payout_token = token_account(PAYOUT, world.mint)
    # `create_po` stored gate i's attestation where this package derives it.
    on_chain = world.escrow(escrow)
    assert [
        (m.gate, gate.attester) for m, gate in zip(on_chain.milestones, on_chain.gates, strict=True)
    ] == [
        (0, ATTESTER),
        (1, ATTESTER),
        (2, ATTESTER),
    ]
    assert [gate.attestation for gate in on_chain.gates] == [
        attestation_pda(world.credential, world.schema, gate_nonce(escrow_key, gate))
        for gate in range(3)
    ]
    # Lamports someone sent to gate 0's attestation address do not block its approval.
    world.svm.airdrop(on_chain.gates[0].attestation, 1_000_000)

    paid = 0
    for index, milestone in enumerate(TOOLING_PLAN):
        evidence = evidence_for(terms.chain_po_id, index)
        assert await chain.gate_attestation(escrow=escrow, gate=index) is None

        signed = await chain.build_release(ReleaseSpec(escrow, index, evidence, attest=True))
        await chain.send(signed)

        assert await chain.status(signed.signature) == TransactionStatus("confirmed")
        assert rpc.compute_units[signed.signature] < RELEASE_ATTEST_COMPUTE_UNITS
        assert len(signed.wire) <= MAX_TRANSACTION_BYTES
        paid += TOOLING_AMOUNTS[index]
        state = await chain.escrow(escrow)
        assert state is not None
        assert state.milestones[index] == EscrowMilestoneState(
            index=index,
            name=milestone.name,
            bps=milestone.bps,
            amount=TOOLING_AMOUNTS[index],
            gate=index,
            status="released",
            evidence_hash=evidence,
            approved_by=str(ATTESTER),
        )
        assert [m.status for m in state.milestones[index + 1 :]] == ["pending"] * (2 - index)
        assert (state.released, world.token_balance(payout_token)) == (paid, paid)
        assert await chain.gate_attestation(escrow=escrow, gate=index) == GateAttestation(
            address=str(on_chain.gates[index].attestation),
            signer=str(ATTESTER),
            po_hash=terms.po_hash.hex(),
            gate=index,
            evidence_hash=evidence,
        )

    final = await chain.escrow(escrow)
    assert final is not None
    assert (final.status, final.released) == ("completed", TOTAL)
    assert world.token_balance(token_account(escrow_key, world.mint)) == 0
    with pytest.raises(PaymentRefused, match="the escrow is completed, not funded"):
        await chain.build_release(
            ReleaseSpec(escrow, 2, evidence_for(terms.chain_po_id, 2), attest=False)
        )


async def test_the_chain_refuses_a_second_attestation_or_release_by_name() -> None:
    """What `send` calls each refusal of a release (the worker decides from the chain)."""
    assert PROGRAM_SO is not None
    world = World(PROGRAM_SO)
    chain, rpc = chain_of(world)
    terms = escrow_terms(TOOLING_PLAN)
    escrow = await funded_escrow(chain, terms)
    state = world.escrow(escrow)
    evidence = [evidence_for(terms.chain_po_id, gate) for gate in range(3)]
    payout_token = token_account(PAYOUT, world.mint)

    def attest_ix(gate: int, signer: Pubkey = ATTESTER) -> Instruction:
        return create_attestation_ix(
            payer=RELAYER.pubkey(),
            signer=signer,
            credential=state.credential,
            schema=state.schema,
            nonce=gate_nonce(Pubkey.from_string(escrow), gate),
            data=encode_gate_approval_data(state.po_hash, gate, bytes.fromhex(evidence[gate])),
        )

    def release(milestone: int) -> Instruction:
        return release_ix(
            program_id=PROGRAM,
            escrow=Pubkey.from_string(escrow),
            vault=state.vault,
            payout_token=state.payout_token,
            mint=state.mint,
            attestation=state.gates[milestone].attestation,
            milestone=milestone,
        )

    # Gate 1's attestation landed on its own (its release did not).
    world.run([attest_ix(1)], GATE_ATTESTER)
    written = await chain.gate_attestation(escrow=escrow, gate=1)
    assert written is not None
    assert (written.signer, written.evidence_hash) == (str(ATTESTER), evidence[1])

    # Attesting it again fails inside SAS, at the attestation's index, with a bare code
    # (the System program's `AccountAlreadyInUse`): nothing is paid.
    again = await chain.build_release(ReleaseSpec(escrow, 1, evidence[1], attest=True))
    assert await refused_code(chain, again) == "InstructionError(2, Custom(0))"
    assert world.token_balance(payout_token) == 0

    # The crank releases milestone 1 against it.
    crank = await chain.build_release(ReleaseSpec(escrow, 1, evidence[1], attest=False))
    await chain.send(crank)
    assert await chain.status(crank.signature) == TransactionStatus("confirmed")
    assert rpc.compute_units[crank.signature] < RELEASE_COMPUTE_UNITS
    assert world.token_balance(payout_token) == TOOLING_AMOUNTS[1]

    # A second release of it is refused by name; the adapter does not even build one.
    assert await refused_code(chain, world.signed([release(1)])) == "MilestoneAlreadyReleased"
    with pytest.raises(PaymentRefused, match="milestone 1 is released"):
        await chain.build_release(ReleaseSpec(escrow, 1, evidence[1], attest=False))

    # A release without its gate's attestation is refused by name.
    assert await refused_code(chain, world.signed([release(2)])) == "AttestationMissing"

    # An attestation by an authorized signer the gate does not pin: the release refuses
    # it, and the whole transaction reverts (no attestation is left).
    world.run(
        [sas_change_authorized_signers_ix(world.credential, [ATTESTER, SOMEONE.pubkey()])],
        ISSUER,
    )
    squat = world.signed([attest_ix(2, SOMEONE.pubkey()), release(2)], SOMEONE)
    assert await refused_code(chain, squat) == "AttesterMismatch"
    assert await chain.gate_attestation(escrow=escrow, gate=2) is None

    # Our attester removed from the credential: SAS refuses it with a bare code; the
    # adapter refuses to build it first (a configuration problem, not a release failure).
    world.run([sas_change_authorized_signers_ix(world.credential, [SOMEONE.pubkey()])], ISSUER)
    with pytest.raises(PaymentsNotConfigured, match="not an authorized signer"):
        await chain.build_release(ReleaseSpec(escrow, 0, evidence[0], attest=True))
    refused = world.signed([attest_ix(0), release(0)], GATE_ATTESTER)
    assert await refused_code(chain, refused) == "InstructionError(2, Custom(5))"

    assert world.token_balance(payout_token) == TOOLING_AMOUNTS[1]


async def test_an_approval_of_an_unfunded_escrow_leaves_no_attestation() -> None:
    assert PROGRAM_SO is not None
    world = World(PROGRAM_SO)
    chain, _ = chain_of(world)
    terms = escrow_terms(TOOLING_PLAN)
    escrow = await created_escrow(chain, terms)
    state = world.escrow(escrow)
    evidence = evidence_for(terms.chain_po_id, 0)

    with pytest.raises(PaymentRefused, match="the escrow is created, not funded"):
        await chain.build_release(ReleaseSpec(escrow, 0, evidence, attest=True))

    attest = create_attestation_ix(
        payer=RELAYER.pubkey(),
        signer=ATTESTER,
        credential=state.credential,
        schema=state.schema,
        nonce=gate_nonce(Pubkey.from_string(escrow), 0),
        data=encode_gate_approval_data(state.po_hash, 0, bytes.fromhex(evidence)),
    )
    release = release_ix(
        program_id=PROGRAM,
        escrow=Pubkey.from_string(escrow),
        vault=state.vault,
        payout_token=state.payout_token,
        mint=state.mint,
        attestation=state.gates[0].attestation,
        milestone=0,
    )
    refused = world.signed([attest, release], GATE_ATTESTER)
    assert await refused_code(chain, refused) == "NotFunded"
    assert await chain.gate_attestation(escrow=escrow, gate=0) is None
