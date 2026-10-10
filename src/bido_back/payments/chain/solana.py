"""`SolanaPaymentChain`: the `PaymentChain` of `bido_back.payments.ports` on Solana.

A payment sends three transactions. The relayer pays every fee and every rent, and
each is one v0 message with a fixed compute budget:

- mint: `[CU, CU, CreateIdempotent(owner's token account), MintToChecked, Memo]`,
  signed by the relayer and Bido's mint authority;
- create escrow: `[CU, CU, create_po, Memo("bido:payout_ref:<hex>")]`, signed by the
  relayer and the operator. Milestone i is released through gate i, every gate is
  attested by `SOLANA_GATE_ATTESTER`, and releases pay `SOLANA_PAYOUT_OWNER`;
- fund: `[CU, CU, fund]`, signed by the company's wallet (remotely: `build_fund`
  zeroes every signature slot, `complete_fund` checks the wallet's answer) and the
  relayer.

Nothing here retries, waits or decides: the pipeline records each signature before
`send`, then asks `status` and `block_height` and reads `escrow`. Exactly-once rests
on three answers:

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
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature

from bido_back.payments import ports
from bido_back.payments.chain.escrow import (
    BPS_TOTAL,
    MAX_GATES,
    MAX_MILESTONE_NAME_BYTES,
    MAX_MILESTONES,
    CreatePoArgs,
    EscrowStatus,
    MilestoneInput,
    create_po_ix,
    decode_config,
    decode_po_escrow,
    fund_ix,
    milestone_amounts,
)
from bido_back.payments.chain.hashes import payout_ref_memo
from bido_back.payments.chain.keys import HotKey, load_hot_key
from bido_back.payments.chain.pdas import (
    MAX_SEED_LEN,
    config_pda,
    escrow_pda,
    token_account,
)
from bido_back.payments.chain.rpc import (
    PROVIDER,
    LatestBlockhash,
    PreflightFailed,
    RpcClient,
    SignatureStatus,
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
    EscrowSpec,
    EscrowState,
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
# Payments run on devnet only; a node of this genesis is mainnet-beta.
MAINNET_GENESIS_HASH: Final = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"

_U64_MAX: Final = 2**64 - 1
_I64_MAX: Final = 2**63 - 1
_HASH_LEN: Final = 32
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
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._rpc = rpc
        self._relayer = relayer
        self._operator = operator
        self._mint_authority = mint_authority
        self._clock = clock
        # `Config.sas_credential`, read once (create_po passes it).
        self._credential: Pubkey | None = None
        # The highest finalized slot `block_height` has seen (see the module docstring).
        self._finalized_slot = 0
        self._not_mainnet = False

    @classmethod
    def from_settings(cls, settings: Settings, *, http: httpx.AsyncClient | None = None) -> Self:
        """Every setting parsed and every key loaded, or `PaymentsNotConfigured`."""
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
        return cls(
            settings=chain,
            rpc=RpcClient(url, http=http),
            relayer=relayer,
            operator=operator,
            mint_authority=mint_authority,
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
        """The escrow at `address` (read at `confirmed`); None if no escrow exists there."""
        key = _address(address, "escrow")
        account = await self._rpc.get_account_info(key, "confirmed")
        if account is None or account.owner != self._settings.program_id:
            # Missing, or only lamports someone sent to the address: no escrow yet.
            return None
        state = decode_po_escrow(account.data)
        return EscrowState(
            address=str(key),
            status=_ESCROW_STATUS[state.status],
            chain_po_id=state.chain_po_id,
            buyer=str(state.buyer),
            mint=str(state.mint),
            total=state.total,
            vault=str(state.vault),
        )

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
        """`Config.sas_credential`, once the config allows our operator and our mint."""
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
        self._credential = config.sas_credential
        return config.sas_credential

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
