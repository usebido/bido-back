"""Measured supplier performance and Bido's internal price benchmark on source evidence.

Hand-computed numbers on synthetic records; the as-of date is the fixture's 2026-10-08.
"""

from datetime import date
from decimal import Decimal
from random import Random
from uuid import uuid4

from bido_back.purchasing.analysis import BomEntry, ItemInfo, Purchase, analyze
from bido_back.purchasing.sources.analysis import assess
from bido_back.purchasing.sources.contracts import (
    AssessmentLine,
    PriceBenchmark,
    Record,
    SourceAssessment,
    SupplierEvidence,
)
from bido_back.purchasing.sources.planning import assess_plan
from bido_back.purchasing.sources.pricing import spend_totals
from evals.source_fixtures import SCOPE, fact, motor_records

BEARING = "000101"
SCREW = "000106"


def order(
    row: int,
    po: str,
    supplier: str,
    quantity: str,
    price: str,
    ordered: str,
    promised: str,
    *,
    status: str = "Closed",
    currency: str = "BRL",
    part: str = BEARING,
) -> Record:
    return fact(
        "purchase",
        row,
        part_number=part,
        unit="pcs",
        location="Plant A",
        quantity=quantity,
        unit_price=price,
        currency=currency,
        ordered_at=ordered,
        promised_at=promised,
        status=status,
        supplier_code=supplier,
        po_number=po,
        po_line="00010",
    )


def receipt(row: int, po: str, quantity: str, received: str, rejected: str = "0") -> Record:
    return fact(
        "receipt",
        row,
        po_number=po,
        po_line="00010",
        location="Plant A",
        quantity=quantity,
        rejected=rejected,
        unit="pcs",
        received_at=received,
        movement="101",
        receipt_id=f"NF-{po}",
    )


def pending(row: int, po: str, quantity: str, expected: str) -> Record:
    return fact(
        "delivery_schedule",
        row,
        part_number=BEARING,
        quantity=quantity,
        unit="pcs",
        location="Plant A",
        expected_at=expected,
        status="Not received",
        po_number=po,
        po_line="00010",
    )


# V1 (the fixture's "Example Supplier") is cheaper, late and rejects; V2 is punctual.
MIXED = [
    # Late (received after the promise), 1 of 10 rejected.
    order(101, "PO-A", "V1", "10", "10.00", "2026-08-01", "2026-08-10"),
    receipt(101, "PO-A", "10", "2026-08-12", rejected="1"),
    # Complete and on time.
    order(102, "PO-B", "V1", "20", "9.00", "2026-09-01", "2026-09-10"),
    receipt(102, "PO-B", "20", "2026-09-09"),
    # Closed short: 6 of 10 arrived. A failure, and never a price reference.
    order(103, "PO-C", "V1", "10", "9.50", "2026-09-05", "2026-09-15"),
    receipt(103, "PO-C", "6", "2026-09-14"),
    # Open and past its promised date: late. Its price is never a price reference.
    order(104, "PO-D", "V1", "5", "8.00", "2026-09-20", "2026-10-01", status="Open"),
    pending(104, "PO-D", "5", "2026-10-25"),
    order(105, "PO-E", "V2", "10", "12.00", "2026-07-01", "2026-07-10"),
    receipt(105, "PO-E", "10", "2026-07-09"),
    order(106, "PO-F", "V2", "10", "12.60", "2026-09-15", "2026-09-25"),
    receipt(106, "PO-F", "10", "2026-09-24"),
]


def history(*extra: Record, part: str = BEARING) -> list[Record]:
    """The motor fixture with one part's completed purchase replaced by `extra`.

    The bearing keeps the fixture's open order OPEN-40 (V1, promised 2026-10-13, not due).
    """
    old = f"OLD-{7 + ['000101', '000102', '000103', '000104', '000105', '000106'].index(part)}"
    rows = [
        r
        for r in motor_records()
        if not (r.kind in {"purchase", "receipt"} and r.values.get("po_number") == old)
    ]
    supplier = fact(
        "supplier", 60, supplier_code="V2", name="Punctual Supplier", email="sales@p.example.test"
    )
    return [*rows, supplier, *extra]


def line_of(rows: list[Record], part: str = BEARING) -> AssessmentLine:
    return next(line for line in assess_plan(rows, SCOPE).lines if line.part_number == part)


def by_code(line: AssessmentLine) -> dict[str | None, SupplierEvidence]:
    return {s.supplier_code: s for s in line.suppliers}


