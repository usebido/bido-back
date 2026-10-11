"""`SolanaPaymentChain`: the `PaymentChain` of `bido_back.payments.ports` on Solana.

A payment sends three transactions, then one per milestone. The relayer pays every
fee and every rent, and each is one v0 message with a fixed compute budget:

- mint: `[CU, CU, CreateIdempotent(owner's token account), MintToChecked, Memo]`,
  signed by the relayer and Bido's mint authority;
- create escrow: `[CU, CU, create_po, Memo("bido:payout_ref:<hex>")]`, signed by the
  relayer and the operator. Milestone i is released through gate i, every gate is
  attested by `SOLANA_GATE_ATTESTER`, and releases pay `SOLANA_PAYOUT_OWNER`. The
  attester must be an authorized signer of the SAS credential, checked here first:
  the devnet build of `create_po` does not, and an escrow pinned to an attester SAS
  refuses could be funded but never released;
- fund: `[CU, CU, fund]`, signed by the company's wallet (remotely: `build_fund`
  zeroes every signature slot, `complete_fund` checks the wallet's answer) and the
  relayer;
- release of milestone i, once a person approved it: `[CU, CU, CreateAttestation(gate
  i), release(i)]`, signed by the relayer (it also pays the attestation's rent) and the
  gate attester (`SOLANA_GATE_ATTESTER_KEY`). Both land or neither does, so an
  attestation without its release exists only if one was written apart; then the crank
  `[CU, CU, release(i)]`, signed by the relayer, releases against it.

Nothing here retries, waits or decides: the pipeline records each signature before
`send`, then asks `status` and `block_height` and reads `escrow` (and, for a release,
`gate_attestation`). Exactly-once rests on three answers:

- `status` searches the node's whole history (`searchTransactionHistory`). A landed
  transaction is `confirmed` or `failed` only once its status is confirmed or
  finalized; anything else is `unknown`.
- `block_height` is the FINALIZED block height, read with its slot (`getEpochInfo`).
- `status` refuses to answer `unknown` (`ProviderError(..., "node_behind",
  retryable=True)`) from a node that answers behind a slot this client has seen
  finalized, or that still has the transaction only `processed` at or below that
  slot: from such a node, "not found" proves nothing.

So `unknown` after `block_height() > last_valid_block_height` means an up-to-date node
does not have the transaction, and it can no longer land.

A failed release is named by `describe_transaction_error`: the escrow's errors by name
(`MilestoneAlreadyReleased`, `AttestationMissing`, `NotFunded`, ...). At the
`CreateAttestation` index (2) the code is SAS's or the System program's and numbers
collide: `InstructionError(2, Custom(0))` is an attestation that already exists,
`InstructionError(2, Custom(5))` an attester SAS does not authorize. Decide from the
chain (`escrow`, `gate_attestation`), never from those codes.
"""

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Self
from urllib.parse import urlsplit

import httpx
from solders.hash import Hash
from solders.instruction import Instruction
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.system_program import ID as SYSTEM_PROGRAM_ID

from bido_back.payments import ports
from bido_back.payments.chain.escrow import (
    BPS_TOTAL,
    MAX_GATES,
    MAX_MILESTONE_NAME_BYTES,
    MAX_MILESTONES,
    CreatePoArgs,
    EscrowStatus,
    Milestone,
    MilestoneInput,
    MilestoneStatus,
    PoEscrowAccount,
    create_po_ix,
    decode_config,
    decode_po_escrow,
    fund_ix,
    milestone_amounts,
    release_ix,
)
from bido_back.payments.chain.hashes import payout_ref_memo
from bido_back.payments.chain.keys import HotKey, load_hot_key
from bido_back.payments.chain.pdas import (
    MAX_SEED_LEN,
    SAS_PROGRAM_ID,
    attestation_pda,
    config_pda,
    escrow_pda,
    gate_nonce,
    token_account,
)
from bido_back.payments.chain.rpc import (
    PROVIDER,
    LatestBlockhash,
    PreflightFailed,
    RpcClient,
    SignatureStatus,
)
from bido_back.payments.chain.sas import (
    GateAttestationAccount,
    create_attestation_ix,
    credential_signers,
    decode_gate_attestation,
    encode_gate_approval_data,
)
from bido_back.payments.chain.spl import create_ata_idempotent_ix, memo_ix, mint_to_checked_ix
from bido_back.payments.chain.transactions import (
    CompiledMessage,
    add_remote_signature,
    compile_message,
    compiled_from_wire,
    describe_transaction_error,
    sign_transaction,
    unsigned_transaction,
)
from bido_back.payments.errors import PaymentRefused, PaymentsNotConfigured, ProviderError
from bido_back.payments.ports import (
    EscrowMilestoneState,
    EscrowSpec,
    EscrowState,
    GateAttestation,
    ReleaseSpec,
    SignedTransaction,
    TransactionStatus,
    UnsignedTransaction,
)
from bido_back.settings import Settings

