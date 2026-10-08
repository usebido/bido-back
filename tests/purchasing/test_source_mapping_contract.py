"""Report-level values, checked interpretations and locale evidence in the mapping contract."""

import json
from typing import cast
from uuid import uuid4

import pytest

from bido_back.agents.context import ToolContext
from bido_back.purchasing.sources import mapping
from bido_back.purchasing.sources.contracts import (
    CellRef,
    ColumnMapping,
    ConstantMapping,
    DateOrder,
    EvidenceTable,
    Issue,
    MappingProposal,
    MappingTask,
    Record,
    TableProposal,
    ValueMapping,
)
from bido_back.purchasing.sources.mapping import map_sources, materialize


def context() -> ToolContext:
    return ToolContext(tenant_id=uuid4(), user_id=uuid4(), run_id=uuid4())


def columns(values: dict[str, int]) -> list[ColumnMapping]:
    return [ColumnMapping(field=field, column=column) for field, column in values.items()]


def stock_table(*rows: list[str | None]) -> EvidenceTable:
    return EvidenceTable(
        source_id="S",
        name="Saldos",
        rows=[
            ["MM / posição de estoque", None, None],
            ["Emissão: 08/10/2026", "Centro: Planta A", None],
            ["Material", "Saldo", "UM"],
            *[list(row) for row in rows],
        ],
    )


def stock_section(**update: object) -> TableProposal:
    section = TableProposal(
        kind="inventory",
        header_row=3,
        first_row=4,
        last_row=5,
        columns=[
            ColumnMapping(field="part_number", column=1, header_text="Material"),
            ColumnMapping(field="available", column=2, header_text="Saldo"),
            ColumnMapping(field="unit", column=3, header_text="UM"),
        ],
        constants=[
            ConstantMapping(field="as_of", ref=CellRef(row=2, column=1), text="08/10/2026"),
            ConstantMapping(field="location", ref=CellRef(row=2, column=2), text="Planta A"),
        ],
        date_order="day_first",
    )
    return section.model_copy(update=update)


def test_report_level_values_apply_to_every_row_with_their_own_evidence() -> None:
    table = stock_table(["000101", "20", "pcs"], ["000102", "10", "pcs"])

    records, issues, covered = materialize(table, MappingProposal(sections=[stock_section()]), 1, 5)

    assert [r.values for r in records] == [
        {
            "part_number": part,
            "available": quantity,
            "unit": "pcs",
            "as_of": "08/10/2026",
            "location": "Planta A",
        }
        for part, quantity in [("000101", "20"), ("000102", "10")]
    ]
    as_of = records[0].evidence["as_of"]
    assert (as_of.row, as_of.column, as_of.start, as_of.end) == (2, 1, 9, 19)
    assert records[0].canonical["as_of"].model_dump() == {
        "value": "2026-10-08",
        "source": "08/10/2026",
        "basis": "declared_date_order",
        "evidence": [],
    }
    assert [(i.code, i.severity) for i in issues] == [("declared_date_order", "warning")]
    assert covered == {2, 4, 5}


def test_a_row_value_wins_over_the_report_level_value() -> None:
    table = stock_table(["000101", "20", "pcs"])
    table.rows[3].append("Planta B")
    section = stock_section(
        last_row=4,
        columns=[*stock_section().columns, ColumnMapping(field="location", column=4)],
    )

    records, _, _ = materialize(table, MappingProposal(sections=[section]), 1, 4)

    assert records[0].values["location"] == "Planta B"
    assert records[0].evidence["location"].row == 4


@pytest.mark.parametrize(
    ("constant", "code"),
    [
        (ConstantMapping(field="as_of", ref=CellRef(row=4, column=2)), "invalid_constant"),
        (
            ConstantMapping(field="as_of", ref=CellRef(row=2, column=1), text="09/10/2026"),
            "invalid_constant_text",
        ),
        (ConstantMapping(field="secret", ref=CellRef(row=2, column=1)), "invalid_constant"),
        (ConstantMapping(field="as_of", ref=CellRef(row=1, column=3)), "invalid_constant"),
    ],
)
def test_report_level_values_must_be_real_cells_outside_the_data_rows(
    constant: ConstantMapping, code: str
) -> None:
    table = stock_table(["000101", "20", "pcs"])
    section = stock_section(last_row=4, constants=[constant])

    records, issues, _ = materialize(table, MappingProposal(sections=[section]), 1, 4)

    assert code in [i.code for i in issues if i.severity == "blocker"]
    assert "as_of" not in records[0].values


