"""The program codec against the golden vectors: IDL, PDAs, instructions and accounts.

`vectors.json` holds what the program itself derived, wrote and accepted in its Rust
tests; here each value is rebuilt with `bido_back.payments.chain` and compared byte
for byte. Keys are the harness's public test seeds.
"""

import hashlib
import struct
from dataclasses import fields, is_dataclass
from enum import StrEnum
from typing import Final, cast

import pytest
from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.token import ID as TOKEN_PROGRAM_ID

from bido_back.payments.chain.borsh import BorshCoder
from bido_back.payments.chain.escrow import (
    CreatePoArgs,
    EscrowStatus,
    MilestoneInput,
    create_po_ix,
    decode_config,
    decode_po_escrow,
    fund_ix,
    milestone_amounts,
    release_ix,
    update_config_ix,
)
from bido_back.payments.chain.hashes import payout_ref_memo
from bido_back.payments.chain.idl import IDL_FILE, load_idl, parse_idl, program_error_names
from bido_back.payments.chain.pdas import (
    config_pda,
    escrow_pda,
    event_authority_pda,
    program_data_address,
    token_account,
)
from bido_back.payments.chain.spl import (
    ASSOCIATED_TOKEN_PROGRAM_ID,
    MEMO_PROGRAM_ID,
    MINT_ACCOUNT_LEN,
    create_ata_idempotent_ix,
    create_mint_account_ix,
    initialize_mint2_ix,
    memo_ix,
    mint_to_checked_ix,
)
from tests.payments.chain.vectors import (
    FUNDED_ESCROW_FILE,
    IDL_SOURCE,
    JsonValue,
    chain_vectors,
    codec_vectors,
    harness_keypair,
    instruction_vector,
)

# The devnet deployment (`solana/deployments/devnet.json`).
DEVNET_PROGRAM: Final = Pubkey.from_string("7992GKVq51ChvsVJQ9GFto2aBozUGuMcuY93SUSxexJw")
DEVNET_PROGRAM_DATA: Final = Pubkey.from_string("G1q6DARqkB9kLDwjJ7Kdpun1pyuhv1EiVhQZEjdb2V1T")
DEVNET_CONFIG: Final = Pubkey.from_string("AmwPNnamKz7TihheU7jiTwvApezJ3VTmCuk3uCegtGjD")
DEVNET_EVENT_AUTHORITY: Final = Pubkey.from_string("pnVMRiecFLrWMcpXZ16ihgL995J9AMTxerTwG7GxfSW")


def pk(base58: str) -> Pubkey:
    return Pubkey.from_string(base58)


def as_json(value: object) -> JsonValue:
    """A decoded value as `vectors.rs` writes it: base58, lowercase hex, enum names."""
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: as_json(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Pubkey):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple | list):
        return [as_json(item) for item in cast("tuple[object, ...] | list[object]", value)]
    if value is None or isinstance(value, bool | int | str):
        return value
    raise TypeError(f"no vector form for {value!r}")


# ---- the IDL ----


def test_the_packaged_idl_is_the_program_idl() -> None:
    """The image has no `solana/`, so the package carries a copy: it must not drift."""
    assert IDL_FILE.read_bytes() == IDL_SOURCE.read_bytes()


def test_every_discriminator_is_anchors_hash() -> None:
    idl = load_idl()
    for name, instruction in idl.instructions.items():
        assert instruction.discriminator == hashlib.sha256(f"global:{name}".encode()).digest()[:8]
    for name, discriminator in idl.accounts.items():
        assert discriminator == hashlib.sha256(f"account:{name}".encode()).digest()[:8]
    for name, discriminator in idl.events.items():
        assert discriminator == hashlib.sha256(f"event:{name}".encode()).digest()[:8]
    assert idl.instructions["create_po"].discriminator.hex() == "46f1413ec788b3f5"
    assert idl.instructions["fund"].discriminator.hex() == "dabc6fdd9871ae07"
    assert idl.instructions["release"].discriminator.hex() == "fdf90fce1c7fc1f1"
    assert idl.instructions["update_config"].discriminator.hex() == "1d9efcbf0a53db63"