logger = logging.getLogger(__name__)

# Compute-unit limits (requested, so they set the priority fee). The program used about
# 20k (mint, creating the token account), 97k (create_po, creating both token accounts)
# and 15k (fund) in LiteSVM (`tests/payments/chain/test_litesvm.py`).
MINT_COMPUTE_UNITS: Final = 60_000
CREATE_ESCROW_COMPUTE_UNITS: Final = 250_000
FUND_COMPUTE_UNITS: Final = 60_000
# A release with its attestation (815 bytes) measured 22,814 on devnet (`solana/
# deployments/devnet.json`) and 23.5k to 33.1k in LiteSVM: the last milestone also
# completes the escrow, and SAS's search for the attestation's PDA bump varies by
# escrow. The release alone (464 bytes): 16,359 on devnet, 16.9k to 20.0k in LiteSVM.
# Both limits keep more than 1.3 times the highest (60 and 40 lamports of priority fee
# at 1,000 micro-lamports per unit).
RELEASE_ATTEST_COMPUTE_UNITS: Final = 60_000
RELEASE_COMPUTE_UNITS: Final = 40_000
# Payments run on devnet only; a node of this genesis is mainnet-beta.
MAINNET_GENESIS_HASH: Final = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"

_U64_MAX: Final = 2**64 - 1
_I64_MAX: Final = 2**63 - 1
_HASH_LEN: Final = 32
_HEX64: Final = re.compile(r"[0-9a-f]{64}")
_MIN_CHAIN_PO_ID_LEN: Final = 6
# `create_po` check 3: the id is a raw PDA seed of `[A-Z0-9-]`.
_CHAIN_PO_ID_CHARS: Final = re.compile(r"[A-Z0-9-]+")
_CONFIRMED: Final = frozenset({"confirmed", "finalized"})
# Preflight failures that may pass on another try of the same bytes (a node behind).
_RETRYABLE_PREFLIGHT: Final = frozenset({"BlockhashNotFound"})
_ESCROW_STATUS: Final[dict[EscrowStatus, ports.EscrowStatus]] = {
    EscrowStatus.CREATED: "created",
    EscrowStatus.FUNDED: "funded",
    EscrowStatus.COMPLETED: "completed",
    EscrowStatus.REFUNDED: "refunded",
}
_MILESTONE_STATUS: Final[dict[MilestoneStatus, ports.EscrowMilestoneStatus]] = {
    MilestoneStatus.PENDING: "pending",
    MilestoneStatus.RELEASED: "released",
    MilestoneStatus.CANCELLED: "cancelled",
}
_LOCAL_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True, slots=True)
class ChainSettings:
    """The public chain settings, parsed (`from_settings` names any bad one)."""

    cluster: str
    program_id: Pubkey
    mint: Pubkey
    decimals: int
    payout_owner: Pubkey
    gate_attester: Pubkey
    compute_unit_price: int

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        payout_owner = require_pubkey(settings.solana_payout_owner, "SOLANA_PAYOUT_OWNER")
        gate_attester = require_pubkey(settings.solana_gate_attester, "SOLANA_GATE_ATTESTER")
        if gate_attester == payout_owner:
            # `create_po` check 9: an attester may not be the payout.
            raise PaymentsNotConfigured("SOLANA_GATE_ATTESTER must differ from SOLANA_PAYOUT_OWNER")
        return cls(
            cluster=settings.solana_cluster,
            program_id=require_pubkey(
                settings.solana_escrow_program_id, "SOLANA_ESCROW_PROGRAM_ID"
            ),
            mint=require_pubkey(settings.solana_stablecoin_mint, "SOLANA_STABLECOIN_MINT"),
            decimals=settings.solana_stablecoin_decimals,
            payout_owner=payout_owner,
            gate_attester=gate_attester,
            compute_unit_price=settings.solana_priority_fee_micro_lamports,
        )


def require_pubkey(value: str | None, setting: str) -> Pubkey:
    """Setting `setting` as a public key other than the default (all zeros)."""
    if not value:
        raise PaymentsNotConfigured(f"{setting} is not set")
    key: Pubkey | None
    try:
        key = Pubkey.from_string(value.strip())
    except ValueError:
        key = None
    if key is None or key == Pubkey.default():
        raise PaymentsNotConfigured(f"{setting} is not a Solana address")
    return key


