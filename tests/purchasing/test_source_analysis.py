"""Source-neutral procurement arithmetic and semantic guardrails, without a model."""

from decimal import Decimal

import pytest

from bido_back.purchasing.sources.analysis import assess
from bido_back.purchasing.sources.contracts import AssessmentLine, Record, RecordKind, SourceRef


def record(kind: RecordKind, row: int = 2, /, **values: str) -> Record:
    return Record(
        kind=kind,
        values=values,
        evidence={
            field: SourceRef(source_id="source-1", table=kind, row=row, column=column)
            for column, field in enumerate(values, 1)
        },
    )


def inventory(**overrides: str) -> Record:
    return record(
        "inventory",
        **{
            "part_number": "00010",
            "location": "BR01",
            "description": "Steel",
            "unit": "KG",
            "available": "8500",
            "on_order": "1500",
            "safety_stock": "6500",
            "receipt_date": "2026-10-20",
            **overrides,
        },
    )


def requirement(**overrides: str) -> Record:
    return record(
        "material_requirement",
        **{
            "part_number": "00010",
            "location": "BR01",
            "unit": "KG",
            "quantity": "11000",
            "need_by": "2026-10-31",
            "demand_basis": "gross",
            **overrides,
        },
    )


def summary(**overrides: str) -> Record:
    return record(
        "purchase_summary",
        **{
            "part_number": "00010",
            "location": "BR01",
            "unit": "KG",
            "available": "8500",
            "on_order": "1500",
            "safety_stock": "6500",
            "demand": "11000",
            "projected": "-1000",
            "reported_to_buy": "7500",
            "unit_price": "78.5",
            "reported_estimated_cost": "588750",
            **overrides,
        },
    )


def codes(line: AssessmentLine) -> set[str]:
    return {issue.code for issue in line.issues}


def test_gross_demand_uses_absolute_safety_and_available_stock_once() -> None:
    result = assess(
        [
            inventory(blocked="3000", reserved="4200", total="12500"),
            requirement(),
            summary(currency="BRL"),
        ]
    )
    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.part_number == "00010"
    assert line.calculated_to_buy == Decimal(7500)
    assert line.reported_to_buy == Decimal(7500)
    assert line.available == Decimal(8500)
    assert line.safety_stock == Decimal(6500)
    assert line.ready
    assert line.issues == []


def test_summary_does_not_add_stock_or_demand_and_missing_currency_stays_unknown() -> None:
    result = assess([inventory(), requirement(), summary()])
    line = result.lines[0]
    assert line.demand == Decimal(11000)
    assert line.calculated_to_buy == Decimal(7500)
    assert line.reported_estimated_cost == Decimal(588750)
    assert line.currency is None
    assert "missing_cost_currency" in codes(line)
    assert result.record_counts == {
        "inventory": 1,
        "material_requirement": 1,
        "purchase_summary": 1,
    }


def test_available_plus_blocked_cannot_exceed_total_but_reported_purchases_remain() -> None:
    stock = inventory(
        total="8200", available="6100", blocked="2200", on_order="1000", safety_stock="4800"
    )
    line = assess(
        [
            stock,
            requirement(quantity="7200"),
            summary(
                available="6100",
                on_order="1000",
                safety_stock="4800",
                demand="7200",
                projected="-100",
                reported_to_buy="4900",
                unit_price="79.9",
                reported_estimated_cost="391510",
            ),
        ]
    ).lines[0]
    assert "inventory_balance_conflict" in codes(line)
    assert line.calculated_to_buy is None
    assert line.reported_to_buy == Decimal(4900)
    assert line.reported_estimated_cost == Decimal(391510)
    assert not line.ready
    conflict = next(issue for issue in line.issues if issue.code == "inventory_balance_conflict")
    assert conflict.evidence == [
        ref for field, ref in stock.evidence.items() if field in {"total", "available", "blocked"}
    ]


def test_available_stock_cannot_exceed_total_even_without_a_blocked_balance() -> None:
    line = assess([inventory(available="8300", total="8200"), requirement()]).lines[0]
    assert "inventory_balance_conflict" in codes(line)
    assert not line.ready


def test_residual_stock_gap_does_not_require_equality_or_subtract_reservations() -> None:
    line = assess(
        [inventory(available="6100", total="8200", blocked="2000", reserved="4200"), requirement()]
    ).lines[0]
    assert "inventory_balance_conflict" not in codes(line)
    assert line.calculated_to_buy == Decimal(9900)
    assert line.ready


