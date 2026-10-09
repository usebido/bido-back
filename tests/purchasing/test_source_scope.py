"""The buyer's planning words become a planning scope in code, or a precise question."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.context import ToolContext
from bido_back.api.deps import get_db, get_principal, get_storage
from bido_back.api.procurement_assessments import get_source_mapper, procurement_assessments_router
from bido_back.auth import Principal
from bido_back.db import create_sessionmaker
from bido_back.onboarding import service as onboarding
from bido_back.purchasing.sources.contracts import (
    PlanningScope,
    Record,
    ScopeRequest,
    SourceAssessment,
)
from bido_back.purchasing.sources.mapping import Mapper
from bido_back.purchasing.sources.planning import assess_plan
from bido_back.purchasing.sources.readers import SourceDocument
from bido_back.purchasing.sources.scope import (
    ResolvedScope,
    as_request,
    direct_scope,
    has_production_plans,
    resolve_scope,
    scope_issues,
)
from bido_back.purchasing.sources.service import analyze_documents
from bido_back.storage import BlobStorage
from evals.source_fixtures import EXPECTED, SCOPE, fact, motor_records
from tests.approvals.conftest import People
from tests.copilot.test_source_tools import SourceMapper, TrackingStorage
from tests.purchasing.motor_sources import motor_workbook
from tests.purchasing.test_api import build_app
from tests.purchasing.test_workbook import XLSX, workbook
from tests.rfqs.test_service import buyer

BUYER = ScopeRequest(
    product="Motor MTR-100", location="plant a", horizon_days=14, safety_stock=Decimal(0)
)


def resolve(request: ScopeRequest, records: list[Record] | None = None) -> ResolvedScope:
    return resolve_scope(motor_records() if records is None else records, request)


def assumed(resolved: ResolvedScope) -> list[str]:
    return [a.code for a in resolved.resolution.assumptions]


def asked(resolved: ResolvedScope) -> dict[str, list[str]]:
    return {q.field: [o.value for o in q.options] for q in resolved.resolution.questions}


def product_records(code: str, description: str, location: str, need_by: str) -> list[Record]:
    return [
        fact("item", 60, part_number=code, description=description, unit="pcs", area="Motors"),
        fact(
            "production_plan",
            60,
            reference=f"BUILD-{code}",
            product_code=code,
            quantity="5",
            unit="pcs",
            location=location,
            need_by=need_by,
            status="Planned",
        ),
    ]


def test_buyer_words_resolve_to_the_explicit_scope_and_its_quantities() -> None:
    resolved = resolve(BUYER)
    assert resolved.scope == SCOPE
    assert assumed(resolved) == ["as_of_latest_snapshot", "horizon_from_days"]
    assert resolved.resolution.questions == []
    assert resolved.resolution.request == BUYER
    assert resolved.scope is not None
    plan = assess_plan(motor_records(), resolved.scope)
    assert {line.part_number: line.calculated_to_buy for line in plan.lines} == EXPECTED


@pytest.mark.parametrize("code", ["MTR-100", "mtr100", " Mtr 100 ", "MTR_100"])
def test_product_code_matches_without_case_spacing_or_punctuation(code: str) -> None:
    resolved = resolve(BUYER.model_copy(update={"product": None, "product_code": code}))
    assert resolved.scope is not None and resolved.scope.product_code == "MTR-100"


@pytest.mark.parametrize("words", ["Motor MTR-100", "motor mtr 100", "o MTR100 da planta"])
def test_a_code_written_in_the_buyers_words_identifies_the_product(words: str) -> None:
    resolved = resolve(BUYER.model_copy(update={"product": words}))
    assert resolved.scope is not None and resolved.scope.product_code == "MTR-100"
    assert "product_from_description" not in assumed(resolved)


def test_a_code_inside_a_longer_code_loses() -> None:
    records = [*motor_records(), *product_records("MTR-100-X", "Motor X", "Plant A", "2026-10-20")]
    longer = resolve(BUYER.model_copy(update={"product": "Motor MTR-100-X"}), records)
    shorter = resolve(BUYER, records)
    assert longer.scope is not None and longer.scope.product_code == "MTR-100-X"
    assert shorter.scope is not None and shorter.scope.product_code == "MTR-100"


def test_the_only_matching_master_description_identifies_the_product() -> None:
    records = [*motor_records(), *product_records("PMP-200", "Pump", "Plant A", "2026-10-20")]
    resolved = resolve(BUYER.model_copy(update={"product": "motores"}), records)
    assert resolved.scope is not None and resolved.scope.product_code == "MTR-100"
    assert assumed(resolved)[0] == "product_from_description"
    assert resolved.resolution.assumptions[0].value == "MTR-100"


def test_an_ambiguous_product_is_asked_with_options_and_never_picked() -> None:
    records = [
        *motor_records(),
        *product_records("MTR-200", "Motor large", "Plant A", "2026-10-20"),
    ]
    resolved = resolve(BUYER.model_copy(update={"product": "motor"}), records)
    assert resolved.scope is None
    assert asked(resolved) == {"product_code": ["MTR-100", "MTR-200"]}
    assert resolved.resolution.questions[0].code == "ambiguous_product"


@pytest.mark.parametrize("update", [{"product": "Bomba BH-40"}, {"product_code": "BH-40"}])
def test_an_unknown_product_lists_the_planned_products(update: dict[str, str]) -> None:
    resolved = resolve(BUYER.model_copy(update=update))
    assert resolved.scope is None
    assert [q.code for q in resolved.resolution.questions] == ["unknown_product"]
    assert asked(resolved)["product_code"] == ["MTR-100"]


PUMP = "BP-200 Bomba centrifuga"
PUMP_SCOPE = ScopeRequest(as_of=date(2026, 10, 8), horizon_end=date(2026, 11, 24))


def named_order(name: str, reference: str, row: int) -> Record:
    """A production order of an ERP that identifies products by their name, at no plant."""
    return fact(
        "production_plan",
        row,
        reference=reference,
        product_code=name,
        quantity="80",
        unit="pcs",
        need_by="2026-11-24",
        status="Planned",
    )


@pytest.mark.parametrize(
    "update",
    [
        {"product_code": "BP-200"},
        {"product_code": "bp 200"},
        {"product": "BP-200"},
        {"product": "a bomba BP-200"},
        {"product": "bomba"},
    ],
)
def test_the_part_of_a_product_name_the_buyer_wrote_identifies_the_only_product(
    update: dict[str, str],
) -> None:
    records = [named_order(PUMP, "OP-2026-118", 2), named_order(PUMP, "OP-2026-119", 3)]
    resolved = resolve(PUMP_SCOPE.model_copy(update=update), records)
    assert resolved.resolution.questions == []
    assert resolved.scope is not None and resolved.scope.product_code == PUMP
    assert assumed(resolved)[0] == "product_from_partial_code"
    assert resolved.resolution.assumptions[0].value == PUMP
    assert {p.field: p.source for p in resolved.resolution.provenance}["product_code"] == "evidence"


@pytest.mark.parametrize("update", [{"product_code": "BP-2"}, {"product": "BP-2"}])
def test_part_of_a_word_in_a_product_name_is_not_the_product(update: dict[str, str]) -> None:
    records = [named_order(PUMP, "OP-2026-118", 2)]
    resolved = resolve(PUMP_SCOPE.model_copy(update=update), records)
    assert resolved.scope is None
    assert [q.code for q in resolved.resolution.questions] == ["unknown_product"]
    assert asked(resolved)["product_code"] == [PUMP]


@pytest.mark.parametrize("update", [{"product_code": "BP-200"}, {"product": "bomba"}])
def test_a_name_part_shared_by_several_products_is_asked(update: dict[str, str]) -> None:
    records = [named_order(PUMP, "OP-2026-118", 2), named_order("BP-200 Bomba axial", "OP-2", 3)]
    resolved = resolve(PUMP_SCOPE.model_copy(update=update), records)
    assert resolved.scope is None
    assert [q.code for q in resolved.resolution.questions] == ["ambiguous_product"]
    assert asked(resolved)["product_code"] == ["BP-200 Bomba axial", PUMP]


def test_an_empty_request_asks_for_product_plant_and_horizon() -> None:
    resolved = resolve(ScopeRequest())
    assert resolved.scope is None
    assert asked(resolved) == {
        "product_code": ["MTR-100"],
        "location": ["Plant A", "Plant B"],
        "horizon_end": ["2026-10-18", "2026-11-01"],
    }
    product = resolved.resolution.questions[0].options[0]
    assert {ref.table for ref in product.evidence} == {"production_plan"}


def test_a_product_planned_at_one_plant_uses_it_and_discloses_it() -> None:
    records = [r for r in motor_records() if r.values.get("location") != "Plant B"]
    resolved = resolve(BUYER.model_copy(update={"location": None}), records)
    assert resolved.scope == SCOPE
    assert assumed(resolved)[0] == "location_single_plant"
    assert resolved.resolution.assumptions[0].value == "Plant A"


def test_several_plants_are_asked_and_cancelled_orders_are_not_options() -> None:
    cancelled = fact(
        "production_plan",
        9,
        reference="BUILD-C",
        product_code="MTR-100",
        quantity="5",
        unit="pcs",
        location="Plant C",
        need_by="2026-10-19",
        status="Cancelled",
    )
    resolved = resolve(BUYER.model_copy(update={"location": None}), [*motor_records(), cancelled])
    assert resolved.scope is None
    assert asked(resolved) == {"location": ["Plant A", "Plant B"]}
    # The reference date waits for the plant: no question and no guess about it.
    assert "as_of_latest_snapshot" not in assumed(resolved)


def test_an_unknown_plant_lists_the_products_plants() -> None:
    resolved = resolve(BUYER.model_copy(update={"location": "Plant C"}))
    assert resolved.scope is None
    assert asked(resolved) == {"location": ["Plant A", "Plant B"]}
    assert resolved.resolution.questions[0].code == "unknown_location"


@pytest.mark.parametrize("location", ["PLANT A", "  plant   a ", "Plánt-A"])
def test_the_plant_matches_without_case_accents_spacing_or_punctuation(location: str) -> None:
    resolved = resolve(BUYER.model_copy(update={"location": location}))
    assert resolved.scope is not None and resolved.scope.location == "Plant A"


def single_site_records() -> list[Record]:
    """The motor evidence as a single-factory ERP exports it: no plant column anywhere."""
    records: list[Record] = []
    for record in motor_records():
        if record.values.get("location") == "Plant B":
            continue
        copy = record.model_copy(deep=True)
        copy.values.pop("location", None)
        copy.evidence.pop("location", None)
        copy.canonical.pop("location", None)
        records.append(copy)
    return records


LOCATION_ISSUES = {"missing_production_location", "missing_inventory_location"}


def test_sources_that_name_no_plant_are_one_site_and_are_planned() -> None:
    records = single_site_records()
    resolved = resolve(BUYER.model_copy(update={"location": None}), records)
    assert resolved.resolution.questions == []
    assert resolved.scope == SCOPE.model_copy(update={"location": None})
    assert assumed(resolved) == [
        "location_single_site",
        "as_of_latest_snapshot",
        "horizon_from_days",
    ]
    assert resolved.resolution.assumptions[0].value is None
    assert "location" not in {p.field for p in resolved.resolution.provenance}
    assert resolved.scope is not None
    plan = assess_plan(records, resolved.scope)
    assert {line.part_number: line.calculated_to_buy for line in plan.lines} == EXPECTED
    assert all(line.ready and line.location is None for line in plan.lines)
    assert (
        not {i.code for line in plan.lines for i in [*plan.issues, *line.issues]} & LOCATION_ISSUES
    )
    # Nothing to choose: only the product and the horizon are open questions.
    assert set(asked(resolve(ScopeRequest(), records))) == {"product_code", "horizon_end"}


def test_a_plant_the_buyer_names_applies_to_every_row_of_a_single_site() -> None:
    records = single_site_records()
    resolved = resolve(BUYER.model_copy(update={"location": "Planta Joinville"}), records)
    assert resolved.resolution.questions == []
    assert resolved.scope == SCOPE.model_copy(update={"location": "Planta Joinville"})
    assert assumed(resolved)[0] == "location_applied_to_all"
    assert resolved.resolution.assumptions[0].value == "Planta Joinville"
    assert {p.field: p.source for p in resolved.resolution.provenance}["location"] == "buyer"
    assert resolved.scope is not None
    plan = assess_plan(records, resolved.scope)
    assert {line.part_number: line.calculated_to_buy for line in plan.lines} == EXPECTED
    assert all(line.ready and line.location == "Planta Joinville" for line in plan.lines)
    assert (
        not {i.code for line in plan.lines for i in [*plan.issues, *line.issues]} & LOCATION_ISSUES
    )


def test_a_single_site_without_production_plans_accepts_a_request_without_a_plant() -> None:
    records = [r for r in single_site_records() if r.kind != "production_plan"]
    request = ScopeRequest(area="Maintenance", as_of=date(2026, 10, 8), horizon_days=14)
    assert direct_scope(records, request).scope == PlanningScope(
        area="Maintenance", as_of=date(2026, 10, 8), horizon_end=date(2026, 10, 22)
    )
    assert direct_scope(motor_records(), request).scope is None


def test_each_resolved_value_names_its_source() -> None:
    words = resolve(BUYER).resolution.provenance
    assert [(p.field, p.source) for p in words] == [
        ("product_code", "buyer"),
        ("location", "buyer"),
        ("as_of", "evidence"),
        ("horizon_end", "buyer"),
        ("safety_stock", "buyer"),
        ("production_lead_days", "default"),
    ]
    by_field = {p.field: p for p in words}
    assert {r.table for r in by_field["product_code"].evidence} == {"production_plan"}
    assert {r.table for r in by_field["as_of"].evidence} == {"inventory"}
    records = [*motor_records(), *product_records("PMP-200", "Pump", "Plant A", "2026-10-20")]
    described = resolve(BUYER.model_copy(update={"product": "motores"}), records)
    assert described.resolution.provenance[0].source == "evidence"
    explicit = resolve(as_request(SCOPE) or ScopeRequest()).resolution.provenance
    assert {p.field: p.source for p in explicit} == {
        "product_code": "buyer",
        "location": "buyer",
        "as_of": "buyer",
        "horizon_end": "buyer",
        "safety_stock": "buyer",
        "production_lead_days": "default",
    }
    assert resolve(BUYER.model_copy(update={"horizon_days": None})).resolution.provenance == []


def test_a_plant_spelled_differently_by_the_orders_uses_the_spelling_of_most_records() -> None:
    records = motor_records()
    for record in records:
        if record.kind == "production_plan" and record.values.get("location") == "Plant A":
            record.values["location"] = "PLANT A"
    resolved = resolve(BUYER, records)
    assert resolved.scope == SCOPE
    plan = assess_plan(records, SCOPE)
    assert {line.part_number: line.calculated_to_buy for line in plan.lines} == EXPECTED


def test_reference_date_is_the_latest_snapshot_of_that_plant_ignoring_older_ones() -> None:
    later_elsewhere = fact(
        "inventory",
        90,
        part_number="000101",
        unit="pcs",
        location="Plant B",
        total="5",
        reserved="0",
        blocked="0",
        as_of="2026-10-09",
    )
    resolved = resolve(BUYER, [*motor_records(), later_elsewhere])
    assert resolved.scope is not None and resolved.scope.as_of == date(2026, 10, 8)
    snapshot = resolved.resolution.assumptions[0]
    assert (snapshot.code, snapshot.field, snapshot.value) == (
        "as_of_latest_snapshot",
        "as_of",
        "2026-10-08",
    )
    # The evidence is the current snapshot's date cells; the 19/09 rows (27+) are not used.
    assert {(ref.table, ref.row) for ref in snapshot.evidence} == {
        ("inventory", row) for row in range(7, 13)
    }


def test_without_a_stock_snapshot_the_reference_date_is_asked() -> None:
    records = [r for r in motor_records() if r.kind != "inventory"]
    resolved = resolve(BUYER, records)
    assert resolved.scope is None
    assert [q.code for q in resolved.resolution.questions] == ["missing_as_of"]


@pytest.mark.parametrize(
    ("days", "end"), [(0, date(2026, 10, 8)), (14, date(2026, 10, 22)), (24, date(2026, 11, 1))]
)
def test_horizon_days_count_from_the_resolved_reference_date(days: int, end: date) -> None:
    resolved = resolve(BUYER.model_copy(update={"horizon_days": days}))
    assert resolved.scope is not None and resolved.scope.horizon_end == end


def test_a_missing_horizon_offers_the_candidate_orders_and_includes_none() -> None:
    resolved = resolve(BUYER.model_copy(update={"horizon_days": None}))
    assert resolved.scope is None
    assert asked(resolved) == {"horizon_end": ["2026-10-18", "2026-11-01"]}
    options = resolved.resolution.questions[0].options
    # Each date offers the orders due on it at the resolved plant, never Plant B's BUILD-B.
    assert [{(ref.table, ref.row) for ref in option.evidence} for option in options] == [
        {("production_plan", 2)},
        {("production_plan", 3)},
    ]


def test_conflicting_horizon_end_and_days_are_asked() -> None:
    request = BUYER.model_copy(update={"horizon_days": 10, "horizon_end": date(2026, 10, 22)})
    resolved = resolve(request)
    assert resolved.scope is None
    assert asked(resolved) == {"horizon_end": ["2026-10-18", "2026-10-22"]}
    consistent = resolve(request.model_copy(update={"horizon_days": 14}))
    assert consistent.scope == SCOPE


def test_an_explicit_complete_scope_is_unchanged() -> None:
    resolved = resolve(as_request(SCOPE) or ScopeRequest())
    assert resolved.scope == SCOPE
    assert resolved.resolution.assumptions == resolved.resolution.questions == []


def test_an_area_request_is_matched_and_planned() -> None:
    resolved = resolve(ScopeRequest(area="MOTORS", location="Plant A", horizon_days=14))
    assert resolved.scope is not None
    assert (resolved.scope.product_code, resolved.scope.area) == (None, "Motors")
    zero = resolved.scope.model_copy(update={"safety_stock": Decimal(0)})
    plan = assess_plan(motor_records(), zero)
    assert {line.part_number: line.calculated_to_buy for line in plan.lines} == EXPECTED


def test_an_unknown_area_lists_the_areas_of_the_demand() -> None:
    resolved = resolve(ScopeRequest(area="Pumps", location="Plant A", horizon_days=14))
    assert resolved.scope is None
    assert asked(resolved)["area"] == ["Maintenance", "Motors"]


def test_a_mismatched_area_does_not_filter_an_identified_product() -> None:
    resolved = resolve(BUYER.model_copy(update={"area": "compras de motores"}))
    assert resolved.scope == SCOPE
    assert "area_not_applied" in assumed(resolved)


def test_sources_without_production_plans_resolve_only_complete_requests() -> None:
    records = [r for r in motor_records() if r.kind != "production_plan"]
    partial = direct_scope(records, BUYER)
    assert partial.scope is None and partial.resolution.request == BUYER
    assert [(i.code, i.severity) for i in scope_issues(partial, planned=False)] == [
        ("scope_not_applied", "warning")
    ]
    complete = direct_scope(
        records,
        ScopeRequest(
            area="Maintenance", location="PLANT A", as_of=date(2026, 10, 8), horizon_days=14
        ),
    )
    assert complete.scope is not None
    assert (complete.scope.location, complete.scope.horizon_end) == ("Plant A", date(2026, 10, 22))
    assert [a.code for a in complete.resolution.assumptions] == ["horizon_from_days"]
    assert scope_issues(direct_scope(records, ScopeRequest()), planned=False) == []


def test_the_request_treats_blanks_as_unknown_and_rejects_reversed_horizons() -> None:
    request = ScopeRequest.model_validate({"product": " ", "location": "", "horizon_days": 3})
    assert (request.product, request.location, request.horizon_days) == (None, None, 3)
    with pytest.raises(ValidationError):
        ScopeRequest.model_validate({"as_of": "2026-10-22", "horizon_end": "2026-10-08"})
    with pytest.raises(ValidationError):
        ScopeRequest.model_validate({"horizon_days": -1})


def _context() -> ToolContext:
    return ToolContext(tenant_id=uuid4(), user_id=uuid4(), run_id=uuid4())


async def _analyze(scope: ScopeRequest | None, data: bytes, mapper: Mapper) -> SourceAssessment:
    return await analyze_documents(
        [SourceDocument("synthetic-erp", "export.xlsx", XLSX, data)],
        "What do we need to buy for the motor?",
        _context(),
        mapper,
        scope=scope,
    )


async def test_unscoped_production_sources_return_candidates_and_questions_not_lines() -> None:
    data, mapper = motor_workbook()
    result = await _analyze(None, data, mapper)
    assert result.lines == []
    assert result.status == "needs_review" and result.scope is None
    blockers = {i.code for i in result.issues if i.severity == "blocker"}
    assert blockers == {"missing_planning_scope"}
    assert {(c.product_code, c.location) for c in result.planning_candidates} == {
        ("MTR-100", "Plant A"),
        ("MTR-100", "Plant B"),
    }
    assert result.snapshot_dates == [date(2026, 9, 19), date(2026, 10, 8)]
    assert result.scope_resolution is not None
    assert result.scope_resolution.request == ScopeRequest()
    assert {q.field for q in result.scope_resolution.questions} == {
        "product_code",
        "location",
        "horizon_end",
    }
    assert result.record_counts["production_plan"] == 3


async def test_the_buyers_words_are_resolved_and_planned_in_the_same_call() -> None:
    data, unscoped = motor_workbook()
    await _analyze(None, data, unscoped)
    _, mapper = motor_workbook()
    result = await _analyze(BUYER.model_copy(update={"location": "PLANT A"}), data, mapper)
    assert result.status == "assessed", result.model_dump_json()
    assert result.scope == SCOPE
    assert {line.part_number: line.calculated_to_buy for line in result.lines} == EXPECTED
    assert result.scope_resolution is not None
    assert [a.code for a in result.scope_resolution.assumptions] == [
        "as_of_latest_snapshot",
        "horizon_from_days",
    ]
    warnings = [i.code for i in result.issues if i.severity == "warning"]
    assert warnings[:2] == ["as_of_latest_snapshot", "horizon_from_days"]
    # One mapping pass, as many model calls as an unscoped read: no second round trip.
    assert mapper.calls == unscoped.calls


async def test_a_workbook_without_a_plant_column_is_planned_for_the_named_plant() -> None:
    data, mapper = motor_workbook(single_site_records())
    request = BUYER.model_copy(update={"location": "Planta Joinville"})
    result = await _analyze(request, data, mapper)
    assert result.status == "assessed", result.model_dump_json()
    assert result.scope == SCOPE.model_copy(update={"location": "Planta Joinville"})
    assert {line.part_number: line.calculated_to_buy for line in result.lines} == EXPECTED
    assert result.scope_resolution is not None
    assert [a.code for a in result.scope_resolution.assumptions] == [
        "location_applied_to_all",
        "as_of_latest_snapshot",
        "horizon_from_days",
    ]
    assert {(c.product_code, c.location) for c in result.planning_candidates} == {("MTR-100", None)}


async def test_a_missing_horizon_blocks_without_lines_and_offers_dates() -> None:
    data, mapper = motor_workbook()
    result = await _analyze(BUYER.model_copy(update={"horizon_days": None}), data, mapper)
    assert result.lines == [] and result.scope is None
    assert result.scope_resolution is not None
    assert [q.field for q in result.scope_resolution.questions] == ["horizon_end"]
    assert "missing_planning_scope" in {i.code for i in result.issues}
    assert "no_material_demand" not in {i.code for i in result.issues}


async def test_direct_demand_sources_keep_the_unscoped_assessment() -> None:
    data = workbook(
        {
            "Stock": [
                ["Material", "Description", "Unit", "Available", "On order", "Safety stock"],
                ["000010", "Bolts", "pcs", 4, 0, 2],
            ],
            "Requirements": [
                ["Material", "Unit", "Quantity", "Need by", "Demand basis"],
                ["000010", "pcs", 20, "2026-11-01", "Gross"],
            ],
        }
    )
    unscoped = await _analyze(None, data, SourceMapper())
    scoped = await _analyze(BUYER, data, SourceMapper())
    assert unscoped.status == scoped.status == "assessed"
    assert scoped.lines == unscoped.lines
    assert [(line.part_number, line.calculated_to_buy) for line in scoped.lines] == [
        ("000010", Decimal(18))
    ]
    assert unscoped.scope_resolution is None and scoped.scope is None
    assert "scope_not_applied" in {i.code for i in scoped.issues}


async def test_only_closed_production_orders_keep_the_unscoped_assessment() -> None:
    records = motor_records()
    for record in records:
        if record.kind == "production_plan":
            record.values["status"] = "Closed"
    assert not has_production_plans(records)
    data, mapper = motor_workbook(records)
    result = await _analyze(None, data, mapper)
    assert result.scope_resolution is None
    assert "missing_planning_scope" not in {i.code for i in result.issues}
    assert result.lines  # The legacy direct reading, not a question about a product.


@pytest.mark.db
async def test_assessment_api_resolves_partial_scopes_and_keeps_complete_ones(
    app_engine: AsyncEngine,
    purchasing_rows: People,
) -> None:
    app = build_app()
    app.include_router(procurement_assessments_router())
    who = buyer(purchasing_rows)
    storage = TrackingStorage()
    data, mapper = motor_workbook()
    upload = await onboarding.store_upload(
        app_engine, storage, who, filename="erp.xlsx", content_type=XLSX, data=data
    )
    sessionmaker = create_sessionmaker(app_engine)

    def principal() -> Principal:
        return who

    def db() -> async_sessionmaker[AsyncSession]:
        return sessionmaker

    def blobs() -> BlobStorage:
        return storage

    def source_mapper() -> Mapper:
        return mapper

    app.dependency_overrides[get_principal] = principal
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_storage] = blobs
    app.dependency_overrides[get_source_mapper] = source_mapper
    request = {"upload_ids": [str(upload.id)], "objective": "Motor MTR-100 for Plant A"}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        partial = {
            "product": "Motor MTR-100",
            "location": "plant a",
            "horizon_days": 14,
            "safety_stock": 0,
        }
        created = await http.post("/v1/procurement-assessments", json={**request, "scope": partial})
        assert created.status_code == 200, created.text
        resolved = SourceAssessment.model_validate(created.json())
        assert resolved.scope == SCOPE and resolved.status == "assessed"
        assert resolved.scope_resolution is not None
        assert resolved.scope_resolution.request == ScopeRequest.model_validate(partial)
        assert {line.part_number: line.calculated_to_buy for line in resolved.lines} == EXPECTED
        saved = await http.get(f"/v1/procurement-assessments/{resolved.assessment_id}")
        assert saved.json() == created.json()

        complete = await http.post(
            "/v1/procurement-assessments",
            json={**request, "scope": SCOPE.model_dump(mode="json")},
        )
        assert complete.status_code == 200, complete.text
        explicit = SourceAssessment.model_validate(complete.json())
        assert explicit.scope == SCOPE and explicit.assessment_id != resolved.assessment_id
        assert explicit.scope_resolution is not None
        assert explicit.scope_resolution.assumptions == []

        open_scope = await http.post(
            "/v1/procurement-assessments",
            json={**request, "scope": {"product_code": "MTR-100", "location": "Plant A"}},
        )
        pending = SourceAssessment.model_validate(open_scope.json())
        assert pending.status == "needs_review" and pending.lines == []
        assert pending.scope_resolution is not None
        assert [q.field for q in pending.scope_resolution.questions] == ["horizon_end"]

        reversed_horizon = {"as_of": "2026-10-22", "horizon_end": "2026-10-08"}
        invalid = await http.post(
            "/v1/procurement-assessments", json={**request, "scope": reversed_horizon}
        )
        assert invalid.status_code == 422
