"""Cards, supplier payout accounts and PO payment requests, in the caller's tenant.

Every read and write runs in a `tenant_session`; roles are re-read from
`memberships`. Nothing here moves money: a payment request is a pending
`pay_purchase_order` approval plus its `po_payments` row, and the worker charges
the card only after a person approved it (`payments.execute`).

- Cards (`payment_methods`): any member saves a card the browser tokenized with
  Basis Theory Elements. Bido enrolls it as an agentic payment method and keeps
  its brand, last 4 digits and expiry, never card data. A buyer pays only with a
  card they saved themselves.
- Payout accounts (`supplier_payout_accounts`): where a supplier is paid. Only an
  approver or the owner adds or disables one: changed bank details are the classic
  payment fraud. The details are sealed (encrypted) for the payout provider that will
  pay the account; Bido keeps in clear a masked view and the account's fingerprint,
  which binds every payment proposed to it.
- `request_po_payment`: refuses early whatever a person would approve in vain (the
  order is not sent or no longer open, something already pays it, the card or the
  account is not usable, no USD quote), freezes the quote, the escrow terms and the
  PO document's hash in the approval's arguments, and records the payment.
"""

import logging
import re
from collections.abc import Collection, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final, cast
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import ColumnElement, and_, func, not_, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.approvals import ApprovalProposal
from bido_back.agents.events import args_hash
from bido_back.approvals.audit import approval_audit
from bido_back.approvals.errors import ApprovalConflict
from bido_back.approvals.sink import PostgresApprovalSink
from bido_back.auth import Principal
from bido_back.awards.orders import po_document
from bido_back.awards.render import content_hash
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    AuditEvent,
    IssuedPurchaseOrder,
    Membership,
    PaymentChainTransaction,
    PaymentMethod,
    PoPayment,
    Supplier,
    SupplierPayoutAccount,
    User,
)
from bido_back.decimals import format_decimal
from bido_back.payments.chain import hashes
from bido_back.payments.config import PaymentProviders, explorer_tx_url, payments_enabled
from bido_back.payments.errors import (
    PaymentError,
    PaymentForbidden,
    PaymentInvalidState,
    PaymentNotFound,
    PaymentRefused,
)
from bido_back.payments.ports import (
    BankAccountDetails,
    BankDetailsCipher,
    CardConsumer,
    CardGateway,
    EnrolledCard,
)
from bido_back.payments.proposals import (
    DEFAULT_MILESTONE_NAME,
    PAY_PURCHASE_ORDER,
    PAYMENT_REQUESTED,
    payment_proposal_ids,
)
from bido_back.payments.quote import CHARGE_CURRENCY, quote_charge, token_base_units
from bido_back.payments.schemas import (
    BPS_TOTAL,
    CreatePayoutAccountRequest,
    EnrollPaymentMethodRequest,
    MilestoneInput,
    PaymentRequested,
    PayPurchaseOrderArgs,
    PayPurchaseOrderRequest,
)
from bido_back.payments.views import (
    CardSetupView,
    ChainSetupView,
    ChainStep,
    ChainTransactionState,
    PaymentApprovalView,
    PaymentCardView,
    PaymentChainTransactionView,
    PaymentChainView,
    PaymentMethodStatus,
    PaymentMethodView,
    PaymentMilestone,
    PaymentPayoutView,
    PaymentsConfigView,
    PaymentState,
    PaymentTokenView,
    PaymentView,
    PayoutAccountStatus,
    PayoutAccountView,
    PurchaseOrderPaymentSummary,
)
from bido_back.rfqs.audit import rfq_event
from bido_back.settings import BASIS_THEORY_TEST_HOST, Settings

logger = logging.getLogger(__name__)

Db = AsyncEngine | async_sessionmaker[AsyncSession]

MANAGER_ROLES: Final = frozenset({"approver", "owner"})
_OPEN_APPROVAL: Final = ("pending", "approved")
# A payment that ended before any money moved; every other payment is live (one per PO).
_ENDED_STATES: Final = ("rejected", "expired", "declined")
# A card cannot be removed while a payment may still charge it.
_CHARGEABLE_STATES: Final = ("pending_approval", "charging")
# An order nobody pays any more.
_UNPAYABLE_FULFILMENT: Final = ("cancelled", "closed")
# What the masked views keep of a beneficiary's name and of an account number.
_NAME_SHOWN: Final = 4
_ACCOUNT_SHOWN: Final = 4
_MIN_ACCOUNT_CHARS: Final = 4
_ACCOUNT_NOISE: Final = re.compile(r"[\s-]")
_CARD_LAST4: Final = re.compile(r"[0-9]{4}")

