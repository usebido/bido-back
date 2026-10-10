"""A charged payment's chain steps: mint, create the escrow, fund it. One step per job.

`payment.advance` (payload `{"payment_id"}`) runs ONE step of a charged payment, chosen by
its state, and wakes the next one after its commit:

    charged --mint--> minted --create_escrow--> escrow_created --fund--> funded

* mint: the approved token amount of Bido's stablecoin into the company's wallet (one
  Privy server wallet per tenant, created on first use and kept in `tenant_wallets`);
  the relayer pays, Bido's mint authority signs; memo `bido:payment:<payment id>`.
* create_escrow: `create_po` on `bido_escrow` with the approved terms (PO hash, total,
  milestones; the refund date is fixed at the first attempt and stored); the operator
  signs, and the memo `bido:payout_ref:<hex>` binds the approved payout account.
* fund: the company's wallet moves the total into the escrow's vault. Privy signs for
  the wallet with a fresh idempotency key per call (Privy caches errors per key); a
  signing error is retried, except the wallet policy's refusal, which fails the payment.

Every step only carries out what was approved: it re-reads the approval and refuses
(`failed`, `approval_mismatch`) unless the row still holds exactly its terms.

Exactly once. The payment row is the outbox: its lock serializes the jobs of one
payment while they decide and record, and its state says what is next.

1. A transaction of this step still `sent` is resolved first. `confirmed`: the step is
   done. `failed`: done only if the chain shows it done anyway (2), else the payment
   fails (`chain_<step>_failed`; the card was charged, so a person must act).
   `unknown`: while its blockhash may still land (the finalized `block_height()` not
   past its `last_valid_block_height` + `EXPIRY_MARGIN`) the job raises `StepPending` and is
   retried later; once it cannot, a second status read (it may have landed in the
   last valid block) still unknown marks it `expired`. Only then may a new attempt be
   built: never while an earlier one can land.
2. On-chain done-checks: the escrow exists with our buyer, total and mint (the escrow
   PDA is `["po", buyer, chain_po_id]`, created once); the escrow is funded (a second
   `fund` fails `AlreadyFunded`). A mint leaves no such mark: rule 1 alone protects it.
3. Build and sign, INSERT the transaction (signature, last valid block height, `sent`)
   and COMMIT; only then send it. A retryable send error raises (the row stays `sent`:
   the next run asks the chain); a refused one (preflight: never forwarded) is a failed
   landing. A signature the wallet returns unusable raises before anything is
   recorded (retried with a fresh key); the chain not set up for us
   (`chain_not_configured`) or inputs it refuses fail the payment.
4. Poll its status for at most `poll_budget` seconds (the worker runs one job at a
   time). Confirmed: record the step and wake the next. Still unknown: `StepPending`.
   A status read from an RPC node behind the finalized chain raises (`node_behind`,
   retryable): it never expires a transaction.

A lost wake-up is healed by the scheduled sweep (`payments.reconcile`).
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.approvals.integrity import payload_matches
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    IssuedPurchaseOrder,
    PaymentChainTransaction,
    PoPayment,
    SupplierPayoutAccount,
    TenantWallet,
)
from bido_back.payments.chain.hashes import payout_ref
from bido_back.payments.config import PaymentProviders
from bido_back.payments.errors import (
    PaymentError,
    PaymentNotFound,
    PaymentRefused,
    PaymentsNotConfigured,
    ProviderError,
)
from bido_back.payments.ports import (
    CardAuthorization,
    CardGateway,
    EscrowMilestone,
    EscrowSpec,
    EscrowState,
    SignedTransaction,
    TransactionStatus,
)
from bido_back.payments.proposals import (
    ESCROW_CREATED,
    ESCROW_FUNDED,
    PAY_PURCHASE_ORDER,
    PAYMENT_ADVANCE,
    PAYMENT_FAILED,
    PAYMENT_MINTED,
    payment_job_key,
)
from bido_back.payments.schemas import PayPurchaseOrderArgs
from bido_back.payments.views import ChainStep
from bido_back.rfqs.audit import rfq_event
from bido_back.worker.envelope import Envelope
from bido_back.worker.jobs import JobQueue

logger = logging.getLogger(__name__)

type Db = AsyncEngine | async_sessionmaker[AsyncSession]
type Clock = Callable[[], datetime]

# The card was charged: a payment in one of these states is never charged again.
CHARGED_STATES: Final = frozenset({"charged", "minted", "escrow_created", "funded"})
# The chain step each state still has to take (the states the sweep wakes).
STEPS: Final[dict[str, ChainStep]] = {
    "charged": "mint",
    "minted": "create_escrow",
    "escrow_created": "fund",
}
# Blocks past a transaction's last valid block height before it counts as dead.
# `block_height()` is the finalized height, so any height past it would do; the margin
# is slack for RPC nodes behind a load balancer that lag each other (a few seconds).
EXPIRY_MARGIN: Final = 10
# Privy's code when the wallet's policy refuses to sign: final, unlike its other errors.
POLICY_VIOLATION: Final = "policy_violation"

_AFTER: Final[dict[ChainStep, str]] = {
    "mint": "minted",
    "create_escrow": "escrow_created",
    "fund": "funded",
}
_AUDIT: Final[dict[ChainStep, str]] = {
    "mint": PAYMENT_MINTED,
    "create_escrow": ESCROW_CREATED,
    "fund": ESCROW_FUNDED,
}
# A payment's approval vouches for it once approved (`executed` after the act ran).
_APPROVED: Final = frozenset({"approved", "executed"})
_CHAIN: Final = "solana"
_ERROR_LIMIT: Final = 120  # payment_chain_transactions_error_check


class StepPending(PaymentError):
    """The step's outcome is not known yet (a transaction may still land): retry later.

    The job fails and the queue redelivers it after its visibility timeout.
    """


class InvalidPaymentJob(PaymentError):
    """A `payment.advance` envelope that can never run (left for the dead-letter queue)."""


def payment_advance_envelope(tenant_id: UUID, payment_id: UUID, state: str) -> Envelope:
    """The wake-up of the step a payment in `state` has to take (one key per state)."""
    return Envelope(
        type=PAYMENT_ADVANCE,
        tenant_id=tenant_id,
        idempotency_key=payment_job_key(payment_id, state),
        payload={"payment_id": str(payment_id)},
    )


async def authorize_payment_card(
    card: CardGateway, payment_method_ref: str, args: PayPurchaseOrderArgs
) -> CardAuthorization:
    """The payment's allowance, built from the approved arguments only. The service (to
    start the Visa passkey) and the act tool send this same request under the same key,
    so Basis Theory answers both with the one allowance."""
    return await card.authorize(
        payment_method_ref=payment_method_ref,
        amount=Decimal(args.charge_amount),
        currency=args.charge_currency,
        description=f"Bido {args.purchase_order_ref}",
        expires_at=args.quote_expires_at,
        idempotency_key=f"payment:{args.payment_id}:authorize",
    )


def approved_terms_match(payment: PoPayment, args: PayPurchaseOrderArgs) -> bool:
    """The row holds exactly what was approved: whose card, how much, which escrow."""
    milestones = [(m.get("name"), m.get("bps")) for m in payment.milestones]
    return (
        payment.id == args.payment_id
        and payment.purchase_order_id == args.purchase_order_id
        and payment.requested_by == args.requested_by
        and payment.payment_method_id == args.payment_method_id
        and payment.payout_account_id == args.payout_account_id
        and payment.po_currency == args.po_currency
        and payment.charge_currency == args.charge_currency
        and payment.charge_amount == Decimal(args.charge_amount)
        and payment.token_amount == Decimal(args.token_amount)
        and payment.chain_po_id == args.chain_po_id
        and payment.po_hash == args.content_hash
        and payment.charge_mode == args.charge_mode
        and payment.card_rail == args.card_rail
        and milestones == [(m.name, m.bps) for m in args.milestones]
    )


async def approved_args(session: AsyncSession, payment: PoPayment) -> PayPurchaseOrderArgs | None:
    """The payment's approved arguments, or None when its approval no longer vouches for it.

    The app role cannot change an approval's args or hash, so they are the record of
    what the person approved; the payment row is checked against them.
    """
    approval = await session.get(Approval, payment.approval_id)
    if (
        approval is None
        or approval.tool != PAY_PURCHASE_ORDER
        or approval.status not in _APPROVED
        or approval.requested_by != payment.requested_by
        or not payload_matches(approval)
    ):
        return None
    try:
        args = PayPurchaseOrderArgs.model_validate(approval.args)
    except ValidationError:
        return None  # never chained: its text holds the values
    return args if approved_terms_match(payment, args) else None


async def locked_payment(session: AsyncSession, payment_id: UUID) -> PoPayment:
    payment = await session.scalar(
        select(PoPayment).where(PoPayment.id == payment_id).with_for_update()
    )
    if payment is None:
        raise PaymentNotFound(f"payment {payment_id} not found")
    return payment


def _payment_id(envelope: Envelope) -> UUID:
    raw = envelope.payload.get("payment_id")
    if set(envelope.payload) != {"payment_id"} or not isinstance(raw, str):
        raise InvalidPaymentJob("payload must be exactly {payment_id}")
    try:
        payment_id = UUID(raw)
    except ValueError:
        raise InvalidPaymentJob("payment_id is not a UUID") from None
    if not envelope.idempotency_key.startswith(f"payment:{payment_id}:"):
        raise InvalidPaymentJob("idempotency_key does not match the payment")
    return payment_id


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _mark(row: PaymentChainTransaction, state: str, error: str | None = None) -> None:
    row.state = state
    row.error = error[:_ERROR_LIMIT] if error else None
    row.updated_at = func.now()


def _ours(escrow: EscrowState, payment: PoPayment, mint: str) -> bool:
    return (
        escrow.buyer == payment.wallet_address
        and escrow.chain_po_id == payment.chain_po_id
        and escrow.total == int(payment.token_amount)
        and escrow.mint == mint
    )


@dataclass(frozen=True, slots=True)
class _Outcome:
    """What a step committed: the payment's state now, and whether its next step is due."""

    state: str
    wake: bool = False