def test_the_program_errors_are_numbered_from_6000_in_order() -> None:
    names = program_error_names()
    assert sorted(names) == list(range(6000, 6038))
    assert names[6001] == "UnauthorizedOperator"
    assert names[6004] == "MintNotAllowed"
    assert names[6019] == "AlreadyFunded"
    assert names[6026] == "AttestationMissing"


UNSUPPORTED_IDLS: Final[list[JsonValue]] = [
    [],
    {"address": "x", "instructions": [{"name": "a", "accounts": [{"accounts": []}]}]},
    {"address": "x", "types": [{"name": "T", "type": {"kind": "struct", "fields": ["u8"]}}]},
    {"address": "x", "types": [{"name": "T", "type": {"kind": "alias", "value": "u8"}}]},
]


@pytest.mark.parametrize("idl", UNSUPPORTED_IDLS)
def test_an_idl_shape_the_coder_cannot_handle_fails_at_load(idl: JsonValue) -> None:
    with pytest.raises(ValueError):
        parse_idl(idl)


# ---- PDAs and token accounts ----


def test_the_pdas_and_token_accounts_equal_the_chain_vectors() -> None:
    vectors = chain_vectors()
    inputs = vectors["inputs"]
    program, buyer, mint = pk(inputs["program_id"]), pk(inputs["buyer"]), pk(inputs["mint"])
    escrow, bump = escrow_pda(program, buyer, inputs["chain_po_id"])

    assert config_pda(program) == pk(vectors["config"])
    assert event_authority_pda(program) == pk(vectors["event_authority"])
    assert (escrow, bump) == (pk(vectors["escrow"]), vectors["escrow_bump"])
    assert token_account(escrow, mint) == pk(vectors["vault"])
    assert token_account(pk(inputs["payout"]), mint) == pk(vectors["payout_token"])


def test_the_devnet_deployments_addresses() -> None:
    assert config_pda(DEVNET_PROGRAM) == DEVNET_CONFIG
    assert event_authority_pda(DEVNET_PROGRAM) == DEVNET_EVENT_AUTHORITY
    assert program_data_address(DEVNET_PROGRAM) == DEVNET_PROGRAM_DATA


def test_a_seed_over_32_bytes_is_a_value_error_not_a_panic() -> None:
    buyer = harness_keypair("buyer").pubkey()
    escrow_pda(DEVNET_PROGRAM, buyer, "A" * 27 + "-7KQ2")  # 32 bytes: the limit
    with pytest.raises(ValueError, match="longer than 32 bytes"):
        escrow_pda(DEVNET_PROGRAM, buyer, "A" * 28 + "-7KQ2")


# ---- instructions ----


def create_po_from_inputs() -> Instruction:
    inputs = codec_vectors()["inputs"]
    args = inputs["create_po"]
    return create_po_ix(
        program_id=pk(inputs["program_id"]),
        operator=pk(inputs["operator"]),
        payer=pk(inputs["relayer"]),
        buyer=pk(inputs["buyer"]),
        payout=pk(inputs["payout"]),
        mint=pk(inputs["mint"]),
        credential=pk(inputs["credential"]),
        args=CreatePoArgs(
            chain_po_id=args["chain_po_id"],
            po_hash=bytes.fromhex(args["po_hash"]),
            cad_hash=bytes.fromhex(args["cad_hash"]),
            total=args["total"],
            gate_attesters=tuple(pk(attester) for attester in args["gate_attesters"]),
            milestones=tuple(
                MilestoneInput(name=m["name"], bps=m["bps"], gate=m["gate"])
                for m in args["milestones"]
            ),
            refund_after=args["refund_after"],
        ),
    )


