"""`SolanaPaymentChain` against a scripted JSON-RPC node (`httpx.MockTransport`).

Keys are the escrow harness's public test seeds, so the `fund` built here is the very
instruction the program accepted in its own tests (`vectors.json`). The releases run on
PO-1042's funded escrow (`po_escrow_funded.bin`) re-encoded on Bido's plan: milestone i
through gate i, every gate pinned to the gate attester. Nothing reaches a network.
"""

import base64
import dataclasses
import json
import logging
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final, cast

import httpx
import pytest
from solders.compute_budget import ID as COMPUTE_BUDGET_ID
from solders.hash import Hash
from solders.instruction import Instruction
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.token import ID as TOKEN_PROGRAM_ID
from solders.transaction import VersionedTransaction

from bido_back.payments.chain import build_payment_chain
from bido_back.payments.chain.borsh import BorshCoder, BorshValue, as_int, as_list, as_struct
from bido_back.payments.chain.escrow import (
    PO_ESCROW_ACCOUNT_LEN,
    CreatePoArgs,
    MilestoneInput,
    create_po_ix,
    decode_config,
    decode_po_escrow,
    release_ix,
)
from bido_back.payments.chain.idl import load_idl
from bido_back.payments.chain.pdas import (
    SAS_PROGRAM_ID,
    attestation_pda,
    config_pda,
    gate_nonce,
    token_account,
)
from bido_back.payments.chain.sas import create_attestation_ix, encode_gate_approval_data
from bido_back.payments.chain.solana import MAINNET_GENESIS_HASH, SolanaPaymentChain
from bido_back.payments.chain.spl import ASSOCIATED_TOKEN_PROGRAM_ID, MEMO_PROGRAM_ID
from bido_back.payments.errors import PaymentRefused, PaymentsNotConfigured, ProviderError
from bido_back.payments.ports import (
    EscrowMilestone,
    EscrowMilestoneState,
    EscrowMilestoneStatus,
    EscrowSpec,
    EscrowState,
    GateAttestation,
    PaymentChain,
    ReleaseSpec,
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
    attestation_account,
    credential_account,
    decoded,
    settings,
    signers,
    wallet_sign,
)
from tests.payments.chain.vectors import (
    FUNDED_ESCROW_FILE,
    JsonValue,
    chain_vectors,
    codec_vectors,
    harness_keypair,
)

type Json = None | bool | int | float | str | list[Json] | dict[str, Json]

BLOCKHASH: Final = Hash.from_string("EkSnNWid2cvwEVnVx9aBqawnmiCNiDgp3gUdkDPTKN1N")
LAST_VALID: Final = 380_000_150
CONFIG_DATA: Final = bytes.fromhex(codec_vectors()["accounts"]["config"]["data"])
CREDENTIAL: Final = decode_config(CONFIG_DATA).sas_credential
SCHEMA: Final = decode_config(CONFIG_DATA).sas_schema
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


def with_config(
    node: FakeNode, data: bytes = CONFIG_DATA, *, signers: tuple[Pubkey, ...] = (ATTESTER,)
) -> FakeNode:
    """The program's `Config` and its SAS credential, whose signers can attest."""
    node.accounts[str(config_pda(PROGRAM))] = (data, PROGRAM)
    node.accounts[str(CREDENTIAL)] = (credential_account(signers), SAS_PROGRAM_ID)
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
            "SOLANA_GATE_ATTESTER_KEY",
            "3xQ-not-a-secret-key",
            "SOLANA_GATE_ATTESTER_KEY is not a base58",
        ),
        (
            "SOLANA_GATE_ATTESTER_KEY",
            str(harness_keypair("not-the-attester")),
            "SOLANA_GATE_ATTESTER_KEY is not the key of SOLANA_GATE_ATTESTER",
        ),
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


def test_without_the_gate_attester_key_the_chain_still_works() -> None:
    node = FakeNode()
    chain = make_chain(node, solana_gate_attester_key=None)
    assert isinstance(chain, SolanaPaymentChain)
    assert node.requests == []


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


