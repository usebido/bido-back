"""A SAP-like export (MD04 + MB52 + ME2N in one workbook) plans every material and plant.

The MRP list states each element once: its element data is a label ("Ordem planej.")
or blank, never a key unique per row, so the rows of one list are never merged or
blocked by it. When a join does fail, the issue names the file, sheet, key columns and
example rows, and the reply writer reads them.
"""

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from bido_back.agents.context import ToolContext
from bido_back.copilot.model_views import assessment_view
from bido_back.purchasing.sources.contracts import (
    ColumnMapping,
    MappingProposal,
    MappingTask,
    ScopeRequest,
    SourceAssessment,
    TableProposal,
    ValueMapping,
)
from bido_back.purchasing.sources.readers import SourceDocument
from bido_back.purchasing.sources.service import analyze_documents
from tests.purchasing.test_workbook import XLSX, workbook

FILENAME = "sap_export_3_centros.xlsx"
MD04_HEADER = [
    "Material",
    "Texto breve",
    "Centro",
    "Data",
    "Elemento MRP",
    "Dados elem.MRP",
    "Qtd.recebida/necessária",
    "Qtd.disponível",
    "UM",
]
ME2N_HEADER = [
    "Documento compras",
    "Item",
    "Material",
    "Centro",
    "Fornecedor",
    "Qtd.pedido",
    "Data remessa",
    "Preço líquido",
]
NAMES = {
    "100000201": "Rolamento 6205-2RS",
    "100000202": "Motor 2 CV 4P IP55",
    "100000203": "Eixo aco 1045 D25",
    "100000204": "Retentor 25x40x7",
}
# Per material and plant: free stock, then its MRP elements (date, element, data, qty).
PLAN: dict[tuple[str, str], tuple[int, list[tuple[str, str, str, int]]]] = {
    ("100000201", "1000"): (120, [("10-16", "NecDep", "", -60), ("10-30", "NecDep", "", -80)]),
    ("100000201", "2000"): (
        30,
        [
            ("10-16", "NecDep", "", -60),
            ("10-25", "PedCompra", "4500019720", 100),
            ("10-30", "NecDep", "", -80),
        ],
    ),
    ("100000201", "3000"): (0, [("10-16", "NecDep", "", -60), ("10-30", "NecDep", "", -80)]),
    ("100000202", "1000"): (
        4,
        [
            ("10-20", "NecDep", "", -6),
            ("11-05", "NecDep", "", -5),
            ("11-20", "PedCompra", "4500019710", 10),
        ],
    ),
    ("100000202", "2000"): (10, [("10-20", "NecDep", "", -6), ("11-05", "NecDep", "", -5)]),
    ("100000202", "3000"): (2, [("10-20", "NecDep", "", -6), ("11-05", "NecDep", "", -5)]),
    ("100000203", "1000"): (50, [("10-23", "NecDep", "", -30), ("11-06", "NecDep", "", -20)]),
    ("100000203", "2000"): (0, [("10-23", "NecDep", "", -30), ("11-06", "NecDep", "", -20)]),
    ("100000203", "3000"): (
        15,
        [
            ("10-18", "PedCompra", "4500019730", 40),
            ("10-23", "NecDep", "", -30),
            ("11-06", "NecDep", "", -20),
        ],
    ),
    ("100000204", "1000"): (200, [("10-21", "NecDep", "", -50)]),
    ("100000204", "2000"): (80, [("10-21", "NecDep", "", -50)]),
    ("100000204", "3000"): (40, [("10-21", "NecDep", "", -50)]),
}
ORDERS: list[list[object]] = [
    ["4500019720", 10, "100000201", "2000", "Fornecedor 2000", 100, "2026-10-25", 10.0],
    ["4500019710", 10, "100000202", "1000", "Fornecedor 1000", 10, "2026-11-20", 17.5],
    ["4500019730", 10, "100000203", "3000", "Fornecedor 3000", 40, "2026-10-18", 25.0],
]
# What Bido buys, per material and plant, and when (NecDep only, 4 weeks from 2026-10-09).
# ME2N states no open quantity, status or order date, so its orders are not subtracted.
EXPECTED: dict[tuple[str, str], list[tuple[date, Decimal]]] = {
    ("100000201", "1000"): [(date(2026, 10, 30), Decimal(20))],
    ("100000201", "2000"): [(date(2026, 10, 16), Decimal(30)), (date(2026, 10, 30), Decimal(80))],
    ("100000201", "3000"): [(date(2026, 10, 16), Decimal(60)), (date(2026, 10, 30), Decimal(80))],
    ("100000202", "1000"): [(date(2026, 10, 20), Decimal(2)), (date(2026, 11, 5), Decimal(5))],
    ("100000202", "2000"): [(date(2026, 11, 5), Decimal(1))],
    ("100000202", "3000"): [(date(2026, 10, 20), Decimal(4)), (date(2026, 11, 5), Decimal(5))],
    ("100000203", "1000"): [],
    ("100000203", "2000"): [(date(2026, 10, 23), Decimal(30)), (date(2026, 11, 6), Decimal(20))],
    ("100000203", "3000"): [(date(2026, 10, 23), Decimal(15)), (date(2026, 11, 6), Decimal(20))],
    ("100000204", "1000"): [],
    ("100000204", "2000"): [],
    ("100000204", "3000"): [(date(2026, 10, 21), Decimal(10))],
}


