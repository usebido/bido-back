"""The payment demo's purchase orders: the demo RFQ's quote by default, one USD line on request."""

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import IssuedPurchaseOrderLine, Supplier
from bido_back.payments.demo import QUOTE_ORDER, create_debug_payment_purchase_order
from tests.approvals.conftest import People

pytestmark = pytest.mark.db

TODAY = date(2026, 10, 12)


async def test_the_default_order_is_the_demo_rfq_quote(
    app_engine: AsyncEngine, payment_rows: People
) -> None:
    principal = Principal(tenant_id=payment_rows.tenant, user_id=payment_rows.owner, role="owner")
    db = async_sessionmaker(app_engine)

    po = await create_debug_payment_purchase_order(
        db, principal, email_domain="mail.example.test", today=TODAY
    )

    assert (po.status, po.fulfilment_state) == ("sent", "sent")
    assert (po.currency, po.total) == ("BRL", Decimal("3939982.60"))
    assert po.supplier_name == "EMBALAGENS PAPELAO JUNDIAI LTDA"
    async with tenant_session(app_engine, payment_rows.tenant) as session:
        lines = list(
            await session.scalars(
                select(IssuedPurchaseOrderLine)
                .where(IssuedPurchaseOrderLine.purchase_order_id == po.id)
                .order_by(IssuedPurchaseOrderLine.position)
            )
        )
    assert [line.position for line in lines] == list(range(1, 22))
    assert sum(line.line_total for line in lines) == po.total
    first, last = lines[0], lines[-1]
    assert (first.quantity, first.unit, first.unit_price) == (
        Decimal("5760"),
        "UN",
        Decimal("225.70"),
    )
    assert first.need_by == TODAY + timedelta(days=16)
    assert (last.description, last.need_by) == (
        QUOTE_ORDER.lines[-1].description,
        TODAY + timedelta(days=44),
    )


async def test_the_quote_reuses_the_tenants_supplier(
    app_engine: AsyncEngine, payment_rows: People
) -> None:
    """A payout account added to the supplier serves every demo order."""
    principal = Principal(tenant_id=payment_rows.tenant, user_id=payment_rows.owner, role="owner")
    db = async_sessionmaker(app_engine)

    first = await create_debug_payment_purchase_order(db, principal, email_domain="m.example.test")
    second = await create_debug_payment_purchase_order(db, principal, email_domain="m.example.test")

    assert first.supplier_id == second.supplier_id
    assert first.ref != second.ref and first.rfq_ref != second.rfq_ref
    async with tenant_session(app_engine, payment_rows.tenant) as session:
        named = await session.scalars(
            select(Supplier.id).where(Supplier.name == QUOTE_ORDER.supplier.name)
        )
        assert list(named) == [first.supplier_id]


async def test_single_line_is_one_usd_line_for_the_amount(
    app_engine: AsyncEngine, payment_rows: People
) -> None:
    principal = Principal(tenant_id=payment_rows.tenant, user_id=payment_rows.owner, role="owner")

    po = await create_debug_payment_purchase_order(
        async_sessionmaker(app_engine),
        principal,
        email_domain="mail.example.test",
        scenario="single_line",
        amount_usd=Decimal("2.50"),
    )

    assert (po.currency, po.total) == ("USD", Decimal("2.50"))
    assert po.supplier_name == "Bido Debug Payments Supplier"