async def test_the_config_and_its_credential_are_read_once() -> None:
    node = with_config(FakeNode())
    chain = make_chain(node)

    await chain.build_create_escrow(spec())
    await chain.build_create_escrow(spec(chain_po_id="PO-13-8MR3"))

    reads = [params for method, params in node.requests if method == "getAccountInfo"]
    assert [cast(list[Json], params)[0] for params in reads] == [
        str(config_pda(PROGRAM)),
        str(CREDENTIAL),
    ]
    assert node.methods().count("getGenesisHash") == 1


@pytest.mark.parametrize(
    ("credential", "owner", "reason"),
    [
        (
            credential_account([harness_keypair("smoke-gate0").pubkey()]),
            SAS_PROGRAM_ID,
            "SOLANA_GATE_ATTESTER is not an authorized signer of the escrow's SAS credential",
        ),
        (credential_account([]), SAS_PROGRAM_ID, "not an authorized signer"),
        (None, SAS_PROGRAM_ID, "is not a SAS credential"),
        (credential_account([ATTESTER]), PROGRAM, "is not a SAS credential"),
        (credential_account([ATTESTER])[:-1], SAS_PROGRAM_ID, "is not a SAS credential"),
    ],
    ids=["other signers", "no signer", "missing", "not SAS's", "malformed"],
)
async def test_an_escrow_needs_an_attester_the_credential_authorizes(
    credential: bytes | None, owner: Pubkey, reason: str
) -> None:
    """The devnet build of `create_po` skips this check: such an escrow could be funded,
    then never released."""
    node = with_config(FakeNode())
    if credential is None:
        del node.accounts[str(CREDENTIAL)]
    else:
        node.accounts[str(CREDENTIAL)] = (credential, owner)
    chain = make_chain(node)

    with pytest.raises(PaymentsNotConfigured, match=reason):
        await chain.build_create_escrow(spec())
    assert "getLatestBlockhash" not in node.methods()

    # Not remembered: once the issuer adds the attester, the next escrow is built.
    node.accounts[str(CREDENTIAL)] = (credential_account([PAYOUT, ATTESTER]), SAS_PROGRAM_ID)
    await chain.build_create_escrow(spec())


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


def milestone_states(fields: dict[str, JsonValue]) -> tuple[EscrowMilestoneState, ...]:
    """The milestones of an escrow vector as `escrow()` reports them."""
    states: list[EscrowMilestoneState] = []
    for index, value in enumerate(cast(list[dict[str, JsonValue]], fields["milestones"])):
        approved_by = cast(str, value["approved_by"])
        states.append(
            EscrowMilestoneState(
                index=index,
                name=cast(str, value["name"]),
                bps=cast(int, value["bps"]),
                amount=cast(int, value["amount"]),
                gate=cast(int, value["gate"]),
                status=cast(EscrowMilestoneStatus, cast(str, value["status"]).lower()),
                evidence_hash=cast(str, value["evidence_hash"]),
                approved_by=None if approved_by == str(Pubkey.default()) else approved_by,
            )
        )
    return tuple(states)


async def test_escrow_reads_the_account_into_our_state() -> None:
    address = chain_vectors()["escrow"]
    node = FakeNode(accounts={address: (FUNDED_ESCROW_FILE.read_bytes(), PROGRAM)})

    state = await make_chain(node).escrow(address)

    fields = codec_vectors()["accounts"]["po_escrow"]["Funded"]["fields"]
    assert state is not None
    assert state == EscrowState(
        address=address,
        status="funded",
        chain_po_id=PO_ID,
        buyer=str(BUYER.pubkey()),
        mint=str(MINT),
        total=1_200_000_000_000,
        vault=chain_vectors()["vault"],
        released=0,
        po_hash="50" * 32,
        milestones=milestone_states(fields),
    )
    assert [(m.name, m.gate, m.status, m.approved_by) for m in state.milestones] == [
        ("Tool start", 0, "pending", None),
        ("T1", 1, "pending", None),
        ("PPAP", 1, "pending", None),
    ]
    assert {m.evidence_hash for m in state.milestones} == {"00" * 32}
    assert node.params("getAccountInfo") == [
        address,
        {"encoding": "base64", "commitment": "confirmed"},
    ]