def flags(line: AssessmentLine) -> list[tuple[str, str | None, Decimal | None]]:
    return [(f.code, f.supplier, f.value) for f in line.flags]


def test_supplier_performance_learns_from_late_short_and_overdue_orders() -> None:
    line = line_of(history(*MIXED))
    assert line.ready and line.calculated_to_buy == 50
    late = by_code(line)["V1"]
    # PO-A..D and the fixture's open OPEN-40; quantities as ordered.
    assert (late.orders, late.quantity) == (5, Decimal(55))
    # Late PO-A, on-time PO-B, short PO-C, overdue PO-D; OPEN-40 is not due yet.
    assert (late.on_time_rate, late.delivery_observations) == (Decimal("0.2500"), 4)
    # 1 rejected of 36 received (PO-A, PO-B, PO-C).
    assert (late.quality_rate, late.quality_observations) == (Decimal("0.9722"), 3)
    assert late.avg_lead_time_days == Decimal("9.3")  # (11 + 8 + 9) / 3 days
    assert late.last_received_at == date(2026, 9, 14)
    # Prices only from completed purchases (PO-A, PO-B): not the short or open orders.
    assert (late.avg_price, late.min_price) == (Decimal("9.3333"), Decimal("9.0000"))
    assert (late.last_price, late.last_price_basis) == (Decimal("9.00"), "received_order")
    punctual = by_code(line)["V2"]
    assert (punctual.orders, punctual.quantity) == (2, Decimal(20))
    assert (punctual.on_time_rate, punctual.quality_rate) == (Decimal(1), Decimal(1))
    assert (punctual.delivery_observations, punctual.quality_observations) == (2, 2)
    assert punctual.avg_lead_time_days == Decimal("8.5")
    assert (punctual.avg_price, punctual.min_price) == (Decimal("12.3000"), Decimal("12.0000"))
    # Nothing was reported by the source: the reported fields stay unknown.
    assert (late.otif_pct, late.quality_pct, late.lead_time_days) == (None, None, None)
    # Measured quality ranks first; both sold the item, so both are suggested.
    assert [s.supplier_code for s in line.suppliers] == ["V2", "V1"]
    assert all(s.suggested for s in line.suppliers)
    # The supplier's evidence points at the failed deliveries and the rejection.
    for failed in (MIXED[0], MIXED[4], MIXED[6]):
        assert failed.evidence["promised_at"] in late.evidence
    assert MIXED[1].evidence["rejected"] in late.evidence
    assert "unverified_purchase_receipt" in {i.code for i in line.issues}


def test_internal_price_benchmark_uses_completed_purchases_only() -> None:
    line = line_of(history(*MIXED))
    price = line.price
    assert price is not None
    assert (price.currency, price.basis, price.observations, price.window_days) == (
        "BRL",
        "received_orders",
        4,
        365,
    )
    assert (price.last_price, price.last_supplier, price.last_ordered_at) == (
        Decimal("12.6000"),
        "Punctual Supplier",
        date(2026, 9, 15),
    )
    assert (price.avg_price, price.min_price) == (Decimal("10.5200"), Decimal("9.0000"))  # 526/50
    assert price.trend_pct == Decimal("-15.42")  # 10.15 in the last 90 days vs 12.00 before
    assert (price.target_price, price.max_price) == (Decimal("10.5200"), Decimal("13.2300"))
    assert (price.estimated_cost_at_target, price.estimated_cost_at_last) == (
        Decimal("526.00"),
        Decimal("630.00"),
    )
    # The price cells of the orders used, oldest first: PO-E, PO-A, PO-B, PO-F.
    assert price.evidence == [MIXED[i].evidence["unit_price"] for i in (8, 0, 2, 10)]
    assert flags(line) == [
        ("supplier_late", "Example Supplier", Decimal("0.25")),
        ("quality_issue", "Example Supplier", Decimal("0.9722")),
    ]


