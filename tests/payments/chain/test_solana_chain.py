"""`SolanaPaymentChain` against a scripted JSON-RPC node (`httpx.MockTransport`).

Keys are the escrow harness's public test seeds, so the `fund` built here is the very
instruction the program accepted in its own tests (`vectors.json`). Nothing reaches a
network.
"""

import base64
import dataclasses
import json
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final, cast

import httpx
import pytest
from solders.compute_budget import ID as COMPUTE_BUDGET_ID
from solders.hash import Hash
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.token import ID as TOKEN_PROGRAM_ID
from solders.transaction import VersionedTransaction

from bido_back.payments.chain import build_payment_chain
from bido_back.payments.chain.borsh import BorshCoder
from bido_back.payments.chain.escrow import (
    CreatePoArgs,
    MilestoneInput,
    create_po_ix,
    decode_config,
)
from bido_back.payments.chain.idl import load_idl
from bido_back.payments.chain.pdas import config_pda, token_account
from bido_back.payments.chain.solana import MAINNET_GENESIS_HASH, SolanaPaymentChain
from bido_back.payments.chain.spl import ASSOCIATED_TOKEN_PROGRAM_ID, MEMO_PROGRAM_ID
from bido_back.payments.errors import PaymentRefused, PaymentsNotConfigured, ProviderError
from bido_back.payments.ports import (
    EscrowMilestone,
    EscrowSpec,
    EscrowState,
    PaymentChain,
    SignedTransaction,
    TransactionStatus,
    UnsignedTransaction,
)
from tests.payments.chain.support import (
    ATTESTER,
    BUYER,
    DEVNET_GENESIS,
    MINT,
    MINT_AUTHORITY,
    OPERATOR,
    PAYOUT,
    PROGRAM,
    RELAYER,
    decoded,
    settings,
    signers,
    wallet_sign,
)
from tests.payments.chain.vectors import (
    FUNDED_ESCROW_FILE,
    chain_vectors,
    codec_vectors,
    harness_keypair,
)

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]

BLOCKHASH: Final = Hash.from_string("EkSnNWid2cvwEVnVx9aBqawnmiCNiDgp3gUdkDPTKN1N")
LAST_VALID: Final = 380_000_150
CONFIG_DATA: Final = bytes.fromhex(codec_vectors()["accounts"]["config"]["data"])
CREDENTIAL: Final = decode_config(CONFIG_DATA).sas_credential
PO_ID: Final = "PO-1042-7KQ2"
PAYOUT_REF: Final = "9e" * 32
DAY: Final = 86_400


def account_json(data: bytes, owner: Pubkey) -> Json:
    return {
        "data": [base64.b64encode(data).decode(), "base64"],
        "executable": False,
        "lamports": 8_338_080,
        "owner": str(owner),
        "rentEpoch": 18_446_744_073_709_551_615,
        "space": len(data),
    }


def status_json(confirmation: str | None, err: Json = None, slot: int = 4_000) -> Json:
    return {
        "slot": slot,
        "confirmations": None if confirmation in ("finalized", None) else 1,
        "err": err,
        "status": {"Ok": None} if err is None else {"Err": err},
        "confirmationStatus": confirmation,
    }


