"""Known ERP reports are read by code, with their conventions disclosed, never by a model."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from bido_back.agents.context import ToolContext
from bido_back.purchasing.sources.contracts import (
    AssessmentLine,
    EvidenceTable,
    MappingProposal,
    MappingTask,
    Record,
    SourceAssessment,
    SourceRef,
)
from bido_back.purchasing.sources.evidence import source_number
from bido_back.purchasing.sources.handoff import wanted_suppliers
from bido_back.purchasing.sources.packs import read_known
from bido_back.purchasing.sources.packs.base import PackContext
from bido_back.purchasing.sources.readers import SourceDocument
from bido_back.purchasing.sources.service import analyze_documents
from bido_back.purchasing.sources.units import convert_units
from bido_back.rfqs.assumptions import line_assumptions
from evals.sap_reports import golden, reports

TODAY = date(2026, 10, 9)


async def _no_model(task: MappingTask, context: ToolContext) -> MappingProposal:
    raise AssertionError(f"the mapper was called for {task.table}")


async def _assess() -> SourceAssessment:
    documents = [
        SourceDocument(name, name, "application/vnd.ms-excel", data)
        for name, data in reports().items()
    ]
    return await analyze_documents(
        documents,
        "O que comprar, quanto e de quem",
        ToolContext(tenant_id=uuid4(), user_id=uuid4(), run_id=uuid4()),
        _no_model,
        today=TODAY,
    )


def _line(result: SourceAssessment, part: str) -> AssessmentLine:
    return next(line for line in result.lines if line.part_number == part)


def _assumed(line: AssessmentLine) -> set[str]:
    return {a.code for a in line.assumptions}


async def test_the_eight_sap_reports_are_read_by_code_into_the_golden_plan() -> None:
    result = await _assess()
    assert sorted(r.report for r in result.reports) == [
        "BP", "MB52", "MD04", "ME1M", "ME2N", "ME6H", "ME80FN", "MM60",
    ]  # fmt: skip
    assert all(row.inspected for row in result.coverage)
    assert {line.part_number: line.calculated_to_buy for line in result.lines} == golden()
    assert result.status == "assessed"
    assert result.scope is not None and result.scope.all_materials
    assert result.scope.as_of == date(2026, 10, 8)
    assert not [i for i in result.issues if i.severity == "blocker"]


async def test_report_conventions_become_disclosed_assumptions() -> None:
    result = await _assess()
    assert "stock_dated_by_mrp_list" in {a.code for a in result.assumptions}
    # An overdue open order counts, as MRP does, with what changes if it never comes.
    rotor = _line(result, "200000110")
    overdue = next(a for a in rotor.assumptions if a.code == "overdue_order_counted")
    assert overdue.alternative is not None and "50" in overdue.alternative
    assert rotor.confidence == "low"
    # The material master's minimum lot and rounding raise the net need.
    screws = _line(result, "200000160")
    assert (screws.net_requirement, screws.calculated_to_buy) == (Decimal(500), Decimal(5000))
    assert "lot_size_applied" in _assumed(screws)
    # Varnish is bought in 18 L buckets: history in BD is read per litre.
    varnish = _line(result, "200000180")
    assert {"unit_converted", "lot_size_applied"} <= _assumed(varnish)
    assert varnish.unit == "L" and varnish.calculated_to_buy == Decimal(72)
    assert varnish.price is not None and varnish.price.last_price == Decimal("30.0000")
    assert varnish.confidence == "medium"


async def test_prices_are_per_one_unit_and_blocked_suppliers_are_never_invited() -> None:
    result = await _assess()
    screws = _line(result, "200000160")
    assert screws.suppliers[0].last_price == Decimal("0.21")  # 210 per 1000
    oring = _line(result, "200000190")
    assert oring.suppliers[0].purchase_blocked and not oring.suppliers[0].suggested
    wanted = wanted_suppliers([oring], 3)
    assert [w.disqualified for w in wanted] == [True]
    bearing = _line(result, "200000130")
    assert bearing.suppliers[0].score_overall == Decimal(85)
    assert bearing.suppliers[0].contract_price == Decimal("9.8")


def _table(rows: list[list[str | None]]) -> EvidenceTable:
    return EvidenceTable(source_id="s", name="Sheet1", rows=rows)


def test_a_table_no_pack_knows_goes_to_the_mapper() -> None:
    known = read_known(
        [_table([["Item", "Qty", "Supplier"], ["A-1", "4", "ACME"]])],
        PackContext(filenames={}, today=TODAY),
    )
    assert known.records == [] and len(known.remaining) == 1


def test_mb52_text_numbers_use_the_separator_the_table_proves() -> None:
    known = read_known(
        [
            _table(
                [
                    ["Material", "Centro", "Depósito", "UM básica", "Utilização livre"],
                    ["100", "1000", "0001", "KG", "1.200,500"],
                    ["101", "1000", "0001", "KG", "1.250"],
                ]
            )
        ],
        PackContext(filenames={"s": "MB52_2026-10-07.xlsx"}, today=TODAY),
    )
    values = {r.values["part_number"]: source_number(r, "available", []) for r in known.records}
    assert values == {"100": Decimal("1200.5"), "101": Decimal(1250)}
    assert {r.values["as_of"] for r in known.records} == {"2026-10-07"}
    assert "stock_dated_by_file_name" in {a.code for a in known.assumptions}


def _ref(row: int) -> SourceRef:
    return SourceRef(source_id="s", table="t", row=row, column=1)


def test_disagreeing_unit_conversions_convert_nothing() -> None:
    def item(row: int, factor: str) -> Record:
        values = {"part_number": "P", "unit": "L", "alt_unit": "BD", "alt_unit_factor": factor}
        return Record(kind="item", values=values, evidence={k: _ref(row) for k in values})

    order = Record(
        kind="purchase",
        values={"part_number": "P", "unit": "BD", "quantity": "2"},
        evidence={"quantity": _ref(5)},
    )
    issues, assumptions = convert_units([item(1, "18"), item(2, "20"), order])
    assert [i.code for i in issues] == ["conflicting_unit_conversion"]
    assert assumptions == [] and order.canonical == {}
    issues, assumptions = convert_units([item(1, "18"), order])
    assert order.canonical["quantity"].value == "36" and order.canonical["unit"].value == "l"
    assert [a.code for a in assumptions] == ["unit_converted"]


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        ({}, (None, 0)),
        (
            {"line": {"confidence": "low", "assumptions": [{"code": "x", "message": "m"}]}},
            ("low", 1),
        ),
        ({"line": {"confidence": "bogus"}}, (None, 0)),
    ],
)
def test_rfq_lines_read_their_assessment_assumptions(
    evidence: dict[str, object], expected: tuple[str | None, int]
) -> None:
    confidence, assumptions = line_assumptions(evidence)  # type: ignore[arg-type]
    assert (confidence, len(assumptions)) == expected
