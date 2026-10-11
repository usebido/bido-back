"""Payment tests: the award World with a sent PO, fake providers, and cleanup.

`payment_rows` deletes the payment tables (as the migrator: the app role may not
DELETE them) before the award, RFQ and people fixtures clean up after
themselves. The helpers below create rows the way the service does, so the act
tool and pipeline tests do not depend on the service under test elsewhere.
"""

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import JsonValue
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.approvals import ApprovalProposal
from bido_back.agents.events import args_hash
from bido_back.approvals import PostgresApprovalSink
from bido_back.awards.orders import po_document
from bido_back.awards.render import content_hash
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    IssuedPurchaseOrder,
    PaymentChainTransaction,
    PaymentMethod,
    PoPayment,
    SupplierPayoutAccount,
    TenantWallet,
)
from bido_back.payments.proposals import PAY_PURCHASE_ORDER, payment_proposal_ids
from bido_back.payments.schemas import MilestoneInput, PayPurchaseOrderArgs, tooling_plan
from tests.approvals.conftest import People
from tests.awards.conftest import (
    app_engine,
    award_rows,
    migrator_engine,
    people,
    rfq_rows,
    two_tenants,
)
from tests.awards.test_awards_db import World
from tests.payments.fakes import FakeProviders

__all__ = [
    "app_engine",
    "award_rows",
    "migrator_engine",
    "people",
    "rfq_rows",
    "two_tenants",
]

PAYMENT_MODELS = (
    PaymentChainTransaction,
    PoPayment,
    TenantWallet,
    PaymentMethod,
    SupplierPayoutAccount,
)
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
USD_PER_EUR = Decimal("1.10")


@pytest_asyncio.fixture
async def payment_rows(migrator_engine: AsyncEngine, award_rows: People) -> AsyncIterator[People]:
    yield award_rows
    for tenant_id in (award_rows.tenant, award_rows.other_tenant):
        async with tenant_session(migrator_engine, tenant_id) as session:
            for model in PAYMENT_MODELS:
                await session.execute(delete(model).where(model.tenant_id == tenant_id))


@pytest.fixture
def providers() -> FakeProviders:
    return FakeProviders()


@pytest_asyncio.fixture
async def world(app_engine: AsyncEngine, payment_rows: People) -> World:
    return await World(app_engine, payment_rows).setup()


async def add_payment_method(
    engine: AsyncEngine,
    tenant_id: UUID,
    created_by: UUID,
    *,
    status: Literal["active", "pending", "failed", "removed"] = "active",
) -> UUID:
    token = uuid4().hex[:12]
    async with tenant_session(engine, tenant_id) as session:
        row = PaymentMethod(
            tenant_id=tenant_id,
            created_by=created_by,
            payment_method_ref=f"pm_{token}",
            fingerprint=f"fp-{token}",
            brand="visa",
            last4="4242",
            exp_month=12,
            exp_year=2030,
            funding="credit",
            status=status,
            removed_at=datetime.now(UTC) if status == "removed" else None,
        )
        session.add(row)
        await session.flush()
        return row.id


async def add_payout_account(
    engine: AsyncEngine, tenant_id: UUID, supplier_id: UUID, created_by: UUID
) -> UUID:
    async with tenant_session(engine, tenant_id) as session:
        row = SupplierPayoutAccount(
            tenant_id=tenant_id,
            supplier_id=supplier_id,
            country="CN",
            currency="CNY",
            details_ciphertext=b"sealed:" + uuid4().bytes,
            bank_fingerprint=secrets.token_hex(32),
            beneficiary_display="Shen… Co., Ltd.",
            bank_name="Bank of China",
            swift_bic="BKCHCNBJ45A",
            account_last4="4567",
            created_by=created_by,
        )
        session.add(row)
        await session.flush()
        return row.id


def chain_po_id(po_ref: str) -> str:
    return f"{po_ref}-{''.join(secrets.choice(CROCKFORD) for _ in range(4))}"


@dataclass(frozen=True, slots=True)
class ProposedPayment:
    payment_id: UUID
    approval_id: UUID
    args: PayPurchaseOrderArgs


