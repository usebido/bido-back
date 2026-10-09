"""Whether a new order arrives in time: lead times against the dated balance, by code."""

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from bido_back.agents.context import ToolContext
from bido_back.purchasing.sources.contracts import (
    AssessmentLine,
    DemandContribution,
    InboundEvidence,
    MappingProposal,
    MappingTask,
    Record,
    RecordKind,
    SourceAssessment,
    SourceRef,
    SupplierEvidence,
)
from bido_back.purchasing.sources.handoff import wanted_suppliers
from bido_back.purchasing.sources.readers import SourceDocument
from bido_back.purchasing.sources.service import analyze_documents
from bido_back.purchasing.sources.timing import check_timing, late
from evals.sap_review_case import OUTCOMES, QUANTITIES, reports

TODAY = date(2026, 10, 9)
AS_OF = date(2026, 10, 8)
REF = SourceRef(source_id="s", table="t", row=1, column=1)


async def _no_model(task: MappingTask, context: ToolContext) -> MappingProposal:
    raise AssertionError(f"the mapper was called for {task.table}")


async def _review() -> SourceAssessment:
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


def _supplier(line: AssessmentLine, name: str) -> SupplierEvidence:
    return next(s for s in line.suppliers if s.name is not None and name in s.name)


async def test_the_review_quantities_hold_and_each_line_says_when_it_runs_short() -> None:
    result = await _review()
    assert {line.part_number: line.calculated_to_buy for line in result.lines} == QUANTITIES
    for part, outcome in OUTCOMES.items():
        timing = _line(result, part).timing
        assert timing is not None and timing.outcome == outcome, part


async def test_a_motor_no_order_can_save_is_an_unavoidable_stockout_bought_from_the_fastest() -> (
    None
):
    line = _line(await _review(), "100000152")
    timing = line.timing
    assert timing is not None
    assert timing.outcome == "unavoidable_stockout"
    assert (timing.first_shortage_date, timing.first_stockout_date) == (
        date(2026, 10, 12),
        date(2026, 10, 12),
    )
    # The cheaper, best-ranked source (75 days) would arrive after every need.
    assert timing.switched_from == "MOTORES EXEMPLO SUL LTDA"
    assert timing.recommended_supplier == "ELETROMOTORES EXEMPLO SA"
    assert timing.recommended_arrival == AS_OF + timedelta(days=45)
    slow = _supplier(line, "MOTORES EXEMPLO SUL")
    assert (slow.estimated_arrival, slow.arrival_status) == (
        AS_OF + timedelta(days=75),
        "does_not_meet",
    )
    assert not slow.suggested and late(slow)
    assert (timing.uncovered_quantity, timing.uncovered_after_expedite) == (
        Decimal(23),
        Decimal(3),
    )
    assert timing.involve == ["production_planning"]
    # The production orders of October and November the motor holds up, and the product
    # they build, known only through its other components.
    assert [(a.reference, a.need_by) for a in timing.affected_demand] == [
        ("0000004113", date(2026, 10, 12)),
        ("0000004114", date(2026, 10, 26)),
        ("0000004115", date(2026, 11, 9)),
    ]
    assert all(a.product_code == "300000030" for a in timing.affected_demand)
    assert "100000103 CARCACA FERRO FUNDIDO GG20 BOMBA BC-125" in (
        timing.affected_demand[0].product_components
    )
    # The first action: bring the open order forward, as MD04 asks.
    first = line.expedite[0]
    assert (first.po_number, first.reason, first.current_date, first.requested_date) == (
        "4500019736",
        "mrp_reschedule_in",
        date(2026, 12, 11),
        date(2026, 10, 12),
    )
    assert first.would_cover == 20
    assert "unavoidable_stockout" in {i.code for i in line.issues}
    assert "arrival_from_lead_time" in {a.code for a in line.assumptions}
    # The RFQ invites only the supplier that can still help.
    wanted = wanted_suppliers([line], per_line=3)
    assert [w.evidence.name for w in wanted if not w.disqualified] == ["ELETROMOTORES EXEMPLO SA"]


