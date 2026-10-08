"""The purchase analysis math, with hand-computed (golden) numbers. No database."""

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from bido_back.purchasing.analysis import (
    AnalysisResult,
    BomEntry,
    ItemInfo,
    LineResult,
    Params,
    Purchase,
    Stock,
    analyze,
    currency_totals,
    round_up_quantity,
)

TODAY = date(2026, 10, 7)
S1, S2, S3 = uuid4(), uuid4(), uuid4()


def item(part: str, unit: str = "pcs") -> ItemInfo:
    return ItemInfo(
        id=uuid4(),
        part_number=part,
        description=f"Part {part}",
        mpn=None,
        manufacturer=None,
        category=None,
        unit=unit,
    )


BEARING = item("A-6205")
COPPER = item("B-CU", unit="kg")
HOUSING = item("C-HOUS")


def buy(
    it: ItemInfo,
    supplier: tuple[UUID, str, str],
    ordered: date,
    qty: str,
    price: str,
    *,
    promised: date | None = None,
    received: date | None = None,
    rejected: str = "0",
    currency: str = "EUR",
) -> Purchase:
    supplier_id, name, kind = supplier
    return Purchase(
        item_id=it.id,
        supplier_id=supplier_id,
        supplier_name=name,
        supplier_type=kind,
        currency=currency,
        ordered_at=ordered,
        quantity=Decimal(qty),
        unit_price=Decimal(price),
        promised_at=promised,
        received_at=received,
        received_qty=Decimal(qty) if received else None,
        rejected_qty=Decimal(rejected),
    )


DIST = (S1, "Cuscinetti Precisione S.r.l.", "distributor")
BROKER = (S2, "Global Parts Brokers Ltd", "reseller")
FOUNDRY = (S3, "Fonderia Veneta S.p.A.", "manufacturer")

PURCHASES = [
    buy(BEARING, DIST, date(2026, 1, 10), "100", "2.00",
        promised=date(2026, 1, 20), received=date(2026, 1, 25)),
    buy(BEARING, DIST, date(2026, 8, 1), "200", "2.20",
        promised=date(2026, 8, 15), received=date(2026, 8, 14), rejected="10"),
    buy(BEARING, BROKER, date(2026, 9, 20), "50", "1.80",
        promised=date(2026, 10, 1), received=date(2026, 9, 30)),
    buy(HOUSING, FOUNDRY, date(2025, 9, 1), "10", "25.00"),  # outside the 365-day window
    buy(HOUSING, FOUNDRY, date(2025, 11, 1), "5", "33.00", currency="USD"),
    buy(HOUSING, FOUNDRY, date(2025, 12, 1), "10", "30.00"),
]  # fmt: skip

BOM = [
    BomEntry(BEARING, Decimal(2)),
    BomEntry(COPPER, Decimal("1.25")),
    BomEntry(HOUSING, Decimal(1)),
]
STOCK = {
    BEARING.id: Stock(on_hand=Decimal(50), on_order=Decimal(30), on_order_due=date(2026, 11, 1)),
    COPPER.id: Stock(on_hand=Decimal("30.5")),
    HOUSING.id: Stock(on_hand=Decimal(150)),
}


def run(params: Params | None = None) -> AnalysisResult:
    return analyze(
        product="Motor X",
        quantity=Decimal(100),
        need_by=date(2026, 11, 15),
        bom=BOM,
        stock=STOCK,
        purchases=PURCHASES,
        today=TODAY,
        params=params,
    )


def codes(line: LineResult) -> list[tuple[str, str | None]]:
    return [(f.code, f.supplier) for f in line.flags]


def test_quantities_to_buy() -> None:
    bearing, copper, housing = run().lines

    assert (bearing.required, bearing.to_buy) == (Decimal(200), Decimal(120))
    # kg are not discrete: rounded up to 3 decimals, not to a whole unit.
    assert (copper.required, copper.to_buy) == (Decimal("125.00"), Decimal("94.500"))
    assert (housing.required, housing.to_buy) == (Decimal(100), Decimal(0))