def test_unknown_performance_is_never_ranked_as_perfect() -> None:
    rows = history(
        *MIXED,
        # Approved for the item, never bought from.
        fact("supplier", 61, supplier_code="V3", name="New Supplier", email="new@example.test"),
        fact(
            "supplier_item",
            61,
            part_number=BEARING,
            supplier_code="V3",
            status="Approved",
            valid_from="2026-01-01",
        ),
        # Sold the item, but its only order is not due yet: nothing measured.
        fact("supplier", 62, supplier_code="V4", name="Waiting Supplier"),
        order(107, "PO-G", "V4", "5", "11.00", "2026-10-01", "2026-10-20", status="Open"),
        pending(107, "PO-G", "5", "2026-10-20"),
    )
    line = line_of(rows)
    assert [s.supplier_code for s in line.suppliers] == ["V2", "V1", "V4", "V3"]
    waiting, new = by_code(line)["V4"], by_code(line)["V3"]
    assert (waiting.orders, waiting.on_time_rate, waiting.quality_rate) == (1, None, None)
    assert (waiting.avg_price, waiting.last_price, waiting.name) == (None, None, "Waiting Supplier")
    assert waiting.suggested  # It sells the item; unknown measures only rank it lower.
    assert (new.orders, new.suggested, new.qualification_status) == (0, False, "Approved")
    assert not any(f.supplier in {"Waiting Supplier", "New Supplier"} for f in line.flags)


def test_closed_orders_without_receipts_and_orders_not_yet_due_stay_unknown() -> None:
    line = line_of(history(order(120, "PO-N", "V1", "10", "10.00", "2026-08-01", "2026-08-10")))
    supplier = by_code(line)["V1"]
    assert (supplier.orders, supplier.delivery_observations, supplier.on_time_rate) == (2, 0, None)
    assert (supplier.quality_rate, supplier.last_price) == (None, None)
    assert line.price is None
    assert {f.code for f in line.flags} == {"single_source"}
    assert "unverified_purchase_receipt" in {i.code for i in line.issues}


def test_other_currency_history_is_flagged_and_left_out() -> None:
    usd = [
        order(108, "PO-H", "V2", "10", "2.50", "2026-09-20", "2026-09-28", currency="USD"),
        receipt(108, "PO-H", "10", "2026-09-27"),
    ]
    line = line_of(history(*MIXED, *usd))
    assert line.price is not None
    assert (line.price.currency, line.price.observations) == ("BRL", 4)
    assert line.price.target_price == Decimal("10.5200")
    punctual = by_code(line)["V2"]
    assert (punctual.orders, punctual.avg_price) == (2, Decimal("12.3000"))
    assert ("mixed_currency", None, None) in flags(line)


def test_rising_prices_and_single_source_are_flagged() -> None:
    line = line_of(
        history(
            order(110, "PO-OLD", "V1", "10", "10.00", "2026-05-01", "2026-05-10"),
            receipt(110, "PO-OLD", "10", "2026-05-09"),
            order(111, "PO-NEW", "V1", "10", "11.00", "2026-09-01", "2026-09-10"),
            receipt(111, "PO-NEW", "10", "2026-09-09"),
        )
    )
    assert line.price is not None
    assert line.price.trend_pct == Decimal("10.00")
    assert (line.price.target_price, line.price.max_price) == (
        Decimal("10.5000"),
        Decimal("11.5500"),
    )
    assert flags(line) == [
        ("single_source", "Example Supplier", None),
        ("price_up", None, Decimal("10.00")),
    ]


def test_covered_and_never_bought_items_are_flagged_without_a_price() -> None:
    result = assess_plan(history(part=SCREW), SCOPE)
    housing = next(line for line in result.lines if line.part_number == "000103")
    assert housing.calculated_to_buy == 0
    assert [f.code for f in housing.flags] == ["covered_by_stock", "single_source"]
    screw = next(line for line in result.lines if line.part_number == SCREW)
    assert (screw.price, screw.suppliers) == (None, [])
    assert [f.code for f in screw.flags] == ["no_history"]
    assert "insufficient_price_history" not in {i.code for i in screw.issues}


def test_thin_history_proposes_no_target_or_maximum() -> None:
    # The fixture has one completed purchase per part: below the minimum of 2.
    line = line_of(motor_records())
    assert line.price is None
    supplier = line.suppliers[0]
    assert (supplier.last_price, supplier.last_price_basis) == (Decimal("10.50"), "received_order")
    assert (supplier.orders, supplier.on_time_rate) == (2, Decimal(1))
    thin = next(i for i in line.issues if i.code == "insufficient_price_history")
    assert thin.severity == "warning" and thin.evidence
    assert line.ready  # A missing benchmark never blocks the quantity.