async def test_a_continued_section_keeps_its_report_level_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mapping, "PAGE_ROWS", 4)
    rows: list[list[str | None]] = [[f"0001{i:02d}", str(i), "pcs"] for i in range(1, 7)]
    table = stock_table(*rows)

    async def mapper(task: MappingTask, _: ToolContext) -> MappingProposal:
        first = max(task.first_row, 4)
        if task.first_row == 1:
            return MappingProposal(sections=[stock_section(first_row=first, last_row=4)])
        # The continuation repeats only the columns; code keeps the page-1 context.
        return MappingProposal(
            sections=[
                stock_section(
                    first_row=first, last_row=task.last_row, constants=[], date_order=None
                )
            ]
        )

    records, issues, _ = await map_sources([table], "Read", context(), mapper)

    assert len(records) == 6
    assert {r.values["as_of"] for r in records} == {"08/10/2026"}
    assert {r.canonical["as_of"].value for r in records} == {"2026-10-08"}
    assert not any(i.severity == "blocker" for i in issues)


def test_value_maps_are_checked_and_recorded_next_to_the_source_spelling() -> None:
    table = EvidenceTable(
        source_id="S",
        name="Bestellungen",
        rows=[
            ["Bestellung", "Status", "ME"],
            ["4500", "Zrealizowane", "Stk"],
            ["4501", "Offen", "szt"],
        ],
    )
    section = TableProposal(
        kind="purchase",
        header_row=1,
        first_row=2,
        last_row=3,
        columns=columns({"po_number": 1, "status": 2, "unit": 3}),
        value_maps=[
            ValueMapping(field="status", source="zrealizowane", canonical="closed"),
            ValueMapping(field="status", source="Offen", canonical="open"),
            ValueMapping(field="unit", source="szt", canonical="count"),
            # Absent from the column, a term outside the field's vocabulary, and a field
            # without a vocabulary: all ignored.
            ValueMapping(field="status", source="Storniert", canonical="cancelled"),
            ValueMapping(field="status", source="Offen", canonical="kit"),
            ValueMapping(field="po_number", source="4500", canonical="open"),
        ],
    )

    records, issues, _ = materialize(table, MappingProposal(sections=[section]), 1, 3)

    closed, opened = records
    assert closed.values["status"] == "Zrealizowane"
    assert closed.canonical["status"].model_dump() == {
        "value": "closed",
        "source": "Zrealizowane",
        "basis": "value_map",
        "evidence": [],
    }
    assert opened.canonical["status"].value == "open"
    assert opened.canonical["unit"].value == "count"
    assert "unit" not in closed.canonical  # "Stk" is read by the built-in vocabulary.
    assert [(i.code, i.severity) for i in issues] == [("invalid_value_map", "warning")]


def test_value_maps_only_accept_closed_vocabulary_terms() -> None:
    with pytest.raises(ValueError, match="canonical"):
        ValueMapping.model_validate({"field": "status", "source": "x", "canonical": "Fechado"})


def test_contradicting_interpretations_leave_the_value_unresolved() -> None:
    table = EvidenceTable(
        source_id="S",
        name="PO",
        rows=[["PO", "Status", "UM"], ["1", "Encerrado", "kits"], ["2", "Feito", "pcs"]],
    )
    section = TableProposal(
        kind="purchase",
        header_row=1,
        first_row=2,
        last_row=3,
        columns=columns({"po_number": 1, "status": 2, "unit": 3}),
        value_maps=[
            ValueMapping(field="status", source="Encerrado", canonical="open"),
            ValueMapping(field="status", source="Feito", canonical="closed"),
            ValueMapping(field="status", source="Feito", canonical="open"),
            ValueMapping(field="unit", source="kits", canonical="count"),
        ],
    )

    records, issues, _ = materialize(table, MappingProposal(sections=[section]), 1, 3)

    assert [r.canonical["status"].value for r in records] == [None, None]
    assert records[0].canonical["unit"].value is None
    assert [i.code for i in issues] == ["conflicting_value_map"]


def _numbers(*values: str) -> EvidenceTable:
    return EvidenceTable(
        source_id="S", name="T", rows=[["Material", "Qtd"], *[[f"P-{v}", v] for v in values]]
    )