def fund_from_inputs() -> Instruction:
    inputs = codec_vectors()["inputs"]
    buyer, mint = pk(inputs["buyer"]), pk(inputs["mint"])
    escrow = pk(chain_vectors()["escrow"])
    buyer_token = pk(inputs["buyer_token"])
    assert buyer_token == token_account(buyer, mint)
    return fund_ix(
        program_id=pk(inputs["program_id"]),
        buyer=buyer,
        escrow=escrow,
        mint=mint,
        buyer_token=buyer_token,
        vault=token_account(escrow, mint),
    )


def test_create_po_and_fund_equal_the_instructions_the_program_accepted() -> None:
    recorded = codec_vectors()["instructions"]
    assert instruction_vector(create_po_from_inputs()) == recorded["create_po"]
    assert instruction_vector(fund_from_inputs()) == recorded["fund"]
    assert len(create_po_from_inputs().data) == 213


def test_create_po_with_its_payout_memo_and_fund_equal_their_templates() -> None:
    templates = codec_vectors()["templates"]
    memo = memo_ix(payout_ref_memo(codec_vectors()["inputs"]["payout_ref"]))
    assert [instruction_vector(create_po_from_inputs()), instruction_vector(memo)] == templates[
        "create_po"
    ]
    assert [instruction_vector(fund_from_inputs())] == templates["fund"]


def release_from_funded(milestone: int) -> Instruction:
    """`release(milestone)` of PO-1042's funded escrow, every account from its decoded state."""
    state = decode_po_escrow(FUNDED_ESCROW_FILE.read_bytes())
    return release_ix(
        program_id=pk(codec_vectors()["inputs"]["program_id"]),
        escrow=pk(chain_vectors()["escrow"]),
        vault=state.vault,
        payout_token=state.payout_token,
        mint=state.mint,
        attestation=state.gates[state.milestones[milestone].gate].attestation,
        milestone=milestone,
    )


def test_release_equals_the_instructions_the_program_accepted() -> None:
    recorded = codec_vectors()["instructions"]["release"]
    assert [instruction_vector(release_from_funded(index)) for index in range(3)] == recorded
    # Permissionless: no account signs; `#[event_cpi]` adds the event authority and program.
    release = release_from_funded(0)
    assert not any(account.is_signer for account in release.accounts)
    assert [account.pubkey for account in release.accounts][-2:] == [
        DEVNET_EVENT_AUTHORITY,
        DEVNET_PROGRAM,
    ]
    assert bytes(release.data) == bytes.fromhex("fdf90fce1c7fc1f1") + b"\x00"


def test_the_release_crank_equals_its_template() -> None:
    crank = [instruction_vector(release_from_funded(index)) for index in (1, 2)]
    assert crank == codec_vectors()["templates"]["release_attempt"]


@pytest.mark.parametrize("milestone", [-1, 256])
def test_a_release_index_is_a_u8(milestone: int) -> None:
    key = harness_keypair("x").pubkey()
    with pytest.raises(ValueError, match="u8"):
        release_ix(
            program_id=DEVNET_PROGRAM,
            escrow=key,
            vault=key,
            payout_token=key,
            mint=key,
            attestation=key,
            milestone=milestone,
        )


def test_update_config_replacing_the_allowed_mints() -> None:
    config = decode_config(bytes.fromhex(codec_vectors()["accounts"]["config"]["data"]))
    admin = harness_keypair("admin").pubkey()
    kept, new_mint = config.allowed_mints[0], harness_keypair("bido-usd").pubkey()

    ix = update_config_ix(
        program_id=DEVNET_PROGRAM, admin=admin, current=config, allowed_mints=(kept, new_mint)
    )

    # The research's worked example: `1d9efcbf0a53db63 00 01 <slot 0> <slot 1> 00 00`.
    assert bytes(ix.data) == (
        bytes.fromhex("1d9efcbf0a53db63")
        + b"\x00\x01"
        + bytes(kept)
        + bytes(new_mint)
        + b"\x00\x00"
    )
    assert len(ix.data) == 76
    assert ix.program_id == DEVNET_PROGRAM
    assert ix.accounts == [
        AccountMeta(admin, is_signer=True, is_writable=False),
        AccountMeta(DEVNET_CONFIG, is_signer=False, is_writable=True),
        AccountMeta(DEVNET_PROGRAM, is_signer=False, is_writable=False),
        AccountMeta(DEVNET_PROGRAM_DATA, is_signer=False, is_writable=False),
        AccountMeta(config.sas_credential, is_signer=False, is_writable=False),
        AccountMeta(config.sas_schema, is_signer=False, is_writable=False),
    ]


