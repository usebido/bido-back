"""The `pay_purchase_order` act tool: charge the approved card the approved amount, once.

Nothing here is offered to a model. `payments.service.request_po_payment` records the
proposal and its `po_payments` row (`pending_approval`); a person approves it (the buyer
by default; in four-eyes tenants an approver or owner who is not the requester: the
guard checks it); the worker's `ApprovalExecutor` runs this handler once, with the
APPROVED arguments, as the APPROVER. The payer is the requester (`args.requested_by`):
it must be the approval's requester and the payment's, and the card must be theirs.

Like `awards.send._SendPurchaseOrder`: claim, then act, then record.

1. Claim (one transaction, the payment row locked). The executing approval must be the
   payment's. From `pending_approval` only: the row must hold exactly the approved
   terms (`approval_mismatch`), the quote must not have expired (`expired`), and the
   purchase order, the supplier's payout account, the card and the charge mode must
   still be as approved (`failed`: nothing is charged). Then `charging`, committed
   BEFORE any provider call. A retry finds `charging` and goes straight on to the
   charge, re-checking nothing: money may already have moved.
2. Charge (no transaction open): the Basis Theory allowance (key
   `payment:<id>:authorize`; its reference is stored in its own short transaction),
   then the gateway's charge keyed by the payment (`payment:<id>`): a replay returns
   the charge already made instead of charging again. Every request is built from the
   approved arguments only (amount, description, the allowance expiring with the
   quote), so a retry repeats it byte for byte: Basis Theory refuses a key reused
   with another body.
3. Record (the row locked, still `charging`): `charged`, `declined` or `failed`, with
   its audit event (`purchase_order.payment_*`, keyed by `rfq_id` for the activity feed).

A refusal or a no-op RETURNS its outcome (the approval becomes `executed`); a transient
failure RAISES (the approval stays `approved` and the job is retried with the same
keys): a retryable provider error, a charge Stripe is still processing, or a minted
card token whose charge Stripe's search does not show yet (`CardCredentialLost`: the
search is eventually consistent, so it is final only `CREDENTIAL_GRACE` after the
approval). A replay that finds a charge of another amount or currency
(`charge_mismatch`) is never settled by code: the payment fails as charged, for a
person, and the order cannot be charged again.

From `charged` on, the payment is its own outbox (`payments.pipeline`): a
`payment.advance` job wakes its next step after the commit, and the scheduled sweep
(`payments.reconcile`) wakes a payment whose job was lost.

Rejected (`PAYMENT_REJECT_HOOKS`): the payment becomes `rejected`; nothing was charged.
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.agents.context import ToolContext
from bido_back.agents.tools import Tool
from bido_back.approvals.executor import ActToolRegistry
from bido_back.approvals.guard import RejectHook
from bido_back.awards.orders import po_document
from bido_back.awards.render import content_hash
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    AuditEvent,
    IssuedPurchaseOrder,
    PaymentMethod,
    PoPayment,
    SupplierPayoutAccount,
)
from bido_back.payments.config import PaymentProviders
from bido_back.payments.errors import (
    CardCredentialLost,
    CardNotVerified,
    PaymentError,
    PaymentInvalidState,
    PaymentRefused,
    ProviderError,
)
from bido_back.payments.pipeline import (
    CHARGED_STATES,
    STEPS,
    Clock,
    Db,
    approved_terms_match,
    authorize_payment_card,
    locked_payment,
    payment_advance_envelope,
)
from bido_back.payments.ports import IssuedCard
from bido_back.payments.proposals import (
    CARD_NOT_VERIFIED,
    PAY_PURCHASE_ORDER,
    PAYMENT_CHARGED,
    PAYMENT_DECLINED,
    PAYMENT_FAILED,
)
from bido_back.payments.schemas import PayPurchaseOrderArgs, PayPurchaseOrderResult
from bido_back.rfqs.audit import rfq_event
from bido_back.rfqs.errors import NotFound
from bido_back.worker.jobs import JobQueue

logger = logging.getLogger(__name__)

# A minted card token with no charge in Stripe's search counts as lost (nothing was
# charged) only this long after the approval: the search is eventually consistent.
CREDENTIAL_GRACE: Final = timedelta(minutes=10)
# The gateway's code for a replay that found a charge of another amount or currency
# (`payments.card.CHARGE_MISMATCH`; not imported: providers load only where they run).
CHARGE_MISMATCH: Final = "charge_mismatch"

# Nothing was charged and nothing will be: a later execution is a no-op.
_SETTLED: Final = frozenset({"rejected", "expired", "declined", "failed"})
_CLOSED_ORDER: Final = frozenset({"cancelled", "closed"})
_CODE: Final = re.compile(r"[a-z0-9_]{1,64}")  # po_payments_failure_code_check

type Recorded = Literal["charged", "declined", "failed"]


class ChargePending(PaymentError):
    """Stripe is still processing the charge: the job fails and is retried (same keys)."""


@dataclass(frozen=True, slots=True)
class PaymentActDeps:
    db: Db
    providers: PaymentProviders
    # Wakes the pipeline after a charge; None: the scheduled sweep does it.
    jobs: JobQueue | None
    # Tests pin the clock; None = now.
    now: Clock | None = None


@dataclass(frozen=True, slots=True)
class _Claim:
    """The payment is `charging` (committed): charge this card."""

    payment_method_ref: str
    authorization_ref: str | None
    # When the approval was decided: the charge has been under way since.
    decided_at: datetime


@dataclass(frozen=True, slots=True)
class _Done:
    """The claim settled the execution; `wake`: the state whose step to wake after commit."""

    result: PayPurchaseOrderResult
    wake: str | None = None


def _code(value: str | None, default: str) -> str:
    """A failure code we can store: the provider's code if it is one of ours, else ours."""
    return value if value is not None and _CODE.fullmatch(value) else default