PAYMENT_METHOD_ADDED: Final = "payment_method.added"
PAYMENT_METHOD_REMOVED: Final = "payment_method.removed"
PAYOUT_ACCOUNT_ADDED: Final = "supplier.payout_account_added"
PAYOUT_ACCOUNT_DISABLED: Final = "supplier.payout_account_disabled"


# --- Configuration ---------------------------------------------------------------------


def payments_config_view(settings: Settings) -> PaymentsConfigView:
    """What the browser needs to show payments (public values only); nothing when off."""
    if not payments_enabled(settings):
        return PaymentsConfigView(enabled=False, card=None, chain=None)
    test = urlsplit(settings.bt_api_base).hostname == BASIS_THEORY_TEST_HOST
    return PaymentsConfigView(
        enabled=True,
        card=CardSetupView(
            provider="basis_theory",
            environment="test" if test else "production",
            public_key=settings.bt_public_key,
            simulated=settings.payments_card_charge == "simulated",
        ),
        chain=ChainSetupView(
            cluster=settings.solana_cluster,
            escrow_program_id=settings.solana_escrow_program_id,
            stablecoin_mint=settings.solana_stablecoin_mint,
            stablecoin_decimals=settings.solana_stablecoin_decimals,
        ),
    )


# --- Shared ------------------------------------------------------------------------------


async def _role(session: AsyncSession, principal: Principal) -> str | None:
    return await session.scalar(
        select(Membership.role).where(
            Membership.tenant_id == principal.tenant_id,
            Membership.user_id == principal.user_id,
        )
    )


async def _require_manager(session: AsyncSession, principal: Principal) -> None:
    if await _role(session, principal) not in MANAGER_ROLES:
        raise PaymentForbidden("the approver or owner role is required")


async def _supplier(session: AsyncSession, supplier_id: UUID) -> Supplier:
    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        raise PaymentNotFound(f"supplier {supplier_id} not found")
    return supplier


def _live() -> ColumnElement[bool]:
    """SQL: the payment still counts against its PO (the partial unique index of 0040)."""
    return not_(
        or_(
            PoPayment.state.in_(_ENDED_STATES),
            and_(PoPayment.state == "failed", PoPayment.charged_at.is_(None)),
        )
    )


def _is_live(payment: PoPayment) -> bool:
    if payment.state in _ENDED_STATES:
        return False
    return not (payment.state == "failed" and payment.charged_at is None)


def _needs_attention(payment: PoPayment) -> bool:
    """Charged, then failed before the escrow was funded: a person must act."""
    return payment.state == "failed" and payment.charged_at is not None


# --- Cards --------------------------------------------------------------------------------


def _method_view(row: PaymentMethod) -> PaymentMethodView:
    return PaymentMethodView(
        id=row.id,
        brand=row.brand,
        last4=row.last4,
        exp_month=row.exp_month,
        exp_year=row.exp_year,
        funding=row.funding,
        status=cast(PaymentMethodStatus, row.status),
        created_by=row.created_by,
        created_at=row.created_at,
    )


async def enroll_payment_method(
    db: Db,
    principal: Principal,
    body: EnrollPaymentMethodRequest,
    card: CardGateway,
    *,
    consumer_country: str,
) -> PaymentMethodView:
    """Save a card the browser tokenized: enrolled at Basis Theory, masked here. Any member."""
    async with tenant_session(db, principal.tenant_id) as session:
        email = await session.scalar(select(User.email).where(User.id == principal.user_id))
    enrolled = await card.enroll(
        token_id=body.token_id,
        consumer=CardConsumer(
            id=principal.user_id, email=email or None, country_code=consumer_country
        ),
        idempotency_key=f"payment-method:{principal.tenant_id}:{body.token_id}",
    )
    if enrolled.status == "failed":
        await _forget_card(card, enrolled.payment_method_ref)
        raise PaymentRefused("this card cannot be charged by Bido")
    try:
        row = await _save_card(db, principal, enrolled)
    except PaymentInvalidState:
        # Keep the enrollment when a saved row already uses it (a replayed request).
        if not await _card_ref_saved(db, principal.tenant_id, enrolled.payment_method_ref):
            await _forget_card(card, enrolled.payment_method_ref)
        raise
    logger.info(
        "payment method added tenant=%s payment_method=%s status=%s",
        principal.tenant_id,
        row.id,
        row.status,
    )
    return _method_view(row)


