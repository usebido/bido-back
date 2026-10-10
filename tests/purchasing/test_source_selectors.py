"""The buyer's plant, MRP element and product selectors are checked against the sources in
code, before anything is planned: a value the data lacks names the valid ones."""

from collections.abc import Sequence
from datetime import date
from typing import Literal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from bido_back.agents.context import ToolContext
from bido_back.purchasing.sources.contracts import Record, ScopeRequest, SourceAssessment
from bido_back.purchasing.sources.readers import SourceDocument
from bido_back.purchasing.sources.selectors import (
    ScopeValidationError,
    apply_mrp_element,
    found_values,
    select_scope,
)
from bido_back.purchasing.sources.service import analyze_documents
from evals.source_fixtures import fact, motor_records
from tests.purchasing.motor_sources import motor_workbook
from tests.purchasing.test_workbook import XLSX

PLANTS = ("1000", "2000", "3000")


def mrp_records(plants: Sequence[str] = PLANTS) -> list[Record]:
    """An MRP list (SAP MD04-like) of one material at several plants: dependent and
    independent requirements, and a stock position per plant (plant 3000 holds enough)."""
    rows: list[Record] = []
    row = 2
    for plant in plants:
        rows.append(
            fact(
                "inventory",
                row,
                part_number="M-1",
                description="Rolamento 6205",
                location=plant,
                as_of="2026-10-08",
                available="100" if plant == "3000" else "10",
                unit="UN",
            )
        )
        row += 1
        for day, element in (
            ("2026-10-12", "NecDep"),
            ("2026-10-20", "NecDep"),
            ("2026-10-25", "NecInd"),
        ):
            rows.append(
                fact(
                    "material_requirement",
                    row,
                    part_number="M-1",
                    description="Rolamento 6205",
                    location=plant,
                    need_by=day,
                    quantity="20",
                    unit="UN",
                    element_type=element,
                    reference=f"R{row}",
                    demand_basis="gross",
                )
            )
            row += 1
    return rows


def test_found_values_list_what_the_sources_hold() -> None:
    found = found_values(mrp_records())
    assert found.plants == ["1000", "2000", "3000"]
    assert found.mrp_elements == ["NecDep", "NecInd"]
    assert found.products == ["M-1"]
    assert (found.first_need_date, found.last_need_date) == (
        date(2026, 10, 12),
        date(2026, 10, 25),
    )
    motors = found_values(motor_records())
    assert motors.products == ["MTR-100"]
    assert motors.plants == ["Plant A", "Plant B"]


@pytest.mark.parametrize("selector", [3, "all", ["1000", "2000", "3000"], ["2000", "1000", "3000"]])
def test_three_plants_or_all_plants_select_the_three_plants_found(
    selector: int | Literal["all"] | list[str],
) -> None:
    selection = select_scope(mrp_records(), ScopeRequest(plants=selector))
    assert selection.question is None
    assert sorted(selection.plants) == ["1000", "2000", "3000"]
    assert selection.request.location is None


def test_a_count_that_is_not_the_number_found_asks_which_plants() -> None:
    selection = select_scope(mrp_records(PLANTS[:2]), ScopeRequest(plants=3))
    assert selection.plants == []
    assert selection.question is not None
    assert selection.question.code == "plant_count_mismatch"
    assert selection.question.field == "plants"
    assert [o.value for o in selection.question.options] == ["1000", "2000"]
    assert "3 plant(s)" in selection.question.message
    assert "1000, 2000" in selection.question.message


def test_one_selected_plant_becomes_the_location_in_the_sources_spelling() -> None:
    selection = select_scope(mrp_records(), ScopeRequest(plants=["2000"]))
    assert selection.plants == ["2000"]
    assert selection.request.location == "2000"
    single = select_scope(mrp_records(("1000",)), ScopeRequest(plants=1))
    assert single.request.location == "1000"


