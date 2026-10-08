"""list_suppliers and purchase_history: the company's imported data, in any conversation.

The copilot used to answer "who are our suppliers" and "what did we pay" only
through a BOM-based analysis, so without a BOM in reach it told the buyer the
data was not "in this conversation" and asked for the file again.
"""

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import Item, PoLine, PurchaseOrder
from tests.approvals.conftest import People

from .conftest import buyer_of, context_of, make_tools, other_buyer_of, supplier

pytestmark = pytest.mark.db


@pytest_asyncio.fixture
async def items_cleanup(
    migrator_engine: AsyncEngine, copilot_rows: People
) -> AsyncIterator[People]:
    yield copilot_rows
    # Suppliers (and with them purchase orders and their lines) go with
    # copilot_rows; the parts stay unless removed here.
    for tenant_id in (copilot_rows.tenant, copilot_rows.other_tenant):
        async with tenant_session(migrator_engine, tenant_id) as session:
            await session.execute(delete(Item).where(Item.tenant_id == tenant_id))


async def _item(
    engine: AsyncEngine, principal: Principal, part: str, category: str, mpn: str | None = None
) -> UUID:
    async with tenant_session(engine, principal.tenant_id) as session:
        item = Item(
            tenant_id=principal.tenant_id,
            part_number=part,
            description=f"{category} part {part}",
            mpn=mpn,
            category=category,
            unit="pcs",
        )
        session.add(item)
        await session.flush()
        return item.id


async def _purchase(
    engine: AsyncEngine,
    principal: Principal,
    supplier_id: UUID,
    item_id: UUID,
    *,
    po: str,
    on: date,
    price: str,
    currency: str = "EUR",
) -> None:
    async with tenant_session(engine, principal.tenant_id) as session:
        order = PurchaseOrder(
            tenant_id=principal.tenant_id,
            po_number=po,
            supplier_id=supplier_id,
            currency=currency,
            ordered_at=on,
        )
        session.add(order)
        await session.flush()
        session.add(
            PoLine(
                tenant_id=principal.tenant_id,
                purchase_order_id=order.id,
                line_no=10,
                item_id=item_id,
                quantity=Decimal(100),
                unit_price=Decimal(price),
            )
        )


async def test_list_suppliers_shows_contacts_and_what_was_bought_from_each(
    app_engine: AsyncEngine, items_cleanup: People
) -> None:
    me = buyer_of(items_cleanup)
    tag = uuid4().hex[:6]
    copper = await supplier(app_engine, me, f"Cobre{tag}")
    magnets = await supplier(app_engine, me, f"Magnetica{tag}")
    await supplier(app_engine, me, f"Unused{tag}")
    wire = await _item(app_engine, me, f"FI-CU-{tag}", "wire")
    magnet = await _item(app_engine, me, f"IM-N48-{tag}", "magnet", mpn=f"N48H-{tag}")
    await _purchase(
        app_engine, me, copper.id, wire, po=f"45-{tag}-1", on=date(2026, 5, 2), price="9.10"
    )
    await _purchase(
        app_engine, me, magnets.id, magnet, po=f"45-{tag}-2", on=date(2026, 7, 1), price="1.92"
    )
    tools = make_tools(app_engine)
    ctx = context_of(me)

    everyone = await tools.call(ctx, "list_suppliers")
    by_category = await tools.call(ctx, "list_suppliers", category="Magnet")
    by_mpn = await tools.call(ctx, "list_suppliers", part_number=f"n48h {tag}")

    # Most recent purchase first; a supplier never bought from comes last.
    assert [s.name for s in everyone.suppliers] == [
        f"Magnetica{tag}",
        f"Cobre{tag}",
        f"Unused{tag}",
    ]
    first = everyone.suppliers[0]
    assert first.contacts[0].email == f"sales@magnetica{tag}.example.test"
    assert (first.purchase_orders, first.last_ordered_at, first.categories) == (
        1,
        date(2026, 7, 1),
        ["magnet"],
    )
    assert [s.name for s in by_category.suppliers] == [f"Magnetica{tag}"]
    assert [s.name for s in by_mpn.suppliers] == [f"Magnetica{tag}"]


async def test_purchase_history_gives_the_last_price_paid_per_part(
    app_engine: AsyncEngine, items_cleanup: People
) -> None:
    me = buyer_of(items_cleanup)
    tag = uuid4().hex[:6]
    old, new = (
        await supplier(app_engine, me, f"Old{tag}"),
        await supplier(app_engine, me, f"New{tag}"),
    )
    bearing = await _item(app_engine, me, f"RL-6206-{tag}", "bearing")
    await _purchase(
        app_engine, me, old.id, bearing, po=f"P1-{tag}", on=date(2026, 1, 5), price="2.40"
    )
    await _purchase(
        app_engine,
        me,
        new.id,
        bearing,
        po=f"P2-{tag}",
        on=date(2026, 6, 9),
        price="2.10",
        currency="BRL",
    )
    tools = make_tools(app_engine)

    found = await tools.call(
        context_of(me), "purchase_history", part_numbers=[f"rl 6206 {tag}", "NOPE-1"]
    )
    recent = await tools.call(context_of(me), "purchase_history")

    assert found.not_found == ["NOPE-1"]
    [part] = found.parts
    assert part.part_number == f"RL-6206-{tag}"
    assert (part.last_paid.supplier_name, part.last_paid.unit_price, part.last_paid.currency) == (
        f"New{tag}",
        Decimal("2.100000"),
        "BRL",
    )
    assert [p.po_number for p in part.purchases] == [f"P2-{tag}", f"P1-{tag}"]
    assert part.purchase_count == 2
    assert [p.part_number for p in recent.parts] == [f"RL-6206-{tag}"]


async def test_company_data_tools_only_see_the_callers_tenant(
    app_engine: AsyncEngine, items_cleanup: People
) -> None:
    me, them = buyer_of(items_cleanup), other_buyer_of(items_cleanup)
    tag = uuid4().hex[:6]
    theirs = await supplier(app_engine, them, f"Theirs{tag}")
    part = await _item(app_engine, them, f"X-{tag}", "secret")
    await _purchase(
        app_engine, them, theirs.id, part, po=f"T-{tag}", on=date(2026, 3, 3), price="5"
    )
    tools = make_tools(app_engine)

    suppliers = await tools.call(context_of(me), "list_suppliers", supplier=f"Theirs{tag}")
    paid = await tools.call(context_of(me), "purchase_history", part_numbers=[f"X-{tag}"])

    assert suppliers.suppliers == []
    assert (paid.parts, paid.not_found) == ([], [f"X-{tag}"])