async def _save_card(db: Db, principal: Principal, enrolled: EnrolledCard) -> PaymentMethod:
    """The new row; `PaymentInvalidState` when the company already saved this card."""
    taken = PaymentInvalidState("this card is already saved")
    last4 = enrolled.last4 if enrolled.last4 and _CARD_LAST4.fullmatch(enrolled.last4) else None
    month = enrolled.exp_month if enrolled.exp_month and 1 <= enrolled.exp_month <= 12 else None
    async with tenant_session(db, principal.tenant_id) as session:
        duplicate = await session.scalar(
            select(PaymentMethod.id)
            .where(
                PaymentMethod.fingerprint == enrolled.fingerprint,
                PaymentMethod.status != "removed",
            )
            .limit(1)
        )
        if duplicate is not None:
            raise taken
        row = PaymentMethod(
            tenant_id=principal.tenant_id,
            created_by=principal.user_id,
            provider="basis_theory",
            payment_method_ref=enrolled.payment_method_ref,
            fingerprint=enrolled.fingerprint,
            brand=enrolled.brand,
            last4=last4,
            exp_month=month,
            exp_year=enrolled.exp_year,
            funding=enrolled.funding,
            status=enrolled.status,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError:
            # A concurrent save of the same card, or this enrollment is already stored.
            raise taken from None
        session.add(
            AuditEvent(
                tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id,
                action=PAYMENT_METHOD_ADDED,
                payload={"payment_method_id": str(row.id), "status": row.status},
            )
        )
        await session.flush()
        await session.refresh(row)
        return row


async def _card_ref_saved(db: Db, tenant_id: UUID, payment_method_ref: str) -> bool:
    async with tenant_session(db, tenant_id) as session:
        found = await session.scalar(
            select(PaymentMethod.id)
            .where(PaymentMethod.payment_method_ref == payment_method_ref)
            .limit(1)
        )
    return found is not None


async def _forget_card(card: CardGateway, payment_method_ref: str) -> None:
    """Best effort: drop an enrollment Bido does not keep. Alone it can charge nothing."""
    try:
        await card.remove(payment_method_ref)
    except PaymentError as exc:
        logger.warning("payment method %s not removed at the provider: %s", payment_method_ref, exc)


async def list_payment_methods(db: Db, principal: Principal) -> list[PaymentMethodView]:
    """The company's saved cards, newest first (a buyer pays only with their own)."""
    async with tenant_session(db, principal.tenant_id) as session:
        rows = await session.scalars(
            select(PaymentMethod)
            .where(PaymentMethod.status != "removed")
            .order_by(PaymentMethod.created_at.desc(), PaymentMethod.id)
        )
        return [_method_view(row) for row in rows]


async def remove_payment_method(
    db: Db, principal: Principal, payment_method_id: UUID, card: CardGateway
) -> None:
    """Remove a card: whoever saved it, an approver or the owner; not while a payment
    waiting for approval or being charged uses it."""
    async with tenant_session(db, principal.tenant_id) as session:
        row = await session.scalar(
            select(PaymentMethod).where(PaymentMethod.id == payment_method_id).with_for_update()
        )
        if row is None or row.status == "removed":
            raise PaymentNotFound(f"payment method {payment_method_id} not found")
        if row.created_by != principal.user_id:
            if await _role(session, principal) not in MANAGER_ROLES:
                raise PaymentForbidden(
                    "only the person who saved the card, an approver or the owner may remove it"
                )
        in_use = await session.scalar(
            select(PoPayment.id)
            .where(
                PoPayment.payment_method_id == payment_method_id,
                PoPayment.state.in_(_CHARGEABLE_STATES),
            )
            .limit(1)
        )
        if in_use is not None:
            raise PaymentInvalidState(
                f"payment method {payment_method_id} is used by payment {in_use}, which is "
                "waiting for approval or being charged"
            )
        now = datetime.now(UTC)
        row.status = "removed"
        row.removed_at = now
        row.updated_at = now
        session.add(
            AuditEvent(
                tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id,
                action=PAYMENT_METHOD_REMOVED,
                payload={"payment_method_id": str(payment_method_id)},
            )
        )
        await session.flush()
        reference = row.payment_method_ref
    # After the commit: the card is unusable here whatever the provider answers.
    await _forget_card(card, reference)


# --- Payout accounts ----------------------------------------------------------------------


def _normalized(code: str) -> str:
    """An account number or bank code as one string: no spaces or hyphens, upper case."""
    return _ACCOUNT_NOISE.sub("", code).upper()


def _masked_name(name: str) -> str:
    return name if len(name) <= _NAME_SHOWN else f"{name[:_NAME_SHOWN]}…"


def _account_view(row: SupplierPayoutAccount) -> PayoutAccountView:
    return PayoutAccountView(
        id=row.id,
        supplier_id=row.supplier_id,
        method="bank_transfer",
        country=row.country,
        currency=row.currency,
        beneficiary_display=row.beneficiary_display,
        bank_name=row.bank_name,
        swift_bic=row.swift_bic,
        account_last4=row.account_last4,
        status=cast(PayoutAccountStatus, row.status),
        created_by=row.created_by,
        created_at=row.created_at,
        disabled_at=row.disabled_at,
    )


async def create_payout_account(
    db: Db,
    principal: Principal,
    supplier_id: UUID,
    body: CreatePayoutAccountRequest,
    bank_details: BankDetailsCipher,
) -> PayoutAccountView:
    """Add where a supplier is paid (approver or owner). The details are sealed for the
    payout provider (`payments.bank_details`); in clear Bido keeps the masked view and the
    fingerprint a payment is bound to."""
    name = body.beneficiary_name.strip()
    bank_name = body.bank_name.strip()
    account_number = _normalized(body.account_number)
    bank_code = _normalized(body.bank_code) if body.bank_code else None
    if len(name) < 2 or len(bank_name) < 2:
        raise PaymentRefused("give the beneficiary's name and the bank's name")
    if len(account_number) < _MIN_ACCOUNT_CHARS:
        raise PaymentRefused(f"an account number has at least {_MIN_ACCOUNT_CHARS} characters")
    # The local clearing code identifies the bank where there is one (CNAPS in China).
    routing = bank_code or body.swift_bic or ""
    fingerprint = hashes.bank_fingerprint(body.country, routing, account_number)
    duplicate = PaymentInvalidState(f"supplier {supplier_id} already has this payout account")

    async with tenant_session(db, principal.tenant_id) as session:
        await _require_manager(session, principal)
        await _supplier(session, supplier_id)
        if await _active_account(session, supplier_id, fingerprint) is not None:
            raise duplicate

    details = BankAccountDetails(
        beneficiary_name=name,
        bank_name=bank_name,
        account_number=account_number,
        country=body.country,
        currency=body.currency,
        swift_bic=body.swift_bic,
        bank_code=bank_code,
        bank_address=body.bank_address,
        beneficiary_address=body.beneficiary_address,
    )
    sealed = await bank_details.seal(
        details, tenant_id=principal.tenant_id, supplier_id=supplier_id
    )

    async with tenant_session(db, principal.tenant_id) as session:
        row = SupplierPayoutAccount(
            tenant_id=principal.tenant_id,
            supplier_id=supplier_id,
            method="bank_transfer",
            country=body.country,
            currency=body.currency,
            details_ciphertext=sealed,
            bank_fingerprint=fingerprint,
            beneficiary_display=_masked_name(name),
            bank_name=bank_name,
            swift_bic=body.swift_bic,
            account_last4=account_number[-_ACCOUNT_SHOWN:],
            status="active",
            created_by=principal.user_id,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError:
            # A concurrent request added the same account first.
            raise duplicate from None
        session.add(
            AuditEvent(
                tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id,
                action=PAYOUT_ACCOUNT_ADDED,
                payload={"supplier_id": str(supplier_id), "payout_account_id": str(row.id)},
            )
        )
        await session.flush()
        await session.refresh(row)
    logger.info(
        "payout account added tenant=%s supplier=%s payout_account=%s",
        principal.tenant_id,
        supplier_id,
        row.id,
    )
    return _account_view(row)


async def _active_account(
    session: AsyncSession, supplier_id: UUID, fingerprint: str
) -> UUID | None:
    return await session.scalar(
        select(SupplierPayoutAccount.id)
        .where(
            SupplierPayoutAccount.supplier_id == supplier_id,
            SupplierPayoutAccount.bank_fingerprint == fingerprint,
            SupplierPayoutAccount.status == "active",
        )
        .limit(1)
    )


async def list_payout_accounts(
    db: Db, principal: Principal, supplier_id: UUID
) -> list[PayoutAccountView]:
    """A supplier's payout accounts, newest first, disabled ones included (masked)."""
    async with tenant_session(db, principal.tenant_id) as session:
        await _supplier(session, supplier_id)
        rows = await session.scalars(
            select(SupplierPayoutAccount)
            .where(SupplierPayoutAccount.supplier_id == supplier_id)
            .order_by(SupplierPayoutAccount.created_at.desc(), SupplierPayoutAccount.id)
        )
        return [_account_view(row) for row in rows]


async def disable_payout_account(
    db: Db, principal: Principal, supplier_id: UUID, account_id: UUID
) -> PayoutAccountView:
    """Stop paying to an account (approver or owner). A pending payment bound to it fails
    when it runs; corrected details are a new account."""
    async with tenant_session(db, principal.tenant_id) as session:
        await _require_manager(session, principal)
        row = await session.scalar(
            select(SupplierPayoutAccount)
            .where(
                SupplierPayoutAccount.id == account_id,
                SupplierPayoutAccount.supplier_id == supplier_id,
            )
            .with_for_update()
        )
        if row is None:
            raise PaymentNotFound(f"payout account {account_id} not found")
        if row.status == "active":
            row.status = "disabled"
            row.disabled_at = datetime.now(UTC)
            row.disabled_by = principal.user_id
            session.add(
                AuditEvent(
                    tenant_id=principal.tenant_id,
                    actor_user_id=principal.user_id,
                    action=PAYOUT_ACCOUNT_DISABLED,
                    payload={"supplier_id": str(supplier_id), "payout_account_id": str(row.id)},
                )
            )
            await session.flush()
            logger.info(
                "payout account disabled tenant=%s supplier=%s payout_account=%s",
                principal.tenant_id,
                supplier_id,
                row.id,
            )
        return _account_view(row)


# --- Paying a purchase order ----------------------------------------------------------------


async def _decided_attempts(session: AsyncSession, call_id: str) -> int:
    """Earlier payment proposals of this PO already decided: the next one's attempt."""
    count = await session.scalar(
        select(func.count()).where(
            Approval.tool == PAY_PURCHASE_ORDER,
            Approval.tool_call_id == call_id,
            Approval.status.not_in(_OPEN_APPROVAL),
        )
    )
    return count or 0


async def _open_approval(session: AsyncSession, call_id: str) -> Approval | None:
    return await session.scalar(
        select(Approval)
        .where(
            Approval.tool == PAY_PURCHASE_ORDER,
            Approval.tool_call_id == call_id,
            Approval.status.in_(_OPEN_APPROVAL),
        )
        .order_by(Approval.created_at.desc())
        .limit(1)
    )


async def _propose(
    db: Db,
    principal: Principal,
    *,
    ids: tuple[UUID, str],
    session_id: str,
    payload: dict[str, JsonValue],
) -> UUID:
    run_id, call_id = ids
    try:
        await PostgresApprovalSink(db).request_approval(
            ApprovalProposal(
                tenant_id=principal.tenant_id,
                run_id=run_id,
                session_id=session_id,
                tool_call_id=call_id,
                tool=PAY_PURCHASE_ORDER,
                args=payload,
                args_hash=args_hash(payload),
                requested_by=principal.user_id,
            )
        )
    except ApprovalConflict:
        raise PaymentInvalidState(
            f"{call_id}: another payment request is already waiting"
        ) from None
    async with tenant_session(db, principal.tenant_id) as session:
        approval_id = await session.scalar(
            select(Approval.id).where(
                Approval.run_id == str(run_id), Approval.tool_call_id == call_id
            )
        )
    if approval_id is None:
        raise PaymentInvalidState(f"{call_id}: the approval was not recorded")
    return approval_id


async def _withdraw(db: Db, tenant_id: UUID, approval_id: UUID) -> None:
    """Expire a payment approval whose payment row could not be recorded.

    Nobody may approve a payment Bido has no row for. Should the row exist after all
    (the commit failed only on the way back), it expires with its approval, so the
    order can be paid again. Best effort: the caller is already failing, and a person
    can still reject the approval.
    """
    try:
        async with tenant_session(db, tenant_id) as session:
            expired = list(
                await session.scalars(
                    update(Approval)
                    .where(Approval.id == approval_id, Approval.status == "pending")
                    .values(status="expired")
                    .returning(Approval)
                )
            )
            for approval in expired:
                session.add(approval_audit(approval, "approval.expired", None))
            await session.execute(
                update(PoPayment)
                .where(PoPayment.approval_id == approval_id, PoPayment.state == "pending_approval")
                .values(state="expired", updated_at=func.now())
            )
            await session.flush()
    except SQLAlchemyError as exc:
        logger.error("payment approval %s not withdrawn: %s", approval_id, type(exc).__name__)


async def request_po_payment(
    db: Db,
    principal: Principal,
    po_id: UUID,
    body: PayPurchaseOrderRequest,
    providers: PaymentProviders,
    *,
    settings: Settings,
    today: date,
) -> PaymentRequested:
    """Propose paying a sent PO by card into its escrow; a person decides (the buyer by
    default, a second person in four-eyes tenants)."""
    call_id = payment_proposal_ids(po_id)[1]
    async with tenant_session(db, principal.tenant_id) as session:
        po = await session.get(IssuedPurchaseOrder, po_id)
        if po is None:
            raise PaymentNotFound(f"purchase order {po_id} not found")
        if po.status != "sent" or po.fulfilment_state in _UNPAYABLE_FULFILMENT:
            state = po.fulfilment_state if po.status == "sent" else po.status
            raise PaymentInvalidState(
                f"purchase order {po_id} is {state}; only a sent, open order can be paid"
            )
        live = await session.scalar(
            select(PoPayment.id).where(PoPayment.purchase_order_id == po_id, _live()).limit(1)
        )
        if live is not None:
            raise PaymentInvalidState(f"purchase order {po_id} already has payment {live}")
        if await _open_approval(session, call_id) is not None:
            raise PaymentInvalidState(f"purchase order {po_id} has a payment waiting for approval")
        method = await session.get(PaymentMethod, body.payment_method_id)
        if method is None or method.created_by != principal.user_id:
            raise PaymentRefused("use a card you saved")
        if method.status != "active":
            raise PaymentRefused(f"card {method.id} is {method.status}: use an active card")
        account = await session.get(SupplierPayoutAccount, body.payout_account_id)
        if account is None or account.supplier_id != po.supplier_id:
            raise PaymentRefused("use a payout account of this order's supplier")
        if account.status != "active":
            raise PaymentRefused(f"payout account {account.id} is disabled")
        quote = await quote_charge(session, po_currency=po.currency, po_total=po.total, today=today)
        try:
            token_amount = token_base_units(quote.amount, settings.solana_stablecoin_decimals)
        except ValueError:
            raise PaymentRefused("this amount cannot be paid in the stablecoin") from None
        attempt = await _decided_attempts(session, call_id)
        expires = datetime.now(UTC) + timedelta(minutes=settings.payments_quote_ttl_minutes)
        args = PayPurchaseOrderArgs(
            payment_id=uuid4(),
            purchase_order_id=po.id,
            purchase_order_ref=po.ref,
            rfq_id=po.rfq_id,
            invite_id=po.invite_id,
            supplier_id=po.supplier_id,
            requested_by=principal.user_id,
            content_hash=content_hash(await po_document(session, po)),
            po_currency=po.currency,
            po_total=format_decimal(po.total, 2),
            charge_currency=CHARGE_CURRENCY,
            charge_amount=format_decimal(quote.amount, 2),
            fx_rate=format_decimal(quote.fx_rate) if quote.fx_rate is not None else None,
            fx_rate_date=quote.fx_rate_date,
            # Whole seconds: the expiry reads back the same from the approval and the row.
            quote_expires_at=expires.replace(microsecond=0),
            token_amount=str(token_amount),
            payment_method_id=method.id,
            card_brand=method.brand,
            card_last4=method.last4,
            payout_account_id=account.id,
            payout_fingerprint=account.bank_fingerprint,
            payout_currency=account.currency,
            payout_account_last4=account.account_last4,
            chain_po_id=hashes.new_chain_po_id(po.ref),
            milestones=body.milestones
            or [MilestoneInput(name=DEFAULT_MILESTONE_NAME, bps=BPS_TOTAL)],
            charge_mode="simulated" if providers.card.simulated else "stripe",
        )

    approval_id = await _propose(
        db,
        principal,
        ids=payment_proposal_ids(po_id, attempt),
        session_id=f"rfq:{args.rfq_id}",
        payload=cast(dict[str, JsonValue], args.model_dump(mode="json")),
    )
    try:
        async with tenant_session(db, principal.tenant_id) as session:
            payment = PoPayment(
                id=args.payment_id,
                tenant_id=principal.tenant_id,
                purchase_order_id=po_id,
                approval_id=approval_id,
                requested_by=principal.user_id,
                payment_method_id=args.payment_method_id,
                payout_account_id=args.payout_account_id,
                state="pending_approval",
                po_currency=args.po_currency,
                po_total=Decimal(args.po_total),
                charge_currency=CHARGE_CURRENCY,
                charge_amount=quote.amount,
                fx_rate=quote.fx_rate,
                fx_rate_date=quote.fx_rate_date,
                quote_expires_at=args.quote_expires_at,
                token_amount=Decimal(token_amount),
                chain_po_id=args.chain_po_id,
                po_hash=args.content_hash,
                milestones=[m.model_dump() for m in args.milestones],
                charge_mode=args.charge_mode,
            )
            session.add(payment)
            await session.flush()
            # Decided in the instant between the proposal and this row: the reject hook
            # found no row to release, so the row takes the decision itself.
            decided = await session.scalar(
                select(Approval.status).where(Approval.id == approval_id).with_for_update()
            )
            if decided in ("rejected", "expired"):
                payment.state = "rejected" if decided == "rejected" else "expired"
                await session.flush()
            session.add(
                rfq_event(
                    principal.tenant_id,
                    principal.user_id,
                    PAYMENT_REQUESTED,
                    args.rfq_id,
                    payment_id=args.payment_id,
                    approval_id=approval_id,
                    purchase_order_id=po_id,
                    purchase_order_ref=args.purchase_order_ref,
                    invite_id=args.invite_id,
                    charge_currency=CHARGE_CURRENCY,
                    charge_amount=args.charge_amount,
                    simulated=args.charge_mode == "simulated",
                )
            )
            await session.flush()
            await session.refresh(payment)
            view = await payment_view(session, payment, settings=settings)
    except Exception as exc:
        # No approval may wait for a payment that was not recorded.
        await _withdraw(db, principal.tenant_id, approval_id)
        if isinstance(exc, IntegrityError):
            # Another payment of this order was recorded in the meantime.
            raise PaymentInvalidState(f"purchase order {po_id} already has a payment") from None
        raise
    logger.info(
        "payment requested tenant=%s po=%s payment=%s approval=%s",
        principal.tenant_id,
        po_id,
        args.payment_id,
        approval_id,
    )
    return PaymentRequested(approval_id=approval_id, payment=view)


# --- Reading payments -------------------------------------------------------------------------


async def payment_view(
    session: AsyncSession, payment: PoPayment, *, settings: Settings
) -> PaymentView:
    """Everything the buyer sees of one payment: amounts, card, payout, chain record."""
    method = await session.get(PaymentMethod, payment.payment_method_id)
    account = await session.get(SupplierPayoutAccount, payment.payout_account_id)
    po_ref = await session.scalar(
        select(IssuedPurchaseOrder.ref).where(IssuedPurchaseOrder.id == payment.purchase_order_id)
    )
    if method is None or account is None or po_ref is None:
        raise PaymentNotFound(f"payment {payment.id}: its card, payout account or order is gone")
    transactions = await session.scalars(
        select(PaymentChainTransaction)
        .where(PaymentChainTransaction.payment_id == payment.id)
        .order_by(PaymentChainTransaction.created_at, PaymentChainTransaction.attempt)
    )
    cluster = settings.solana_cluster
    decimals = settings.solana_stablecoin_decimals
    return PaymentView(
        id=payment.id,
        purchase_order_id=payment.purchase_order_id,
        purchase_order_ref=po_ref,
        state=cast(PaymentState, payment.state),
        approval_id=payment.approval_id,
        requested_by=payment.requested_by,
        po_currency=payment.po_currency,
        po_total=payment.po_total,
        charge_currency=payment.charge_currency,
        charge_amount=payment.charge_amount,
        fx_rate=payment.fx_rate,
        fx_rate_date=payment.fx_rate_date,
        quote_expires_at=payment.quote_expires_at,
        simulated=payment.charge_mode == "simulated",
        card=PaymentCardView(payment_method_id=method.id, brand=method.brand, last4=method.last4),
        payout=PaymentPayoutView(
            payout_account_id=account.id,
            currency=account.currency,
            country=account.country,
            bank_name=account.bank_name,
            account_last4=account.account_last4,
        ),
        token=PaymentTokenView(
            mint=settings.solana_stablecoin_mint,
            decimals=decimals,
            amount=payment.token_amount.scaleb(-decimals),
        ),
        milestones=[PaymentMilestone.model_validate(m) for m in payment.milestones],
        chain=PaymentChainView(
            cluster=cluster,
            chain_po_id=payment.chain_po_id,
            wallet_address=payment.wallet_address,
            escrow_address=payment.escrow_address,
            vault_address=payment.vault_address,
            refund_after=payment.refund_after,
            transactions=[
                PaymentChainTransactionView(
                    step=cast(ChainStep, tx.step),
                    attempt=tx.attempt,
                    signature=tx.signature,
                    state=cast(ChainTransactionState, tx.state),
                    explorer_url=explorer_tx_url(cluster, tx.signature),
                    error=tx.error,
                    created_at=tx.created_at,
                )
                for tx in transactions
            ],
        ),
        failure_code=payment.failure_code,
        needs_attention=_needs_attention(payment),
        created_at=payment.created_at,
        charged_at=payment.charged_at,
        funded_at=payment.funded_at,
    )


async def _current_payments(
    session: AsyncSession, po_ids: Collection[UUID]
) -> dict[UUID, PoPayment]:
    """Per PO: its live payment, else its latest one. POs never paid are left out."""
    if not po_ids:
        return {}
    rows = await session.scalars(
        select(PoPayment)
        .where(PoPayment.purchase_order_id.in_(list(po_ids)))
        .order_by(PoPayment.created_at.desc(), PoPayment.id)
    )
    current: dict[UUID, PoPayment] = {}
    for payment in rows:
        chosen = current.get(payment.purchase_order_id)
        if chosen is None or (not _is_live(chosen) and _is_live(payment)):
            current[payment.purchase_order_id] = payment
    return current


async def get_po_payment(
    db: Db, principal: Principal, po_id: UUID, settings: Settings
) -> PaymentView:
    """The PO's live payment, else its latest one."""
    async with tenant_session(db, principal.tenant_id) as session:
        found = await session.scalar(
            select(IssuedPurchaseOrder.id).where(IssuedPurchaseOrder.id == po_id)
        )
        if found is None:
            raise PaymentNotFound(f"purchase order {po_id} not found")
        payment = (await _current_payments(session, [po_id])).get(po_id)
        if payment is None:
            raise PaymentNotFound(f"purchase order {po_id} has no payment")
        return await payment_view(session, payment, settings=settings)


async def payment_summaries(
    session: AsyncSession, po_ids: Collection[UUID]
) -> dict[UUID, PurchaseOrderPaymentSummary]:
    """The purchase-order views' `payment`, by PO id (live payment, else the latest)."""
    return {
        po_id: PurchaseOrderPaymentSummary(
            payment_id=payment.id,
            state=cast(PaymentState, payment.state),
            charge_currency=payment.charge_currency,
            charge_amount=payment.charge_amount,
            simulated=payment.charge_mode == "simulated",
            escrow_address=payment.escrow_address,
            funded_at=payment.funded_at,
            needs_attention=_needs_attention(payment),
        )
        for po_id, payment in (await _current_payments(session, po_ids)).items()
    }


async def queue_details(
    session: AsyncSession, approvals: Sequence[Approval]
) -> dict[UUID, PaymentApprovalView]:
    """What the approval queue shows for `pay_purchase_order`, by approval id: exactly the
    approved arguments (amount, card, payout account, milestones)."""
    parsed: dict[UUID, PayPurchaseOrderArgs] = {}
    for approval in approvals:
        if approval.tool != PAY_PURCHASE_ORDER:
            continue
        try:
            parsed[approval.id] = PayPurchaseOrderArgs.model_validate(approval.args)
        except ValueError:
            logger.error("approval %s has args that do not validate", approval.id)
    if not parsed:
        return {}
    rows = await session.execute(
        select(Supplier.id, Supplier.name).where(
            Supplier.id.in_({args.supplier_id for args in parsed.values()})
        )
    )
    names: dict[UUID, str] = {supplier_id: name for supplier_id, name in rows}
    return {
        approval_id: PaymentApprovalView(
            payment_id=args.payment_id,
            purchase_order_id=args.purchase_order_id,
            purchase_order_ref=args.purchase_order_ref,
            supplier_name=names.get(args.supplier_id),
            po_currency=args.po_currency,
            po_total=Decimal(args.po_total),
            charge_currency=args.charge_currency,
            charge_amount=Decimal(args.charge_amount),
            fx_rate=Decimal(args.fx_rate) if args.fx_rate is not None else None,
            fx_rate_date=args.fx_rate_date,
            quote_expires_at=args.quote_expires_at,
            card_brand=args.card_brand,
            card_last4=args.card_last4,
            payout_currency=args.payout_currency,
            payout_account_last4=args.payout_account_last4,
            milestones=[PaymentMilestone(name=m.name, bps=m.bps) for m in args.milestones],
            simulated=args.charge_mode == "simulated",
        )
        for approval_id, args in parsed.items()
    }
