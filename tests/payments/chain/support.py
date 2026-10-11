"""Settings, keys, a stand-in for the buyer's wallet and SAS accounts, shared by the chain
tests.

Every key is one of the escrow harness's public test seeds: none can ever hold value.
"""

import struct
from collections.abc import Sequence
from typing import Final

from solders.keypair import Keypair
from solders.message import MessageV0, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from bido_back.payments.chain.sas import encode_gate_approval_data
from bido_back.settings import Settings
from tests.payments.chain.vectors import codec_vectors, harness_keypair

RPC_URL: Final = "https://devnet.rpc.invalid/?api-key=sk-not-a-real-key-77c1"
DEVNET_GENESIS: Final = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"
PROGRAM: Final = Pubkey.from_string(codec_vectors()["inputs"]["program_id"])
RELAYER: Final = harness_keypair("relayer")
OPERATOR: Final = harness_keypair("operator")
MINT_AUTHORITY: Final = harness_keypair("mint-authority")
# The company's wallet: in production a Privy server wallet that holds no SOL.
BUYER: Final = harness_keypair("buyer")
MINT: Final = harness_keypair("tusd-mint").pubkey()
PAYOUT: Final = harness_keypair("payout-owner").pubkey()
# Attests every gate: Bido holds its secret key to sign each approved milestone.
GATE_ATTESTER: Final = harness_keypair("gate-attester")
ATTESTER: Final = GATE_ATTESTER.pubkey()
SAS_CREDENTIAL_NAME: Final = b"bido-approvals"


def settings(**overrides: object) -> Settings:
    """Settings with every SOLANA_* value set; `overrides` by field name."""
    values: dict[str, object] = {
        "environment": "test",
        "database_url": "postgresql://app@localhost:5432/unused",
        "solana_rpc_url": RPC_URL,
        "solana_escrow_program_id": str(PROGRAM),
        "solana_stablecoin_mint": str(MINT),
        "solana_stablecoin_decimals": 6,
        "solana_mint_authority_key": str(MINT_AUTHORITY),
        "solana_operator_key": str(OPERATOR),
        "solana_relayer_key": str(RELAYER),
        "solana_payout_owner": str(PAYOUT),
        "solana_gate_attester": str(ATTESTER),
        "solana_gate_attester_key": str(GATE_ATTESTER),
        "solana_priority_fee_micro_lamports": 1_000,
    }
    return Settings.model_validate(values | overrides)


def decoded(wire: bytes) -> tuple[VersionedTransaction, MessageV0]:
    tx = VersionedTransaction.from_bytes(wire)
    message = tx.message
    assert isinstance(message, MessageV0)
    return tx, message


def signers(message: MessageV0) -> list[Pubkey]:
    return message.account_keys[: message.header.num_required_signatures]


def wallet_sign(wire: bytes, wallet: Keypair) -> bytes:
    """What the wallet provider returns: the same transaction with the wallet's signature."""
    tx, message = decoded(wire)
    signatures = list(tx.signatures)
    signatures[signers(message).index(wallet.pubkey())] = wallet.sign_message(
        to_bytes_versioned(message)
    )
    return bytes(VersionedTransaction.populate(message, signatures))


def credential_account(signers: Sequence[Pubkey], *, name: bytes = SAS_CREDENTIAL_NAME) -> bytes:
    """A SAS `Credential` account: `[0] ‖ authority ‖ u32 len ‖ name ‖ u32 count ‖ signers`."""
    authority = harness_keypair("sas-issuer").pubkey()
    return b"".join(
        [b"\x00", bytes(authority), struct.pack("<I", len(name)), name]
        + [struct.pack("<I", len(signers)), *(bytes(signer) for signer in signers)]
    )


def attestation_account(
    *,
    nonce: Pubkey,
    credential: Pubkey,
    schema: Pubkey,
    po_hash: bytes,
    gate: int,
    evidence_hash: bytes,
    signer: Pubkey,
    expiry: int = 0,
) -> bytes:
    """The 246 bytes SAS writes for a gate attestation: `[2] ‖ nonce ‖ credential ‖ schema ‖
    u32 73 ‖ data ‖ signer ‖ i64 expiry ‖ token account (zero)`."""
    data = encode_gate_approval_data(po_hash, gate, evidence_hash)
    return b"".join(
        [b"\x02", bytes(nonce), bytes(credential), bytes(schema), struct.pack("<I", len(data))]
        + [data, bytes(signer), struct.pack("<q", expiry), bytes(32)]
    )
