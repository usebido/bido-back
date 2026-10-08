"""Structural procurement regressions: quantities, dates, joins and source provenance."""

from datetime import date
from decimal import Decimal
from random import Random
from uuid import uuid4

import pytest
from pydantic import ValidationError

from bido_back.agents.context import ToolContext
from bido_back.purchasing.sources.contracts import (
    ColumnMapping,
    MappingProposal,
    MappingTask,
    Record,
    RecordKind,
    TableProposal,
)
from bido_back.purchasing.sources.planning import assess_plan
from bido_back.purchasing.sources.readers import SourceDocument
from bido_back.purchasing.sources.service import analyze_documents
from evals.source_fixtures import EXPECTED, SCOPE, fact, motor_records
from tests.purchasing.test_workbook import XLSX, workbook


def quantities(records: list[Record]) -> dict[str, Decimal | None]:
    return {line.part_number: line.calculated_to_buy for line in assess_plan(records, SCOPE).lines}


def test_scattered_motor_plan_is_derived_without_direct_component_requisitions() -> None:
    result = assess_plan(motor_records(), SCOPE)
    assert {line.part_number: line.calculated_to_buy for line in result.lines} == EXPECTED
    assert all(line.ready for line in result.lines)
    bearing, shaft, housing, copper, magnet, screw = result.lines
    assert [line.demand for line in result.lines] == [80, 40, 40, 32, 320, 160]
    assert [line.available for line in result.lines] == [20, 10, 45, 10, 70, 100]
    assert [line.on_order for line in result.lines] == [10, 0, 0, 0, 30, 0]
    assert bearing.need_by == date(2026, 10, 18)
    assert not shaft.inbound[0].eligible
    assert housing.netting[0].projected_balance == 5
    assert copper.unit == "kg" and screw.unit == magnet.unit == "pcs"
    for line in result.lines:
        assert {ref.table for ref in line.demand_contributions[0].evidence} == {
            "bom",
            "production_plan",
        }
        assert len(line.demand_contributions) == 1
        assert line.suppliers[0].last_price == Decimal("10.50")
        assert line.suppliers[0].last_price_basis == "received_order"
        assert line.suppliers[0].email == "sales@supplier.example.test"
        assert "receipt" in {ref.table for ref in line.suppliers[0].evidence}


def test_row_order_and_copied_business_records_do_not_change_quantities() -> None:
    rows = motor_records()
    copies: list[Record] = []
    for row in rows:
        copy = row.model_copy(deep=True)
        for ref in copy.evidence.values():
            ref.source_id = "second-export"
            ref.row += 100
        copies.append(copy)
    rows.extend(copies)
    Random(42).shuffle(rows)  # noqa: S311 - reproducible layout permutation, not a secret.
    assert quantities(rows) == EXPECTED


def test_area_scope_resolves_the_same_product_via_the_master() -> None:
    scope = SCOPE.model_copy(update={"product_code": None, "area": "Motors"})
    result = assess_plan(motor_records(), scope)
    assert {line.part_number: line.calculated_to_buy for line in result.lines} == EXPECTED


@pytest.mark.parametrize(
    ("kind", "field", "replacement", "code"),
    [
        ("inventory", "reserved", None, "missing_available"),
        ("inventory", "as_of", None, "missing_snapshot_date"),
        ("inventory", "total", "1", "inventory_balance_conflict"),
        ("inventory", "unit", "kg", "unit_conflict"),
        ("delivery_schedule", "quantity", "11", "outstanding_quantity_conflict"),
        ("delivery_schedule", "unit", "kg", "incompatible_delivery_schedule"),
        ("purchase", "po_line", None, "missing_join_key"),
        ("purchase", "status", "Unknown", "unknown_order_status"),
    ],
)
def test_inconsistent_evidence_blocks_the_affected_quantity(
    kind: str,
    field: str,
    replacement: str | None,
    code: str,
) -> None:
    rows = motor_records()
    row = next(
        r
        for r in rows
        if r.kind == kind
        and r.values.get("part_number") == "000101"
        and (kind != "purchase" or r.values.get("status") == "Open")
    )
    if replacement is None:
        row.values.pop(field)
        row.evidence.pop(field)
    else:
        row.values[field] = replacement
    line = assess_plan(rows, SCOPE).lines[0]
    assert code in {i.code for i in line.issues}
    assert line.calculated_to_buy is None and not line.ready


def test_conflicting_current_snapshot_is_not_summed_or_arbitrarily_selected() -> None:
    rows = motor_records()
    row = next(r for r in rows if r.kind == "inventory")
    duplicate = row.model_copy(deep=True)
    duplicate.values["total"] = "26"
    rows.append(duplicate)
    line = assess_plan(rows, SCOPE).lines[0]
    assert "conflicting_inventory_snapshot" in {i.code for i in line.issues}
    assert not line.ready


def test_available_stock_is_not_reduced_by_reservations_twice() -> None:
    rows = motor_records()
    row = next(r for r in rows if r.kind == "inventory")
    row.values["available"] = "20"
    assert quantities(rows) == EXPECTED