@dataclass
class FakeNode:
    """A scripted JSON-RPC node; records `(method, params)` of every request."""

    accounts: dict[str, tuple[bytes, Pubkey]] = field(
        default_factory=dict[str, tuple[bytes, Pubkey]]
    )
    genesis: str = DEVNET_GENESIS
    statuses: list[Json] = field(default_factory=lambda: [None])
    statuses_slot: int = 5_000
    epoch: dict[str, Json] = field(
        default_factory=lambda: {"absoluteSlot": 5_000, "blockHeight": 4_800, "epoch": 1}
    )
    send_error: Json = None
    sent: list[bytes] = field(default_factory=list[bytes])
    requests: list[tuple[str, Json]] = field(default_factory=list[tuple[str, Json]])

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def methods(self) -> list[str]:
        return [method for method, _ in self.requests]

    def params(self, method: str) -> Json:
        return next(params for name, params in self.requests if name == method)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = cast(dict[str, Json], json.loads(request.content))
        method, params = cast(str, body["method"]), cast(list[Json], body["params"])
        self.requests.append((method, params))
        handlers: dict[str, Callable[[list[Json]], Json]] = {
            "getGenesisHash": lambda _: self.genesis,
            "getLatestBlockhash": lambda _: {
                "context": {"slot": 4_990},
                "value": {"blockhash": str(BLOCKHASH), "lastValidBlockHeight": LAST_VALID},
            },
            "getAccountInfo": self._account,
            "getSignatureStatuses": lambda _: {
                "context": {"slot": self.statuses_slot},
                "value": self.statuses,
            },
            "getEpochInfo": lambda _: self.epoch,
        }
        if method == "sendTransaction":
            wire = base64.b64decode(cast(str, params[0]))
            self.sent.append(wire)
            if self.send_error is not None:
                error: Json = {
                    "code": -32002,
                    "message": "Transaction simulation failed",
                    "data": {"err": self.send_error, "logs": [], "unitsConsumed": 0},
                }
                return httpx.Response(
                    200, json={"jsonrpc": "2.0", "id": body["id"], "error": error}
                )
            result: Json = str(VersionedTransaction.from_bytes(wire).signatures[0])
        else:
            result = handlers[method](params)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    def _account(self, params: list[Json]) -> Json:
        entry = self.accounts.get(cast(str, params[0]))
        value = None if entry is None else account_json(*entry)
        return {"context": {"slot": 4_990}, "value": value}


def make_chain(node: FakeNode, **overrides: object) -> PaymentChain:
    return build_payment_chain(settings(**overrides), http=node.http())


def with_config(node: FakeNode, data: bytes = CONFIG_DATA) -> FakeNode:
    node.accounts[str(config_pda(PROGRAM))] = (data, PROGRAM)
    return node


def config_data(*, operator: Pubkey, mints: tuple[Pubkey, Pubkey]) -> bytes:
    coder = BorshCoder(load_idl())
    fields = coder.decode_account("Config", CONFIG_DATA)
    fields["operator"], fields["allowed_mints"] = operator, list(mints)
    return coder.encode_account("Config", fields)


def instructions(message: MessageV0) -> list[tuple[Pubkey, list[Pubkey], bytes]]:
    """`(program, accounts, data)` of each instruction after the two compute-budget ones."""
    keys = message.account_keys
    return [
        (keys[ix.program_id_index], [keys[i] for i in ix.accounts], bytes(ix.data))
        for ix in message.instructions[2:]
    ]


def compute_budget(message: MessageV0) -> tuple[int, int]:
    limit, price = message.instructions[:2]
    keys = message.account_keys
    assert keys[limit.program_id_index] == keys[price.program_id_index] == COMPUTE_BUDGET_ID
    assert (limit.data[0], price.data[0]) == (2, 3)
    return struct.unpack("<I", limit.data[1:])[0], struct.unpack("<Q", price.data[1:])[0]


def assert_sent_shape(signed: SignedTransaction) -> None:
    tx, message = decoded(signed.wire)
    assert signed.signature == str(tx.signatures[0])
    assert signed.last_valid_block_height == LAST_VALID
    assert message.recent_blockhash == BLOCKHASH
    assert message.address_table_lookups == []
    assert all(tx.verify_with_results())
    assert len(signed.wire) <= 1232


def spec(**overrides: object) -> EscrowSpec:
    base = EscrowSpec(
        chain_po_id="PO-12-7KQ2",
        buyer=str(BUYER.pubkey()),
        po_hash=b"\x50" * 32,
        cad_hash=bytes(32),
        total=1_200_000_000,
        milestones=(EscrowMilestone("Tooling", 3_000), EscrowMilestone("Delivery", 7_000)),
        refund_after=int(time.time()) + 90 * DAY,
        payout_ref=PAYOUT_REF,
    )
    return dataclasses.replace(base, **overrides)


# ---- settings ----


async def test_build_payment_chain_reads_every_setting_and_contacts_nothing() -> None:
    node = FakeNode()
    chain = make_chain(node)

    assert isinstance(chain, SolanaPaymentChain)
    assert (chain.cluster, chain.mint) == ("devnet", str(MINT))
    assert node.requests == []


@pytest.mark.parametrize(
    "setting",
    [
        "SOLANA_RPC_URL",
        "SOLANA_STABLECOIN_MINT",
        "SOLANA_MINT_AUTHORITY_KEY",
        "SOLANA_OPERATOR_KEY",
        "SOLANA_RELAYER_KEY",
        "SOLANA_PAYOUT_OWNER",
        "SOLANA_GATE_ATTESTER",
    ],
)
def test_a_missing_setting_is_named(setting: str) -> None:
    with pytest.raises(PaymentsNotConfigured, match=f"{setting} is not set"):
        make_chain(FakeNode(), **{setting.lower(): None})