async def propose_payment(
    engine: AsyncEngine,
    po: IssuedPurchaseOrder,
    *,
    requested_by: UUID,
    payment_method_id: UUID,
    payout_account_id: UUID,
    milestones: list[MilestoneInput] | None = None,
    charge_mode: Literal["stripe", "simulated"] = "stripe",
    quote_ttl: timedelta = timedelta(hours=24),
    attempt: int = 0,
    card_rail: Literal["spt", "vic"] = "spt",
) -> ProposedPayment:
    """A pending `pay_purchase_order` approval and its `po_payments` row (as the service does:
    the tooling plan unless `milestones` are given)."""
    tenant_id = po.tenant_id
    async with tenant_session(engine, tenant_id) as session:
        fresh = await session.get(IssuedPurchaseOrder, po.id)
        assert fresh is not None
        doc_hash = content_hash(await po_document(session, fresh))
        account = await session.get(SupplierPayoutAccount, payout_account_id)
        method = await session.get(PaymentMethod, payment_method_id)
        assert account is not None and method is not None
        total = fresh.total
        currency = fresh.currency
        rfq_id, invite_id, supplier_id, ref = (
            fresh.rfq_id,
            fresh.invite_id,
            fresh.supplier_id,
            fresh.ref,
        )
        fingerprint, last4, payout_currency = (
            account.bank_fingerprint,
            account.account_last4,
            account.currency,
        )
        brand, card_last4 = method.brand, method.last4
    rate = Decimal(1) if currency == "USD" else USD_PER_EUR
    charge = (total * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    payment_id = uuid4()
    chosen = milestones or tooling_plan()
    args = PayPurchaseOrderArgs(
        payment_id=payment_id,
        purchase_order_id=po.id,
        purchase_order_ref=ref,
        rfq_id=rfq_id,
        invite_id=invite_id,
        supplier_id=supplier_id,
        requested_by=requested_by,
        content_hash=doc_hash,
        po_currency=currency,
        po_total=f"{total:f}",
        charge_currency="USD",
        charge_amount=f"{charge:f}",
        fx_rate=None if currency == "USD" else f"{rate:f}",
        fx_rate_date=None if currency == "USD" else datetime.now(UTC).date(),
        quote_expires_at=(datetime.now(UTC) + quote_ttl).replace(microsecond=0),
        token_amount=str(int(charge * 10**6)),
        payment_method_id=payment_method_id,
        card_brand=brand,
        card_last4=card_last4,
        payout_account_id=payout_account_id,
        payout_fingerprint=fingerprint,
        payout_currency=payout_currency,
        payout_account_last4=last4,
        chain_po_id=chain_po_id(ref),
        milestones=chosen,
        charge_mode=charge_mode,
        card_rail=card_rail,
    )
    payload = cast(dict[str, JsonValue], args.model_dump(mode="json"))
    run_id, call_id = payment_proposal_ids(po.id, attempt)
    await PostgresApprovalSink(engine).request_approval(
        ApprovalProposal(
            tenant_id=tenant_id,
            run_id=run_id,
            session_id=f"rfq:{rfq_id}",
            tool_call_id=call_id,
            tool=PAY_PURCHASE_ORDER,
            args=payload,
            args_hash=args_hash(payload),
            requested_by=requested_by,
        )
    )
    async with tenant_session(engine, tenant_id) as session:
        approval_id = await session.scalar(
            select(Approval.id).where(
                Approval.run_id == str(run_id), Approval.tool_call_id == call_id
            )
        )
        assert approval_id is not None
        session.add(
            PoPayment(
                id=payment_id,
                tenant_id=tenant_id,
                purchase_order_id=po.id,
                approval_id=approval_id,
                requested_by=requested_by,
                payment_method_id=payment_method_id,
                payout_account_id=payout_account_id,
                state="pending_approval",
                po_currency=currency,
                po_total=total,
                charge_currency="USD",
                charge_amount=charge,
                fx_rate=None if currency == "USD" else rate,
                fx_rate_date=args.fx_rate_date,
                quote_expires_at=args.quote_expires_at,
                token_amount=Decimal(args.token_amount),
                chain_po_id=args.chain_po_id,
                po_hash=doc_hash,
                milestones=[m.model_dump() for m in chosen],
                charge_mode=charge_mode,
                card_rail=card_rail,
            )
        )
        await session.flush()
    return ProposedPayment(payment_id=payment_id, approval_id=approval_id, args=args)


async def load_payment(engine: AsyncEngine, tenant_id: UUID, payment_id: UUID) -> PoPayment:
    async with tenant_session(engine, tenant_id) as session:
        row = await session.get(PoPayment, payment_id)
        assert row is not None
        return row


async def chain_transactions(
    engine: AsyncEngine, tenant_id: UUID, payment_id: UUID
) -> list[PaymentChainTransaction]:
    async with tenant_session(engine, tenant_id) as session:
        return list(
            await session.scalars(
                select(PaymentChainTransaction)
                .where(PaymentChainTransaction.payment_id == payment_id)
                .order_by(PaymentChainTransaction.created_at, PaymentChainTransaction.attempt)
            )
        )
