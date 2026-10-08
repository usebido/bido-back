"""Chronological net requirements and evidence readiness, without model calls."""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from bido_back.purchasing.analysis import BomEntry, Demand, Params, Stock, analyze
from bido_back.purchasing.importers import parse_rows
from bido_back.purchasing.schemas import AnalysisRequest
from tests.purchasing.test_analysis import BEARING, DIST, FOUNDRY, TODAY, buy


def test_supply_arriving_after_first_demand_covers_only_later_demand() -> None:
    first, second = date(2026, 10, 9), date(2026, 11, 9)
    result = analyze(
        product="Motor",
        quantity=Decimal(440),
        need_by=first,
        bom=[BomEntry(BEARING, Decimal(1))],
        stock={BEARING.id: Stock(on_hand=Decimal(10))},
        purchases=[buy(BEARING, DIST, TODAY, "400", "2", promised=date(2026, 10, 20))],
        today=TODAY,
        demands=[Demand(Decimal(400), second), Demand(Decimal(40), first)],
    )
    [line] = result.lines
    assert line.to_buy == 30
    assert line.need_by == first
    assert [
        (a.required, a.stock_used, a.inbound_used, a.to_buy) for a in line.calculation.allocations
    ] == [(40, 10, 0, 30), (400, 0, 400, 0)]


def test_stock_and_inbound_are_each_allocated_once() -> None:
    result = analyze(
        product="Motor",
        quantity=Decimal(80),
        need_by=TODAY,
        bom=[BomEntry(BEARING, Decimal(1))],
        stock={BEARING.id: Stock(on_hand=Decimal(30), on_order=Decimal(20), on_order_due=TODAY)},
        purchases=[buy(BEARING, DIST, TODAY, "20", "2", promised=TODAY)],
        today=TODAY,
        demands=[Demand(Decimal(40), TODAY), Demand(Decimal(40), date(2026, 10, 9))],
    )
    [line] = result.lines
    assert (line.on_hand, line.on_order, line.to_buy) == (30, 20, 30)
    assert [(a.stock_used, a.inbound_used, a.to_buy) for a in line.calculation.allocations] == [
        (30, 10, 0),
        (0, 10, 30),
    ]


def test_rounding_surplus_is_reused_for_next_demand() -> None:
    [line] = analyze(
        product="Motor",
        quantity=Decimal(2),
        need_by=TODAY,
        bom=[BomEntry(BEARING, Decimal("0.5"))],
        stock={},
        purchases=[],
        today=TODAY,
        demands=[Demand(Decimal(1), TODAY), Demand(Decimal(1), date(2026, 10, 9))],
    ).lines
    assert line.to_buy == 1
    assert line.calculation.allocations[1].planned_supply_used == Decimal("0.5")


def test_undated_inbound_cannot_cover_a_dated_requirement() -> None:
    [line] = analyze(
        product="Motor",
        quantity=Decimal(40),
        need_by=TODAY,
        bom=[BomEntry(BEARING, Decimal(1))],
        stock={BEARING.id: Stock(on_order=Decimal(100))},
        purchases=[],
        today=TODAY,
    ).lines
    assert (line.on_order, line.to_buy) == (0, 40)
    assert line.calculation.excluded_inbound == 100
    assert "inbound_date_unknown" in line.calculation.stock_issues


def test_missing_inventory_differs_from_a_known_zero_balance() -> None:
    unknown = analyze(
        product="Motor",
        quantity=Decimal(1),
        need_by=TODAY,
        bom=[BomEntry(BEARING, Decimal(1))],
        stock={},
        purchases=[],
        today=TODAY,
    )
    known = analyze(
        product="Motor",
        quantity=Decimal(1),
        need_by=TODAY,
        bom=[BomEntry(BEARING, Decimal(1))],
        stock={BEARING.id: Stock()},
        purchases=[],
        today=TODAY,
    )
    assert unknown.lines[0].to_buy == known.lines[0].to_buy == 1
    assert unknown.lines[0].calculation.stock_known is False
    assert unknown.lines[0].calculation.stock_issues == ("stock_unknown",)
    assert known.lines[0].calculation.stock_known is True