def require_rpc_url(settings: Settings) -> str:
    """`SOLANA_RPC_URL`: https, or http to this machine. Errors never show the URL."""
    url = settings.solana_rpc_url
    if not url:
        raise PaymentsNotConfigured("SOLANA_RPC_URL is not set")
    scheme: str | None = None
    host: str | None = None
    try:
        parts = urlsplit(url)
        scheme, host = parts.scheme, parts.hostname
    except ValueError:
        scheme = None
    if not host or scheme not in ("https", "http"):
        raise PaymentsNotConfigured("SOLANA_RPC_URL is not an http(s) URL")
    if scheme == "http" and host not in _LOCAL_HOSTS:
        raise PaymentsNotConfigured("SOLANA_RPC_URL must use https")
    return url


class SolanaPaymentChain:
    """Builds, signs, sends and reads a payment's transactions (see the module docstring)."""

    def __init__(
        self,
        *,
        settings: ChainSettings,
        rpc: RpcClient,
        relayer: HotKey,
        operator: HotKey,
        mint_authority: HotKey,
        gate_attester: HotKey | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """`gate_attester` signs milestone attestations; without it everything else works
        and `build_release` refuses to attest."""
        if gate_attester is not None and gate_attester.pubkey() != settings.gate_attester:
            raise PaymentsNotConfigured(
                "SOLANA_GATE_ATTESTER_KEY is not the key of SOLANA_GATE_ATTESTER"
            )
        self._settings = settings
        self._rpc = rpc
        self._relayer = relayer
        self._operator = operator
        self._mint_authority = mint_authority
        self._gate_attester = gate_attester
        self._clock = clock
        # `Config.sas_credential`, read once (create_po passes it) once the gate attester
        # is one of its signers.
        self._credential: Pubkey | None = None
        # The highest finalized slot `block_height` has seen (see the module docstring).
        self._finalized_slot = 0
        self._not_mainnet = False

    @classmethod
    def from_settings(cls, settings: Settings, *, http: httpx.AsyncClient | None = None) -> Self:
        """Every setting parsed and every key loaded, or `PaymentsNotConfigured`.

        `SOLANA_GATE_ATTESTER_KEY` is optional; when set it must be the secret key of
        `SOLANA_GATE_ATTESTER`.
        """
        chain = ChainSettings.from_settings(settings)
        url = require_rpc_url(settings)
        relayer = load_hot_key(
            settings.solana_relayer_key, setting="SOLANA_RELAYER_KEY", role="relayer"
        )
        operator = load_hot_key(
            settings.solana_operator_key, setting="SOLANA_OPERATOR_KEY", role="operator"
        )
        mint_authority = load_hot_key(
            settings.solana_mint_authority_key,
            setting="SOLANA_MINT_AUTHORITY_KEY",
            role="mint_authority",
        )
        gate_attester: HotKey | None = None
        if settings.solana_gate_attester_key:
            gate_attester = load_hot_key(
                settings.solana_gate_attester_key,
                setting="SOLANA_GATE_ATTESTER_KEY",
                role="gate_attester",
            )
        return cls(
            settings=chain,
            rpc=RpcClient(url, http=http),
            relayer=relayer,
            operator=operator,
            mint_authority=mint_authority,
            gate_attester=gate_attester,
        )

    async def aclose(self) -> None:
        await self._rpc.aclose()

    @property
    def cluster(self) -> str:
        return self._settings.cluster

    @property
    def mint(self) -> str:
        return str(self._settings.mint)

    def escrow_address(self, *, buyer: str, chain_po_id: str) -> str:
        _check_chain_po_id(chain_po_id)
        return str(escrow_pda(self._settings.program_id, _address(buyer, "buyer"), chain_po_id)[0])

    def vault_address(self, *, escrow: str) -> str:
        return str(token_account(_address(escrow, "escrow"), self._settings.mint))

    async def build_mint(self, *, owner: str, amount: int, memo: str) -> SignedTransaction:
        """Mints `amount` base units into `owner`'s token account, creating it if needed.

        The mint is not idempotent on chain: only the pipeline's signature and expiry
        rule keeps a second one from being built while the first can still land.
        """
        owner_key = _address(owner, "owner")
        if not 0 < amount <= _U64_MAX:
            raise PaymentRefused("the mint amount must be a positive u64")
        if not memo:
            raise PaymentRefused("the mint needs a memo")
        mint, relayer = self._settings.mint, self._relayer.pubkey()
        authority = self._mint_authority.pubkey()
        latest = await self._latest_blockhash()
        compiled = compile_message(
            payer=relayer,
            instructions=[
                create_ata_idempotent_ix(payer=relayer, owner=owner_key, mint=mint),
                mint_to_checked_ix(
                    mint=mint,
                    destination=token_account(owner_key, mint),
                    authority=authority,
                    amount=amount,
                    decimals=self._settings.decimals,
                ),
                memo_ix(memo),
            ],
            signers={relayer, authority},
            blockhash=latest.blockhash,
            last_valid_block_height=latest.last_valid_block_height,
            compute_unit_limit=MINT_COMPUTE_UNITS,
            compute_unit_price=self._settings.compute_unit_price,
        )
        return self._signed("mint", compiled, (self._relayer, self._mint_authority))

    async def build_create_escrow(self, spec: EscrowSpec) -> SignedTransaction:
        """`create_po` of `spec` and its payout reference memo.

        `PaymentRefused` when the program would refuse the terms; `PaymentsNotConfigured`
        when its `Config` does not allow our operator or our mint.
        """
        buyer, args = self._create_po_args(spec)
        memo = memo_ix(_payout_memo(spec.payout_ref))
        credential = await self._sas_credential()
        relayer, operator = self._relayer.pubkey(), self._operator.pubkey()
        latest = await self._latest_blockhash()
        create = create_po_ix(
            program_id=self._settings.program_id,
            operator=operator,
            payer=relayer,
            buyer=buyer,
            payout=self._settings.payout_owner,
            mint=self._settings.mint,
            credential=credential,
            args=args,
        )
        compiled = compile_message(
            payer=relayer,
            instructions=[create, memo],
            signers={relayer, operator},
            blockhash=latest.blockhash,
            last_valid_block_height=latest.last_valid_block_height,
            compute_unit_limit=CREATE_ESCROW_COMPUTE_UNITS,
            compute_unit_price=self._settings.compute_unit_price,
        )
        return self._signed("create_escrow", compiled, (self._relayer, self._operator))

    async def build_fund(self, *, buyer: str, escrow: str) -> UnsignedTransaction:
        """`fund` of `escrow` by `buyer`, every signature slot zeroed for the buyer's wallet."""
        buyer_key, escrow_key = _address(buyer, "buyer"), _address(escrow, "escrow")
        if buyer_key == self._relayer.pubkey():
            raise PaymentRefused("the buyer wallet cannot be the relayer")
        latest = await self._latest_blockhash()
        compiled = self._fund_message(
            buyer_key, escrow_key, latest.blockhash, latest.last_valid_block_height
        )
        return unsigned_transaction(compiled)

    def complete_fund(
        self, unsigned: UnsignedTransaction, *, buyer: str, signed_by_buyer: bytes
    ) -> SignedTransaction:
        """The buyer's signature from `signed_by_buyer`, then the relayer's.

        The relayer signs only the exact `fund` message `build_fund` makes (rebuilt here
        from the buyer, the escrow and the blockhash), and only once the wallet returned
        that message unchanged with a signature that verifies (else `ProviderError`).
        """
        buyer_key = _address(buyer, "buyer")
        ours = compiled_from_wire(unsigned.wire, unsigned.last_valid_block_height)
        escrow = _fund_escrow(ours.message, self._settings.program_id)
        expected = self._fund_message(
            buyer_key, escrow, ours.message.recent_blockhash, unsigned.last_valid_block_height
        )
        if expected.message_bytes != ours.message_bytes:
            raise ValueError("not a fund transaction built by this chain for this buyer")
        signed = add_remote_signature(
            expected, remote=buyer_key, remote_signed=signed_by_buyer, local=(self._relayer,)
        )
        logger.info("solana transaction signed kind=fund signature=%s", signed.signature)
        return signed

    async def send(self, transaction: SignedTransaction) -> None:
        """Sends the bytes once, preflight at `confirmed`, no retries by the node.

        A preflight failure means the node did not forward them: `ProviderError` whose
        code names the failure (the escrow program's error name, `BlockhashNotFound`,
        ...). `AlreadyProcessed` is not a failure: the bytes landed before.
        """
        await self._refuse_mainnet()
        try:
            sent = await self._rpc.send_transaction(transaction.wire)
        except PreflightFailed as failure:
            code = describe_transaction_error(
                failure.transaction_error,
                program_id=self._settings.program_id,
                message=_message_of(transaction.wire),
            )
        else:
            if str(sent) != transaction.signature:
                raise ProviderError(PROVIDER, "sendTransaction", "invalid_response", retryable=True)
            return
        if code == "AlreadyProcessed":
            return
        logger.warning("solana preflight refused signature=%s code=%s", transaction.signature, code)
        raise ProviderError(
            PROVIDER, "sendTransaction", code, retryable=code in _RETRYABLE_PREFLIGHT
        )

    async def status(self, signature: str) -> TransactionStatus:
        sig = _signature(signature)
        result = await self._rpc.get_signature_statuses([sig])
        found = result.statuses[0]
        if found is not None and _is_confirmed(found):
            if found.err is None:
                return TransactionStatus("confirmed")
            error = describe_transaction_error(found.err, program_id=self._settings.program_id)
            return TransactionStatus("failed", error)
        behind = result.context_slot < self._finalized_slot
        if behind or (found is not None and found.slot <= self._finalized_slot):
            raise ProviderError(PROVIDER, "getSignatureStatuses", "node_behind", retryable=True)
        return TransactionStatus("unknown")

    async def block_height(self) -> int:
        """The finalized block height (expiry is decided against finality, not `confirmed`)."""
        position = await self._rpc.get_epoch_info("finalized")
        self._finalized_slot = max(self._finalized_slot, position.slot)
        return position.block_height

    async def escrow(self, address: str) -> EscrowState | None:
        """The escrow at `address` (read at `confirmed`) with its milestones; None if no
        escrow exists there."""
        key = _address(address, "escrow")
        state = await self._escrow_account(key)
        if state is None:
            return None
        return EscrowState(
            address=str(key),
            status=_ESCROW_STATUS[state.status],
            chain_po_id=state.chain_po_id,
            buyer=str(state.buyer),
            mint=str(state.mint),
            total=state.total,
            vault=str(state.vault),
            released=state.released,
            po_hash=state.po_hash.hex(),
            milestones=tuple(
                _milestone_state(index, milestone)
                for index, milestone in enumerate(state.milestones)
            ),
        )

    async def gate_attestation(self, *, escrow: str, gate: int) -> GateAttestation | None:
        """The SAS attestation of the escrow's `gate` (read at `confirmed`); None while none
        exists.

        Its address is fixed by the escrow's credential, schema and the gate's nonce
        (`gates[gate].attestation`). A System-owned account there without data is only
        lamports someone sent: no attestation, and SAS still creates one there.
        `ProviderError("solana", "getAccountInfo", "attestation_invalid")` (not
        retryable) when the account there is not an attestation of the gate schema under
        the escrow's credential; `PaymentRefused` when no escrow of the program is at
        `escrow` or it has no such gate. The signer, PO hash, gate and evidence are
        returned as found: whether it is ours is the caller's decision.
        """
        key, state = await self._existing_escrow(escrow)
        if not 0 <= gate < len(state.gates):
            raise PaymentRefused(f"the escrow has no gate {gate}")
        attestation = await self._attestation(key, state, gate)
        if attestation is None:
            return None
        return GateAttestation(
            address=str(state.gates[gate].attestation),
            signer=str(attestation.signer),
            po_hash=attestation.po_hash.hex(),
            gate=attestation.gate,
            evidence_hash=attestation.evidence_hash.hex(),
        )

    async def build_release(self, spec: ReleaseSpec) -> SignedTransaction:
        """Release one milestone to the escrow's fixed payout.

        With `spec.attest`: `[CU, CU, CreateAttestation(gate), release(milestone)]`, the
        attestation carrying `spec.evidence_hash` and the escrow's PO hash, signed by the
        relayer (fee payer, the attestation's rent) and the gate attester. Without: the
        crank `[CU, CU, release(milestone)]`, signed by the relayer, against the gate's
        existing attestation.

        Built from the escrow read at `confirmed` (its credential, schema, PO hash, vault,
        payout token and mint; never a copy). `PaymentRefused` unless the evidence hash is
        64 lowercase hex, an escrow of the program is there and funded, and the milestone
        exists, is pending and has its own gate (Bido's plan: gate = index, one approved
        evidence per attestation). With `attest`: `PaymentsNotConfigured` without
        SOLANA_GATE_ATTESTER_KEY, when the gate is pinned to another attester, or when
        the attester is not an authorized signer of the escrow's SAS credential (SAS
        would refuse it). Without `attest`: `PaymentRefused` unless the attestation is
        the approved one (the gate's attester signed it for this PO document and gate,
        with `spec.evidence_hash`, never expiring), so a crank pays only what a person
        approved.
        """
        if _HEX64.fullmatch(spec.evidence_hash) is None:
            raise PaymentRefused("the evidence hash must be 64 lowercase hex digits")
        evidence_hash = bytes.fromhex(spec.evidence_hash)
        escrow, state = await self._existing_escrow(spec.escrow)
        gate = _releasable_milestone(state, spec.milestone).gate
        relayer = self._relayer.pubkey()
        keys: tuple[HotKey, ...] = (self._relayer,)
        instructions: list[Instruction] = []
        if spec.attest:
            attester = await self._attester_of(state, gate)
            nonce, _ = _gate_attestation_address(escrow, state, gate)
            instructions.append(
                create_attestation_ix(
                    payer=relayer,
                    signer=attester.pubkey(),
                    credential=state.credential,
                    schema=state.schema,
                    nonce=nonce,
                    data=encode_gate_approval_data(state.po_hash, gate, evidence_hash),
                )
            )
            keys = (self._relayer, attester)
        else:
            await self._require_approved_attestation(escrow, state, gate, evidence_hash)
        instructions.append(
            release_ix(
                program_id=self._settings.program_id,
                escrow=escrow,
                vault=state.vault,
                payout_token=state.payout_token,
                mint=state.mint,
                attestation=state.gates[gate].attestation,
                milestone=spec.milestone,
            )
        )
        latest = await self._latest_blockhash()
        compiled = compile_message(
            payer=relayer,
            instructions=instructions,
            signers={key.pubkey() for key in keys},
            blockhash=latest.blockhash,
            last_valid_block_height=latest.last_valid_block_height,
            compute_unit_limit=(
                RELEASE_ATTEST_COMPUTE_UNITS if spec.attest else RELEASE_COMPUTE_UNITS
            ),
            compute_unit_price=self._settings.compute_unit_price,
        )
        return self._signed("release" if spec.attest else "release_crank", compiled, keys)

    # --- internals ---------------------------------------------------------------------

    def _fund_message(
        self, buyer: Pubkey, escrow: Pubkey, blockhash: Hash, last_valid_block_height: int
    ) -> CompiledMessage:
        mint, relayer = self._settings.mint, self._relayer.pubkey()
        fund = fund_ix(
            program_id=self._settings.program_id,
            buyer=buyer,
            escrow=escrow,
            mint=mint,
            buyer_token=token_account(buyer, mint),
            vault=token_account(escrow, mint),
        )
        return compile_message(
            payer=relayer,
            instructions=[fund],
            signers={relayer, buyer},
            blockhash=blockhash,
            last_valid_block_height=last_valid_block_height,
            compute_unit_limit=FUND_COMPUTE_UNITS,
            compute_unit_price=self._settings.compute_unit_price,
        )

    def _create_po_args(self, spec: EscrowSpec) -> tuple[Pubkey, CreatePoArgs]:
        """`spec` as `create_po` arguments, refused here when the program would refuse it."""
        buyer = _address(spec.buyer, "buyer")
        _check_chain_po_id(spec.chain_po_id)
        if buyer == self._settings.payout_owner:
            raise PaymentRefused("the buyer cannot be the payout owner")
        if len(spec.po_hash) != _HASH_LEN or len(spec.cad_hash) != _HASH_LEN:
            raise PaymentRefused("po_hash and cad_hash must be 32 bytes")
        if not 0 < spec.total <= _U64_MAX:
            raise PaymentRefused("the escrow total must be a positive u64")
        milestones = spec.milestones
        if not 1 <= len(milestones) <= min(MAX_MILESTONES, MAX_GATES):
            raise PaymentRefused(f"an escrow has 1 to {MAX_MILESTONES} milestones")
        for milestone in milestones:
            if not 1 <= len(milestone.name.encode("utf-8")) <= MAX_MILESTONE_NAME_BYTES:
                raise PaymentRefused(
                    f"a milestone name is 1 to {MAX_MILESTONE_NAME_BYTES} bytes long"
                )
            if milestone.bps <= 0:
                raise PaymentRefused("every milestone has a positive share")
        shares = [milestone.bps for milestone in milestones]
        if sum(shares) != BPS_TOTAL:
            raise PaymentRefused(f"the milestone shares must add up to {BPS_TOTAL}")
        if any(amount <= 0 for amount in milestone_amounts(spec.total, shares)):
            raise PaymentRefused("a milestone's share of the total rounds down to zero")
        if not int(self._clock()) < spec.refund_after <= _I64_MAX:
            raise PaymentRefused("refund_after must be in the future")
        args = CreatePoArgs(
            chain_po_id=spec.chain_po_id,
            po_hash=spec.po_hash,
            cad_hash=spec.cad_hash,
            total=spec.total,
            gate_attesters=(self._settings.gate_attester,) * len(milestones),
            milestones=tuple(
                MilestoneInput(name=milestone.name, bps=milestone.bps, gate=gate)
                for gate, milestone in enumerate(milestones)
            ),
            refund_after=spec.refund_after,
        )
        return buyer, args

    async def _sas_credential(self) -> Pubkey:
        """`Config.sas_credential`, once the config allows our operator and our mint and
        SOLANA_GATE_ATTESTER is an authorized signer of that credential.

        The program checks the last one itself (`create_po` check 10), but its devnet
        build predates the check: there, an escrow pinned to an attester SAS refuses
        would be created and funded, then never released.
        """
        if self._credential is not None:
            return self._credential
        program_id = self._settings.program_id
        account = await self._rpc.get_account_info(config_pda(program_id), "confirmed")
        if account is None or account.owner != program_id:
            raise PaymentsNotConfigured("the escrow program has no Config account")
        config = decode_config(account.data)
        if config.operator != self._operator.pubkey():
            raise PaymentsNotConfigured("SOLANA_OPERATOR_KEY is not the escrow program's operator")
        if self._settings.mint not in config.allowed_mints:
            raise PaymentsNotConfigured(
                "the escrow program does not allow SOLANA_STABLECOIN_MINT "
                "(scripts/payments_chain.py allow-mint)"
            )
        await self._require_attester_signer(config.sas_credential)
        self._credential = config.sas_credential
        return config.sas_credential

    async def _require_attester_signer(self, credential: Pubkey) -> None:
        """`PaymentsNotConfigured` unless SOLANA_GATE_ATTESTER may attest under `credential`
        (read at `confirmed`): SAS refuses an attestation by anyone else."""
        account = await self._rpc.get_account_info(credential, "confirmed")
        signers: tuple[Pubkey, ...] | None = None
        if account is not None and account.owner == SAS_PROGRAM_ID:
            try:
                signers = credential_signers(account.data)
            except ValueError:
                signers = None
        if signers is None:
            raise PaymentsNotConfigured("the escrow's SAS credential is not a SAS credential")
        if self._settings.gate_attester not in signers:
            raise PaymentsNotConfigured(
                "SOLANA_GATE_ATTESTER is not an authorized signer of the escrow's SAS credential"
            )

    async def _escrow_account(self, key: Pubkey) -> PoEscrowAccount | None:
        """The escrow at `key` (read at `confirmed`); None if the program has none there."""
        account = await self._rpc.get_account_info(key, "confirmed")
        if account is None or account.owner != self._settings.program_id:
            # Missing, or only lamports someone sent to the address: no escrow yet.
            return None
        return decode_po_escrow(account.data)

    async def _existing_escrow(self, address: str) -> tuple[Pubkey, PoEscrowAccount]:
        """The escrow at `address` (read at `confirmed`), or `PaymentRefused`."""
        key = _address(address, "escrow")
        account = await self._rpc.get_account_info(key, "confirmed")
        state: PoEscrowAccount | None = None
        if account is not None and account.owner == self._settings.program_id:
            try:
                state = decode_po_escrow(account.data)
            except ValueError:  # another account of the program (its Config)
                state = None
        if state is None:
            raise PaymentRefused("no escrow of the program exists at this address")
        return key, state

    async def _attestation(
        self, escrow: Pubkey, state: PoEscrowAccount, gate: int
    ) -> GateAttestationAccount | None:
        """The gate's attestation (read at `confirmed`); None while none exists (see
        `gate_attestation`), `ProviderError` when the account there is not one."""
        nonce, address = _gate_attestation_address(escrow, state, gate)
        account = await self._rpc.get_account_info(address, "confirmed")
        if account is None or (account.owner == SYSTEM_PROGRAM_ID and not account.data):
            return None
        found: GateAttestationAccount | None = None
        if account.owner == SAS_PROGRAM_ID:
            try:
                found = decode_gate_attestation(account.data)
            except ValueError:
                found = None
        # SAS writes the PDA's own seeds, so a mismatch is not an attestation SAS made
        # there; `release` would refuse it too (checks 8 and 9).
        if found is None or (found.nonce, found.credential, found.schema) != (
            nonce,
            state.credential,
            state.schema,
        ):
            raise ProviderError(PROVIDER, "getAccountInfo", "attestation_invalid")
        return found

    async def _attester_of(self, state: PoEscrowAccount, gate: int) -> HotKey:
        """Our gate attester's key, once it can attest `gate` of this escrow."""
        attester = self._gate_attester
        if attester is None:
            raise PaymentsNotConfigured("SOLANA_GATE_ATTESTER_KEY is not set")
        if state.gates[gate].attester != attester.pubkey():
            raise PaymentsNotConfigured(
                f"gate {gate} of the escrow is pinned to an attester other than "
                "SOLANA_GATE_ATTESTER_KEY"
            )
        await self._require_attester_signer(state.credential)
        return attester

    async def _require_approved_attestation(
        self, escrow: Pubkey, state: PoEscrowAccount, gate: int, evidence_hash: bytes
    ) -> None:
        """`PaymentRefused` unless the gate's attestation is the one the release needs and
        the person approved: by the gate's attester, for this PO document and gate, with
        `evidence_hash`, never expiring (Bido writes expiry 0)."""
        found = await self._attestation(escrow, state, gate)
        if found is None:
            raise PaymentRefused(f"gate {gate} has no attestation to release against")
        if (
            found.signer != state.gates[gate].attester
            or found.po_hash != state.po_hash
            or found.gate != gate
            or found.evidence_hash != evidence_hash
            or found.expiry != 0
        ):
            raise PaymentRefused(f"the attestation of gate {gate} is not the approved one")

    async def _latest_blockhash(self) -> LatestBlockhash:
        await self._refuse_mainnet()
        return await self._rpc.get_latest_blockhash("confirmed")

    async def _refuse_mainnet(self) -> None:
        """Payments run on devnet only: refuse a mainnet node, once per process."""
        if self._not_mainnet:
            return
        if await self._rpc.get_genesis_hash() == MAINNET_GENESIS_HASH:
            raise PaymentsNotConfigured(
                "SOLANA_RPC_URL is a mainnet node; payments run on devnet only"
            )
        self._not_mainnet = True

    def _signed(
        self, kind: str, compiled: CompiledMessage, keys: tuple[HotKey, ...]
    ) -> SignedTransaction:
        signed = sign_transaction(compiled, keys)
        logger.info("solana transaction signed kind=%s signature=%s", kind, signed.signature)
        return signed


def _releasable_milestone(state: PoEscrowAccount, index: int) -> Milestone:
    """Milestone `index` if `release` can pay it now through its own gate, else
    `PaymentRefused`.

    Bido gives milestone i gate i (`_create_po_args`): one attestation, so one approved
    evidence, per milestone. Through a shared gate, a milestone would be released against
    another milestone's evidence.
    """
    if state.status is not EscrowStatus.FUNDED:
        raise PaymentRefused(f"the escrow is {_ESCROW_STATUS[state.status]}, not funded")
    if not 0 <= index < len(state.milestones):
        raise PaymentRefused(f"the escrow has no milestone {index}")
    milestone = state.milestones[index]
    if milestone.status is not MilestoneStatus.PENDING:
        status = _MILESTONE_STATUS[milestone.status]
        raise PaymentRefused(f"milestone {index} is {status}, not pending")
    if milestone.gate != index or milestone.gate >= len(state.gates):
        raise PaymentRefused(f"milestone {index} is not released through its own gate")
    return milestone


def _gate_attestation_address(
    escrow: Pubkey, state: PoEscrowAccount, gate: int
) -> tuple[Pubkey, Pubkey]:
    """The gate's SAS nonce and attestation address, derived from the escrow's own
    credential and schema; `PaymentRefused` if they are not the address it stores."""
    nonce = gate_nonce(escrow, gate)
    address = attestation_pda(state.credential, state.schema, nonce)
    if address != state.gates[gate].attestation:
        raise PaymentRefused(f"gate {gate} of the escrow expects another attestation address")
    return nonce, address


def _milestone_state(index: int, milestone: Milestone) -> EscrowMilestoneState:
    return EscrowMilestoneState(
        index=index,
        name=milestone.name,
        bps=milestone.bps,
        amount=milestone.amount,
        gate=milestone.gate,
        status=_MILESTONE_STATUS[milestone.status],
        evidence_hash=milestone.evidence_hash.hex(),
        # The default key until released.
        approved_by=None
        if milestone.approved_by == Pubkey.default()
        else str(milestone.approved_by),
    )


def _check_chain_po_id(chain_po_id: str) -> None:
    """`create_po` check 3 before the id is used as a PDA seed (solders panics past 32 bytes)."""
    if (
        not _MIN_CHAIN_PO_ID_LEN <= len(chain_po_id) <= MAX_SEED_LEN
        or _CHAIN_PO_ID_CHARS.fullmatch(chain_po_id) is None
    ):
        raise PaymentRefused("chain_po_id must be 6 to 32 of A-Z, 0-9 and '-'")


def _address(value: str, what: str) -> Pubkey:
    key: Pubkey | None
    try:
        key = Pubkey.from_string(value)
    except ValueError:
        key = None
    if key is None:
        raise PaymentRefused(f"the {what} is not a Solana address")
    return key


def _signature(value: str) -> Signature:
    signature: Signature | None
    try:
        signature = Signature.from_string(value)
    except ValueError:
        signature = None
    if signature is None:
        raise PaymentRefused("not a Solana transaction signature")
    return signature


def _payout_memo(payout_ref_hex: str) -> str:
    memo: str | None
    try:
        memo = payout_ref_memo(payout_ref_hex)
    except ValueError:
        memo = None
    if memo is None:
        raise PaymentRefused("the payout reference must be 64 lowercase hex digits")
    return memo


def _is_confirmed(status: SignatureStatus) -> bool:
    # `confirmations` is null once the block is rooted (finalized), for nodes that
    # leave `confirmationStatus` out.
    if status.confirmation_status is None:
        return status.confirmations is None
    return status.confirmation_status in _CONFIRMED


def _fund_escrow(message: MessageV0, program_id: Pubkey) -> Pubkey:
    """The escrow of a `[CU, CU, fund]` message: account 1 of the program instruction."""
    instructions = message.instructions
    keys = message.account_keys
    if len(instructions) != 3:
        raise ValueError("not a fund transaction")
    fund = instructions[2]
    indexes = (fund.program_id_index, *fund.accounts[:2])
    if len(fund.accounts) < 2 or max(indexes) >= len(keys):
        raise ValueError("not a fund transaction")
    if keys[fund.program_id_index] != program_id:
        raise ValueError("not a fund transaction")
    return keys[fund.accounts[1]]


def _message_of(wire: bytes) -> MessageV0 | None:
    try:
        return compiled_from_wire(wire, 0).message
    except ValueError:
        return None
