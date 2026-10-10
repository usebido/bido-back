"""What the copilot's model reads of tool outputs: no cell references, bounded assessments."""

import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, cast

from pydantic import BaseModel, JsonValue

from bido_back.copilot.model_views import (
    FULL_LINES_CHARS,
    SUMMARY_FIELDS,
    SUMMARY_LINES_CHARS,
    assessment_view,
    cell_free_view,
    without_cell_refs,
)
from bido_back.purchasing.sources.contracts import (
    AssessmentLine,
    Assumption,
    DatedQuantity,
    Issue,
    PlanningCandidate,
    ProjectedBalance,
    SourceAssessment,
    SourceRef,
    SupplierEvidence,
    TimingAlert,
    TimingCheck,
)

STOCK_DATE = Assumption(
    code="as_of_from_stock", message="The latest stock snapshot of Planta A, 08/10/2026."
)


def refs(count: int) -> list[SourceRef]:
    return [SourceRef(source_id="f1", table="Estoque", row=2 + i, column=3) for i in range(count)]


def line(part: str, to_buy: str | None, *, suppliers: int = 2) -> AssessmentLine:
    return AssessmentLine(
        part_number=part,
        description=f"Peça {part}",
        unit="pcs",
        need_by=date(2026, 10, 21),
        demand=Decimal(20),
        calculated_to_buy=None if to_buy is None else Decimal(to_buy),
        suppliers=[
            SupplierEvidence(
                name=f"Fornecedor {i}",
                last_price=Decimal("1.50"),
                currency="BRL",
                # The last one: a cut list must still keep it.
                suggested=i == suppliers - 1,
                evidence=refs(3),
            )
            for i in range(suppliers)
        ],
        evidence=refs(4),
        # Each line also lists the assessment-wide assumptions (sources.service._disclose).
        assumptions=[
            Assumption(code="unit_kept", message=f"{part}: unit kept", part_numbers=[part]),
            STOCK_DATE,
        ],
    )


def size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def test_cell_references_are_counted_never_listed() -> None:
    ref: JsonValue = {"source_id": "f1", "table": "T", "row": 3, "column": 2, "start": None}
    output: JsonValue = {
        "evidence": [ref, ref],
        "source_evidence": {
            "line": {
                "part_number": "P1",
                "evidence": [ref],
                "issues": [{"code": "x", "evidence": []}],
            }
        },
        "record": {"evidence": {"quantity": ref, "part_number": ref}},
        "quotes": ["exact words", "more words"],
        "notes": [],
    }

    assert without_cell_refs(output) == {
        "evidence_cells": 2,
        "source_evidence": {
            "line": {"part_number": "P1", "evidence_cells": 1, "issues": [{"code": "x"}]}
        },
        "record": {"evidence_cells": 2},
        "quotes": ["exact words", "more words"],
        "notes": [],
    }


def test_the_default_view_only_drops_cell_references() -> None:
    class Out(BaseModel):
        currency: str | None
        evidence: list[SourceRef]

    # A null can mean something to the model ("currency is empty": mixed currencies).
    assert cell_free_view(Out(currency=None, evidence=refs(2))) == {
        "currency": None,
        "evidence_cells": 2,
    }


def test_a_small_assessment_is_read_whole_in_its_own_order() -> None:
    assessment = SourceAssessment(
        lines=[line("P1", "0"), line("P2", "40"), line("P3", None)],
        assumptions=[
            STOCK_DATE,
            Assumption(code="unit_kept", message="P2: unit kept", part_numbers=["P2"]),
        ],
        issues=[
            Issue(
                code="missing_currency",
                severity="warning",
                message="No currency.",
                evidence=refs(2),
            )
        ],
    )

    view = cast(dict[str, Any], assessment_view(assessment))

    text = json.dumps(view)
    assert "source_id" not in text
    assert "null" not in text
    assert [item["part_number"] for item in view["lines"]] == ["P1", "P2", "P3"]
    assert "more_lines" not in view
    assert "lines_left_out" not in view
    first, second, third = view["lines"]
    assert first["evidence_cells"] == 4
    assert first["suppliers"][0]["evidence_cells"] == 3
    assert first["ready"] is False
    # No best estimate: the quantity is left out, never shown as empty or zero.
    assert "calculated_to_buy" not in third
    assert view["issues"] == [
        {
            "code": "missing_currency",
            "severity": "warning",
            "message": "No currency.",
            "evidence_cells": 2,
        }
    ]
    # An assumption about every line is read once, at the top; a line's own stays on it.
    assert view["assumptions"] == [
        {"code": "as_of_from_stock", "message": STOCK_DATE.message, "confidence": "medium"}
    ]
    assert second["assumptions"] == [
        {
            "code": "unit_kept",
            "message": "P2: unit kept",
            "confidence": "medium",
            "part_numbers": ["P2"],
        }
    ]