def test_explicit_pending_balance_is_required_before_subtracting_an_open_order() -> None:
    rows = [r for r in motor_records() if r.kind != "delivery_schedule"]
    result = assess_plan(rows, SCOPE)
    assert not result.lines[0].ready
    assert "missing_pending_confirmation" in {i.code for i in result.lines[0].issues}


def test_partial_physical_receipts_are_not_added_again_to_stock() -> None:
    rows = motor_records()
    order = next(r for r in rows if r.kind == "purchase" and r.values.get("po_number") == "OPEN-40")
    order.values["quantity"] = "16"
    for i in range(2):
        rows.append(
            fact(
                "receipt",
                50 + i,
                po_number="OPEN-40",
                po_line="00010",
                location="Plant A",
                quantity="3",
                unit="pcs",
                received_at="2026-10-07",
                movement="101",
                receipt_id=f"PARTIAL-{i}",
                rejected="0",
            )
        )
    assert quantities(rows) == EXPECTED


def test_early_shortage_is_not_hidden_by_a_receipt_before_a_later_need() -> None:
    rows = motor_records()
    first = next(r for r in rows if r.kind == "production_plan")
    first.values.update(quantity="15", need_by="2026-10-10")
    second = first.model_copy(deep=True)
    second.values.update(reference="BUILD-EARLY-2", quantity="5", need_by="2026-10-18")
    rows.append(second)
    line = assess_plan(rows, SCOPE).lines[0]
    assert line.demand == 40
    assert line.calculated_to_buy == 10
    assert [(step.need_by, step.purchase_quantity, step.incoming) for step in line.netting] == [
        (date(2026, 10, 10), Decimal(10), Decimal(0)),
        (date(2026, 10, 18), Decimal(0), Decimal(10)),
    ]


def test_much_later_receipt_cannot_retroactively_erase_the_early_shortage() -> None:
    rows = motor_records()
    first = next(r for r in rows if r.kind == "production_plan")
    first.values.update(quantity="15", need_by="2026-10-10")
    second = first.model_copy(deep=True)
    second.values.update(reference="BUILD-EARLY-2", quantity="5", need_by="2026-10-18")
    rows.append(second)
    for row in rows:
        if row.values.get("po_number") == "OPEN-40":
            row.values["quantity"] = "100"
    line = assess_plan(rows, SCOPE).lines[0]
    assert line.calculated_to_buy == 10  # Aggregate netting would incorrectly return zero.


def test_ambiguous_bom_revision_and_repeated_demand_are_blocked() -> None:
    rows = motor_records()
    bom = next(r for r in rows if r.kind == "bom").model_copy(deep=True)
    bom.values["revision"] = "B"
    rows.append(bom)
    result = assess_plan(rows, SCOPE)
    assert "ambiguous_bom_revision" in {i.code for i in result.issues}
    assert not result.lines
    rows = motor_records()
    rows.append(
        fact(
            "material_requirement",
            reference="REQ-2",
            part_number="000101",
            quantity="80",
            unit="pcs",
            location="Plant A",
            need_by="2026-10-18",
            demand_basis="gross",
        )
    )
    result = assess_plan(rows, SCOPE)
    assert "unreconciled_component_requirement" in {i.code for i in result.issues}
    assert not any(line.ready for line in result.lines)


def test_safety_stock_is_an_explicit_policy_and_not_an_implicit_zero() -> None:
    result = assess_plan(motor_records(), SCOPE.model_copy(update={"safety_stock": None}))
    assert all(not line.ready for line in result.lines)
    assert all("missing_safety_stock" in {i.code for i in line.issues} for line in result.lines)


def test_planning_scope_rejects_reversed_horizons_and_missing_product_or_area() -> None:
    with pytest.raises(ValidationError):
        type(SCOPE).model_validate({**SCOPE.model_dump(), "horizon_end": "2026-10-01"})
    with pytest.raises(ValidationError):
        type(SCOPE).model_validate({**SCOPE.model_dump(), "product_code": None})