def test_update_config_passes_the_sas_accounts_the_config_will_hold() -> None:
    config = decode_config(bytes.fromhex(codec_vectors()["accounts"]["config"]["data"]))
    credential = harness_keypair("other-credential").pubkey()
    ix = update_config_ix(
        program_id=DEVNET_PROGRAM,
        admin=harness_keypair("admin").pubkey(),
        current=config,
        sas_credential=credential,
    )
    assert ix.accounts[4].pubkey == credential
    assert ix.accounts[5].pubkey == config.sas_schema
    assert bytes(ix.data)[8:] == b"\x00\x00\x01" + bytes(credential) + b"\x00"


def test_the_milestone_split_is_the_programs() -> None:
    assert milestone_amounts(1_200_000_000_000, [3000, 3000, 4000]) == [
        360_000_000_000,
        360_000_000_000,
        480_000_000_000,
    ]
    assert milestone_amounts(1_000_001, [3333, 3333, 3334]) == [333_300, 333_300, 333_401]
    assert milestone_amounts(3, [3000, 3000, 4000]) == [0, 0, 3]
    assert milestone_amounts(5_000_000, [10_000]) == [5_000_000]


# ---- accounts ----


def test_config_decodes_to_its_fields_and_re_encodes() -> None:
    account = codec_vectors()["accounts"]["config"]
    data = bytes.fromhex(account["data"])
    assert as_json(decode_config(data)) == account["fields"]
    coder = BorshCoder(load_idl())
    assert coder.encode_account("Config", coder.decode_account("Config", data)) == data


@pytest.mark.parametrize("status", list(EscrowStatus))
def test_po_escrow_decodes_to_its_fields_in_every_status(status: EscrowStatus) -> None:
    account = codec_vectors()["accounts"]["po_escrow"][status.value]
    data = bytes.fromhex(account["data"])
    decoded = decode_po_escrow(data)
    assert decoded.status is status
    assert as_json(decoded) == account["fields"]
    coder = BorshCoder(load_idl())
    encoded = coder.encode_account("PoEscrow", coder.decode_account("PoEscrow", data))
    assert data == encoded + bytes(len(data) - len(encoded))


def test_the_funded_fixture_file_decodes() -> None:
    data = FUNDED_ESCROW_FILE.read_bytes()
    assert len(data) == 1070
    funded = decode_po_escrow(data)
    assert funded.status is EscrowStatus.FUNDED
    assert (funded.chain_po_id, funded.total, funded.released) == (
        "PO-1042-7KQ2",
        1_200_000_000_000,
        0,
    )
    assert funded.buyer == pk(chain_vectors()["inputs"]["buyer"])
    assert funded.vault == pk(chain_vectors()["vault"])
    assert [m.amount for m in funded.milestones] == milestone_amounts(
        funded.total, [m.bps for m in funded.milestones]
    )
    assert data == bytes.fromhex(codec_vectors()["accounts"]["po_escrow"]["Funded"]["data"])


def test_data_that_is_not_the_account_is_refused() -> None:
    config = bytes.fromhex(codec_vectors()["accounts"]["config"]["data"])
    with pytest.raises(ValueError, match="discriminator"):
        decode_po_escrow(config)
    with pytest.raises(ValueError, match="data ends"):
        decode_po_escrow(FUNDED_ESCROW_FILE.read_bytes()[:100])


# ---- SPL Token, Associated Token Account, System and Memo ----