@pytest.mark.parametrize(
    ("setting", "value", "message"),
    [
        ("SOLANA_ESCROW_PROGRAM_ID", "not-an-address", "SOLANA_ESCROW_PROGRAM_ID is not a Solana"),
        (
            "SOLANA_STABLECOIN_MINT",
            "11111111111111111111111111111111",
            "SOLANA_STABLECOIN_MINT is not",
        ),
        ("SOLANA_GATE_ATTESTER", str(PAYOUT), "SOLANA_GATE_ATTESTER must differ"),
        ("SOLANA_RELAYER_KEY", "3xQ-not-a-secret-key", "SOLANA_RELAYER_KEY is not a base58"),
        (
            "SOLANA_RPC_URL",
            "ftp://devnet.rpc.invalid/?api-key=sk-x",
            "SOLANA_RPC_URL is not an http",
        ),
        (
            "SOLANA_RPC_URL",
            "http://devnet.rpc.invalid/?api-key=sk-x",
            "SOLANA_RPC_URL must use https",
        ),
    ],
)
def test_a_malformed_setting_is_named_never_shown(setting: str, value: str, message: str) -> None:
    with pytest.raises(PaymentsNotConfigured, match=message) as caught:
        make_chain(FakeNode(), **{setting.lower(): value})
    assert value not in str(caught.value)


def test_a_local_rpc_url_may_use_http() -> None:
    make_chain(FakeNode(), solana_rpc_url="http://127.0.0.1:8899")


# ---- addresses ----


def test_the_escrow_and_vault_addresses_equal_the_vectors() -> None:
    chain = make_chain(FakeNode())
    escrow = chain.escrow_address(buyer=str(BUYER.pubkey()), chain_po_id=PO_ID)
    assert escrow == chain_vectors()["escrow"]
    assert chain.vault_address(escrow=escrow) == chain_vectors()["vault"]


@pytest.mark.parametrize("chain_po_id", ["A" * 28 + "-7KQ2", "po-1-7kq2", "PO-1", "PO_1-7KQ2"])
def test_an_escrow_address_needs_a_valid_chain_po_id(chain_po_id: str) -> None:
    with pytest.raises(PaymentRefused, match="chain_po_id"):
        make_chain(FakeNode()).escrow_address(buyer=str(BUYER.pubkey()), chain_po_id=chain_po_id)


# ---- mint ----


async def test_build_mint_creates_the_token_account_mints_and_tags_the_payment() -> None:
    node = FakeNode()
    owner = BUYER.pubkey()
    memo = "bido:payment:00000000-0000-4000-8000-000000000012"

    signed = await make_chain(node).build_mint(owner=str(owner), amount=1_250_000_000, memo=memo)

    assert_sent_shape(signed)
    _, message = decoded(signed.wire)
    assert signers(message) == [RELAYER.pubkey(), MINT_AUTHORITY.pubkey()]
    assert compute_budget(message) == (60_000, 1_000)
    owner_token = token_account(owner, MINT)
    assert instructions(message) == [
        (
            ASSOCIATED_TOKEN_PROGRAM_ID,
            [RELAYER.pubkey(), owner_token, owner, MINT, SYSTEM_PROGRAM_ID, TOKEN_PROGRAM_ID],
            b"\x01",
        ),
        (
            TOKEN_PROGRAM_ID,
            [MINT, owner_token, MINT_AUTHORITY.pubkey()],
            struct.pack("<BQB", 14, 1_250_000_000, 6),
        ),
        (MEMO_PROGRAM_ID, [], memo.encode()),
    ]
    assert node.methods() == ["getGenesisHash", "getLatestBlockhash"]
    assert node.params("getLatestBlockhash") == [{"commitment": "confirmed"}]


@pytest.mark.parametrize(
    ("owner", "amount", "memo"),
    [
        (str(BUYER.pubkey()), 0, "bido:payment:x"),
        (str(BUYER.pubkey()), 2**64, "bido:payment:x"),
        ("not-an-address", 1, "bido:payment:x"),
        (str(BUYER.pubkey()), 1, ""),
    ],
)
async def test_build_mint_refuses_bad_terms_before_any_request(
    owner: str, amount: int, memo: str
) -> None:
    node = FakeNode()
    with pytest.raises(PaymentRefused):
        await make_chain(node).build_mint(owner=owner, amount=amount, memo=memo)
    assert node.requests == []


