"""Which currency a new RFQ is in: stated, from the analysis, from the purchase history.

Never a silent default: when nothing tells, creating the RFQ is refused so the
buyer is asked. Changing a draft's currency converts its negotiation limits.
"""

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import FxRate, PurchaseOrder, Rfq, RfqLine, Supplier
from bido_back.fx import DailyRates, upsert_rates
from bido_back.rfqs import service
from bido_back.rfqs.currency import ASK_THE_BUYER
from bido_back.rfqs.errors import CurrencyRequired, InvalidState, UnknownCurrency
from bido_back.rfqs.schemas import UpdateRfqRequest
from tests.approvals.conftest import People

from .conftest import FakeIntake

pytestmark = pytest.mark.db

# Rates far in the past, so no other test's or real rate is "the latest" for these days.
RATE_DAY = date(2000, 1, 3)
NO_RATE_DAY = date(1990, 1, 2)


def buyer(people: People) -> Principal:
    return Principal(tenant_id=people.tenant, user_id=people.owner, role="owner")


async def purchases(engine: AsyncEngine, tenant_id: UUID, *currencies: str) -> None:
    """Imported purchase orders of one supplier, one per currency given."""
    async with tenant_session(engine, tenant_id) as session:
        supplier = Supplier(tenant_id=tenant_id, name="Rolamentos Paulista Ltda")
        session.add(supplier)
        await session.flush()
        for number, currency in enumerate(currencies, start=1):
            session.add(
                PurchaseOrder(
                    tenant_id=tenant_id,
                    po_number=f"PO-{number}",
                    supplier_id=supplier.id,
                    currency=currency,
                    ordered_at=date(2026, 8, number),
                )
            )


async def rfq_count(engine: AsyncEngine, tenant_id: UUID) -> int:
    async with tenant_session(engine, tenant_id) as session:
        return await session.scalar(select(func.count()).select_from(Rfq)) or 0


@pytest_asyncio.fixture
async def brl_rate(app_engine: AsyncEngine, migrator_engine: AsyncEngine) -> AsyncIterator[None]:
    await upsert_rates(app_engine, DailyRates(RATE_DAY, {"BRL": Decimal("6.0")}))
    yield
    async with migrator_engine.begin() as conn:
        await conn.execute(delete(FxRate).where(FxRate.rate_date == RATE_DAY))


# --- Creating ------------------------------------------------------------------------