def test_stale_purchases_are_not_history() -> None:
    stale = [
        order(140, "PO-S1", "V2", "10", "5.00", "2025-08-01", "2025-08-10"),
        receipt(140, "PO-S1", "10", "2025-08-09"),
        order(141, "PO-S2", "V2", "10", "5.00", "2025-09-01", "2025-09-10"),
        receipt(141, "PO-S2", "10", "2025-09-09"),
    ]
    line = line_of(history(*MIXED, *stale))
    assert line.price is not None
    assert (line.price.observations, line.price.min_price) == (4, Decimal("9.0000"))
    assert by_code(line)["V2"].orders == 2
    only_stale = line_of(history(*stale))
    assert only_stale.price is None
    # V2 is listed (it has orders in the file) but nothing inside the window is measured.
    stale_supplier = by_code(only_stale)["V2"]
    assert (stale_supplier.orders, stale_supplier.on_time_rate, stale_supplier.suggested) == (
        0,
        None,
        False,
    )
    assert [f.code for f in only_stale.flags] == ["single_source"]


def test_purchases_in_another_unit_are_not_comparable_prices() -> None:
    boxes = order(150, "PO-BOX", "V2", "2", "120.00", "2026-09-20", "2026-09-28")
    boxes.values["unit"] = "box"
    other_unit = receipt(150, "PO-BOX", "2", "2026-09-27")
    other_unit.values["unit"] = "box"
    line = line_of(history(*MIXED, boxes, other_unit))
    assert line.price is not None
    assert (line.price.observations, line.price.last_price) == (4, Decimal("12.6000"))
    assert "unpriced_purchase_history" in {i.code for i in line.issues}
    # Still a measured delivery for the supplier (on time, complete).
    assert by_code(line)["V2"].delivery_observations == 3


def test_a_completed_purchase_without_currency_is_history_but_not_a_price() -> None:
    unpriced = order(112, "PO-X", "V1", "10", "10.00", "2026-09-01", "2026-09-10")
    del unpriced.values["currency"], unpriced.evidence["currency"]
    line = line_of(history(unpriced, receipt(112, "PO-X", "10", "2026-09-09")))
    assert line.price is None
    assert "no_history" not in {f.code for f in line.flags}
    assert "unpriced_purchase_history" in {i.code for i in line.issues}
    assert by_code(line)["V1"].on_time_rate == Decimal(1)


def test_reported_supplier_ratings_stay_separate_from_measured_performance() -> None:
    rating = fact(
        "supplier_performance",
        70,
        supplier_code="V1",
        otif_pct="97",
        quality_pct="99.5",
        lead_time_days="5",
    )
    line = line_of(history(*MIXED, rating))
    supplier = by_code(line)["V1"]
    assert (supplier.otif_pct, supplier.quality_pct, supplier.lead_time_days) == (
        Decimal(97),
        Decimal("99.5"),
        Decimal(5),
    )
    assert (supplier.on_time_rate, supplier.quality_rate, supplier.avg_lead_time_days) == (
        Decimal("0.2500"),
        Decimal("0.9722"),
        Decimal("9.3"),
    )
    # Flags and ranking follow what Bido measured, not what the source claims.
    assert ("supplier_late", "Example Supplier", Decimal("0.25")) in flags(line)
    assert [s.supplier_code for s in line.suppliers] == ["V2", "V1"]


def test_row_order_and_repeated_reports_do_not_change_prices_or_measures() -> None:
    baseline = line_of(history(*MIXED))
    rows = history(*MIXED)
    copies: list[Record] = []
    for row in rows:
        copy = row.model_copy(deep=True)
        for ref in copy.evidence.values():
            ref.source_id = "second-export"
            ref.row += 1000
        copies.append(copy)
    rows.extend(copies)
    Random(7).shuffle(rows)  # noqa: S311 - reproducible layout permutation, not a secret.
    line = line_of(rows)
    assert line.price is not None and baseline.price is not None
    # Evidence cells differ between the copies; the facts must not.
    cells = {"evidence": True, "outliers": {"__all__": {"evidence"}}}
    assert line.price.model_dump(exclude=cells) == baseline.price.model_dump(exclude=cells)
    assert [s.model_dump(exclude={"evidence"}) for s in line.suppliers] == [
        s.model_dump(exclude={"evidence"}) for s in baseline.suppliers
    ]
    assert line.flags == baseline.flags