def test_safety_stock_is_added_before_stock_is_subtracted() -> None:
    bearing = run(Params(safety_stock_pct=Decimal(10))).lines[0]

    assert bearing.to_buy == Decimal(140)  # 200 * 1.1 - 50 - 30


def test_prices_trend_and_targets() -> None:
    bearing = run().lines[0]

    assert bearing.currency == "EUR"
    assert bearing.last_price == Decimal("1.8000")  # the broker's, the most recent
    assert bearing.avg_price == Decimal("2.0857")  # 730 / 350
    assert bearing.min_price == Decimal("1.8000")
    assert bearing.trend_pct == Decimal("6.00")  # 2.12 in the last 90 days vs 2.00 before
    assert bearing.target_price == Decimal("1.8000")  # min(last, avg)
    assert bearing.max_price == Decimal("2.1900")  # max(last, avg) * 1.05


def test_supplier_stats_and_ranking() -> None:
    bearing = run().lines[0]
    regular, broker = bearing.suppliers

    assert regular.name == "Cuscinetti Precisione S.r.l."
    assert (regular.orders, regular.quantity) == (2, Decimal(300))
    assert (regular.last_price, regular.avg_price) == (Decimal("2.2000"), Decimal("2.1333"))
    assert regular.on_time_rate == Decimal("0.5000")  # late in January, on time in August
    assert regular.quality_rate == Decimal("0.9667")  # 10 of 300 rejected
    assert regular.suggested is True
    # Brokers rank last and are not suggested while a regular supplier exists.
    assert broker.name == "Global Parts Brokers Ltd"
    assert broker.suggested is False
    assert [s.name for s in bearing.suggested] == ["Cuscinetti Precisione S.r.l."]


def test_flags() -> None:
    bearing, copper, housing = run().lines

    assert codes(bearing) == [
        ("price_up", None),
        ("supplier_late", "Cuscinetti Precisione S.r.l."),
        ("quality_issue", "Cuscinetti Precisione S.r.l."),
        ("broker_source", "Global Parts Brokers Ltd"),
    ]
    assert codes(copper) == [("no_history", None)]
    assert copper.last_price is None and copper.target_price is None
    assert codes(housing) == [
        ("covered_by_stock", None),
        ("mixed_currency", None),
        ("single_source", "Fonderia Veneta S.p.A."),
    ]
    # Only the EUR purchase inside the window counts.
    assert (housing.last_price, housing.avg_price) == (Decimal("30.0000"), Decimal("30.0000"))
    assert housing.trend_pct is None
    assert housing.max_price == Decimal("31.5000")


def test_summary() -> None:
    summary = run().summary

    assert (summary.items, summary.items_to_buy, summary.flagged_items) == (3, 2, 3)
    assert summary.currency == "EUR"
    assert summary.total_at_target == Decimal("216.00")  # 120 bearings at 1.80
    assert summary.total_at_last_price == Decimal("216.00")


def test_round_up_quantity() -> None:
    assert round_up_quantity(Decimal("10.01"), "pcs") == Decimal(11)
    assert round_up_quantity(Decimal("10.0001"), "kg") == Decimal("10.001")
    assert round_up_quantity(Decimal("-3"), "pcs") == Decimal(0)
    assert round_up_quantity(Decimal(7), "Pz") == Decimal(7)