def _quantities(table: EvidenceTable) -> list[Record]:
    section = TableProposal(
        kind="inventory",
        header_row=1,
        first_row=2,
        last_row=1 + sum(1 for row in table.rows[1:] if row and (row[0] or "").startswith("P-")),
        columns=columns({"part_number": 1, "available": 2}),
    )
    records, _, _ = materialize(table, MappingProposal(sections=[section]), 1, len(table.rows))
    return records


@pytest.mark.parametrize(
    ("values", "expected"),
    [(("2,5", "1,000"), "1.000"), (("12.5", "1,000"), "1000"), (("1.234,56", "1.000"), "1000")],
)
def test_the_column_decides_an_ambiguous_decimal_separator(
    values: tuple[str, str], expected: str
) -> None:
    records = _quantities(_numbers(*values))

    derived = records[1].canonical["available"]
    assert (derived.value, derived.basis) == (expected, "decimal_separator")
    assert [(ref.row, ref.column) for ref in derived.evidence] == [(2, 2)]


def test_a_mixed_column_does_not_resolve_ambiguous_numbers() -> None:
    records = _quantities(_numbers("2,5", "12.5", "1,000"))

    assert "available" not in records[2].canonical


def test_the_sheet_decides_when_the_column_cannot() -> None:
    table = _numbers("1,000")
    table.rows.extend([[], ["Valor total", "7,25"]])

    records = _quantities(table)

    assert records[0].canonical["available"].value == "1.000"


def test_numbers_stored_by_the_workbook_need_no_separator() -> None:
    table = _numbers("1.125")
    table.numbers = {(2, 2)}

    records = _quantities(table)

    assert records[0].canonical["available"].model_dump() == {
        "value": "1.125",
        "source": "1.125",
        "basis": "native_number",
        "evidence": [],
    }


def _dates(*values: str, declared: DateOrder | None = None) -> tuple[list[Record], list[Issue]]:
    table = EvidenceTable(
        source_id="S",
        name="Plan",
        rows=[["Ref", "Need"], *[[f"R-{i}", v] for i, v in enumerate(values)]],
    )
    section = TableProposal(
        kind="material_requirement",
        header_row=1,
        first_row=2,
        last_row=len(table.rows),
        columns=columns({"reference": 1, "need_by": 2}),
        date_order=declared,
    )
    records, issues, _ = materialize(table, MappingProposal(sections=[section]), 1, len(table.rows))
    return records, issues


@pytest.mark.parametrize(
    ("values", "declared", "expected", "basis"),
    [
        (("25/09/2026", "08/10/2026"), None, "2026-10-08", "date_order"),
        (("10/15/2026", "08/10/2026"), None, "2026-08-10", "date_order"),
        (("10/15/2026", "08/10/2026"), "day_first", "2026-08-10", "date_order"),
        (("08/10/2026",), "day_first", "2026-10-08", "declared_date_order"),
    ],
)
def test_day_month_order_comes_from_proof_then_from_a_declaration(
    values: tuple[str, ...], declared: DateOrder | None, expected: str, basis: str
) -> None:
    records, issues = _dates(*values, declared=declared)

    derived = records[-1].canonical["need_by"]
    assert (derived.value, derived.basis) == (expected, basis)
    contradicted = "declared_date_order_contradicted" in [i.code for i in issues]
    assert contradicted == (declared == "day_first" and basis == "date_order")


@pytest.mark.parametrize("values", [("08/10/2026",), ("25/09/2026", "10/15/2026", "08/10/2026")])
def test_unproven_or_contradicting_dates_stay_ambiguous(values: tuple[str, ...]) -> None:
    records, _ = _dates(*values, declared=None)

    assert "need_by" not in records[-1].canonical


def test_an_echoed_header_must_be_the_mapped_columns_header() -> None:
    table = stock_table(["000101", "20", "pcs"])
    shifted = stock_section(
        last_row=4,
        columns=[
            ColumnMapping(field="part_number", column=1, header_text="Material"),
            ColumnMapping(field="available", column=3, header_text="Saldo"),
        ],
    )

    records, issues, _ = materialize(table, MappingProposal(sections=[shifted]), 1, 4)

    assert records == []
    assert [(i.code, i.severity) for i in issues] == [("header_mismatch", "blocker")]