def test_source_rules_match_the_imported_data_analysis() -> None:
    """The same completed purchases give the legacy analysis's prices, rates and flags."""
    completed = [
        # (po, supplier, quantity, price, ordered, promised, received, rejected)
        ("PO-1", "V1", "10", "2.00", "2026-03-01", "2026-03-10", "2026-03-12", "1"),
        ("PO-2", "V1", "30", "2.20", "2026-08-01", "2026-08-10", "2026-08-09", "0"),
        ("PO-3", "V2", "20", "2.50", "2026-06-01", "2026-06-10", "2026-06-10", "0"),
        ("PO-4", "V2", "20", "2.40", "2026-09-20", "2026-09-30", "2026-09-29", "2"),
    ]
    rows: list[Record] = []
    for row, (po, supplier, quantity, price, ordered, promised, received, rejected) in enumerate(
        completed, 130
    ):
        rows.append(order(row, po, supplier, quantity, price, ordered, promised, part=SCREW))
        rows.append(receipt(row, po, quantity, received, rejected))
    source = line_of(history(*rows, part=SCREW), SCREW)

    item = ItemInfo(uuid4(), SCREW, "Screw", None, None, None, "pcs")
    ids = {"V1": uuid4(), "V2": uuid4()}
    names = {"V1": "Example Supplier", "V2": "Punctual Supplier"}
    [legacy] = analyze(
        product="MTR-100",
        quantity=Decimal(1),
        need_by=None,
        bom=[BomEntry(item, Decimal(1))],
        stock={},
        purchases=[
            Purchase(
                item_id=item.id,
                supplier_id=ids[supplier],
                supplier_name=names[supplier],
                supplier_type="distributor",
                currency="BRL",
                ordered_at=date.fromisoformat(ordered),
                quantity=Decimal(quantity),
                unit_price=Decimal(price),
                promised_at=date.fromisoformat(promised),
                received_at=date.fromisoformat(received),
                received_qty=Decimal(quantity),
                rejected_qty=Decimal(rejected),
            )
            for _, supplier, quantity, price, ordered, promised, received, rejected in completed
        ],
        today=SCOPE.as_of,
    ).lines

    price = source.price
    assert price is not None
    assert (
        price.last_price,
        price.avg_price,
        price.min_price,
        price.trend_pct,
        price.target_price,
        price.max_price,
    ) == (
        legacy.last_price,
        legacy.avg_price,
        legacy.min_price,
        legacy.trend_pct,
        legacy.target_price,
        legacy.max_price,
    )
    assert (price.target_price, price.max_price) == (Decimal("2.3000"), Decimal("2.5200"))
    assert [
        (s.name, s.orders, s.quantity, s.avg_price, s.min_price, s.on_time_rate, s.quality_rate)
        for s in source.suppliers
    ] == [
        (s.name, s.orders, s.quantity, s.avg_price, s.min_price, s.on_time_rate, s.quality_rate)
        for s in legacy.suppliers
    ]
    assert [(s.delivery_observations, s.quality_observations) for s in source.suppliers] == [
        (s.delivery_observations, s.quality_observations) for s in legacy.suppliers
    ]
    assert [s.suggested for s in source.suppliers] == [s.suggested for s in legacy.suppliers]
    assert flags(source) == [(f.code, f.supplier, f.value) for f in legacy.flags]
    assert [f.code for f in source.flags] == ["supplier_late", "quality_issue", "quality_issue"]


def test_unscoped_assessment_shares_the_measures_and_the_conservative_last_price() -> None:
    rows = [
        fact(
            "inventory",
            2,
            part_number=BEARING,
            location="Plant A",
            unit="pcs",
            available="5",
            on_order="0",
            safety_stock="0",
            as_of="2026-10-08",
        ),
        fact(
            "material_requirement",
            3,
            part_number=BEARING,
            location="Plant A",
            unit="pcs",
            quantity="25",
            need_by="2026-10-20",
            demand_basis="gross",
        ),
        fact("supplier", 4, supplier_code="V1", name="Example Supplier"),
        fact("supplier", 5, supplier_code="V2", name="Punctual Supplier"),
        *(r for r in MIXED if r.kind != "delivery_schedule"),
    ]
    [line] = assess(rows).lines
    assert line.calculated_to_buy == 20
    price = line.price
    assert price is not None
    assert (price.last_price, price.target_price, price.max_price) == (
        Decimal("12.6000"),
        Decimal("10.5200"),
        Decimal("13.2300"),
    )
    assert (price.estimated_cost_at_target, price.estimated_cost_at_last) == (
        Decimal("210.40"),
        Decimal("252.00"),
    )
    late = by_code(line)["V1"]
    # Same measures as the scoped path (the fixture's OPEN-40 is not part of this file).
    assert (late.orders, late.on_time_rate, late.quality_rate) == (
        4,
        Decimal("0.2500"),
        Decimal("0.9722"),
    )
    # The open PO-D (8.00) is the latest purchase order; the completed PO-B sets the price.
    assert (late.last_price, late.last_price_basis) == (Decimal("9.00"), "received_order")
    assert [s.supplier_code for s in line.suppliers] == ["V2", "V1"]
    assert [f.code for f in line.flags] == ["supplier_late", "quality_issue"]


