"""Joins across differently formatted ERP exports, without a model: quantities never move."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from bido_back.agents.context import ToolContext
from bido_back.purchasing.sources.analysis import assess
from bido_back.purchasing.sources.contracts import (
    CanonicalValue,
    CellRef,
    ColumnMapping,
    ConstantMapping,
    Issue,
    MappingProposal,
    MappingTask,
    Record,
    RecordKind,
    SourceRef,
    TableProposal,
)
from bido_back.purchasing.sources.joins import receipts_for
from bido_back.purchasing.sources.planning import assess_plan
from bido_back.purchasing.sources.readers import SourceDocument
from bido_back.purchasing.sources.service import analyze_documents
from evals.source_fixtures import EXPECTED, SCOPE, fact, motor_records
from tests.purchasing.test_workbook import XLSX, workbook


def quantities(records: list[Record]) -> dict[str, Decimal | None]:
    return {line.part_number: line.calculated_to_buy for line in assess_plan(records, SCOPE).lines}


def extend(record: Record, **values: str) -> None:
    """Add source values with their own cells, as the mapper would locate them."""
    for i, (name, value) in enumerate(values.items(), start=len(record.evidence) + 1):
        record.values[name] = value
        ref = next(iter(record.evidence.values()))
        record.evidence[name] = SourceRef(
            source_id=ref.source_id, table=ref.table, row=ref.row, column=i
        )


def inline_register() -> list[Record]:
    """A purchase register with receipt and balance columns, and no other table for them."""
    rows = [r for r in motor_records() if r.kind not in {"receipt", "delivery_schedule"}]
    for order in rows:
        if order.kind != "purchase":
            continue
        if order.values["status"] == "Closed":
            extend(order, received_at="2026-09-10", received_quantity="20", rejected_quantity="1")
        else:
            extend(order, open_quantity=order.values["quantity"])
    return rows


def test_inline_receipts_and_open_balances_replace_separate_tables() -> None:
    result = assess_plan(inline_register(), SCOPE)

    assert {line.part_number: line.calculated_to_buy for line in result.lines} == EXPECTED
    assert all(line.ready for line in result.lines)
    shaft = next(line for line in result.lines if line.part_number == "000102")
    # The promised date stands for the expected one: 26/10 is after the need, so excluded.
    assert [(i.quantity, i.expected_at, i.eligible) for i in shaft.inbound] == [
        (Decimal(20), date(2026, 10, 26), False)
    ]
    for line in result.lines:
        [supplier] = line.suppliers
        assert supplier.last_price == Decimal("10.50")
        assert supplier.last_price_basis == "received_order"


def test_an_explicit_expected_date_wins_over_the_promised_date() -> None:
    rows = inline_register()
    order = next(r for r in rows if r.values.get("po_number") == "OPEN-41")
    extend(order, expected_at="2026-10-15")
    shaft = next(line for line in assess_plan(rows, SCOPE).lines if line.part_number == "000102")
    assert shaft.inbound[0].eligible and shaft.calculated_to_buy == 10


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"open_quantity": "12"}, "outstanding_quantity_conflict"),
        ({"received_quantity": "3", "received_at": "2026-10-06"}, "outstanding_quantity_conflict"),
        ({"open_quantity": "abc"}, "invalid_number"),
    ],
)
def test_an_open_balance_must_reconcile_with_the_order(change: dict[str, str], code: str) -> None:
    rows = inline_register()
    order = next(r for r in rows if r.values.get("po_number") == "OPEN-40")
    extend(order, **change)
    line = assess_plan(rows, SCOPE).lines[0]
    assert code in {i.code for i in line.issues}
    assert line.confidence == "low"
    assert "open_orders_not_counted" in {a.code for a in line.assumptions}


def test_inline_and_separate_evidence_must_agree() -> None:
    rows = motor_records()
    order = next(r for r in rows if r.values.get("po_number") == "OPEN-40")
    extend(order, open_quantity="9")
    line = assess_plan(rows, SCOPE).lines[0]
    assert "conflicting_pending_evidence" in {i.code for i in line.issues}
    assert line.confidence == "low"

    rows = motor_records()
    order = next(r for r in rows if r.values.get("po_number") == "OPEN-40")
    extend(order, open_quantity="10", expected_at="2026-10-13")
    assert quantities(rows) == EXPECTED  # Agreement is corroboration, not double counting.


def test_inline_received_quantity_must_equal_the_separate_receipts() -> None:
    order = next(r for r in motor_records() if r.values.get("po_number") == "OLD-7")
    receipts = [r for r in motor_records() if r.kind == "receipt"]
    extend(order, received_quantity="19")
    issues: list[Issue] = []
    receipts_for(order, receipts, SCOPE.as_of, issues)
    assert [i.code for i in issues] == ["conflicting_receipt_evidence"]

    order.values["received_quantity"] = "20"
    issues = []
    found, received = receipts_for(order, receipts, SCOPE.as_of, issues)
    assert issues == [] and received == 20 and found[0].values["receipt_id"] == "RECEIPT-7"


def test_nothing_received_needs_no_receipt_date() -> None:
    order = fact("purchase", po_number="P", po_line="1", quantity="5", received_quantity="0")
    issues: list[Issue] = []
    assert receipts_for(order, [], SCOPE.as_of, issues) == ([], 0)
    assert issues == []


def test_identifiers_join_across_formats_and_display_the_master_spelling() -> None:
    rows = motor_records()
    for row in rows:
        if row.kind in {"bom", "inventory", "purchase", "delivery_schedule"}:
            part = row.values.get("part_number", "")
            if part.startswith("000"):
                row.values["part_number"] = (
                    f"{int(part)}.0" if row.kind == "bom" else str(int(part))
                )
        if row.kind in {"receipt", "delivery_schedule"}:
            row.values["po_line"] = "10"
        if row.kind == "inventory":
            row.values["location"] = "PLANT  A"
    result = assess_plan(rows, SCOPE)
    assert {line.part_number: line.calculated_to_buy for line in result.lines} == EXPECTED
    assert all(line.ready for line in result.lines)
    assert [line.description for line in result.lines][0] == "Bearing"
    assert all(line.suppliers[0].last_price == Decimal("10.50") for line in result.lines)


@pytest.mark.parametrize(
    ("kind", "status", "unit"),
    [
        ("purchase", ("Erledigt", "Offen"), "Stk"),
        ("purchase", ("Chiuso", "Aperto"), "pz"),
        ("purchase", ("Concluído", "Em aberto"), "pç"),
        ("purchase", ("Cerrado", "Abierto"), "unidades"),
    ],
)
def test_statuses_and_units_in_other_languages_keep_the_same_result(
    kind: str, status: tuple[str, str], unit: str
) -> None:
    rows = motor_records()
    for row in rows:
        if row.kind == kind:
            row.values["status"] = status[0] if row.values["status"] == "Closed" else status[1]
        if row.kind == "delivery_schedule":
            row.values["status"] = "Ausstehend"
        if row.kind == "production_plan":
            row.values["status"] = "Geplant"
        if row.values.get("unit") == "pcs" and row.kind in {"inventory", "purchase", "receipt"}:
            row.values["unit"] = unit
    assert quantities(rows) == EXPECTED


def test_a_checked_interpretation_of_an_unknown_status_is_used() -> None:
    rows = motor_records()
    for row in rows:
        if row.kind == "purchase" and row.values["status"] == "Open":
            row.values["status"] = "W realizacji"
            row.canonical["status"] = CanonicalValue(
                value="open", source="W realizacji", basis="value_map"
            )
    assert quantities(rows) == EXPECTED


def test_a_gross_balance_basis_does_not_disqualify_a_separate_free_stock_column() -> None:
    # The basis describes the gross total; free stock is its own (computed) column.
    rows = motor_records()
    stock = next(r for r in rows if r.kind == "inventory")
    extend(stock, available="20", balance_basis="Livre utilização + reserva + bloqueio")
    stock.canonical["balance_basis"] = CanonicalValue(
        value="gross", source="Livre utilização + reserva + bloqueio", basis="value_map"
    )
    assert quantities(rows) == EXPECTED


def test_unscoped_assessment_reads_bases_and_identifiers_the_same_way() -> None:
    def record(kind: str, row: int, **values: str) -> Record:
        return fact(kind, row, **values)  # pyright: ignore[reportArgumentType]

    result = assess(
        [
            record(
                "inventory",
                2,
                part_number="00010",
                location="BR01",
                unit="Stk",
                available="8500",
                on_order="0",
                safety_stock="6500",
            ),  # fmt: skip
            record(
                "material_requirement",
                3,
                part_number="10",
                location="br01",
                unit="UN",
                quantity="11000",
                need_by="2026-10-31",
                demand_basis="Bruttobedarf",
            ),  # fmt: skip
        ]
    )
    [line] = result.lines
    assert line.part_number == "00010"
    assert line.calculated_to_buy == Decimal(9000) and line.ready


async def test_a_stock_report_dated_only_in_its_title_is_assessed() -> None:
    records = [r for r in motor_records() if r.kind != "inventory"]
    stock = [
        r for r in motor_records() if r.kind == "inventory" and r.values["as_of"] == "2026-10-08"
    ]
    fields = ["part_number", "unit", "location", "total", "reserved", "blocked"]
    layouts: dict[str, list[list[object]]] = {
        "Saldos": [
            ["MM / posição de estoque"],
            ["Posição em 08/10/2026"],
            [],
            list[object](fields),
            *[[r.values[f] for f in fields] for r in stock],
        ]
    }
    sections: dict[str, list[TableProposal]] = {
        "Saldos": [
            TableProposal(
                kind="inventory",
                header_row=4,
                first_row=5,
                last_row=4 + len(stock),
                columns=[
                    ColumnMapping(field=f, column=i, header_text=f) for i, f in enumerate(fields, 1)
                ],
                constants=[
                    ConstantMapping(field="as_of", ref=CellRef(row=2, column=1), text="08/10/2026")
                ],
                date_order="day_first",
            )
        ]
    }
    kinds: list[RecordKind] = list(dict.fromkeys(r.kind for r in records))
    for kind in kinds:
        matching = [r for r in records if r.kind == kind]
        names = list(dict.fromkeys(f for r in matching for f in r.values))
        layouts[kind] = [list[object](names), *[[r.values.get(f) for f in names] for r in matching]]
        sections[kind] = [
            TableProposal(
                kind=kind,
                header_row=1,
                first_row=2,
                last_row=1 + len(matching),
                columns=[ColumnMapping(field=f, column=i) for i, f in enumerate(names, 1)],
            )
        ]

    async def mapper(task: MappingTask, _: ToolContext) -> MappingProposal:
        return MappingProposal(sections=sections[task.table])

    result = await analyze_documents(
        [SourceDocument("erp", "export.xlsx", XLSX, workbook(layouts))],
        "Motor MTR-100",
        ToolContext(tenant_id=uuid4(), user_id=uuid4(), run_id=uuid4()),
        mapper,
        scope=SCOPE,
    )

    assert {line.part_number: line.calculated_to_buy for line in result.lines} == EXPECTED
    assert result.snapshot_dates == [date(2026, 10, 8)]
    assert "missing_snapshot_date" not in {i.code for line in result.lines for i in line.issues}
    assert "declared_date_order" in {i.code for i in result.issues}
    assert not any(i.severity == "blocker" for i in result.issues)