async def test_rfq_from_text_is_in_the_currency_the_company_buys_in(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    # A buyer in São José dos Campos: every past purchase in BRL, nothing said about currency.
    await purchases(app_engine, rfq_rows.tenant, "BRL", "BRL", "BRL")
    intake = FakeIntake()

    created = await service.create_rfq_from_text(
        app_engine, buyer(rfq_rows), "400 rolamentos 6205", intake
    )

    assert (created.base_currency, created.currency_source) == ("BRL", "purchase_history")
    assert intake.calls[0][0].base_currency == "BRL"


async def test_without_history_or_a_stated_currency_nothing_is_created(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    intake = FakeIntake()

    with pytest.raises(CurrencyRequired) as refused:
        await service.create_rfq_from_text(app_engine, buyer(rfq_rows), "400 rolamentos", intake)

    assert str(refused.value) == ASK_THE_BUYER
    assert "ask the buyer which currency this RFQ is in (e.g. BRL, EUR, USD)" in ASK_THE_BUYER
    assert intake.calls == []  # refused before the model runs
    assert await rfq_count(app_engine, rfq_rows.tenant) == 0


async def test_a_stated_currency_wins_and_is_validated(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    await purchases(app_engine, rfq_rows.tenant, "BRL", "BRL", "BRL")

    created = await service.create_rfq_from_text(
        app_engine, buyer(rfq_rows), "400 rolamentos", FakeIntake(), currency=" usd "
    )
    assert (created.base_currency, created.currency_source) == ("USD", "stated")

    with pytest.raises(UnknownCurrency):
        await service.create_rfq_from_text(
            app_engine, buyer(rfq_rows), "400 rolamentos", FakeIntake(), currency="REAIS"
        )


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        (("BRL", "BRL", "EUR"), "BRL"),  # two thirds: clear enough
        (("BRL", "BRL", "EUR", "EUR"), None),  # no clear winner: ask
        (("BRL", "EUR", "USD"), None),
        (("XAF", "XAF", "XAF"), None),  # not a currency Bido can convert: ask
    ],
)
async def test_the_history_decides_only_with_a_clear_majority(
    app_engine: AsyncEngine, rfq_rows: People, history: tuple[str, ...], expected: str | None
) -> None:
    await purchases(app_engine, rfq_rows.tenant, *history)

    if expected is None:
        with pytest.raises(CurrencyRequired):
            await service.create_rfq_from_text(app_engine, buyer(rfq_rows), "x", FakeIntake())
        return
    created = await service.create_rfq_from_text(app_engine, buyer(rfq_rows), "x", FakeIntake())
    assert created.base_currency == expected


async def test_another_tenants_history_does_not_count(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    await purchases(app_engine, rfq_rows.other_tenant, "BRL", "BRL", "BRL")

    with pytest.raises(CurrencyRequired):
        await service.create_rfq_from_text(app_engine, buyer(rfq_rows), "x", FakeIntake())


# --- Changing a draft's currency -------------------------------------------------------


async def eur_rfq_with_limits(engine: AsyncEngine, people: People) -> UUID:
    created = await service.create_rfq_from_text(
        engine, buyer(people), "MLCC and connectors", FakeIntake(), currency="EUR"
    )
    async with tenant_session(engine, people.tenant) as session:
        await session.execute(
            update(RfqLine)
            .where(RfqLine.rfq_id == created.id, RfqLine.position == 1)
            .values(target_unit_price=Decimal("10"), max_unit_price=Decimal("12.5"))
        )
    return created.id


async def limits(engine: AsyncEngine, tenant_id: UUID, rfq_id: UUID) -> list[object]:
    async with tenant_session(engine, tenant_id) as session:
        rows = await session.scalars(
            select(RfqLine).where(RfqLine.rfq_id == rfq_id).order_by(RfqLine.position)
        )
        return [(line.target_unit_price, line.max_unit_price) for line in rows]


async def test_changing_the_currency_converts_the_negotiation_limits(
    app_engine: AsyncEngine, rfq_rows: People, brl_rate: None
) -> None:
    rfq_id = await eur_rfq_with_limits(app_engine, rfq_rows)

    edited = await service.edit_rfq(
        app_engine, buyer(rfq_rows), rfq_id, UpdateRfqRequest(currency="brl"), today=RATE_DAY
    )

    assert edited.detail.base_currency == "BRL"
    change = edited.currency_change
    assert change is not None
    assert (change.old, change.new, change.converted_lines, change.rate_date) == (
        "EUR",
        "BRL",
        (1,),
        RATE_DAY,
    )
    assert await limits(app_engine, rfq_rows.tenant, rfq_id) == [
        (Decimal("60.000000"), Decimal("75.000000")),
        (None, None),
    ]


async def test_without_a_rate_limits_keep_the_currency_unchanged(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    rfq_id = await eur_rfq_with_limits(app_engine, rfq_rows)

    with pytest.raises(InvalidState, match="no exchange rate"):
        await service.edit_rfq(
            app_engine,
            buyer(rfq_rows),
            rfq_id,
            UpdateRfqRequest(currency="BRL", title="Renamed"),
            today=NO_RATE_DAY,
        )

    detail = await service.get_rfq(app_engine, buyer(rfq_rows), rfq_id)
    assert detail.base_currency == "EUR" and detail.title != "Renamed"
    assert await limits(app_engine, rfq_rows.tenant, rfq_id) == [
        (Decimal("10.000000"), Decimal("12.500000")),
        (None, None),
    ]


async def test_a_draft_without_limits_changes_currency_without_rates(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    created = await service.create_rfq_from_text(
        app_engine, buyer(rfq_rows), "MLCC", FakeIntake(), currency="EUR"
    )

    edited = await service.edit_rfq(
        app_engine, buyer(rfq_rows), created.id, UpdateRfqRequest(currency="USD"), today=NO_RATE_DAY
    )
    same = await service.edit_rfq(
        app_engine, buyer(rfq_rows), created.id, UpdateRfqRequest(currency="USD")
    )

    assert edited.detail.base_currency == "USD"
    assert edited.currency_change is not None and edited.currency_change.converted_lines == ()
    assert same.currency_change is None
    with pytest.raises(InvalidState, match="cannot be cleared"):
        await service.update_rfq(
            app_engine, buyer(rfq_rows), created.id, UpdateRfqRequest(currency=None)
        )