@dataclass(frozen=True, slots=True)
class _Attempt:
    """A signed transaction, recorded `sent` and committed: it may be sent now."""

    step: ChainStep
    transaction: SignedTransaction


@dataclass(frozen=True, slots=True)
class _Landed:
    """What the step's earlier transactions came to (none of them can land any more)."""

    confirmed: str | None = None  # the signature of the one that landed
    failed: bool = False  # one landed with an error (or was refused) and none succeeded


@dataclass(frozen=True, slots=True)
class _Accounts:
    wallet_id: UUID
    wallet: str  # the company's wallet: the escrow's buyer
    escrow: str | None  # the escrow PDA (None while minting)


class PaymentAdvanceHandler:
    """The `payment.advance` job handler: one chain step of one charged payment."""

    def __init__(
        self,
        db: Db,
        providers: PaymentProviders,
        jobs: JobQueue | None,
        *,
        poll_interval: float = 1.5,
        poll_budget: float = 20.0,
        refund_after_days: int = 90,
        now: Clock | None = None,
    ) -> None:
        self._db = db
        self._providers = providers
        # Wakes the next step after a commit; None: the scheduled sweep does.
        self._jobs = jobs
        self._poll_interval = poll_interval
        self._poll_budget = poll_budget
        self._refund_after = timedelta(days=refund_after_days)
        self._now: Clock = now or _utcnow

    async def __call__(self, envelope: Envelope) -> None:
        await self.advance(envelope.tenant_id, _payment_id(envelope))

    async def advance(self, tenant_id: UUID, payment_id: UUID) -> str:
        """Run the payment's next step; returns its state after it.

        Raises `StepPending` while a transaction may still land, and provider errors
        that are worth retrying; the job is then retried.
        """
        decided = await self._decide(tenant_id, payment_id)
        if isinstance(decided, _Attempt):
            status = await self._send(decided)
            if status.state == "unknown":
                raise StepPending(
                    f"payment {payment_id}: {decided.step} {decided.transaction.signature} "
                    "not confirmed yet"
                )
            decided = await self._settle(tenant_id, payment_id, decided, status)
        if decided.wake and self._jobs is not None:
            try:
                await self._jobs.enqueue(
                    payment_advance_envelope(tenant_id, payment_id, decided.state)
                )
            except Exception:
                # Committed: the scheduled sweep wakes it (`payments.reconcile`).
                logger.exception("payment %s: the next step's job was not enqueued", payment_id)
        return decided.state

    # --- 1. Decide, under the payment's row lock ---------------------------------------------

    async def _decide(self, tenant_id: UUID, payment_id: UUID) -> _Outcome | _Attempt:
        async with tenant_session(self._db, tenant_id) as session:
            payment = await locked_payment(session, payment_id)
            step = STEPS.get(payment.state)
            if step is None:
                return _Outcome(payment.state)  # not charged, or nothing left to do
            args = await approved_args(session, payment)
            if args is None:
                return await self._fail(session, payment, step, "approval_mismatch")
            landed = await self._resolve_sent(session, payment.id, step)
            if landed.confirmed is not None:
                return await self._advance(session, payment, step, landed.confirmed)
            accounts = await self._accounts(session, payment, step)
            if isinstance(accounts, _Outcome):
                return accounts
            on_chain = await self._on_chain(payment, step)
            if on_chain == "done":
                return await self._advance(session, payment, step, None)
            if on_chain == "mismatch":
                return await self._fail(session, payment, step, "escrow_mismatch")
            if landed.failed:
                return await self._fail(session, payment, step, f"chain_{step}_failed")
            attempt = (
                await session.scalar(
                    select(func.max(PaymentChainTransaction.attempt)).where(
                        PaymentChainTransaction.payment_id == payment.id,
                        PaymentChainTransaction.step == step,
                    )
                )
                or 0
            ) + 1
            try:
                built = await self._build(session, payment, step, args, accounts, attempt)
            except PaymentsNotConfigured:
                # Our operator or mint is not set up on chain: no retry can fix that.
                return await self._fail(session, payment, step, "chain_not_configured")
            except PaymentRefused:
                # Inputs the chain code refuses before any call: they are the approved ones.
                return await self._fail(session, payment, step, f"chain_{step}_failed")
            if isinstance(built, _Outcome):
                return built
            session.add(
                PaymentChainTransaction(
                    tenant_id=payment.tenant_id,
                    payment_id=payment.id,
                    step=step,
                    attempt=attempt,
                    signature=built.signature,
                    last_valid_block_height=built.last_valid_block_height,
                    state="sent",
                )
            )
            payment.updated_at = func.now()
            await session.flush()
            logger.info(
                "payment %s %s attempt=%d signature=%s tenant=%s: recorded, sending",
                payment.id,
                step,
                attempt,
                built.signature,
                tenant_id,
            )
            return _Attempt(step, built)

    async def _resolve_sent(
        self, session: AsyncSession, payment_id: UUID, step: ChainStep
    ) -> _Landed:
        """Settle the step's transactions still `sent`; raises `StepPending` if one may land."""
        rows = await session.scalars(
            select(PaymentChainTransaction)
            .where(
                PaymentChainTransaction.payment_id == payment_id,
                PaymentChainTransaction.step == step,
                PaymentChainTransaction.state == "sent",
            )
            .order_by(PaymentChainTransaction.attempt.desc())
        )
        failed = False
        for row in list(rows):
            status = await self._landing(row)
            if status is None:
                _mark(row, "expired")
                logger.warning(
                    "payment %s %s signature=%s expired: it can no longer land",
                    payment_id,
                    step,
                    row.signature,
                )
            elif status.state == "confirmed":
                _mark(row, "confirmed")
                return _Landed(confirmed=row.signature)
            else:
                _mark(row, "failed", status.error)
                failed = True
        return _Landed(failed=failed)

    async def _landing(self, row: PaymentChainTransaction) -> TransactionStatus | None:
        """Where a sent transaction stands; None once it provably cannot land.

        Raises `StepPending` while its blockhash is still valid: it may land yet.
        """
        chain = self._providers.chain
        status = await chain.status(row.signature)
        if status.state != "unknown":
            return status
        if await chain.block_height() <= row.last_valid_block_height + EXPIRY_MARGIN:
            raise StepPending(f"{row.step} {row.signature} may still land")
        # Past its last valid block height: it may still have landed in the last block.
        status = await chain.status(row.signature)
        return None if status.state == "unknown" else status

    async def _accounts(
        self, session: AsyncSession, payment: PoPayment, step: ChainStep
    ) -> _Accounts | _Outcome:
        """The addresses the step works on: the company's wallet and the escrow."""
        if step == "mint":
            wallet_id, wallet = await self._wallet(session, payment)
            return _Accounts(wallet_id, wallet, None)
        if payment.wallet_id is None or payment.wallet_address is None:
            return await self._fail(session, payment, step, "wallet_missing")
        chain = self._providers.chain
        escrow = chain.escrow_address(buyer=payment.wallet_address, chain_po_id=payment.chain_po_id)
        if payment.escrow_address is None and step == "create_escrow":
            payment.escrow_address = escrow
            payment.vault_address = chain.vault_address(escrow=escrow)
        elif payment.escrow_address != escrow:
            return await self._fail(session, payment, step, "escrow_mismatch")
        return _Accounts(payment.wallet_id, payment.wallet_address, escrow)

    async def _wallet(self, session: AsyncSession, payment: PoPayment) -> tuple[UUID, str]:
        """The company's wallet: recorded once per tenant, created at Privy on first use."""
        if payment.wallet_id is not None and payment.wallet_address is not None:
            return payment.wallet_id, payment.wallet_address
        row = await self._tenant_wallet(session, payment.tenant_id)
        if row is None:
            wallet = await self._providers.wallets.ensure_wallet(tenant_id=payment.tenant_id)
            # A concurrent payment of the same company may have recorded it first.
            await session.execute(
                insert(TenantWallet)
                .values(
                    tenant_id=payment.tenant_id,
                    chain=_CHAIN,
                    wallet_ref=wallet.ref,
                    address=wallet.address,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "chain"])
            )
            row = await self._tenant_wallet(session, payment.tenant_id)
            if row is None:
                raise PaymentError(f"tenant {payment.tenant_id}: its wallet was not recorded")
            logger.info("tenant %s wallet %s recorded", payment.tenant_id, row.id)
        payment.wallet_id = row.id
        payment.wallet_address = row.address
        return row.id, row.address

    @staticmethod
    async def _tenant_wallet(session: AsyncSession, tenant_id: UUID) -> TenantWallet | None:
        return await session.scalar(
            select(TenantWallet).where(
                TenantWallet.tenant_id == tenant_id, TenantWallet.chain == _CHAIN
            )
        )

    async def _on_chain(
        self, payment: PoPayment, step: ChainStep
    ) -> Literal["done", "todo", "mismatch"]:
        """Whether the chain already shows the step done: `create_po` and `fund` leave a mark."""
        if step == "mint" or payment.escrow_address is None:
            return "todo"  # a mint leaves none: only the sent-transaction rule protects it
        chain = self._providers.chain
        escrow = await chain.escrow(payment.escrow_address)
        if escrow is None:
            if step == "fund":
                # It was created (confirmed): this node does not show it yet.
                raise StepPending(f"payment {payment.id}: its escrow is not visible yet")
            return "todo"
        if not _ours(escrow, payment, chain.mint):
            return "mismatch"
        if step == "create_escrow":
            return "done"
        return "todo" if escrow.status == "created" else "done"

    async def _build(
        self,
        session: AsyncSession,
        payment: PoPayment,
        step: ChainStep,
        args: PayPurchaseOrderArgs,
        accounts: _Accounts,
        attempt: int,
    ) -> SignedTransaction | _Outcome:
        """The step's next transaction, signed: nothing of this step can land any more."""
        chain = self._providers.chain
        if step == "mint":
            return await chain.build_mint(
                owner=accounts.wallet,
                amount=int(payment.token_amount),
                memo=f"bido:payment:{payment.id}",
            )
        if accounts.escrow is None:
            return await self._fail(session, payment, step, "escrow_mismatch")
        if step == "create_escrow":
            account = await session.get(SupplierPayoutAccount, payment.payout_account_id)
            if account is None or account.bank_fingerprint != args.payout_fingerprint:
                return await self._fail(session, payment, step, "payout_account_changed")
            if payment.refund_after is None:
                # Fixed at the first attempt: a rebuilt transaction carries the same terms.
                payment.refund_after = (self._now() + self._refund_after).replace(microsecond=0)
            return await chain.build_create_escrow(
                EscrowSpec(
                    chain_po_id=payment.chain_po_id,
                    buyer=accounts.wallet,
                    po_hash=bytes.fromhex(payment.po_hash),
                    cad_hash=bytes(32),
                    total=int(payment.token_amount),
                    milestones=tuple(
                        EscrowMilestone(name=m.name, bps=m.bps) for m in args.milestones
                    ),
                    refund_after=int(payment.refund_after.timestamp()),
                    payout_ref=payout_ref(
                        str(payment.payout_account_id),
                        account.bank_fingerprint,
                        payment.chain_po_id,
                    ),
                )
            )
        wallet = await session.get(TenantWallet, accounts.wallet_id)
        if wallet is None or wallet.address != accounts.wallet:
            return await self._fail(session, payment, step, "wallet_missing")
        unsigned = await chain.build_fund(buyer=accounts.wallet, escrow=accounts.escrow)
        try:
            signed_by_buyer = await self._providers.wallets.sign_transaction(
                wallet_ref=wallet.wallet_ref,
                transaction=unsigned.wire,
                # Privy caches an error per key: a fresh one per call, so a retry can pass.
                idempotency_key=f"payment:{payment.id}:fund:{attempt}:{uuid4().hex[:8]}",
            )
        except ProviderError as exc:
            if exc.code != POLICY_VIOLATION:
                raise  # retried with a new key (a configuration error waits for its fix)
            # The wallet's policy refuses this transaction: no retry can change that.
            return await self._fail(session, payment, step, "chain_fund_failed")
        return chain.complete_fund(unsigned, buyer=accounts.wallet, signed_by_buyer=signed_by_buyer)

    # --- 2. Send and wait (no transaction open) ----------------------------------------------

    async def _send(self, attempt: _Attempt) -> TransactionStatus:
        signature = attempt.transaction.signature
        try:
            await self._providers.chain.send(attempt.transaction)
        except ProviderError as exc:
            if exc.retryable:
                # It may have been forwarded: the row stays `sent` and the next run asks.
                logger.warning(
                    "%s signature=%s: send failed code=%s; the chain is asked next time",
                    attempt.step,
                    signature,
                    exc.code,
                )
                raise
            # Refused before it was forwarded (preflight): it cannot land.
            logger.warning("%s signature=%s refused code=%s", attempt.step, signature, exc.code)
            return TransactionStatus("failed", exc.code)
        return await self._poll(signature)

    async def _poll(self, signature: str) -> TransactionStatus:
        """The status, asked every `poll_interval` for at most `poll_budget` seconds."""
        deadline = time.monotonic() + self._poll_budget
        while True:
            status = await self._providers.chain.status(signature)
            remaining = deadline - time.monotonic()
            if status.state != "unknown" or remaining <= 0:
                return status
            await asyncio.sleep(min(self._poll_interval, remaining))

    # --- 3. Record what landed, under the row lock again -------------------------------------

    async def _settle(
        self, tenant_id: UUID, payment_id: UUID, attempt: _Attempt, status: TransactionStatus
    ) -> _Outcome:
        async with tenant_session(self._db, tenant_id) as session:
            payment = await locked_payment(session, payment_id)
            row = await session.scalar(
                select(PaymentChainTransaction).where(
                    PaymentChainTransaction.signature == attempt.transaction.signature
                )
            )
            if STEPS.get(payment.state) != attempt.step or row is None or row.state != "sent":
                return _Outcome(payment.state)  # another run of this payment settled it first
            if status.state == "confirmed":
                _mark(row, "confirmed")
                return await self._advance(session, payment, attempt.step, row.signature)
            _mark(row, "failed", status.error)
            on_chain = await self._on_chain(payment, attempt.step)
            if on_chain == "done":
                return await self._advance(session, payment, attempt.step, None)
            code = "escrow_mismatch" if on_chain == "mismatch" else f"chain_{attempt.step}_failed"
            return await self._fail(session, payment, attempt.step, code)

    async def _advance(
        self, session: AsyncSession, payment: PoPayment, step: ChainStep, signature: str | None
    ) -> _Outcome:
        """The step is done (`signature`: the transaction that did it; None: found done)."""
        state = _AFTER[step]
        payment.state = state
        payment.updated_at = func.now()
        fields: dict[str, object] = {"signature": signature}
        if step == "mint":
            fields |= {
                "wallet_address": payment.wallet_address,
                "mint": self._providers.chain.mint,
                "token_amount": str(int(payment.token_amount)),
            }
        else:
            fields |= {
                "chain_po_id": payment.chain_po_id,
                "escrow_address": payment.escrow_address,
                "vault_address": payment.vault_address,
            }
        if step == "fund":
            payment.funded_at = self._now()
        await self._event(session, payment, _AUDIT[step], **fields)
        await session.flush()
        logger.info(
            "payment %s %s tenant=%s signature=%s", payment.id, state, payment.tenant_id, signature
        )
        return _Outcome(state, wake=state in STEPS)

    async def _fail(
        self, session: AsyncSession, payment: PoPayment, step: ChainStep, code: str
    ) -> _Outcome:
        """The payment stops here. It was charged: it needs a person (`needs_attention`)."""
        payment.state = "failed"
        payment.failure_code = code
        payment.updated_at = func.now()
        await self._event(session, payment, PAYMENT_FAILED, step=step, failure_code=code)
        await session.flush()
        logger.error(
            "payment %s failed at %s code=%s tenant=%s: charged and not funded",
            payment.id,
            step,
            code,
            payment.tenant_id,
        )
        return _Outcome("failed")

    @staticmethod
    async def _event(
        session: AsyncSession, payment: PoPayment, action: str, **fields: object
    ) -> None:
        """An audit event keyed by the PO's RFQ, so the RFQ's activity feed shows it."""
        po = await session.get(IssuedPurchaseOrder, payment.purchase_order_id)
        if po is None:
            raise PaymentNotFound(f"purchase order {payment.purchase_order_id} not found")
        session.add(
            rfq_event(
                payment.tenant_id,
                None,
                action,
                po.rfq_id,
                payment_id=payment.id,
                purchase_order_id=po.id,
                purchase_order_ref=po.ref,
                invite_id=po.invite_id,
                approval_id=payment.approval_id,
                **fields,
            )
        )