def test_source_units_remain_visible_when_conversion_is_missing() -> None:
    result = assess(
        [
            inventory(unit="UN", price_unit="KG"),
            requirement(),
            summary(unit="UN"),
        ]
    )
    line = result.lines[0]
    assert line.calculated_to_buy is None
    assert not line.ready
    assert line.reported_to_buy == Decimal(7500)
    assert line.reported_estimated_cost == Decimal(588750)
    assert line.inventory_unit == line.reported_unit == "UN"
    assert line.demand_unit == "KG"
    assert "unit_conflict" in codes(line)
    issue = next(issue for issue in line.issues if issue.code == "unit_conflict")
    assert {ref.table for ref in issue.evidence} == {
        "inventory",
        "material_requirement",
        "purchase_summary",
    }


def test_equivalent_count_units_are_allowed_without_physical_conversion() -> None:
    result = assess([inventory(unit="UN"), requirement(unit="EA"), summary(unit="pcs")])
    assert result.lines[0].calculated_to_buy == Decimal(7500)
    assert "unit_conflict" not in codes(result.lines[0])


def test_net_requirement_is_not_netted_again_or_given_extra_safety_stock() -> None:
    result = assess([inventory(), requirement(quantity="7500", demand_basis="net")])
    assert result.lines[0].calculated_to_buy == Decimal(7500)
    assert result.lines[0].ready
    alone = assess([requirement(quantity="7500", demand_basis="necessidade líquida")])
    assert alone.lines[0].calculated_to_buy == Decimal(7500)
    assert alone.lines[0].ready


def test_missing_gross_net_semantics_preserves_reported_purchase() -> None:
    result = assess([inventory(), requirement(demand_basis=""), summary()])
    line = result.lines[0]
    assert line.calculated_to_buy is None
    assert line.reported_to_buy == Decimal(7500)
    assert "ambiguous_demand_basis" in codes(line)
    assert all("BOM" not in issue.message for issue in line.issues)


def test_unknown_inbound_date_does_not_promise_timed_coverage() -> None:
    result = assess([inventory(receipt_date=""), requirement(), summary()])
    line = result.lines[0]
    assert line.on_order == Decimal(1500)
    assert line.calculated_to_buy is None
    assert line.reported_to_buy == Decimal(7500)
    assert "unconfirmed_inbound_date" in codes(line)


def test_late_inbound_is_excluded_from_coverage() -> None:
    result = assess([inventory(receipt_date="2026-11-01"), requirement()])
    line = result.lines[0]
    assert line.calculated_to_buy == Decimal(9000)
    assert "late_inbound_stock" in codes(line)
    assert line.ready


def test_zero_on_order_needs_no_receipt_date() -> None:
    result = assess([inventory(on_order="0", receipt_date=""), requirement()])
    assert result.lines[0].calculated_to_buy == Decimal(9000)
    assert result.lines[0].ready


@pytest.mark.parametrize("field", ["available", "on_order", "safety_stock"])
def test_absent_inventory_quantities_do_not_silently_become_zero(field: str) -> None:
    result = assess([inventory(**{field: ""}), requirement()])
    line = result.lines[0]
    assert line.calculated_to_buy is None
    assert f"missing_{field}" in codes(line)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("projected", "-999", "summary_projection_mismatch"),
        ("reported_to_buy", "7000", "summary_purchase_mismatch"),
        ("reported_estimated_cost", "580000", "summary_cost_mismatch"),
        ("available", "8000", "summary_inventory_mismatch"),
        ("demand", "12000", "summary_demand_mismatch"),
    ],
)
def test_consolidated_arithmetic_and_cross_source_conflicts_are_reported(
    field: str,
    value: str,
    code: str,
) -> None:
    line = assess([inventory(), requirement(), summary(**{field: value})]).lines[0]
    assert code in codes(line)
    assert not line.ready
    assert line.reported_to_buy is not None


def test_duplicate_mapping_of_same_cells_deduplicates_but_different_rows_do_not() -> None:
    stock = inventory()
    line = assess([stock, stock, requirement()]).lines[0]
    assert line.calculated_to_buy == Decimal(7500)
    other = record("inventory", 3, **stock.values)
    line = assess([stock, other, requirement(), summary()]).lines[0]
    assert line.calculated_to_buy is None
    assert line.reported_to_buy == Decimal(7500)
    assert "ambiguous_inventory" in codes(line)