# ---- create escrow ----


async def test_build_create_escrow_gives_each_milestone_its_gate_and_tags_the_payout() -> None:
    node = with_config(FakeNode())
    terms = spec()

    signed = await make_chain(node).build_create_escrow(terms)

    assert_sent_shape(signed)
    _, message = decoded(signed.wire)
    assert signers(message) == [RELAYER.pubkey(), OPERATOR.pubkey()]
    assert compute_budget(message) == (250_000, 1_000)
    expected = create_po_ix(
        program_id=PROGRAM,
        operator=OPERATOR.pubkey(),
        payer=RELAYER.pubkey(),
        buyer=BUYER.pubkey(),
        payout=PAYOUT,
        mint=MINT,
        credential=CREDENTIAL,
        args=CreatePoArgs(
            chain_po_id="PO-12-7KQ2",
            po_hash=b"\x50" * 32,
            cad_hash=bytes(32),
            total=1_200_000_000,
            gate_attesters=(ATTESTER, ATTESTER),
            milestones=(MilestoneInput("Tooling", 3_000, 0), MilestoneInput("Delivery", 7_000, 1)),
            refund_after=terms.refund_after,
        ),
    )
    create, memo = instructions(message)
    assert create == (
        expected.program_id,
        [meta.pubkey for meta in expected.accounts],
        bytes(expected.data),
    )
    assert memo == (MEMO_PROGRAM_ID, [], f"bido:payout_ref:{PAYOUT_REF}".encode())
    assert node.params("getAccountInfo") == [
        str(config_pda(PROGRAM)),
        {"encoding": "base64", "commitment": "confirmed"},
    ]


async def test_the_config_is_read_once() -> None:
    node = with_config(FakeNode())
    chain = make_chain(node)

    await chain.build_create_escrow(spec())
    await chain.build_create_escrow(spec(chain_po_id="PO-13-8MR3"))

    assert node.methods().count("getAccountInfo") == 1
    assert node.methods().count("getGenesisHash") == 1


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"milestones": (EscrowMilestone("A", 5_000), EscrowMilestone("B", 4_000))}, "add up"),
        ({"milestones": tuple(EscrowMilestone(f"M{i}", 2_000) for i in range(5))}, "1 to 4"),
        ({"milestones": ()}, "1 to 4"),
        ({"milestones": (EscrowMilestone("x" * 25, 10_000),)}, "1 to 24 bytes"),
        ({"milestones": (EscrowMilestone("", 10_000),)}, "1 to 24 bytes"),
        ({"milestones": (EscrowMilestone("A", 0), EscrowMilestone("B", 10_000))}, "positive share"),
        ({"total": 0}, "positive u64"),
        (
            {"total": 1, "milestones": (EscrowMilestone("A", 5_000), EscrowMilestone("B", 5_000))},
            "zero",
        ),
        ({"refund_after": 1_000}, "future"),
        ({"payout_ref": PAYOUT_REF.upper()}, "payout reference"),
        ({"buyer": str(PAYOUT)}, "payout owner"),
        ({"chain_po_id": "PO-12-" + "7" * 30}, "chain_po_id"),
        ({"po_hash": b"\x50" * 31}, "32 bytes"),
        ({"buyer": "not-an-address"}, "not a Solana address"),
    ],
)
async def test_build_create_escrow_refuses_what_the_program_would_refuse(
    overrides: dict[str, object], reason: str
) -> None:
    node = with_config(FakeNode())
    with pytest.raises(PaymentRefused, match=reason):
        await make_chain(node).build_create_escrow(spec(**overrides))
    assert node.requests == []


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        (None, "no Config account"),
        (
            config_data(operator=PAYOUT, mints=(MINT, Pubkey.default())),
            "SOLANA_OPERATOR_KEY is not",
        ),
        (config_data(operator=OPERATOR.pubkey(), mints=(PAYOUT, Pubkey.default())), "allow-mint"),
    ],
)
async def test_an_escrow_needs_a_config_that_allows_our_operator_and_mint(
    config: bytes | None, reason: str
) -> None:
    node = FakeNode() if config is None else with_config(FakeNode(), config)
    with pytest.raises(PaymentsNotConfigured, match=reason):
        await make_chain(node).build_create_escrow(spec())