def test_location_scoped_analysis_does_not_assume_po_destination() -> None:
    [line] = analyze(
        product="Motor",
        quantity=Decimal(10),
        need_by=TODAY,
        bom=[BomEntry(BEARING, Decimal(1))],
        stock={},
        purchases=[buy(BEARING, DIST, TODAY, "10", "2", promised=TODAY)],
        today=TODAY,
        params=Params(location="Plant A"),
    ).lines
    assert line.on_order == 0 and line.to_buy == 10
    assert "inbound_location_unknown" in line.calculation.stock_issues


def test_unmeasured_supplier_is_not_ranked_above_measured_supplier() -> None:
    [line] = analyze(
        product="Motor",
        quantity=Decimal(1),
        need_by=TODAY,
        bom=[BomEntry(BEARING, Decimal(1))],
        stock={},
        today=TODAY,
        purchases=[
            buy(
                BEARING,
                DIST,
                date(2026, 9, 1),
                "10",
                "5",
                promised=date(2026, 9, 10),
                received=date(2026, 9, 11),
                rejected="1",
            ),
            buy(BEARING, FOUNDRY, date(2026, 9, 1), "10", "1"),
        ],
    ).lines
    measured, unknown = line.suppliers
    assert measured.supplier_id == DIST[0]
    assert (measured.delivery_observations, measured.quality_observations) == (1, 1)
    assert measured.last_received_at == date(2026, 9, 11)
    assert unknown.delivery_observations == unknown.quality_observations == 0
    assert unknown.on_time_rate is unknown.quality_rate is None


def test_partial_and_overdue_receipts_are_failures_and_missing_quantities_are_unknown() -> None:
    last_week = TODAY - timedelta(days=7)
    history = [
        replace(
            buy(BEARING, DIST, last_week, "10", "5", promised=last_week, received=last_week),
            received_qty=Decimal(5),
        ),
        replace(
            buy(BEARING, DIST, last_week, "10", "5", promised=last_week, received=last_week),
            received_qty=None,
        ),
        buy(BEARING, DIST, last_week, "10", "5", promised=last_week),
        buy(BEARING, DIST, last_week, "10", "5", promised=TODAY + timedelta(days=7)),
    ]
    [line] = analyze(
        product="Motor",
        quantity=Decimal(1),
        need_by=TODAY,
        bom=[BomEntry(BEARING, Decimal(1))],
        stock={},
        purchases=history,
        today=TODAY,
    ).lines
    [supplier] = line.suppliers
    assert supplier.delivery_observations == 2
    assert supplier.on_time_rate == 0
    assert supplier.quality_observations == 1
    assert supplier.quality_rate == 1
    assert "partial_receipt_status_unknown" in line.calculation.stock_issues


@pytest.mark.parametrize(
    "header,expected_gross",
    [
        ("On hand", 100),
        ("Available stock", 130),
        ("Unrestricted stock", 110),
    ],
)
def test_import_preserves_gross_vs_available_stock_semantics(
    header: str,
    expected_gross: int,
) -> None:
    table = parse_rows(
        "inventory",
        [
            ["Part number", header, "Reserved qty", "Blocked qty", "On order due"],
            ["A", 100, 20, 10, date(2026, 10, 9)],
        ],
    )
    assert not table.errors
    values = table.rows[0][1]
    assert values["on_hand"] == expected_gross
    assert values["reserved_qty"] == 20 and values["blocked_qty"] == 10


def test_planning_window_rejects_reversed_dates() -> None:
    with pytest.raises(ValidationError, match="planning_end"):
        AnalysisRequest(product="Motor", planning_start=date(2026, 10, 9), planning_end=TODAY)