@pytest.mark.parametrize(
    ("status", "released", "milestones"),
    [("Completed", 1_200_000_000_000, "rrr"), ("Refunded", 360_000_000_000, "rcc")],
)
async def test_escrow_reports_released_and_cancelled_milestones(
    status: str, released: int, milestones: str
) -> None:
    address = chain_vectors()["escrow"]
    account = codec_vectors()["accounts"]["po_escrow"][status]
    node = FakeNode(accounts={address: (bytes.fromhex(account["data"]), PROGRAM)})

    state = await make_chain(node).escrow(address)

    assert state is not None
    assert (state.status, state.released) == (status.lower(), released)
    assert state.milestones == milestone_states(account["fields"])
    assert "".join(m.status[0] for m in state.milestones) == milestones
    first = state.milestones[0]
    assert (first.approved_by, first.evidence_hash) == (
        str(BUYER.pubkey()),
        chain_vectors()["gates"][0]["evidence_hash"],
    )


async def test_no_escrow_is_none() -> None:
    address = chain_vectors()["escrow"]
    assert await make_chain(FakeNode()).escrow(address) is None
    # Lamports someone sent to the address make a System account, not an escrow.
    system_owned = FakeNode(accounts={address: (b"", SYSTEM_PROGRAM_ID)})
    assert await make_chain(system_owned).escrow(address) is None


# ---- releases: PO-1042's escrow on Bido's plan ----

ESCROW: Final = Pubkey.from_string(chain_vectors()["escrow"])
PO_HASH: Final = b"\x50" * 32
# The evidence a person approved for each milestone: the vectors' for gates 0 and 1.
EVIDENCE: Final = (
    chain_vectors()["gates"][0]["evidence_hash"],
    chain_vectors()["gates"][1]["evidence_hash"],
    "e2" * 32,
)
SOMEONE: Final = harness_keypair("someone").pubkey()


def escrow_account(
    *,
    status: str = "Funded",
    attesters: tuple[Pubkey, ...] = (ATTESTER, ATTESTER, ATTESTER),
    gates: tuple[int, ...] = (0, 1, 2),
    released: tuple[int, ...] = (),
) -> bytes:
    """PO-1042's escrow with milestone i through gate `gates[i]` and gate g pinned to
    `attesters[g]`; each `released` milestone carries its approved evidence."""
    coder = BorshCoder(load_idl())
    fields = coder.decode_account("PoEscrow", FUNDED_ESCROW_FILE.read_bytes())
    pinned: list[BorshValue] = [
        {
            "attester": attester,
            "attestation": attestation_pda(CREDENTIAL, SCHEMA, gate_nonce(ESCROW, gate)),
        }
        for gate, attester in enumerate(attesters)
    ]
    milestones: list[BorshValue] = []
    total_released = 0
    for index, value in enumerate(as_list(fields["milestones"])):
        milestone: dict[str, BorshValue] = {**as_struct(value), "gate": gates[index]}
        if index in released:
            milestone |= {
                "status": "Released",
                "released_slot": 1_000,
                "approved_by": attesters[gates[index]],
                "evidence_hash": bytes.fromhex(EVIDENCE[index]),
            }
            total_released += as_int(milestone["amount"])
        milestones.append(milestone)
    changes: dict[str, BorshValue] = {
        "status": status,
        "gates": pinned,
        "milestones": milestones,
        "released": total_released,
    }
    data = coder.encode_account("PoEscrow", fields | changes)
    return data + bytes(PO_ESCROW_ACCOUNT_LEN - len(data))


def release_node(
    escrow: bytes | None = None, *, signers: tuple[Pubkey, ...] = (ATTESTER,)
) -> FakeNode:
    """A node holding the escrow and its SAS credential (whose signers may attest)."""
    node = FakeNode()
    node.accounts[str(ESCROW)] = (escrow_account() if escrow is None else escrow, PROGRAM)
    node.accounts[str(CREDENTIAL)] = (credential_account(signers), SAS_PROGRAM_ID)
    return node


def gate_address(gate: int) -> Pubkey:
    return attestation_pda(CREDENTIAL, SCHEMA, gate_nonce(ESCROW, gate))


def attested(
    node: FakeNode,
    gate: int,
    *,
    evidence: str | None = None,
    signer: Pubkey = ATTESTER,
    po_hash: bytes = PO_HASH,
    expiry: int = 0,
) -> str:
    """Writes gate `gate`'s attestation (the approved one unless told otherwise)."""
    data = attestation_account(
        nonce=gate_nonce(ESCROW, gate),
        credential=CREDENTIAL,
        schema=SCHEMA,
        po_hash=po_hash,
        gate=gate,
        evidence_hash=bytes.fromhex(EVIDENCE[gate] if evidence is None else evidence),
        signer=signer,
        expiry=expiry,
    )
    node.accounts[str(gate_address(gate))] = (data, SAS_PROGRAM_ID)
    return str(gate_address(gate))