def _event(
    ctx: ToolContext, args: PayPurchaseOrderArgs, approval_id: UUID, action: str, **fields: object
) -> AuditEvent:
    """The payment's audit event, keyed by its RFQ (ids and codes; the amount as text)."""
    return rfq_event(
        ctx.tenant_id,
        ctx.user_id,
        action,
        args.rfq_id,
        payment_id=args.payment_id,
        purchase_order_id=args.purchase_order_id,
        purchase_order_ref=args.purchase_order_ref,
        invite_id=args.invite_id,
        approval_id=approval_id,
        requested_by=args.requested_by,
        **fields,
    )


class _PayPurchaseOrder:
    def __init__(self, deps: PaymentActDeps) -> None:
        self._deps = deps

    def _mode_matches(self, args: PayPurchaseOrderArgs) -> bool:
        """The gateway charges the way the approver was shown (a real or a simulated charge,
        and on the approved rail: a Visa payment is never minted without its passkey)."""
        card = self._deps.providers.card
        return card.simulated == (args.charge_mode == "simulated") and card.rail == args.card_rail

    async def __call__(
        self, ctx: ToolContext, args: PayPurchaseOrderArgs
    ) -> PayPurchaseOrderResult:
        if ctx.user_id is None:
            raise PaymentInvalidState("a payment is executed for the person who approved it")
        async with tenant_session(self._deps.db, ctx.tenant_id) as session:
            claim = await self._claim(session, ctx, args)
        if isinstance(claim, _Claim):
            return await self._charge(ctx, args, claim)
        if claim.wake is not None:
            # Its step's job may have been lost with an earlier run: wake it again.
            await self._wake(ctx.tenant_id, args.payment_id, claim.wake)
        return claim.result

    # --- 1. Claim ------------------------------------------------------------------------

    async def _claim(
        self, session: AsyncSession, ctx: ToolContext, args: PayPurchaseOrderArgs
    ) -> _Claim | _Done:
        payment = await session.scalar(
            select(PoPayment).where(PoPayment.id == args.payment_id).with_for_update()
        )
        if payment is None:
            # The service records the row right after the proposal: retried until it is.
            raise NotFound(f"payment {args.payment_id} not found")
        approval = await session.scalar(
            select(Approval).where(
                Approval.run_id == str(ctx.run_id), Approval.tool == PAY_PURCHASE_ORDER
            )
        )
        if approval is None or approval.id != payment.approval_id:
            # Not this payment's approval: it must not touch the payment.
            logger.error(
                "payment %s: run %s is not its approval; not charged", payment.id, ctx.run_id
            )
            return _Done(self._result(args, "failed", "approval_mismatch"))
        if not approval.requested_by == args.requested_by == payment.requested_by:
            if payment.state != "pending_approval":
                return _Done(self._result(args, "failed", "approval_mismatch"))
            return await self._refuse(session, ctx, args, payment, "failed", "approval_mismatch")
        state = payment.state
        if state in CHARGED_STATES:
            return _Done(self._result(args, "already_charged"), state if state in STEPS else None)
        if state in _SETTLED:
            return _Done(self._result(args, "skipped", payment.failure_code))
        decided_at = approval.decided_at or self._now()
        if state == "charging":
            # A retry: the card may have been charged already, so nothing is re-checked;
            # the providers replay the same keys.
            if not self._mode_matches(args):
                raise PaymentInvalidState(
                    f"payment {payment.id}: the card gateway's mode changed while charging"
                )
            method_ref = await session.scalar(
                select(PaymentMethod.payment_method_ref).where(
                    PaymentMethod.id == payment.payment_method_id
                )
            )
            if method_ref is None:
                raise NotFound(f"payment method {payment.payment_method_id} not found")
            return _Claim(method_ref, payment.card_authorization_ref, decided_at)
        if state != "pending_approval":
            raise PaymentInvalidState(f"payment {payment.id} is {state}")
        return await self._check(session, ctx, args, payment, decided_at)

    async def _check(
        self,
        session: AsyncSession,
        ctx: ToolContext,
        args: PayPurchaseOrderArgs,
        payment: PoPayment,
        decided_at: datetime,
    ) -> _Claim | _Done:
        """Everything the approval assumed still holds; then the payment is `charging`."""
        if not approved_terms_match(payment, args):
            return await self._refuse(session, ctx, args, payment, "failed", "approval_mismatch")
        if self._now() >= args.quote_expires_at:
            return await self._refuse(session, ctx, args, payment, "expired", "quote_expired")
        po = await session.get(IssuedPurchaseOrder, args.purchase_order_id)
        if (
            po is None
            or po.status != "sent"
            or po.fulfilment_state in _CLOSED_ORDER
            or po.supplier_id != args.supplier_id
            or content_hash(await po_document(session, po)) != args.content_hash
        ):
            return await self._refuse(
                session, ctx, args, payment, "failed", "purchase_order_changed"
            )
        account = await session.get(SupplierPayoutAccount, args.payout_account_id)
        if (
            account is None
            or account.status != "active"
            or account.supplier_id != args.supplier_id
            or account.bank_fingerprint != args.payout_fingerprint
        ):
            return await self._refuse(
                session, ctx, args, payment, "failed", "payout_account_changed"
            )
        method = await session.get(PaymentMethod, args.payment_method_id)
        if method is None or method.status != "active" or method.created_by != args.requested_by:
            return await self._refuse(
                session, ctx, args, payment, "failed", "payment_method_unavailable"
            )
        if not self._mode_matches(args):
            # The approver was shown a real charge and the server simulates, or the reverse.
            return await self._refuse(session, ctx, args, payment, "failed", "charge_mode_changed")
        payment.state = "charging"
        payment.updated_at = func.now()
        await session.flush()
        return _Claim(method.payment_method_ref, payment.card_authorization_ref, decided_at)

    async def _refuse(
        self,
        session: AsyncSession,
        ctx: ToolContext,
        args: PayPurchaseOrderArgs,
        payment: PoPayment,
        state: Literal["expired", "failed"],
        code: str,
    ) -> _Done:
        """Not charged, and never will be: the payment ends here."""
        payment.state = state
        payment.failure_code = code
        payment.updated_at = func.now()
        if state == "failed":
            session.add(_event(ctx, args, payment.approval_id, PAYMENT_FAILED, failure_code=code))
        await session.flush()
        logger.warning(
            "payment %s %s code=%s tenant=%s: not charged", payment.id, state, code, ctx.tenant_id
        )
        return _Done(self._result(args, state, code))

    # --- 2. Charge ------------------------------------------------------------------------

    async def _charge(
        self, ctx: ToolContext, args: PayPurchaseOrderArgs, claim: _Claim
    ) -> PayPurchaseOrderResult:
        card = self._deps.providers.card
        # From the approved arguments only: a retry sends the same requests, byte for byte.
        amount = Decimal(args.charge_amount)
        description = f"Bido {args.purchase_order_ref}"
        authorization_ref = claim.authorization_ref
        if authorization_ref is None:
            try:
                authorization = await authorize_payment_card(card, claim.payment_method_ref, args)
            except PaymentRefused:
                return await self._record(ctx, args, "failed", code="card_authorization_failed")
            except ProviderError as exc:
                if exc.retryable:
                    raise
                return await self._record(ctx, args, "failed", code="card_authorization_failed")
            if not authorization.usable:
                if args.card_rail == "vic":
                    # Created just now: nobody approved it with the passkey.
                    return await self._record(
                        ctx, args, "failed", code=CARD_NOT_VERIFIED, allowance=authorization.ref
                    )
                return await self._record(
                    ctx, args, "declined", code="card_rail_unavailable", allowance=authorization.ref
                )
            await self._store_allowance(ctx, args.payment_id, authorization.ref)
            authorization_ref = authorization.ref
        try:
            charge = await card.charge(
                authorization_ref=authorization_ref,
                payment_id=args.payment_id,
                amount=amount,
                currency=args.charge_currency,
                description=description,
                idempotency_key=f"payment:{args.payment_id}",
            )
        except CardNotVerified:
            # The cardholder never approved the allowance with the Visa passkey: nothing minted.
            return await self._record(ctx, args, "failed", code=CARD_NOT_VERIFIED)
        except CardCredentialLost:
            if self._now() - claim.decided_at < CREDENTIAL_GRACE:
                # Stripe's search may not show the charge yet: asked again on the retry.
                logger.warning("payment %s: no charge found yet; retried", args.payment_id)
                raise
            # The single-use token was minted and nothing was charged.
            return await self._record(ctx, args, "failed", code="card_credential_lost")
        except PaymentRefused:
            # Refused before any call: nothing was charged.
            return await self._record(ctx, args, "failed", code="card_charge_failed")
        except ProviderError as exc:
            if exc.code == CHARGE_MISMATCH:
                # A charge exists, of another amount or currency: a person settles it.
                return await self._record(ctx, args, "failed", code=CHARGE_MISMATCH, charged=True)
            if exc.retryable:
                raise
            return await self._record(ctx, args, "failed", code="card_charge_failed")
        if charge.outcome == "pending":
            raise ChargePending(f"payment {args.payment_id}: the card charge is still processing")
        if charge.outcome == "declined":
            return await self._record(
                ctx, args, "declined", code=_code(charge.decline_code, "card_declined")
            )
        return await self._record(
            ctx, args, "charged", reference=charge.reference, issued_card=charge.issued_card
        )

    async def _store_allowance(self, ctx: ToolContext, payment_id: UUID, ref: str) -> None:
        """Kept before the charge, so a retry charges against the same allowance."""
        async with tenant_session(self._deps.db, ctx.tenant_id) as session:
            payment = await locked_payment(session, payment_id)
            if payment.state == "charging" and payment.card_authorization_ref is None:
                payment.card_authorization_ref = ref
                payment.updated_at = func.now()
                await session.flush()

    # --- 3. Record ------------------------------------------------------------------------

    async def _record(
        self,
        ctx: ToolContext,
        args: PayPurchaseOrderArgs,
        state: Recorded,
        *,
        code: str | None = None,
        reference: str | None = None,
        allowance: str | None = None,
        charged: bool = False,
        issued_card: IssuedCard | None = None,
    ) -> PayPurchaseOrderResult:
        """`charged`: a `failed` payment whose card WAS charged (it needs a person, and keeps
        the order from being charged again: it stays the order's live payment)."""
        async with tenant_session(self._deps.db, ctx.tenant_id) as session:
            payment = await locked_payment(session, args.payment_id)
            if payment.state != "charging":
                # Recorded by an earlier run (the executor's lock makes this unlikely).
                outcome = "already_charged" if payment.state in CHARGED_STATES else "skipped"
                return self._result(args, outcome, payment.failure_code)
            payment.state = state
            payment.updated_at = func.now()
            if allowance is not None and payment.card_authorization_ref is None:
                payment.card_authorization_ref = allowance
            if charged and state == "failed":
                payment.charged_at = self._now()
                payment.charge_reference = None
            if state == "charged":
                payment.charge_reference = reference
                payment.charged_at = self._now()
                if args.card_rail == "vic":
                    # Minted: Basis Theory found the allowance approved by the passkey.
                    if payment.card_verified_at is None:
                        payment.card_verified_at = self._now()
                    if issued_card is not None:
                        payment.issued_card_ref = issued_card.credential_ref
                        payment.issued_card_last4 = issued_card.last4
                        payment.issued_card_exp_month = issued_card.exp_month
                        payment.issued_card_exp_year = issued_card.exp_year
                session.add(
                    _event(
                        ctx,
                        args,
                        payment.approval_id,
                        PAYMENT_CHARGED,
                        charge_currency=args.charge_currency,
                        charge_amount=args.charge_amount,
                        charge_reference=reference,
                        simulated=args.charge_mode == "simulated",
                        card_rail=args.card_rail,
                    )
                )
            else:
                payment.failure_code = code
                action = PAYMENT_DECLINED if state == "declined" else PAYMENT_FAILED
                session.add(_event(ctx, args, payment.approval_id, action, failure_code=code))
            await session.flush()
        if state == "charged":
            logger.info("payment %s charged tenant=%s", args.payment_id, ctx.tenant_id)
            await self._wake(ctx.tenant_id, args.payment_id, "charged")
        else:
            logger.warning(
                "payment %s %s code=%s tenant=%s", args.payment_id, state, code, ctx.tenant_id
            )
        return self._result(args, state, code)

    async def _wake(self, tenant_id: UUID, payment_id: UUID, state: str) -> None:
        if self._deps.jobs is None:
            return  # the scheduled sweep wakes it
        try:
            await self._deps.jobs.enqueue(payment_advance_envelope(tenant_id, payment_id, state))
        except Exception:
            # The charge is committed: the scheduled sweep wakes it (`payments.reconcile`).
            logger.exception("payment %s: its advance job was not enqueued", payment_id)

    def _now(self) -> datetime:
        return self._deps.now() if self._deps.now is not None else datetime.now(UTC)

    @staticmethod
    def _result(
        args: PayPurchaseOrderArgs,
        outcome: Literal["charged", "already_charged", "declined", "expired", "skipped", "failed"],
        code: str | None = None,
    ) -> PayPurchaseOrderResult:
        return PayPurchaseOrderResult(
            outcome=outcome, payment_id=args.payment_id, failure_code=code
        )


# --- Registration and rejection -------------------------------------------------------------


def payment_tools(
    deps: PaymentActDeps,
) -> tuple[Tool[PayPurchaseOrderArgs, PayPurchaseOrderResult]]:
    return (
        Tool(
            name=PAY_PURCHASE_ORDER,
            description=(
                "Charge the approved card for one purchase order; the escrow is funded after."
            ),
            input_model=PayPurchaseOrderArgs,
            output_model=PayPurchaseOrderResult,
            kind="act",
            handler=_PayPurchaseOrder(deps),
        ),
    )


def register_payment_act_tools(registry: ActToolRegistry, deps: PaymentActDeps) -> None:
    for tool in payment_tools(deps):
        registry.register(tool)


async def release_rejected_payment(session: AsyncSession, approval: Approval) -> None:
    """A rejected payment is not charged: its row becomes `rejected`."""
    await session.execute(
        update(PoPayment)
        .where(PoPayment.approval_id == approval.id, PoPayment.state == "pending_approval")
        .values(state="rejected", updated_at=func.now())
    )


PAYMENT_REJECT_HOOKS: Final[dict[str, RejectHook]] = {
    PAY_PURCHASE_ORDER: release_rejected_payment,
}