# ---- fund: the buyer's wallet signs remotely ----


async def test_build_fund_is_the_accepted_fund_instruction_with_every_slot_zeroed() -> None:
    node = FakeNode()
    chain = make_chain(node)
    escrow = chain.escrow_address(buyer=str(BUYER.pubkey()), chain_po_id=PO_ID)

    unsigned = await chain.build_fund(buyer=str(BUYER.pubkey()), escrow=escrow)

    tx, message = decoded(unsigned.wire)
    assert tx.signatures == [Signature.default(), Signature.default()]
    assert signers(message) == [RELAYER.pubkey(), BUYER.pubkey()]
    assert compute_budget(message) == (60_000, 1_000)
    assert unsigned.last_valid_block_height == LAST_VALID
    accepted = codec_vectors()["instructions"]["fund"]
    [fund] = instructions(message)
    assert fund == (
        Pubkey.from_string(accepted["program_id"]),
        [Pubkey.from_string(meta["pubkey"]) for meta in accepted["accounts"]],
        bytes.fromhex(accepted["data"]),
    )


async def test_complete_fund_takes_the_wallets_signature_then_the_relayers() -> None:
    chain = make_chain(FakeNode())
    escrow = chain.escrow_address(buyer=str(BUYER.pubkey()), chain_po_id=PO_ID)
    unsigned = await chain.build_fund(buyer=str(BUYER.pubkey()), escrow=escrow)

    signed = chain.complete_fund(
        unsigned, buyer=str(BUYER.pubkey()), signed_by_buyer=wallet_sign(unsigned.wire, BUYER)
    )

    assert_sent_shape(signed)
    tx, message = decoded(signed.wire)
    _, ours = decoded(unsigned.wire)
    assert to_bytes_versioned(message) == to_bytes_versioned(ours)
    assert tx.verify_with_results() == [True, True]
    assert tx.signatures[1] == BUYER.sign_message(to_bytes_versioned(ours))


async def test_complete_fund_refuses_a_changed_message() -> None:
    chain = make_chain(FakeNode())
    escrow = chain.escrow_address(buyer=str(BUYER.pubkey()), chain_po_id=PO_ID)
    unsigned = await chain.build_fund(buyer=str(BUYER.pubkey()), escrow=escrow)
    other = await chain.build_fund(
        buyer=str(BUYER.pubkey()),
        escrow=chain.escrow_address(buyer=str(BUYER.pubkey()), chain_po_id="PO-1043-8MR3"),
    )

    with pytest.raises(ProviderError) as caught:
        chain.complete_fund(
            unsigned, buyer=str(BUYER.pubkey()), signed_by_buyer=wallet_sign(other.wire, BUYER)
        )

    assert (caught.value.provider, caught.value.code) == ("wallet", "message_changed")


async def test_complete_fund_refuses_a_signature_that_does_not_verify() -> None:
    chain = make_chain(FakeNode())
    escrow = chain.escrow_address(buyer=str(BUYER.pubkey()), chain_po_id=PO_ID)
    unsigned = await chain.build_fund(buyer=str(BUYER.pubkey()), escrow=escrow)
    tx, message = decoded(unsigned.wire)
    forged = harness_keypair("not-the-buyer").sign_message(to_bytes_versioned(message))
    returned = bytes(VersionedTransaction.populate(message, [tx.signatures[0], forged]))

    with pytest.raises(ProviderError) as caught:
        chain.complete_fund(unsigned, buyer=str(BUYER.pubkey()), signed_by_buyer=returned)

    assert (caught.value.provider, caught.value.code) == ("wallet", "invalid_signature")


async def test_complete_fund_refuses_bytes_that_are_not_a_transaction() -> None:
    chain = make_chain(FakeNode())
    escrow = chain.escrow_address(buyer=str(BUYER.pubkey()), chain_po_id=PO_ID)
    unsigned = await chain.build_fund(buyer=str(BUYER.pubkey()), escrow=escrow)

    with pytest.raises(ProviderError) as caught:
        chain.complete_fund(unsigned, buyer=str(BUYER.pubkey()), signed_by_buyer=b"\x00garbage")

    assert caught.value.code == "unreadable_transaction"