def test_one_source_row_can_supply_distinct_concepts_without_double_counting() -> None:
    stock, demand, reported = inventory(), requirement(), summary(currency="BRL")
    for evidence_record in (stock, demand, reported):
        for field in ("part_number", "available", "on_order", "safety_stock"):
            if field in evidence_record.evidence and field in stock.evidence:
                evidence_record.evidence[field] = stock.evidence[field]
    line = assess([stock, demand, reported]).lines[0]
    assert line.calculated_to_buy == Decimal(7500)
    assert line.ready


def test_one_source_cell_cannot_be_both_stock_and_demand() -> None:
    stock, demand = inventory(), requirement(quantity="8500")
    demand.evidence["quantity"] = stock.evidence["available"]
    line = assess([stock, demand]).lines[0]
    assert line.calculated_to_buy is None
    assert not line.ready
    assert "overlapping_stock_demand_evidence" in codes(line)


def test_multiple_demand_rows_are_not_arbitrarily_added_or_overwritten() -> None:
    first = requirement()
    second = record("material_requirement", 3, **{**first.values, "quantity": "1000"})
    line = assess([inventory(), first, second, summary()]).lines[0]
    assert line.calculated_to_buy is None
    assert line.demand is None
    assert "ambiguous_material_requirement" in codes(line)


def test_location_matching_keeps_stock_pools_separate() -> None:
    result = assess(
        [
            inventory(location="A", on_order="0", available="100", safety_stock="0"),
            inventory(location="B", on_order="0", available="0", safety_stock="0"),
            requirement(location="A", quantity="50"),
            requirement(location="B", quantity="50"),
        ]
    )
    assert {(line.location, line.calculated_to_buy) for line in result.lines} == {
        ("A", Decimal(0)),
        ("B", Decimal(50)),
    }


def test_unlocated_requirement_cannot_choose_between_multiple_sites() -> None:
    result = assess([inventory(location="A"), inventory(location="B"), requirement(location="")])
    assert all(line.calculated_to_buy is None for line in result.lines)
    unlocated = next(line for line in result.lines if line.location is None)
    assert "ambiguous_location" in codes(unlocated)


def test_one_ambiguous_item_does_not_block_other_items() -> None:
    result = assess(
        [
            inventory(),
            requirement(unit="UN"),
            inventory(part_number="B"),
            requirement(part_number="B"),
        ]
    )
    by_part = {line.part_number: line for line in result.lines}
    assert by_part["00010"].calculated_to_buy is None
    assert by_part["B"].calculated_to_buy == Decimal(7500)
    assert by_part["B"].ready


def test_supplier_code_joins_master_history_and_performance_without_guessing_currency() -> None:
    result = assess(
        [
            inventory(),
            requirement(),
            summary(),
            record(
                "supplier", supplier_code="001", name="Local Supply", email="supply@example.test"
            ),
            record(
                "purchase",
                2,
                part_number="00010",
                supplier_code="001",
                ordered_at="2026-09-01",
                unit_price="76.20",
                currency="BRL",
            ),
            record(
                "purchase",
                3,
                part_number="00010",
                supplier_code="001",
                ordered_at="2026-10-01",
                unit_price="78.50",
                currency="BRL",
            ),
            record(
                "supplier_performance",
                supplier_code="001",
                otif_pct="95",
                quality_pct="92",
                lead_time_days="7",
            ),
        ]
    )
    line = result.lines[0]
    assert line.currency is None
    assert len(line.suppliers) == 1
    supplier = line.suppliers[0]
    assert supplier.supplier_code == "001"
    assert supplier.name == "Local Supply"
    assert supplier.email == "supply@example.test"
    assert supplier.last_price == Decimal("78.50")
    assert supplier.currency == "BRL"
    assert supplier.otif_pct == Decimal(95)
    assert supplier.quality_pct == Decimal(92)
    assert supplier.lead_time_days == Decimal(7)
    assert {ref.table for ref in supplier.evidence} == {
        "supplier",
        "purchase",
        "supplier_performance",
    }


def test_conflicting_supplier_identity_does_not_erase_calculated_quantities() -> None:
    result = assess(
        [
            inventory(),
            requirement(),
            record("supplier", supplier_code="001", name="Supplier A"),
            record("supplier", 3, supplier_code="001", name="Supplier B"),
            record(
                "purchase",
                part_number="00010",
                supplier_code="001",
                ordered_at="2026-09-01",
                unit_price="10",
                currency="BRL",
            ),
        ]
    )
    line = result.lines[0]
    assert line.calculated_to_buy == Decimal(7500)
    assert not line.ready
    assert line.suppliers[0].name is None
    assert "supplier_code_conflict" in codes(line)