def account_reads(node: FakeNode) -> list[Json]:
    return [
        cast(list[Json], params)[0]
        for method, params in node.requests
        if method == "getAccountInfo"
    ]


def compiled(ix: Instruction) -> tuple[Pubkey, list[Pubkey], bytes]:
    return ix.program_id, [meta.pubkey for meta in ix.accounts], bytes(ix.data)


def expected_attestation(gate: int) -> Instruction:
    return create_attestation_ix(
        payer=RELAYER.pubkey(),
        signer=ATTESTER,
        credential=CREDENTIAL,
        schema=SCHEMA,
        nonce=gate_nonce(ESCROW, gate),
        data=encode_gate_approval_data(PO_HASH, gate, bytes.fromhex(EVIDENCE[gate])),
    )


def expected_release(milestone: int) -> Instruction:
    state = decode_po_escrow(escrow_account())
    return release_ix(
        program_id=PROGRAM,
        escrow=ESCROW,
        vault=state.vault,
        payout_token=state.payout_token,
        mint=state.mint,
        attestation=gate_address(milestone),
        milestone=milestone,
    )


def test_the_release_fixture_is_po_1042s_escrow_on_bidos_plan() -> None:
    state = decode_po_escrow(escrow_account())
    assert (state.credential, state.schema, state.po_hash) == (CREDENTIAL, SCHEMA, PO_HASH)
    assert [(m.name, m.gate) for m in state.milestones] == [
        ("Tool start", 0),
        ("T1", 1),
        ("PPAP", 2),
    ]
    assert [gate.attester for gate in state.gates] == [ATTESTER] * 3
    assert str(state.gates[1].attestation) == chain_vectors()["gates"][1]["attestation"]


# ---- gate attestations ----


async def test_a_gate_without_an_attestation_has_none() -> None:
    node = release_node()

    assert await make_chain(node).gate_attestation(escrow=str(ESCROW), gate=0) is None
    assert account_reads(node) == [str(ESCROW), str(gate_address(0))]

    # Lamports someone sent to the address are not an attestation: SAS still writes one.
    node.accounts[str(gate_address(0))] = (b"", SYSTEM_PROGRAM_ID)
    assert await make_chain(node).gate_attestation(escrow=str(ESCROW), gate=0) is None


async def test_a_gate_attestation_is_read_as_written() -> None:
    node = release_node()
    address = attested(node, 1)

    found = await make_chain(node).gate_attestation(escrow=str(ESCROW), gate=1)

    assert found == GateAttestation(
        address=address,
        signer=str(ATTESTER),
        po_hash="50" * 32,
        gate=1,
        evidence_hash=EVIDENCE[1],
    )
    assert node.params("getAccountInfo") == [
        str(ESCROW),
        {"encoding": "base64", "commitment": "confirmed"},
    ]


async def test_another_signers_attestation_is_reported_as_found() -> None:
    """Whether it is ours is the caller's decision: signer, PO hash, gate and evidence."""
    node = release_node()
    attested(node, 2, evidence="ff" * 32, signer=SOMEONE)

    found = await make_chain(node).gate_attestation(escrow=str(ESCROW), gate=2)

    assert found is not None
    assert (found.signer, found.gate, found.evidence_hash) == (str(SOMEONE), 2, "ff" * 32)


def _gate_1_attestation(**changes: Pubkey) -> bytes:
    return attestation_account(
        nonce=changes.get("nonce", gate_nonce(ESCROW, 1)),
        credential=changes.get("credential", CREDENTIAL),
        schema=changes.get("schema", SCHEMA),
        po_hash=PO_HASH,
        gate=1,
        evidence_hash=bytes.fromhex(EVIDENCE[1]),
        signer=ATTESTER,
    )