def test_unscoped_history_without_receipts_keeps_the_purchase_order_basis() -> None:
    rows = [
        fact(
            "material_requirement",
            part_number=BEARING,
            location="Plant A",
            unit="pcs",
            quantity="5",
            need_by="2026-10-20",
            demand_basis="net",
        ),
        fact(
            "purchase",
            3,
            part_number=BEARING,
            supplier_code="V1",
            ordered_at="2026-09-01",
            unit_price="10",
            currency="BRL",
            quantity="4",
            unit="pcs",
        ),
    ]
    [line] = assess(rows).lines
    supplier = line.suppliers[0]
    assert (supplier.last_price, supplier.last_price_basis) == (Decimal(10), "purchase_order")
    assert (supplier.orders, supplier.on_time_rate, supplier.suggested) == (1, None, True)
    assert line.price is None  # No completed (received) purchase: no internal benchmark.
    assert [f.code for f in line.flags] == ["single_source"]


def benchmark(currency: str, target: str, last: str, quantity: str) -> AssessmentLine:
    price = PriceBenchmark(
        currency=currency,
        observations=1,
        window_days=365,
        last_price=Decimal(last),
        avg_price=Decimal(target),
        min_price=Decimal(target),
        target_price=Decimal(target),
        max_price=Decimal(last),
        estimated_cost_at_target=Decimal(quantity) * Decimal(target),
        estimated_cost_at_last=Decimal(quantity) * Decimal(last),
    )
    return AssessmentLine(part_number=currency, calculated_to_buy=Decimal(quantity), price=price)


def test_spend_totals_are_per_currency_and_never_converted() -> None:
    lines = [
        benchmark("BRL", "2.00", "2.50", "10"),
        benchmark("EUR", "5.00", "5.20", "4"),
        benchmark("BRL", "1.00", "1.10", "1"),
        benchmark("USD", "9.00", "9.00", "0"),  # Covered by stock: nothing to spend.
        AssessmentLine(part_number="NO-PRICE", calculated_to_buy=Decimal(3)),
        AssessmentLine(part_number="NO-QUANTITY", price=benchmark("BRL", "1", "1", "1").price),
    ]
    assert [
        (t.currency, t.lines, t.total_at_target, t.total_at_last_price) for t in spend_totals(lines)
    ] == [
        ("BRL", 2, Decimal("21.00"), Decimal("26.10")),
        ("EUR", 1, Decimal("20.00"), Decimal("20.80")),
    ]
    assert spend_totals([]) == []


def test_snapshots_saved_before_the_benchmark_still_load() -> None:
    current = SourceAssessment(**assess_plan(history(*MIXED), SCOPE).model_dump(), scope=SCOPE)
    measured = (
        "orders",
        "quantity",
        "avg_price",
        "min_price",
        "on_time_rate",
        "quality_rate",
        "delivery_observations",
        "quality_observations",
        "avg_lead_time_days",
        "last_received_at",
        "suggested",
    )
    saved = current.model_dump(mode="json")
    del saved["totals"]
    for line in saved["lines"]:
        del line["price"], line["flags"]
        for supplier in line["suppliers"]:
            for field in measured:
                del supplier[field]
    loaded = SourceAssessment.model_validate(saved)
    assert loaded.totals == []
    assert all(line.price is None and line.flags == [] for line in loaded.lines)
    suppliers = [s for line in loaded.lines for s in line.suppliers]
    assert suppliers and all(
        (s.orders, s.on_time_rate, s.quality_rate, s.suggested) == (0, None, None, False)
        for s in suppliers
    )
    assert [line.calculated_to_buy for line in loaded.lines] == [
        line.calculated_to_buy for line in current.lines
    ]