def md04_rows(
    plan: dict[tuple[str, str], tuple[int, list[tuple[str, str, str, int]]]],
) -> list[list[object]]:
    """MD04 as exported: per material and plant a stock line (empty element data), then
    its dated elements, with the running available quantity."""
    rows: list[list[object]] = [list(MD04_HEADER)]
    for (material, plant), (stock, elements) in plan.items():
        name = NAMES[material]
        rows.append([material, name, plant, "2026-10-10", "Estoque", "", stock, stock, "UN"])
        balance = stock
        for day, element, data, quantity in elements:
            balance += quantity
            label = data or "Ordem planej."
            rows.append(
                [material, name, plant, f"2026-{day}", element, label, quantity, balance, "UN"]
            )
    return rows


def mb52_rows() -> list[list[object]]:
    rows: list[list[object]] = [
        ["Material", "Texto breve", "Centro", "Depósito", "Utilização livre", "UM"]
    ]
    for (material, plant), (stock, _) in PLAN.items():
        rows.append([material, NAMES[material], plant, "0001", stock, "UN"])
    return rows


def md04_section(
    last_row: int, *, reference: bool = True, value_maps: bool = True
) -> TableProposal:
    """The section a correct mapper proposes for MD04 (as the live mapper did)."""
    fields = [
        ("part_number", 1),
        ("description", 2),
        ("location", 3),
        ("need_by", 4),
        ("element_type", 5),
        *([("reference", 6)] if reference else []),
        ("quantity", 7),
        ("unit", 9),
    ]
    return TableProposal(
        kind="material_requirement",
        header_row=1,
        first_row=2,
        last_row=last_row,
        columns=[
            ColumnMapping(field=f, column=c, header_text=MD04_HEADER[c - 1]) for f, c in fields
        ],
        value_maps=[
            ValueMapping(field="element_type", source="Estoque", canonical="stock"),
            ValueMapping(field="element_type", source="NecDep", canonical="requirement"),
            ValueMapping(field="element_type", source="PedCompra", canonical="receipt"),
        ]
        if value_maps
        else [],
        required_fields=["part_number", "location", "element_type", "quantity"],
    )


def me2n_section(last_row: int) -> TableProposal:
    fields = [
        ("po_number", 1),
        ("po_line", 2),
        ("part_number", 3),
        ("location", 4),
        ("supplier_name", 5),
        ("quantity", 6),
        ("promised_at", 7),
        ("unit_price", 8),
    ]
    return TableProposal(
        kind="purchase",
        header_row=1,
        first_row=2,
        last_row=last_row,
        columns=[
            ColumnMapping(field=f, column=c, header_text=ME2N_HEADER[c - 1]) for f, c in fields
        ],
        required_fields=["po_number", "part_number", "quantity"],
    )


class SapMapper:
    """The source mapper's proposals per sheet; MB52 is read by the SAP pack."""

    def __init__(self, sections: dict[str, TableProposal]) -> None:
        self.sections = sections

    async def __call__(self, task: MappingTask, _: ToolContext) -> MappingProposal:
        section = self.sections[task.table]
        if section.last_row < task.first_row or section.first_row > task.last_row:
            return MappingProposal()
        return MappingProposal(
            sections=[
                section.model_copy(
                    update={
                        "first_row": max(section.first_row, task.first_row),
                        "last_row": min(section.last_row, task.last_row),
                    }
                )
            ]
        )


async def analyze(
    sheets: dict[str, Sequence[Sequence[object]]],
    sections: dict[str, TableProposal],
    *,
    element: str | None = "NecDep",
) -> SourceAssessment:
    return await analyze_documents(
        [SourceDocument("sap-export", FILENAME, XLSX, workbook(sheets))],
        "Quais materiais vao faltar nos tres centros nas proximas 4 semanas?",
        ToolContext(tenant_id=uuid4(), user_id=uuid4(), run_id=uuid4()),
        SapMapper(sections),
        scope=ScopeRequest(plants=3, horizon_days=28, mrp_element=element),
        today=date(2026, 10, 9),
    )


def export() -> tuple[dict[str, Sequence[Sequence[object]]], int, int]:
    md04 = md04_rows(PLAN)
    me2n: list[list[object]] = [list(ME2N_HEADER), *ORDERS]
    return {"MD04": md04, "MB52": mb52_rows(), "ME2N": me2n}, len(md04), len(me2n)