NOT_ATTESTATIONS: Final[dict[str, tuple[bytes, Pubkey]]] = {
    "malformed": (_gate_1_attestation()[:-1], SAS_PROGRAM_ID),
    "tokenized": (_gate_1_attestation()[:-1] + b"\x01", SAS_PROGRAM_ID),
    "another credential": (_gate_1_attestation(credential=SOMEONE), SAS_PROGRAM_ID),
    "another schema": (_gate_1_attestation(schema=SOMEONE), SAS_PROGRAM_ID),
    "another nonce": (_gate_1_attestation(nonce=gate_nonce(ESCROW, 0)), SAS_PROGRAM_ID),
    "not SAS's": (_gate_1_attestation(), PROGRAM),
    "System's with data": (_gate_1_attestation(), SYSTEM_PROGRAM_ID),
}


@pytest.mark.parametrize(("data", "owner"), NOT_ATTESTATIONS.values(), ids=NOT_ATTESTATIONS.keys())
async def test_an_account_at_the_gate_that_is_not_an_attestation_is_an_error(
    data: bytes, owner: Pubkey
) -> None:
    node = release_node()
    node.accounts[str(gate_address(1))] = (data, owner)

    with pytest.raises(ProviderError) as caught:
        await make_chain(node).gate_attestation(escrow=str(ESCROW), gate=1)

    error = caught.value
    assert (error.provider, error.operation, error.code, error.retryable) == (
        "solana",
        "getAccountInfo",
        "attestation_invalid",
        False,
    )


@pytest.mark.parametrize("gate", [-1, 3])
async def test_a_gate_the_escrow_does_not_have_is_refused(gate: int) -> None:
    with pytest.raises(PaymentRefused, match="no gate"):
        await make_chain(release_node()).gate_attestation(escrow=str(ESCROW), gate=gate)


async def test_an_attestation_needs_an_escrow_of_the_program() -> None:
    node = with_config(FakeNode())
    chain = make_chain(node)

    with pytest.raises(PaymentRefused, match="no escrow"):
        await chain.gate_attestation(escrow=str(ESCROW), gate=0)
    # Another account of the program (its Config) is not an escrow either.
    with pytest.raises(PaymentRefused, match="no escrow"):
        await chain.gate_attestation(escrow=str(config_pda(PROGRAM)), gate=0)


# ---- building a release ----


async def test_build_release_attests_the_gate_and_releases_its_milestone() -> None:
    node = release_node()

    signed = await make_chain(node).build_release(
        ReleaseSpec(str(ESCROW), 1, EVIDENCE[1], attest=True)
    )

    assert_sent_shape(signed)
    _, message = decoded(signed.wire)
    assert signers(message) == [RELAYER.pubkey(), ATTESTER]
    # The relayer pays the fee and the attestation's rent; the attester only signs.
    assert message.header.num_readonly_signed_accounts == 1
    assert compute_budget(message) == (60_000, 1_000)
    assert instructions(message) == [
        compiled(expected_attestation(1)),
        compiled(expected_release(1)),
    ]
    # Gate 1's attestation of PO-1042 with the vector's evidence: the vector's very bytes.
    assert (
        instructions(message)[0][2].hex() == chain_vectors()["gates"][1]["create_attestation_data"]
    )
    assert account_reads(node) == [str(ESCROW), str(CREDENTIAL)]
    assert node.methods()[-1] == "getLatestBlockhash"


async def test_build_release_cranks_against_the_approved_attestation() -> None:
    node = release_node()
    attested(node, 2)

    # The relayer alone signs a crank: the attester's key is not needed.
    signed = await make_chain(node, solana_gate_attester_key=None).build_release(
        ReleaseSpec(str(ESCROW), 2, EVIDENCE[2], attest=False)
    )

    assert_sent_shape(signed)
    _, message = decoded(signed.wire)
    assert signers(message) == [RELAYER.pubkey()]
    assert compute_budget(message) == (40_000, 1_000)
    assert instructions(message) == [compiled(expected_release(2))]
    assert account_reads(node) == [str(ESCROW), str(gate_address(2))]


async def test_a_release_logs_its_kind_and_signature_only(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="bido_back.payments.chain.solana")
    node = release_node()
    attested(node, 2)
    chain = make_chain(node)

    attest = await chain.build_release(ReleaseSpec(str(ESCROW), 1, EVIDENCE[1], attest=True))
    crank = await chain.build_release(ReleaseSpec(str(ESCROW), 2, EVIDENCE[2], attest=False))

    assert [record.getMessage() for record in caplog.records] == [
        f"solana transaction signed kind=release signature={attest.signature}",
        f"solana transaction signed kind=release_crank signature={crank.signature}",
    ]