@pytest.mark.parametrize(
    "total_row",
    [
        ["Total", "30", "pcs"],
        ["SUBTOTAL PLANTA A", "30", "pcs"],
        ["**", "30", "pcs"],
        [None, "30", "pcs"],
    ],
)
def test_total_rows_never_become_transactions(total_row: list[str | None]) -> None:
    table = stock_table(["000101", "20", "pcs"], ["000102", "10", "pcs"], total_row)
    section = stock_section(last_row=6)

    records, issues, _ = materialize(table, MappingProposal(sections=[section]), 1, 6)

    assert [r.values["part_number"] for r in records] == ["000101", "000102"]
    assert "derived_rows_skipped" in [i.code for i in issues]


@pytest.mark.parametrize(
    ("row", "field"),
    [(["000101", "Planta A", "pcs"], "available"), (["000101", "20", "caixas de 12"], "unit")],
)
def test_columns_whose_values_do_not_read_as_the_field_are_refused(
    row: list[str | None], field: str
) -> None:
    table = stock_table(row)

    records, issues, _ = materialize(
        table, MappingProposal(sections=[stock_section(last_row=4)]), 1, 4
    )

    assert records == []
    [issue] = [i for i in issues if i.severity == "blocker"]
    assert issue.code == "inadmissible_mapping" and field in issue.message


def test_blank_grouping_keys_repeat_the_value_above_only_when_asked() -> None:
    table = EvidenceTable(
        source_id="S",
        name="PO",
        rows=[
            ["Pedido", "Item", "Material", "Qtd"],
            ["4500", "10", "000101", "5"],
            [None, "20", "000102", "7"],
            [None, "30", "000103", "9"],
        ],
    )
    section = TableProposal(
        kind="purchase",
        header_row=1,
        first_row=2,
        last_row=4,
        columns=[
            ColumnMapping(field="po_number", column=1, fill_down=True),
            *columns({"po_line": 2, "part_number": 3, "quantity": 4}),
        ],
    )

    records, issues, _ = materialize(table, MappingProposal(sections=[section]), 1, 4)

    assert [r.values["po_number"] for r in records] == ["4500"] * 3
    assert {r.evidence["po_number"].row for r in records} == {2}
    assert [(i.code, i.severity) for i in issues] == [("filled_down", "warning")]
    quantities = section.model_copy(
        update={"columns": [ColumnMapping(field="quantity", column=4, fill_down=True)]}
    )
    _, issues, _ = materialize(table, MappingProposal(sections=[quantities]), 1, 4)
    assert [i.code for i in issues] == ["invalid_fill_down"]


async def test_identifier_spellings_are_disclosed_and_collisions_in_one_table_block() -> None:
    tables = [
        EvidenceTable(source_id="S", name="Master", rows=[["Material"], ["000101"], ["000102"]]),
        EvidenceTable(source_id="S", name="PO", rows=[["Material"], ["101"], ["102"], ["0102"]]),
    ]

    async def mapper(task: MappingTask, _: ToolContext) -> MappingProposal:
        return MappingProposal(
            sections=[
                TableProposal(
                    kind="item" if task.table == "Master" else "purchase",
                    header_row=1,
                    first_row=2,
                    last_row=task.last_row,
                    columns=columns({"part_number": 1}),
                )
            ]
        )

    _, issues, _ = await map_sources(tables, "Read", context(), mapper)

    found = {i.code: i for i in issues if i.code != "unmapped_rows"}
    assert found["normalized_identifier_match"].severity == "warning"
    assert found["identifier_collision"].severity == "blocker"
    assert {(r.table, r.row) for r in found["identifier_collision"].evidence} == {
        ("PO", 3),
        ("PO", 4),
    }


async def test_every_task_carries_the_closed_vocabulary() -> None:
    seen: list[MappingTask] = []

    async def mapper(task: MappingTask, _: ToolContext) -> MappingProposal:
        seen.append(task)
        return MappingProposal()

    table = EvidenceTable(source_id="S", name="T", rows=[["x"]])
    await map_sources([table], "Read", context(), mapper)

    assert seen[0].vocabulary["purchase.status"] == ["open", "closed", "cancelled"]
    assert "count" in seen[0].vocabulary["unit"]


def test_proposal_schema_is_accepted_by_strict_structured_output() -> None:
    schema = MappingProposal.model_json_schema()
    assert "(?" not in json.dumps(schema)

    def siblings(node: object) -> list[str]:
        if isinstance(node, dict):
            fields = cast(dict[str, object], node)
            bad = [str(fields)] if "$ref" in fields and len(fields) > 1 else []
            return bad + [b for value in fields.values() for b in siblings(value)]
        if isinstance(node, list):
            return [b for value in cast(list[object], node) for b in siblings(value)]
        return []

    assert siblings(schema) == []