@pytest.mark.parametrize(
    ("reference", "value_maps", "element"),
    [
        # As the live mapper read the file: element data mapped as the reference.
        (True, True, "NecDep"),
        # The production failure: the element data column left unmapped.
        (False, True, "NecDep"),
        # No interpretation of the elements and every element asked for: the built-in
        # vocabulary keeps the stock line and the purchase orders out of the demand.
        (True, False, None),
    ],
)
async def test_an_md04_export_plans_every_material_at_every_plant(
    reference: bool, value_maps: bool, element: str | None
) -> None:
    sheets, md04, me2n = export()
    result = await analyze(
        sheets,
        {
            "MD04": md04_section(md04, reference=reference, value_maps=value_maps),
            "ME2N": me2n_section(me2n),
        },
        element=element,
    )
    found = {
        (line.part_number, line.location): [(n.need_by, n.quantity) for n in line.need_schedule]
        for line in result.lines
    }
    assert found == EXPECTED
    bearing = next(
        line for line in result.lines if (line.part_number, line.location) == ("100000201", "3000")
    )
    assert bearing.calculated_to_buy == 140 and bearing.available == 0
    assert bearing.need_schedule[0].need_by == date(2026, 10, 16)
    codes = {i.code for i in result.issues} | {i.code for line in result.lines for i in line.issues}
    assert not codes & {"missing_join_key", "conflicting_material_requirement"}
    assert "no_material_demand" not in codes


async def test_a_purchase_order_without_its_item_names_the_sheet_column_and_row() -> None:
    sheets, md04, me2n = export()
    orders = [list(row) for row in sheets["ME2N"]]
    orders[2][1] = None  # 4500019710 (100000202 at plant 1000) lost its item number.
    sheets["ME2N"] = orders
    result = await analyze(
        sheets, {"MD04": md04_section(md04), "ME2N": me2n_section(me2n)}, element=None
    )
    motor = next(
        line for line in result.lines if (line.part_number, line.location) == ("100000202", "1000")
    )
    [issue] = [i for i in motor.issues if i.code == "missing_join_key"]
    assert issue.severity == "blocker"
    assert "po_line" in issue.message
    [where] = issue.locations
    assert (where.filename, where.table, where.rows, where.rows_total) == (
        FILENAME,
        "ME2N",
        [3],
        1,
    )
    columns = {c.field: (c.letter, c.header, c.missing) for c in where.columns}
    assert columns == {
        "po_number": ("A", "Documento compras", False),
        "po_line": ("B", "Item", True),
        "location": ("D", "Centro", False),
    }
    # The reply writer reads the assessment's model view: the location survives it.
    view = assessment_view(result)
    assert isinstance(view, dict) and isinstance(view["lines"], list)
    [line] = [
        line
        for line in view["lines"]
        if isinstance(line, dict)
        and (line.get("part_number"), line.get("location")) == ("100000202", "1000")
    ]
    assert isinstance(line["issues"], list)
    [seen] = [i for i in line["issues"] if isinstance(i, dict) and i["code"] == "missing_join_key"]
    assert seen["locations"] == [
        {
            "source_id": "sap-export",
            "filename": FILENAME,
            "table": "ME2N",
            "columns": [
                {
                    "field": "po_number",
                    "column": 1,
                    "letter": "A",
                    "header": "Documento compras",
                    "missing": False,
                },
                {"field": "po_line", "column": 2, "letter": "B", "header": "Item", "missing": True},
                {
                    "field": "location",
                    "column": 4,
                    "letter": "D",
                    "header": "Centro",
                    "missing": False,
                },
            ],
            "rows": [3],
            "rows_total": 1,
        }
    ]


async def test_requirements_repeated_in_two_sheets_without_a_reference_name_both() -> None:
    """Across two lists of the same material and plant a requirement is matched by its
    reference: rows without one are blocked, and the issue says where they are."""
    sheets, md04, me2n = export()
    again = md04_rows({("100000201", "3000"): PLAN["100000201", "3000"]})
    again[2][5] = None  # The 2026-10-16 requirement has no element data in this copy.
    sheets["MD04 3000"] = again
    sections = {
        "MD04": md04_section(md04),
        "ME2N": me2n_section(me2n),
        "MD04 3000": md04_section(len(again)),
    }
    result = await analyze(sheets, sections)
    [issue] = [i for i in result.issues if i.code == "missing_join_key"]
    [where] = issue.locations
    assert (where.table, where.rows, where.rows_total, where.filename) == (
        "MD04 3000",
        [3],
        1,
        FILENAME,
    )
    reference = next(c for c in where.columns if c.field == "reference")
    assert (reference.letter, reference.header, reference.missing) == ("F", "Dados elem.MRP", True)
    # The other rows share the label "Ordem planej.": the conflict names its rows too.
    conflicts = [i for i in result.issues if i.code == "conflicting_material_requirement"]
    assert conflicts and all(i.locations and i.locations[0].rows for i in conflicts)
    # Materials and plants listed in one sheet only are planned as before.
    planned = {(line.part_number, line.location) for line in result.lines}
    assert ("100000202", "3000") in planned