@pytest.mark.parametrize(
    ("escrow", "milestone", "reason"),
    [
        (escrow_account(status="Created"), 0, "the escrow is created, not funded"),
        (escrow_account(status="Completed"), 0, "the escrow is completed, not funded"),
        (escrow_account(status="Refunded"), 0, "the escrow is refunded, not funded"),
        (escrow_account(released=(0,)), 0, "milestone 0 is released, not pending"),
        (escrow_account(), 3, "no milestone 3"),
        (escrow_account(), -1, "no milestone -1"),
        # The fixture's old plan, T1 and PPAP through one gate: PPAP would be released
        # against T1's evidence.
        (escrow_account(attesters=(ATTESTER, ATTESTER), gates=(0, 1, 1)), 2, "own gate"),
    ],
    ids=["created", "completed", "refunded", "released", "past the end", "negative", "shared gate"],
)
async def test_build_release_refuses_what_must_not_be_paid(
    escrow: bytes, milestone: int, reason: str
) -> None:
    node = release_node(escrow)
    chain = make_chain(node)

    for attest in (True, False):
        with pytest.raises(PaymentRefused, match=reason):
            await chain.build_release(ReleaseSpec(str(ESCROW), milestone, "ab" * 32, attest=attest))
    assert "getLatestBlockhash" not in node.methods()


@pytest.mark.parametrize("evidence", ["AB" * 32, "ab" * 31, "ab" * 33, "", "zz" * 32])
async def test_build_release_needs_an_evidence_hash_of_64_lowercase_hex(evidence: str) -> None:
    node = release_node()
    with pytest.raises(PaymentRefused, match="64 lowercase hex"):
        await make_chain(node).build_release(ReleaseSpec(str(ESCROW), 0, evidence, attest=True))
    assert node.requests == []


async def test_build_release_needs_an_escrow_of_the_program() -> None:
    node = FakeNode()
    chain = make_chain(node)

    with pytest.raises(PaymentRefused, match="no escrow"):
        await chain.build_release(ReleaseSpec(str(ESCROW), 0, EVIDENCE[0], attest=True))
    node.accounts[str(ESCROW)] = (b"", SYSTEM_PROGRAM_ID)  # only lamports
    with pytest.raises(PaymentRefused, match="no escrow"):
        await chain.build_release(ReleaseSpec(str(ESCROW), 0, EVIDENCE[0], attest=True))
    with pytest.raises(PaymentRefused, match="not a Solana address"):
        await chain.build_release(ReleaseSpec("not-an-address", 0, EVIDENCE[0], attest=True))


@pytest.mark.parametrize(
    ("attesters", "authorized", "overrides", "reason"),
    [
        (
            (ATTESTER,) * 3,
            (ATTESTER,),
            {"solana_gate_attester_key": None},
            "SOLANA_GATE_ATTESTER_KEY is not set",
        ),
        (
            (ATTESTER, SOMEONE, ATTESTER),
            (ATTESTER,),
            {},
            "gate 1 of the escrow is pinned to an attester other than SOLANA_GATE_ATTESTER_KEY",
        ),
        (
            (ATTESTER,) * 3,
            (SOMEONE,),
            {},
            "SOLANA_GATE_ATTESTER is not an authorized signer of the escrow's SAS credential",
        ),
    ],
    ids=["no key", "another attester", "not a signer"],
)
async def test_an_attestation_needs_our_attester_and_its_key(
    attesters: tuple[Pubkey, ...],
    authorized: tuple[Pubkey, ...],
    overrides: dict[str, object],
    reason: str,
) -> None:
    node = release_node(escrow_account(attesters=attesters), signers=authorized)
    with pytest.raises(PaymentsNotConfigured, match=reason):
        await make_chain(node, **overrides).build_release(
            ReleaseSpec(str(ESCROW), 1, EVIDENCE[1], attest=True)
        )
    assert "getLatestBlockhash" not in node.methods()


async def test_a_crank_needs_an_attestation() -> None:
    node = release_node()
    with pytest.raises(PaymentRefused, match="gate 2 has no attestation"):
        await make_chain(node).build_release(ReleaseSpec(str(ESCROW), 2, EVIDENCE[2], attest=False))