async def test_a_shaft_short_five_units_is_mitigated_by_its_expedite() -> None:
    line = _line(await _review(), "100000120")
    timing = line.timing
    assert timing is not None
    assert timing.first_stockout_date == date(2026, 10, 26)
    assert (timing.uncovered_quantity, timing.uncovered_after_expedite) == (Decimal(5), 0)
    assert [(e.po_number, e.reason, e.requested_date) for e in line.expedite] == [
        ("4500019744", "mrp_reschedule_in", date(2026, 10, 26))
    ]


async def test_lines_that_dip_below_safety_stock_say_so_until_the_purchase_arrives() -> None:
    result = await _review()
    for part in ("100000101", "100000110", "100000180"):
        timing = _line(result, part).timing
        assert timing is not None
        assert timing.outcome == "meets_consuming_safety_stock", part
        assert timing.first_shortage_date is not None
        assert timing.recommended_arrival is not None
        assert timing.first_shortage_date < timing.recommended_arrival, part
    # 100000101 sits 15 below its safety stock of 30 until then; MD04's expedite fixes it.
    carcass = _line(result, "100000101")
    assert carcass.timing is not None
    assert carcass.timing.projected[0].balance == 15
    assert [(e.po_number, e.would_cover) for e in carcass.expedite] == [("4500019725", 15)]
    # The overdue rotor order is chased.
    rotor = _line(result, "100000110")
    assert [(e.po_number, e.reason) for e in rotor.expedite] == [("4500019706", "overdue")]


async def test_a_seal_without_usable_supplier_gets_sourcing_of_its_own_group_only() -> None:
    line = _line(await _review(), "100000141")
    assert line.sourcing is not None
    assert line.sourcing.material_group == "VEDA"
    # No other sealing supplier is in the evidence: never the metal or steel suppliers.
    assert line.sourcing.candidates == []
    assert line.sourcing.excluded == ["VEDACOES EXEMPLO SUL LTDA: blocked for purchasing"]
    assert not any(s.suggested for s in line.suppliers)
    assert "no_usable_supplier" in {i.code for i in line.issues}


async def test_an_outlier_price_is_flagged_and_its_fast_delivery_noted() -> None:
    line = _line(await _review(), "100000120")
    assert line.price is not None
    [outlier] = line.price.outliers
    assert (outlier.unit_price, outlier.deviation_pct) == (Decimal("179.2500"), Decimal("32.2"))
    assert outlier.in_benchmark
    # The target is the last price either way; only the maximum moves with it.
    assert outlier.target_without == line.price.target_price
    assert outlier.max_without is not None and outlier.max_without < line.price.max_price
    assert (outlier.lead_time_days, outlier.reference_lead_time_days) == (8, Decimal(19))
    assert outlier.possible_emergency
    assert "price_outlier" in {i.code for i in line.issues}


async def test_dated_needs_stay_a_schedule() -> None:
    line = _line(await _review(), "100000103")
    assert line.calculated_to_buy == 0
    assert [(d.need_by, d.quantity) for d in line.demand_schedule] == [
        (date(2026, 10, 12), 10),
        (date(2026, 10, 26), 10),
        (date(2026, 11, 9), 12),
        (date(2026, 11, 23), 12),
        (date(2026, 12, 7), 10),
        (date(2026, 12, 21), 6),
    ]


# ── Rules on a single line ──────────────────────────────────────────────────


def _demand(*needs: tuple[int, int]) -> list[DemandContribution]:
    return [
        DemandContribution(
            reference=f"R{i}",
            quantity=Decimal(q),
            need_by=AS_OF + timedelta(days=d),
            evidence=[REF],
        )
        for i, (d, q) in enumerate(needs)
    ]


def _source(name: str, days: int, *, orders: int = 1) -> SupplierEvidence:
    return SupplierEvidence(
        supplier_code=name,
        name=name,
        orders=orders,
        contract_lead_time_days=Decimal(days),
        contract_expired=False,
    )


def _timed(line: AssessmentLine, records: list[Record] | None = None) -> AssessmentLine:
    line.calculated_to_buy = line.calculated_to_buy or Decimal(1)
    check_timing(line, as_of=AS_OF, master=None, records=records or [])
    return line