async def test_the_relayer_co_signs_only_a_fund_built_for_that_buyer() -> None:
    chain = make_chain(FakeNode())
    escrow = chain.escrow_address(buyer=str(BUYER.pubkey()), chain_po_id=PO_ID)
    unsigned = await chain.build_fund(buyer=str(BUYER.pubkey()), escrow=escrow)
    someone = harness_keypair("someone")
    mint = await chain.build_mint(owner=str(BUYER.pubkey()), amount=1, memo="bido:payment:x")

    with pytest.raises(ValueError, match="not a fund"):
        chain.complete_fund(
            unsigned, buyer=str(someone.pubkey()), signed_by_buyer=wallet_sign(unsigned.wire, BUYER)
        )
    with pytest.raises(ValueError, match="not a fund"):
        chain.complete_fund(
            UnsignedTransaction(mint.wire, mint.last_valid_block_height),
            buyer=str(BUYER.pubkey()),
            signed_by_buyer=mint.wire,
        )


# ---- send ----


async def test_send_sends_the_bytes_once_without_node_retries() -> None:
    node = FakeNode()
    chain = make_chain(node)
    signed = await chain.build_mint(owner=str(BUYER.pubkey()), amount=5, memo="bido:payment:x")

    await chain.send(signed)
    await chain.send(signed)

    assert node.sent == [signed.wire, signed.wire]
    assert node.params("sendTransaction") == [
        base64.b64encode(signed.wire).decode(),
        {
            "encoding": "base64",
            "skipPreflight": False,
            "preflightCommitment": "confirmed",
            "maxRetries": 0,
        },
    ]
    assert node.methods().count("getGenesisHash") == 1


async def funded_by_relayer(node: FakeNode) -> tuple[PaymentChain, SignedTransaction]:
    chain = make_chain(node)
    escrow = chain.escrow_address(buyer=str(BUYER.pubkey()), chain_po_id=PO_ID)
    unsigned = await chain.build_fund(buyer=str(BUYER.pubkey()), escrow=escrow)
    signed = chain.complete_fund(
        unsigned, buyer=str(BUYER.pubkey()), signed_by_buyer=wallet_sign(unsigned.wire, BUYER)
    )
    return chain, signed


@pytest.mark.parametrize(
    ("err", "code", "retryable"),
    [
        # `fund` is instruction 2; 6019 is the escrow program's `AlreadyFunded`.
        ({"InstructionError": [2, {"Custom": 6019}]}, "AlreadyFunded", False),
        # The same number from another program (the compute budget) is not the escrow's.
        ({"InstructionError": [0, {"Custom": 6019}]}, "InstructionError(0, Custom(6019))", False),
        ({"InstructionError": [2, {"Custom": 1}]}, "InstructionError(2, Custom(1))", False),
        (
            {"InstructionError": [2, "InvalidAccountData"]},
            "InstructionError(2, InvalidAccountData)",
            False,
        ),
        ("BlockhashNotFound", "BlockhashNotFound", True),
        ("InsufficientFundsForFee", "InsufficientFundsForFee", False),
        ({"InsufficientFundsForRent": {"account_index": 0}}, "InsufficientFundsForRent", False),
        (
            {"InstructionError": [2, {"Custom": "6019 <script>"}]},
            "InstructionError(2, Custom)",
            False,
        ),
        ("not a variant name!", "Unknown", False),
    ],
)
async def test_a_preflight_failure_is_named_and_never_forwarded(
    err: Json, code: str, retryable: bool
) -> None:
    node = FakeNode(send_error=err)
    chain, signed = await funded_by_relayer(node)

    with pytest.raises(ProviderError) as caught:
        await chain.send(signed)

    assert (caught.value.provider, caught.value.operation) == ("solana", "sendTransaction")
    assert (caught.value.code, caught.value.retryable) == (code, retryable)
    assert caught.value.__context__ is None


async def test_bytes_that_already_landed_are_sent() -> None:
    chain, signed = await funded_by_relayer(FakeNode(send_error="AlreadyProcessed"))
    await chain.send(signed)


async def test_a_mainnet_node_is_refused_before_anything_is_built_or_sent() -> None:
    node = FakeNode(genesis=MAINNET_GENESIS_HASH)
    chain = make_chain(node)

    with pytest.raises(PaymentsNotConfigured, match="devnet only"):
        await chain.build_mint(owner=str(BUYER.pubkey()), amount=5, memo="bido:payment:x")

    assert node.methods() == ["getGenesisHash"]