@pytest.mark.parametrize(
    ("evidence", "signer", "po_hash", "expiry"),
    [
        ("ff" * 32, ATTESTER, PO_HASH, 0),
        (EVIDENCE[2], SOMEONE, PO_HASH, 0),
        (EVIDENCE[2], ATTESTER, b"\x51" * 32, 0),
        (EVIDENCE[2], ATTESTER, PO_HASH, 1_900_000_000),
    ],
    ids=["other evidence", "another signer", "another PO document", "an expiry"],
)
async def test_a_crank_pays_only_against_the_approved_attestation(
    evidence: str, signer: Pubkey, po_hash: bytes, expiry: int
) -> None:
    node = release_node()
    attested(node, 2, evidence=evidence, signer=signer, po_hash=po_hash, expiry=expiry)

    with pytest.raises(PaymentRefused, match="the attestation of gate 2 is not the approved one"):
        await make_chain(node).build_release(ReleaseSpec(str(ESCROW), 2, EVIDENCE[2], attest=False))
    assert "getLatestBlockhash" not in node.methods()


async def test_a_crank_refuses_an_attestation_of_another_gate() -> None:
    node = release_node()
    node.accounts[str(gate_address(2))] = (
        attestation_account(
            nonce=gate_nonce(ESCROW, 2),
            credential=CREDENTIAL,
            schema=SCHEMA,
            po_hash=PO_HASH,
            gate=1,
            evidence_hash=bytes.fromhex(EVIDENCE[2]),
            signer=ATTESTER,
        ),
        SAS_PROGRAM_ID,
    )
    with pytest.raises(PaymentRefused, match="not the approved one"):
        await make_chain(node).build_release(ReleaseSpec(str(ESCROW), 2, EVIDENCE[2], attest=False))


# ---- what a failed release is called ----


@pytest.mark.parametrize(
    ("attest", "err", "code"),
    [
        # In `[CU, CU, CreateAttestation, release]` the escrow's errors are at index 3.
        (True, {"InstructionError": [3, {"Custom": 6024}]}, "MilestoneAlreadyReleased"),
        (True, {"InstructionError": [3, {"Custom": 6020}]}, "NotFunded"),
        (True, {"InstructionError": [3, {"Custom": 6033}]}, "AttesterMismatch"),
        # At index 2 the code is SAS's or System's: never one of the escrow's names.
        (True, {"InstructionError": [2, {"Custom": 0}]}, "InstructionError(2, Custom(0))"),
        (True, {"InstructionError": [2, {"Custom": 5}]}, "InstructionError(2, Custom(5))"),
        (True, {"InstructionError": [2, {"Custom": 6026}]}, "InstructionError(2, Custom(6026))"),
        # In the crank `[CU, CU, release]` they are at index 2.
        (False, {"InstructionError": [2, {"Custom": 6026}]}, "AttestationMissing"),
        (False, {"InstructionError": [2, {"Custom": 6024}]}, "MilestoneAlreadyReleased"),
    ],
)
async def test_a_refused_release_is_named_by_the_failing_program(
    attest: bool, err: Json, code: str
) -> None:
    node = release_node()
    attested(node, 1)
    chain = make_chain(node)
    signed = await chain.build_release(ReleaseSpec(str(ESCROW), 1, EVIDENCE[1], attest=attest))
    node.send_error = err

    with pytest.raises(ProviderError) as caught:
        await chain.send(signed)

    assert (caught.value.code, caught.value.retryable) == (code, False)


@pytest.mark.parametrize(
    ("err", "code"),
    [
        ({"InstructionError": [3, {"Custom": 6024}]}, "MilestoneAlreadyReleased"),
        ({"InstructionError": [2, {"Custom": 6026}]}, "AttestationMissing"),
        ({"InstructionError": [2, {"Custom": 0}]}, "InstructionError(2, Custom(0))"),
        ({"InstructionError": [2, {"Custom": 5}]}, "InstructionError(2, Custom(5))"),
    ],
)
async def test_a_landed_release_failure_is_named(err: Json, code: str) -> None:
    node = FakeNode(statuses=[status_json("confirmed", err)])
    signature = str(Signature.from_bytes(bytes(range(64))))
    assert await make_chain(node).status(signature) == TransactionStatus("failed", code)