def _plain_line(available: int, safety: int, *needs: tuple[int, int]) -> AssessmentLine:
    return AssessmentLine(
        part_number="P1",
        unit="UN",
        available=Decimal(available),
        safety_stock=Decimal(safety),
        demand_contributions=_demand(*needs),
        need_by=AS_OF + timedelta(days=needs[0][0]),
    )


def test_the_best_ranked_source_stays_when_it_arrives_in_time() -> None:
    line = _plain_line(100, 10, (40, 80), (60, 50))
    line.suppliers = [_source("CHEAP", 30), _source("FAST", 10)]
    _timed(line)
    assert line.timing is not None
    # 100 - 80 = 20 on day 40 (above 10): short only on day 60.
    assert line.timing.first_shortage_date == AS_OF + timedelta(days=60)
    assert line.timing.outcome == "meets"
    assert line.timing.recommended_supplier == "CHEAP"
    assert line.timing.switched_from is None
    assert all(s.suggested for s in line.suppliers)


def test_a_late_best_ranked_source_is_switched_for_the_fastest_that_meets() -> None:
    line = _plain_line(10, 0, (20, 30), (50, 30))
    line.suppliers = [_source("SLOW", 40), _source("FAST", 15), _source("MID", 18)]
    _timed(line)
    assert line.timing is not None
    assert line.timing.outcome == "meets"
    assert (line.timing.recommended_supplier, line.timing.switched_from) == ("FAST", "SLOW")
    assert [s.name for s in line.suppliers if s.suggested] == ["FAST", "MID"]
    slow = next(s for s in line.suppliers if s.name == "SLOW")
    assert slow.arrival_status == "does_not_meet" and late(slow)


def test_without_a_known_lead_time_the_outcome_is_unknown() -> None:
    line = _plain_line(10, 0, (20, 30))
    line.suppliers = [SupplierEvidence(supplier_code="X", name="X", orders=2)]
    _timed(line)
    assert line.timing is not None
    assert line.timing.outcome == "unknown"
    assert line.timing.recommended_supplier is None


def test_open_orders_count_on_their_date_and_overdue_ones_now() -> None:
    line = _plain_line(0, 0, (10, 50), (30, 50))
    line.inbound = [
        InboundEvidence(
            po_number="PO1",
            po_line="10",
            quantity=Decimal(50),
            expected_at=AS_OF - timedelta(days=3),
            eligible=True,
            evidence=[REF],
        ),
        InboundEvidence(
            po_number="PO2",
            po_line="10",
            quantity=Decimal(50),
            expected_at=AS_OF + timedelta(days=40),
            eligible=False,
            evidence=[REF],
        ),
    ]
    line.suppliers = [_source("S", 5)]
    _timed(line)
    assert line.timing is not None
    # The overdue order covers day 10; PO2 only counts if it were eligible.
    assert [p.balance for p in line.timing.projected] == [0, -50]
    actions = {(a.po_number, a.reason) for a in line.expedite}
    assert ("PO1", "overdue") in actions
    assert ("PO2", "due_after_shortage") in actions


def test_sourcing_proposes_suppliers_of_the_same_group_only() -> None:
    def record(kind: RecordKind, **values: str) -> Record:
        return Record(kind=kind, values=values, evidence={k: REF for k in values})

    records = [
        record("item", part_number="P1", material_group="VEDA"),
        record("item", part_number="P2", material_group="VEDA"),
        record("item", part_number="P3", material_group="FUND"),
        record("supplier_item", part_number="P2", supplier_code="SEALS"),
        record("supplier_item", part_number="P3", supplier_code="STEEL"),
        record("supplier", supplier_code="SEALS", name="Seals Example"),
        record("supplier", supplier_code="STEEL", name="Steel Example"),
    ]
    line = _plain_line(5, 0, (20, 30))
    line.suppliers = [
        SupplierEvidence(supplier_code="OLD", name="Old Seals", purchase_blocked=True)
    ]
    _timed(line, records)
    assert line.sourcing is not None
    assert line.sourcing.material_group == "VEDA"
    assert line.sourcing.candidates == ["Seals Example"]