@pytest.mark.parametrize("request_", [ScopeRequest(plants=["4000"]), ScopeRequest(location="4000")])
def test_an_unknown_plant_is_a_validation_error_naming_the_plants_found(
    request_: ScopeRequest,
) -> None:
    with pytest.raises(ScopeValidationError) as caught:
        select_scope(mrp_records(), request_)
    error = caught.value
    assert error.code == "unknown_plant"
    assert error.value == "4000"
    assert error.valid_values == ["1000", "2000", "3000"]
    assert error.field == ("plants" if request_.plants else "location")


def test_plants_match_without_case_accents_or_spacing() -> None:
    selection = select_scope(motor_records(), ScopeRequest(plants=["PLANT  A", "plant b"]))
    assert selection.plants == ["Plant A", "Plant B"]
    # "planta 1000" names plant 1000, as the planner already reads it.
    worded = select_scope(mrp_records(), ScopeRequest(plants=["planta 1000", "centro 2000"]))
    assert worded.plants == ["1000", "2000"]
    assert select_scope(mrp_records(), ScopeRequest(location="planta 1000")).request.location == (
        "planta 1000"
    )


def test_a_named_mrp_element_is_a_filter_on_that_column_never_a_field() -> None:
    records = mrp_records()
    selection = select_scope(records, ScopeRequest(mrp_element="necdep"))
    assert selection.request.mrp_element == "necdep"
    kept, note = apply_mrp_element(records, "necdep")
    elements = {r.values["element_type"] for r in kept if r.kind == "material_requirement"}
    assert elements == {"NecDep"}
    assert len(kept) == len(records) - 3
    assert note is not None and note.code == "mrp_element_filter"
    assert "3 requirement row(s)" in note.message and "NecInd (3)" in note.message


def test_an_unknown_mrp_element_names_the_elements_found() -> None:
    with pytest.raises(ScopeValidationError) as caught:
        select_scope(mrp_records(), ScopeRequest(mrp_element="PedCpa"))
    assert caught.value.code == "unknown_mrp_element"
    assert caught.value.field == "mrp_element"
    assert caught.value.valid_values == ["NecDep", "NecInd"]
    with pytest.raises(ScopeValidationError) as none:
        select_scope(motor_records(), ScopeRequest(mrp_element="NecDep"))
    assert none.value.valid_values == []
    assert "no MRP element column" in str(none.value)


def test_an_unknown_product_names_the_products_found() -> None:
    with pytest.raises(ScopeValidationError) as caught:
        select_scope(motor_records(), ScopeRequest(product_code="BP-999"))
    assert caught.value.code == "unknown_product"
    assert caught.value.field == "product_code"
    assert caught.value.valid_values == ["MTR-100"]
    assert "MTR-100 (Motor)" in str(caught.value)
    with pytest.raises(ScopeValidationError) as words:
        select_scope(motor_records(), ScopeRequest(product="a bomba centrífuga"))
    assert words.value.field == "product"
    # Without production orders the product is not checked (scope_not_applied says so).
    select_scope(mrp_records(), ScopeRequest(product="Motor MTR-100", product_code="BP-999"))
    # Words naming the product's code or its description pass; the resolver settles them.
    for product in ("Motor MTR-100", "o motor", "mtr100", "os motores"):
        select_scope(motor_records(), ScopeRequest(product=product))
    select_scope(motor_records(), ScopeRequest(product_code="MTR 100"))


def test_the_request_reads_plant_selectors_as_the_buyer_says_them() -> None:
    assert ScopeRequest.model_validate({"plants": "3"}).plants == 3
    assert ScopeRequest.model_validate({"plants": "todos"}).plants == "all"
    assert ScopeRequest.model_validate({"plants": "Planta A"}).plants == ["Planta A"]
    assert ScopeRequest.model_validate({"plants": ["1000", " ", "2000"]}).plants == ["1000", "2000"]
    assert ScopeRequest.model_validate({"plants": "", "mrp_element": " "}).plants is None
    with pytest.raises(ValidationError):
        ScopeRequest(plants=0)