async def test_workbook_read_mapping_calculation_and_disclosed_scope() -> None:
    # Multiple tables in each opaque sheet, offset headers, shuffled row and sheet order.
    records = motor_records()
    kinds: list[RecordKind] = list(dict.fromkeys(r.kind for r in records))
    layouts: dict[str, list[list[object]]] = {}
    sections: dict[str, list[TableProposal]] = {}
    for index, kind in enumerate(kinds):
        name = f"Export-{index % 3}"
        grid = layouts.setdefault(
            name, [["ERP report"], [], ["Export date", date(2026, 10, 8)], []]
        )
        matching = [r for r in records if r.kind == kind]
        Random(31).shuffle(matching)  # noqa: S311 - reproducible fixture order.
        fields = list(dict.fromkeys(f for r in matching for f in r.values))
        Random(47).shuffle(fields)  # noqa: S311 - reproducible fixture columns.
        header = len(grid) + 1
        grid.append(list(fields))
        grid.extend([[r.values.get(f) for f in fields] for r in matching])
        sections.setdefault(name, []).append(
            TableProposal(
                kind=kind,
                header_row=header,
                first_row=header + 1,
                last_row=len(grid),
                columns=[ColumnMapping(field=f, column=i) for i, f in enumerate(fields, 1)],
            )
        )
        grid.append([])

    async def mapper(task: MappingTask, _: ToolContext) -> MappingProposal:
        return MappingProposal(
            sections=[
                s.model_copy(
                    update={
                        "first_row": max(s.first_row, task.first_row),
                        "last_row": min(s.last_row, task.last_row),
                    }
                )
                for s in sections[task.table]
                if s.last_row >= task.first_row and s.first_row <= task.last_row
            ]
        )

    result = await analyze_documents(
        [SourceDocument("synthetic-erp", "industrial.xlsx", XLSX, workbook(layouts))],
        "Analyze motors in Plant A through October 22, safety stock zero",
        ToolContext(tenant_id=uuid4(), user_id=uuid4(), run_id=uuid4()),
        mapper,
        scope=SCOPE,
    )
    assert result.status == "assessed", result.model_dump_json()
    assert result.scope == SCOPE
    assert result.snapshot_dates == [date(2026, 9, 19), date(2026, 10, 8)]
    assert {(c.product_code, c.location) for c in result.planning_candidates} == {
        ("MTR-100", "Plant A"),
        ("MTR-100", "Plant B"),
    }
    assert {line.part_number: line.calculated_to_buy for line in result.lines} == EXPECTED
    assert len(result.coverage) == 3 and all(row.inspected for row in result.coverage)
    assert {ref.table for line in result.lines for ref in line.evidence} == set(layouts)


@pytest.mark.parametrize("variant", ["missing_order", "changed_line", "closed_order"])
def test_pending_deliveries_cannot_disappear_when_the_purchase_join_fails(variant: str) -> None:
    rows = motor_records()
    order = next(r for r in rows if r.kind == "purchase" and r.values.get("po_number") == "OPEN-40")
    if variant == "missing_order":
        rows.remove(order)
    elif variant == "changed_line":
        order.values["po_line"] = "00020"
    else:
        order.values["status"] = "Closed"
    line = assess_plan(rows, SCOPE).lines[0]
    assert not line.ready and line.calculated_to_buy is None
    assert {i.code for i in line.issues} & {
        "orphan_delivery_schedule",
        "closed_order_has_pending_delivery",
    }


@pytest.mark.parametrize("kind", ["production_plan", "bom"])
def test_missing_production_relationship_cannot_be_replaced_by_unrelated_requisitions(
    kind: str,
) -> None:
    rows = [r for r in motor_records() if r.kind != kind]
    result = assess_plan(rows, SCOPE)
    assert not result.lines
    assert "no_scoped_demand" in {i.code for i in result.issues}


def test_manufacturing_offset_changes_component_dates_and_inbound_eligibility() -> None:
    result = assess_plan(motor_records(), SCOPE.model_copy(update={"production_lead_days": 6}))
    assert all(line.need_by == date(2026, 10, 12) for line in result.lines)
    assert result.lines[0].calculated_to_buy == 60
    assert result.lines[4].calculated_to_buy == 250
    assert not any(i.code == "component_date_uses_production_date" for i in result.issues)


def test_missing_plant_on_relevant_production_cannot_silently_remove_demand() -> None:
    rows = motor_records()
    other = next(r for r in rows if r.kind == "production_plan").model_copy(deep=True)
    other.values["reference"] = "BUILD-NO-PLANT"
    other.values.pop("location")
    other.evidence.pop("location")
    rows.append(other)
    result = assess_plan(rows, SCOPE)
    assert "missing_production_location" in {i.code for i in result.issues}
    assert not any(line.ready for line in result.lines)


def test_invalid_unrelated_maintenance_date_does_not_block_motor_planning() -> None:
    rows = motor_records()
    next(r for r in rows if r.kind == "material_requirement").values["need_by"] = "unknown"
    assert quantities(rows) == EXPECTED


def test_qualified_supplier_without_purchase_history_keeps_an_unknown_price() -> None:
    rows = motor_records()
    rows.extend(
        [
            fact("supplier", 50, supplier_code="V2", name="New Supplier", email="new@example.test"),
            fact(
                "supplier_item",
                part_number="000101",
                supplier_code="V2",
                status="Approved",
                valid_from="2026-01-01",
            ),
        ]
    )
    line = assess_plan(rows, SCOPE).lines[0]
    assert line.ready
    supplier = next(s for s in line.suppliers if s.supplier_code == "V2")
    assert supplier.last_price is None
    assert supplier.qualification_status == "Approved"
    assert supplier.name == "New Supplier" and supplier.email == "new@example.test"


def test_confirmed_pending_delivery_on_snapshot_day_is_still_eligible() -> None:
    rows = motor_records()
    delivery = next(r for r in rows if r.kind == "delivery_schedule")
    delivery.values["expected_at"] = SCOPE.as_of.isoformat()
    assert quantities(rows) == EXPECTED