def test_mint_to_checked_layout() -> None:
    mint, owner = harness_keypair("tusd-mint").pubkey(), harness_keypair("buyer").pubkey()
    authority = harness_keypair("mint-authority").pubkey()
    destination = token_account(owner, mint)
    ix = mint_to_checked_ix(
        mint=mint, destination=destination, authority=authority, amount=1_000_000, decimals=6
    )
    assert ix.program_id == TOKEN_PROGRAM_ID
    assert bytes(ix.data).hex() == "0e40420f000000000006"  # 1 token of 6 decimals
    assert ix.accounts == [
        AccountMeta(mint, is_signer=False, is_writable=True),
        AccountMeta(destination, is_signer=False, is_writable=True),
        AccountMeta(authority, is_signer=True, is_writable=False),
    ]


@pytest.mark.parametrize(("amount", "decimals"), [(2**64, 6), (-1, 6), (1, 256), (1, -1)])
def test_mint_to_checked_bounds(amount: int, decimals: int) -> None:
    key = harness_keypair("x").pubkey()
    with pytest.raises(ValueError, match="must be a u"):
        mint_to_checked_ix(
            mint=key, destination=key, authority=key, amount=amount, decimals=decimals
        )


def test_create_ata_idempotent_layout() -> None:
    vectors = chain_vectors()
    escrow, mint = pk(vectors["escrow"]), pk(vectors["inputs"]["mint"])
    relayer = harness_keypair("relayer").pubkey()
    ix = create_ata_idempotent_ix(payer=relayer, owner=escrow, mint=mint)
    assert ix.program_id == ASSOCIATED_TOKEN_PROGRAM_ID
    assert bytes(ix.data) == b"\x01"
    assert ix.accounts == [
        AccountMeta(relayer, is_signer=True, is_writable=True),
        AccountMeta(pk(vectors["vault"]), is_signer=False, is_writable=True),
        AccountMeta(escrow, is_signer=False, is_writable=False),
        AccountMeta(mint, is_signer=False, is_writable=False),
        AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(TOKEN_PROGRAM_ID, is_signer=False, is_writable=False),
    ]


def test_create_mint_account_and_initialize_mint2_layouts() -> None:
    payer, mint = harness_keypair("relayer").pubkey(), harness_keypair("bido-usd").pubkey()
    authority = harness_keypair("mint-authority").pubkey()

    create = create_mint_account_ix(payer=payer, mint=mint, lamports=1_461_600)
    initialize = initialize_mint2_ix(
        mint=mint, decimals=6, mint_authority=authority, freeze_authority=None
    )

    # System `CreateAccount`: u32 tag 0, u64 lamports, u64 space, owner.
    assert create.program_id == SYSTEM_PROGRAM_ID
    assert bytes(create.data) == (
        struct.pack("<IQQ", 0, 1_461_600, MINT_ACCOUNT_LEN) + bytes(TOKEN_PROGRAM_ID)
    )
    assert create.accounts == [
        AccountMeta(payer, is_signer=True, is_writable=True),
        AccountMeta(mint, is_signer=True, is_writable=True),
    ]
    assert initialize.program_id == TOKEN_PROGRAM_ID
    assert bytes(initialize.data) == bytes([20, 6]) + bytes(authority) + b"\x00"
    assert initialize.accounts == [AccountMeta(mint, is_signer=False, is_writable=True)]
    frozen = initialize_mint2_ix(
        mint=mint, decimals=6, mint_authority=authority, freeze_authority=payer
    )
    assert bytes(frozen.data)[-33:] == b"\x01" + bytes(payer)


def test_memo_layout() -> None:
    ix = memo_ix("bido:payment:00000000-0000-4000-8000-000000000001")
    assert ix.program_id == MEMO_PROGRAM_ID
    assert ix.accounts == []
    assert bytes(ix.data) == b"bido:payment:00000000-0000-4000-8000-000000000001"
    with pytest.raises(ValueError):
        memo_ix("")