def _context() -> ToolContext:
    return ToolContext(tenant_id=uuid4(), user_id=uuid4(), run_id=uuid4())


async def _analyze(scope: ScopeRequest | None, records: list[Record]) -> SourceAssessment:
    data, mapper = motor_workbook(records)
    return await analyze_documents(
        [SourceDocument("synthetic-erp", "md04.xlsx", XLSX, data)],
        "O que preciso comprar nos três centros?",
        _context(),
        mapper,
        scope=scope,
        today=date(2026, 10, 9),
    )


async def test_three_plants_found_plan_all_three_and_say_what_was_found() -> None:
    result = await _analyze(ScopeRequest(plants=3), mrp_records())
    assert {line.location for line in result.lines} == {"1000", "2000", "3000"}
    assert result.found is not None
    assert result.found.plants == ["1000", "2000", "3000"]
    assert result.found.mrp_elements == ["NecDep", "NecInd"]
    assert result.scope_resolution is not None
    assert result.scope_resolution.questions == []
    assert ("plants", "buyer") in {(p.field, p.source) for p in result.scope_resolution.provenance}


async def test_two_plants_found_for_three_asked_is_a_question_and_no_plan() -> None:
    result = await _analyze(ScopeRequest(plants=3), mrp_records(PLANTS[:2]))
    assert result.lines == []
    assert result.scope_resolution is not None
    [question] = result.scope_resolution.questions
    assert question.code == "plant_count_mismatch"
    assert [o.value for o in question.options] == ["1000", "2000"]
    assert [i.code for i in result.issues if i.severity == "blocker"] == ["plant_selection_open"]
    assert result.found is not None and result.found.plants == ["1000", "2000"]


async def test_an_unknown_plant_raises_before_anything_is_planned() -> None:
    with pytest.raises(ScopeValidationError) as caught:
        await _analyze(ScopeRequest(plants=["4000"]), mrp_records())
    assert caught.value.valid_values == ["1000", "2000", "3000"]


async def test_the_mrp_element_filters_the_demand_and_is_disclosed() -> None:
    everything = await _analyze(ScopeRequest(plants="all"), mrp_records())
    dependent = await _analyze(ScopeRequest(plants="all", mrp_element="NecDep"), mrp_records())
    for plant in PLANTS:
        full = next(line for line in everything.lines if line.location == plant)
        only = next(line for line in dependent.lines if line.location == plant)
        assert full.demand_schedule and len(full.demand_schedule) == 3
        assert [d.need_by for d in only.demand_schedule] == [date(2026, 10, 12), date(2026, 10, 20)]
    assert "mrp_element_filter" in {a.code for a in dependent.assumptions}
    assert dependent.scope_resolution is not None
    assert ("mrp_element", "buyer") in {
        (p.field, p.source) for p in dependent.scope_resolution.provenance
    }


async def test_every_evaluated_line_has_a_reason_for_being_or_not_being_an_alert() -> None:
    result = await _analyze(ScopeRequest(plants="all"), mrp_records())
    assert len(result.lines) == 3
    # Plants 1000 and 2000 run short (10 in stock against 20 due on 2026-10-12); no
    # supplier has a lead time, so the arrival cannot be checked: alerts, each with why.
    assert [a.outcome for a in result.timing_alerts] == ["unknown", "unknown"]
    for alert in result.timing_alerts:
        assert alert.reason is not None
        assert "falls below the safety stock on 2026-10-12" in alert.reason
    # Plant 3000 holds 100: not an alert, and it says why instead of vanishing.
    [excluded] = result.alerts_excluded
    assert excluded.part_number == "M-1"
    assert (
        excluded.reason
        == "stock 100 UN covers the demand 60 UN through 2026-10-25: nothing to buy."
    )
    assert len(result.timing_alerts) + len(result.alerts_excluded) == len(result.lines)
    assert result.alerts_excluded_left_out == 0