@pytest.mark.parametrize("other_kind", ["supplier", "purchase", "supplier_performance"])
def test_supplier_identity_conflicts_consider_every_source(other_kind: RecordKind) -> None:
    other = record(
        other_kind,
        3,
        **{
            "supplier_code": "001",
            "supplier_name" if other_kind == "purchase" else "name": "Second supplier",
            "part_number": "00010",
            "ordered_at": "2026-10-01",
            "unit_price": "20",
        },
    )
    line = assess(
        [
            requirement(demand_basis="net"),
            record(
                "purchase",
                part_number="00010",
                supplier_code="001",
                supplier_name="First supplier",
                ordered_at="2026-09-01",
                unit_price="10",
            ),
            other,
        ]
    ).lines[0]
    assert "supplier_code_conflict" in codes(line)
    assert line.suppliers[0].name is None
    assert line.suppliers[0].email is None
    assert line.calculated_to_buy == Decimal(11000)
    assert not line.ready
    conflict = next(issue for issue in line.issues if issue.code == "supplier_code_conflict")
    assert {ref.row for ref in conflict.evidence} == {2, 3}


def test_supplier_performance_can_resolve_a_code_without_a_supplier_master() -> None:
    line = assess(
        [
            requirement(demand_basis="net"),
            record(
                "purchase",
                part_number="00010",
                supplier_code="001",
                ordered_at="2026-09-01",
                unit_price="10",
                currency="BRL",
            ),
            record("supplier_performance", supplier_code="001", name="Local Supply", otif_pct="95"),
        ]
    ).lines[0]
    assert line.suppliers[0].name == "Local Supply"
    assert line.suppliers[0].email is None
    assert line.suppliers[0].otif_pct == Decimal(95)
    assert "unresolved_supplier_code" not in codes(line)


def test_supplier_identity_is_shared_across_items_but_price_is_item_specific() -> None:
    line = assess(
        [
            requirement(demand_basis="net"),
            record(
                "purchase",
                part_number="00010",
                supplier_code="001",
                supplier_name="First supplier",
                ordered_at="2026-09-01",
                unit_price="10",
            ),
            record(
                "purchase",
                3,
                part_number="OTHER",
                supplier_code="001",
                supplier_name="Second supplier",
                ordered_at="2026-10-01",
                unit_price="20",
            ),
        ]
    ).lines[0]
    assert "supplier_code_conflict" in codes(line)
    assert line.suppliers[0].name is None
    assert line.suppliers[0].last_price == Decimal(10)


def test_name_only_purchase_uses_unique_master_code_to_join_performance() -> None:
    line = assess(
        [
            requirement(demand_basis="net"),
            record(
                "purchase",
                part_number="00010",
                supplier_name="Local Supply",
                ordered_at="2026-09-01",
                unit_price="10",
                currency="BRL",
            ),
            record("supplier", supplier_code="001", name="Local Supply"),
            record("supplier_performance", supplier_code="001", otif_pct="95"),
        ]
    ).lines[0]
    assert line.suppliers[0].supplier_code == "001"
    assert line.suppliers[0].otif_pct == Decimal(95)


def test_undated_history_does_not_choose_a_latest_price() -> None:
    result = assess(
        [
            requirement(demand_basis="net"),
            record(
                "purchase",
                part_number="00010",
                supplier_name="Supplier",
                unit_price="10",
                currency="BRL",
            ),
        ]
    )
    supplier = result.lines[0].suppliers[0]
    assert supplier.last_price is None
    assert "undated_purchase_history" in codes(result.lines[0])


@pytest.mark.parametrize("quantity", ["NaN", "Infinity", "-2", "1,000", "1000000000000"])
def test_invalid_or_ambiguous_numbers_do_not_reach_the_calculation(quantity: str) -> None:
    line = assess([inventory(), requirement(quantity=quantity)]).lines[0]
    assert line.calculated_to_buy is None
    assert "invalid_number" in codes(line)


def test_decimal_arithmetic_does_not_introduce_binary_float_error() -> None:
    line = assess(
        [
            inventory(available="0.1", on_order="0", safety_stock="0.2"),
            requirement(quantity="0.3"),
        ]
    ).lines[0]
    assert line.calculated_to_buy == Decimal("0.4")


def test_summary_only_remains_useful_without_fabricating_a_requirement() -> None:
    line = assess([summary()]).lines[0]
    assert line.reported_to_buy == Decimal(7500)
    assert line.reported_estimated_cost == Decimal(588750)
    assert line.calculated_to_buy is None
    assert "missing_material_requirement" in codes(line)