def test_a_large_assessment_is_bounded_and_counts_what_it_left_out() -> None:
    lines = [line(f"P{i:04d}", "0" if i % 3 == 0 else str(i), suppliers=9) for i in range(1200)]
    for each in lines:
        each.issues = [
            Issue(code=f"issue_{k}", severity="warning", message="x" * 40, evidence=refs(3))
            for k in range(15)
        ]
    assessment = SourceAssessment(
        lines=lines,
        issues=[Issue(code=f"general_{k}", severity="warning", message="m") for k in range(100)],
        planning_candidates=[
            PlanningCandidate(
                product_code=f"MTR-{k}",
                need_dates=[date(2026, 10, 1 + d) for d in range(10)],
            )
            for k in range(30)
        ],
    )

    view = cast(dict[str, Any], assessment_view(assessment))

    assert size(view) < FULL_LINES_CHARS + SUMMARY_LINES_CHARS + 10_000
    full, more, left_out = view["lines"], view["more_lines"], view["lines_left_out"]
    assert len(full) + len(more) + left_out == 1200
    # Lines with something to buy are read in full first, in their own order.
    assert all(Decimal(item["calculated_to_buy"]) > 0 for item in full)
    numbers = [int(item["part_number"][1:]) for item in full]
    assert numbers == sorted(numbers)
    assert all(set(summary) <= set(SUMMARY_FIELDS) for summary in more)
    # A cut supplier list keeps the suggested supplier and its order.
    suppliers = full[0]["suppliers"]
    assert len(suppliers) == 6
    assert full[0]["suppliers_left_out"] == 3
    assert suppliers[-1]["suggested"] is True
    assert (len(full[0]["issues"]), full[0]["issues_left_out"]) == (10, 5)
    assert (len(view["issues"]), view["issues_left_out"]) == (40, 60)
    assert (len(view["planning_candidates"]), view["planning_candidates_left_out"]) == (20, 10)
    candidate = view["planning_candidates"][0]
    assert (len(candidate["need_dates"]), candidate["need_dates_left_out"]) == (6, 4)


def test_assessment_wide_assumptions_survive_many_line_assumptions() -> None:
    parts = [f"P{i}" for i in range(45)]
    assessment = SourceAssessment(
        lines=[line(part, "10") for part in parts],
        assumptions=[
            *(
                Assumption(code="unit_kept", message=f"{p}: unit kept", part_numbers=[p])
                for p in parts
            ),
            STOCK_DATE,
        ],
    )

    view = cast(dict[str, Any], assessment_view(assessment))

    assert [a["code"] for a in view["assumptions"]] == ["as_of_from_stock"]
    assert "assumptions_left_out" not in view


def test_a_lines_timing_is_read_with_its_lists_bounded() -> None:
    long = line("P1", "40", suppliers=8)
    # The recommended supplier is first and not suggested: a cut list still keeps it.
    long.suppliers[0].recommended = True
    long.timing = TimingCheck(
        order_date=date(2026, 10, 8),
        outcome="unavoidable_stockout",
        projected=[
            ProjectedBalance(on=date(2026, 10, 9) + timedelta(days=i), balance=Decimal(-i))
            for i in range(40)
        ],
    )
    long.need_schedule = [
        DatedQuantity(need_by=date(2026, 10, 9) + timedelta(days=i), quantity=Decimal(1))
        for i in range(30)
    ]

    view = cast(dict[str, Any], assessment_view(SourceAssessment(lines=[long])))

    [read] = view["lines"]
    assert read["timing"]["outcome"] == "unavoidable_stockout"
    assert len(read["timing"]["projected"]) == 16
    assert read["timing"]["projected_left_out"] == 24
    assert (len(read["need_schedule"]), read["need_schedule_left_out"]) == (16, 14)
    names = [s["name"] for s in read["suppliers"]]
    assert "Fornecedor 0" in names and "Fornecedor 7" in names


def test_timing_alerts_are_read_before_the_lines() -> None:
    assessment = SourceAssessment(
        lines=[line("P1", "40")],
        timing_alerts=[
            TimingAlert(
                part_number="P1",
                outcome="unavoidable_stockout",
                summary="Unavoidable stockout from 2026-10-12.",
                actions=["Ask S to move PO 1/10 from 2026-12-11 to 2026-10-12."],
            )
        ],
    )

    view = cast(dict[str, Any], assessment_view(assessment))

    assert list(view)[:2] == ["timing_alerts", "lines"]
    assert view["timing_alerts"][0]["summary"] == "Unavoidable stockout from 2026-10-12."


def test_issue_codes_reach_the_model_as_plain_words() -> None:
    # 2026-10-10: the copilot showed the buyer `lead_time_days:wrong_locator` and
    # `above_buyer_lead_time_limit`. The model reads words; the output keeps the codes.
    class Out(BaseModel):
        issues: list[str]
        excluded: list[dict[str, object]]
        status: str

    out = Out(
        issues=["line 3: lead_time_days:wrong_locator", "missing:currency", "Free text."],
        excluded=[
            {"supplier_name": "Roltec", "reasons": ["needs_review", "above_buyer_lead_time_limit"]}
        ],
        status="needs_review",
    )

    view = cell_free_view(out)

    assert view == {
        "issues": [
            "line 3: the lead time is not where the file points, so it is not confirmed",
            "the file does not give the currency",
            "Free text.",
        ],
        "excluded": [
            {
                "supplier_name": "Roltec",
                "reasons": [
                    "needs a person to check it",
                    "delivers later than the lead time you set",
                ],
            }
        ],
        "status": "needs_review",
    }
    assert out.issues[0] == "line 3: lead_time_days:wrong_locator"


def test_the_plan_is_counted_whole_even_when_its_lines_are_cut() -> None:
    lines = [line(f"P{i:03}", "40", suppliers=6) for i in range(300)]
    lines += [line(f"C{i:03}", "0") for i in range(50)]
    for i, item in enumerate(lines):
        item.location = "1000" if i % 2 else "2000"
    view = cast(dict[str, Any], assessment_view(SourceAssessment(lines=lines)))

    assert view["lines_left_out"] > 0
    plan = view["plan"]
    assert plan["rows"] == 350
    assert plan["by_status"] == {"Buy": 300, "Covered, nothing to buy": 50}
    assert plan["by_plant"] == {
        "1000": {"rows": 175, "to_buy": 150},
        "2000": {"rows": 175, "to_buy": 150},
    }
    assert "export_procurement_plan" in plan["full_plan"]