# ---- status and block height ----


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (status_json("finalized"), TransactionStatus("confirmed")),
        (status_json("confirmed"), TransactionStatus("confirmed")),
        (
            status_json("confirmed", {"InstructionError": [2, {"Custom": 6019}]}),
            TransactionStatus("failed", "AlreadyFunded"),
        ),
        (
            status_json("finalized", {"InstructionError": [3, {"Custom": 1}]}),
            TransactionStatus("failed", "InstructionError(3, Custom(1))"),
        ),
        (
            status_json("finalized", {"SomeFutureError": [7]}),
            TransactionStatus("failed", "SomeFutureError"),
        ),
        (status_json("processed"), TransactionStatus("unknown")),
        (
            status_json("processed", {"InstructionError": [2, {"Custom": 6019}]}),
            TransactionStatus("unknown"),
        ),
        (None, TransactionStatus("unknown")),
        # A node that leaves `confirmationStatus` out: `confirmations: null` means rooted.
        ({"slot": 4_000, "confirmations": None, "err": None}, TransactionStatus("confirmed")),
        ({"slot": 4_000, "confirmations": 3, "err": None}, TransactionStatus("unknown")),
    ],
)
async def test_status_is_confirmed_or_failed_only_once_confirmed(
    status: Json, expected: TransactionStatus
) -> None:
    node = FakeNode(statuses=[status])

    assert await make_chain(node).status(str(Signature.from_bytes(bytes(range(64))))) == expected
    assert node.params("getSignatureStatuses") == [
        [str(Signature.from_bytes(bytes(range(64))))],
        {"searchTransactionHistory": True},
    ]


async def test_block_height_is_the_finalized_height() -> None:
    node = FakeNode()

    assert await make_chain(node).block_height() == 4_800
    assert node.params("getEpochInfo") == [{"commitment": "finalized"}]


async def test_status_will_not_call_a_transaction_unknown_from_a_node_behind() -> None:
    node = FakeNode()
    chain = make_chain(node)
    signature = str(Signature.from_bytes(bytes(range(64))))
    await chain.block_height()  # finalized slot 5_000 seen

    node.statuses, node.statuses_slot = [None], 4_999
    with pytest.raises(ProviderError) as behind:
        await chain.status(signature)
    assert (behind.value.code, behind.value.retryable) == ("node_behind", True)

    # Still only `processed` in a slot already finalized: this node's view lags.
    node.statuses, node.statuses_slot = [status_json("processed", slot=4_990)], 5_100
    with pytest.raises(ProviderError, match="node_behind"):
        await chain.status(signature)

    node.statuses, node.statuses_slot = [None], 5_000
    assert await chain.status(signature) == TransactionStatus("unknown")
    node.statuses, node.statuses_slot = [status_json("processed", slot=5_010)], 5_020
    assert await chain.status(signature) == TransactionStatus("unknown")
    # A confirmation is an answer whatever the node's slot.
    node.statuses, node.statuses_slot = [status_json("confirmed")], 10
    assert await chain.status(signature) == TransactionStatus("confirmed")


async def test_status_refuses_a_string_that_is_not_a_signature() -> None:
    node = FakeNode()
    with pytest.raises(PaymentRefused):
        await make_chain(node).status("not-a-signature")
    assert node.requests == []


# ---- escrow ----


async def test_escrow_reads_the_account_into_our_state() -> None:
    address = chain_vectors()["escrow"]
    node = FakeNode(accounts={address: (FUNDED_ESCROW_FILE.read_bytes(), PROGRAM)})

    state = await make_chain(node).escrow(address)

    assert state == EscrowState(
        address=address,
        status="funded",
        chain_po_id=PO_ID,
        buyer=str(BUYER.pubkey()),
        mint=str(MINT),
        total=1_200_000_000_000,
        vault=chain_vectors()["vault"],
    )
    assert node.params("getAccountInfo") == [
        address,
        {"encoding": "base64", "commitment": "confirmed"},
    ]


async def test_no_escrow_is_none() -> None:
    address = chain_vectors()["escrow"]
    assert await make_chain(FakeNode()).escrow(address) is None
    # Lamports someone sent to the address make a System account, not an escrow.
    system_owned = FakeNode(accounts={address: (b"", SYSTEM_PROGRAM_ID)})
    assert await make_chain(system_owned).escrow(address) is None