def test_the_currency_most_purchases_were_in_wins() -> None:
    # Three BRL purchases from the distributor, one later EUR purchase from a broker.
    history = [
        buy(BEARING, DIST, date(2026, 3, 1), "100", "58.00", currency="BRL"),
        buy(BEARING, DIST, date(2026, 5, 1), "100", "59.00", currency="BRL"),
        buy(BEARING, DIST, date(2026, 7, 1), "100", "57.00", currency="BRL"),
        buy(BEARING, BROKER, date(2026, 9, 1), "50", "8.20", currency="EUR"),
    ]
    [line] = analyze(
        product="Motor",
        quantity=Decimal(10),
        need_by=None,
        bom=[BomEntry(BEARING, Decimal(1))],
        stock={},
        purchases=history,
        today=TODAY,
    ).lines
    assert line.currency == "BRL"
    assert [s.name for s in line.suggested] == ["Cuscinetti Precisione S.r.l."]
    assert ("mixed_currency", None) in codes(line)

    # A tie goes to the currency a non-broker sold in, even if the broker was later.
    [line] = analyze(
        product="Motor",
        quantity=Decimal(10),
        need_by=None,
        bom=[BomEntry(BEARING, Decimal(1))],
        stock={},
        purchases=[history[0], history[3]],
        today=TODAY,
    ).lines
    assert line.currency == "BRL"


def test_totals_are_per_currency_and_never_added_up() -> None:
    result = analyze(
        product="Motor",
        quantity=Decimal(10),
        need_by=None,
        bom=[BomEntry(BEARING, Decimal(1)), BomEntry(HOUSING, Decimal(2))],
        stock={},
        purchases=[
            buy(BEARING, DIST, date(2026, 9, 1), "100", "58.00", currency="BRL"),
            buy(HOUSING, FOUNDRY, date(2026, 9, 1), "10", "30.00"),
        ],
        today=TODAY,
    )
    summary = result.summary
    assert (summary.currency, summary.total_at_target) == (None, None)
    assert [(t.currency, t.items_to_buy, t.total_at_target) for t in summary.totals] == [
        ("BRL", 1, Decimal("580.00")),
        ("EUR", 1, Decimal("600.00")),
    ]
    assert currency_totals([(None, Decimal(5), None, None)]) == ()


def test_open_purchase_orders_due_in_time_count_as_on_order() -> None:
    it = item("D-OPEN")
    history = [
        buy(it, DIST, date(2026, 5, 1), "100", "2.00", promised=date(2026, 5, 20),
            received=date(2026, 5, 22)),
        # Open and due before the need date: on order.
        buy(it, DIST, date(2026, 9, 20), "300", "2.10", promised=date(2026, 10, 30)),
        # Open, promised a little late (still coming): on order.
        buy(it, DIST, date(2026, 8, 20), "50", "2.10", promised=date(2026, 9, 25)),
        # Due after the need date, stale, or with no promised date: not counted.
        buy(it, DIST, date(2026, 9, 25), "400", "2.10", promised=date(2026, 12, 1)),
        buy(it, DIST, date(2026, 3, 1), "70", "2.10", promised=date(2026, 4, 1)),
        buy(it, DIST, date(2026, 9, 28), "80", "2.10"),
    ]  # fmt: skip
    [line] = analyze(
        product="P",
        quantity=Decimal(500),
        need_by=date(2026, 11, 15),
        bom=[BomEntry(it, Decimal(1))],
        stock={it.id: Stock(on_hand=Decimal(20))},
        purchases=history,
        today=TODAY,
    ).lines
    assert (line.on_order, line.to_buy) == (Decimal(350), Decimal(130))
    assert ("on_order_from_pos", None) in codes(line)
    assert next(f.value for f in line.flags if f.code == "on_order_from_pos") == Decimal(350)


def test_the_stock_on_order_wins_over_open_purchase_orders() -> None:
    it = item("D-STOCK")
    history = [buy(it, DIST, date(2026, 9, 20), "300", "2.10", promised=date(2026, 10, 30))]
    [line] = analyze(
        product="P",
        quantity=Decimal(500),
        need_by=date(2026, 11, 15),
        bom=[BomEntry(it, Decimal(1))],
        stock={
            it.id: Stock(
                on_hand=Decimal(20), on_order=Decimal(300), on_order_due=date(2026, 10, 30)
            )
        },
        purchases=history,
        today=TODAY,
    ).lines
    # The stock export already counts its open orders: not added twice.
    assert (line.on_order, line.to_buy) == (Decimal(300), Decimal(180))
    assert "on_order_from_pos" not in [c for c, _ in codes(line)]
