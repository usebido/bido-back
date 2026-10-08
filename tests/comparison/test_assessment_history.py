"""History measured by a source assessment, for lines drafted from it. Pure.

The RFQ line evidence is built with the shared contract models
(`AssessmentLine`, `SupplierEvidence`) and the supplier-link shape the
assessment-to-RFQ handoff records, so a contract change breaks these tests.
"""

from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue

from bido_back.comparison.assessment_history import (
    AFTER_TODAY,
    AMBIGUOUS,
    BASIS_ISSUE,
    UNDATED,
    assessment_performance,
    with_assessment_history,
)
from bido_back.comparison.compute import RequestedLine, compute_comparison
from bido_back.comparison.policy import ComparisonPolicy, SupplierPerformance, evidence_issues
from bido_back.comparison.rules import apply_rules
from bido_back.purchasing.sources.contracts import AssessmentLine, SupplierEvidence
from tests.comparison.test_rules import offer

TODAY = date(2026, 10, 7)
D = Decimal
POLICY = ComparisonPolicy()
SUPPLIER = uuid4()

# Cheaper but late with rejections, and reliable: as the analysis of the file measured them.
METALNOVA = SupplierEvidence(
    supplier_code="F001",
    name="MetalNova",
    email="vendas@metalnova.example.test",
    last_price=D("9.80"),
    currency="EUR",
    ordered_at=date(2026, 8, 3),
    orders=5,
    on_time_rate=D("0.4"),
    quality_rate=D("0.9"),
    delivery_observations=5,
    quality_observations=5,
    last_received_at=date(2026, 9, 1),
)
ACOS_SUL = SupplierEvidence(
    supplier_code="F002",
    name="Aços Sul",
    email="comercial@acossul.example.test",
    last_price=D("10.40"),
    currency="EUR",
    ordered_at=date(2026, 9, 10),
    orders=4,
    on_time_rate=D(1),
    quality_rate=D(1),
    delivery_observations=4,
    quality_observations=4,
    last_received_at=date(2026, 9, 20),
)
# Qualified for the item, never bought from: nothing measured.
QUALIFIED_ONLY = SupplierEvidence(supplier_code="F003", name="Fundição Leste")

Evidence = dict[str, JsonValue]


def link(supplier_id: UUID, evidence: SupplierEvidence) -> JsonValue:
    return {
        "supplier_id": str(supplier_id),
        "supplier_code": evidence.supplier_code,
        "name": evidence.name,
        "email": evidence.email,
    }


def source_evidence(
    *suppliers: SupplierEvidence, links: list[JsonValue] | None = None, part: str = "000101"
) -> Evidence:
    """What an RFQ line drafted from an assessment keeps, with the handoff's links."""
    assessed = AssessmentLine(
        part_number=part,
        description="Motor housing",
        unit="pcs",
        calculated_to_buy=D(50),
        ready=True,
        suppliers=list(suppliers),
    )
    evidence: Evidence = {"assessment_id": str(uuid4()), "line": assessed.model_dump(mode="json")}
    if links is not None:
        evidence["supplier_links"] = links
    return evidence


def requested(evidence: Evidence, part_number: str | None = "000101") -> RequestedLine:
    return RequestedLine(
        id=uuid4(),
        position=1,
        description="Motor housing",
        mpn=None,
        unit="pcs",
        quantities=(D(50),),
        part_number=part_number,
        source_evidence=evidence,
    )


def assessed_history(on_time: str, quality: str, samples: int, latest: date) -> SupplierPerformance:
    return SupplierPerformance(
        basis="source_assessment",
        observations=samples,
        delivery_samples=samples,
        on_time_rate=D(on_time),
        quality_samples=samples,
        accepted_quantity_rate=D(quality),
        latest_observation_on=latest,
        issues=[BASIS_ISSUE],
    )


def test_an_invited_supplier_gets_the_history_its_assessment_measured() -> None:
    metalnova, acos, uninvited = uuid4(), uuid4(), uuid4()
    line = requested(
        source_evidence(
            METALNOVA,
            ACOS_SUL,
            QUALIFIED_ONLY,
            links=[
                link(metalnova, METALNOVA),
                link(acos, ACOS_SUL),
                link(uninvited, QUALIFIED_ONLY),
            ],
        )
    )

    result = assessment_performance(line, [metalnova, acos, uuid4()], today=TODAY, policy=POLICY)

    assert result == {
        metalnova: assessed_history("0.4", "0.9", 5, date(2026, 9, 1)),
        acos: assessed_history("1", "1", 4, date(2026, 9, 20)),
    }
    # No imported purchase line backs it, and the start of its window is not known.
    assert result[metalnova].evidence_po_line_ids == []
    assert result[metalnova].oldest_observation_on is None
    assert evidence_issues(result[metalnova], POLICY) == [BASIS_ISSUE]


def test_the_policy_window_applies_to_the_whole_aggregate() -> None:
    # An order placed after the last receipt is the latest observation: 22 days ago.
    evidence = METALNOVA.model_copy(update={"ordered_at": date(2026, 9, 15)})
    line = requested(source_evidence(evidence, links=[link(SUPPLIER, evidence)]))

    def history(window_days: int) -> SupplierPerformance:
        policy = ComparisonPolicy(history_window_days=window_days)
        return assessment_performance(line, [SUPPLIER], today=TODAY, policy=policy)[SUPPLIER]

    within = history(22)
    stale = history(21)

    assert within.latest_observation_on == date(2026, 9, 15)
    assert (within.observations, within.delivery_samples) == (5, 5)
    assert stale == SupplierPerformance(
        basis="source_assessment", excluded_stale_observations=5, issues=[BASIS_ISSUE]
    )
    assert evidence_issues(stale, ComparisonPolicy(history_window_days=21)) == [
        BASIS_ISSUE,
        "delivery_history_insufficient",
        "quality_history_insufficient",
        "stale_history_excluded",
    ]


@pytest.mark.parametrize(
    ("dates", "issue"),
    [
        ({"ordered_at": None, "last_received_at": None}, UNDATED),
        ({"last_received_at": TODAY + timedelta(days=1)}, AFTER_TODAY),
    ],
)
def test_undated_or_future_statistics_are_not_used(
    dates: dict[str, date | None], issue: str
) -> None:
    evidence = METALNOVA.model_copy(update=dates)
    line = requested(source_evidence(evidence, links=[link(SUPPLIER, evidence)]))

    result = assessment_performance(line, [SUPPLIER], today=TODAY, policy=POLICY)

    assert result == {
        SUPPLIER: SupplierPerformance(basis="source_assessment", issues=[BASIS_ISSUE, issue])
    }


def test_rates_without_samples_or_samples_without_rates_stay_unknown() -> None:
    evidence = METALNOVA.model_copy(
        update={"on_time_rate": None, "delivery_observations": 4, "quality_observations": 0}
    )
    line = requested(source_evidence(evidence, links=[link(SUPPLIER, evidence)]))

    history = assessment_performance(line, [SUPPLIER], today=TODAY, policy=POLICY)[SUPPLIER]

    assert history.observations == 5
    assert (history.delivery_samples, history.on_time_rate) == (0, None)
    assert (history.quality_samples, history.accepted_quantity_rate) == (0, None)
    assert evidence_issues(history, POLICY) == [
        BASIS_ISSUE,
        "delivery_history_insufficient",
        "quality_history_insufficient",
    ]


def _first_supplier(evidence: Evidence) -> dict[str, JsonValue]:
    line = evidence["line"]
    assert isinstance(line, dict)
    suppliers = line["suppliers"]
    assert isinstance(suppliers, list) and isinstance(suppliers[0], dict)
    return suppliers[0]


def _without_links(evidence: Evidence) -> None:
    del evidence["supplier_links"]


def _links_not_a_list(evidence: Evidence) -> None:
    evidence["supplier_links"] = "F001"


def _link_without_valid_id(evidence: Evidence) -> None:
    evidence["supplier_links"] = [{"supplier_id": "MetalNova", "supplier_code": "F001"}]


def _link_to_unknown_code(evidence: Evidence) -> None:
    evidence["supplier_links"] = [{"supplier_id": str(SUPPLIER), "supplier_code": "F999"}]


def _link_without_identity(evidence: Evidence) -> None:
    evidence["supplier_links"] = [{"supplier_id": str(SUPPLIER)}]


def _without_assessment_id(evidence: Evidence) -> None:
    del evidence["assessment_id"]


def _invalid_assessment_id(evidence: Evidence) -> None:
    evidence["assessment_id"] = "assessment-1"


def _line_not_an_object(evidence: Evidence) -> None:
    evidence["line"] = "000101"


def _suppliers_not_a_list(evidence: Evidence) -> None:
    line = evidence["line"]
    assert isinstance(line, dict)
    line["suppliers"] = {"F001": "MetalNova"}


def _rate_out_of_range(evidence: Evidence) -> None:
    _first_supplier(evidence)["on_time_rate"] = "1.5"


def _negative_count(evidence: Evidence) -> None:
    _first_supplier(evidence)["delivery_observations"] = -5


def _count_not_an_integer(evidence: Evidence) -> None:
    _first_supplier(evidence)["orders"] = "5"


def _legacy_analysis(evidence: Evidence) -> None:
    evidence.clear()
    evidence.update({"kind": "purchase_analysis", "analysis_id": str(uuid4())})


@pytest.mark.parametrize(
    "damage",
    [
        _without_links,
        _links_not_a_list,
        _link_without_valid_id,
        _link_to_unknown_code,
        _link_without_identity,
        _without_assessment_id,
        _invalid_assessment_id,
        _line_not_an_object,
        _suppliers_not_a_list,
        _rate_out_of_range,
        _negative_count,
        _count_not_an_integer,
        _legacy_analysis,
    ],
)
def test_missing_or_malformed_evidence_contributes_nothing(
    damage: Callable[[Evidence], None],
) -> None:
    evidence = source_evidence(METALNOVA, links=[link(SUPPLIER, METALNOVA)])
    assert assessment_performance(requested(evidence), [SUPPLIER], today=TODAY, policy=POLICY)

    damage(evidence)

    assert assessment_performance(requested(evidence), [SUPPLIER], today=TODAY, policy=POLICY) == {}


def test_nothing_is_used_for_another_item_or_an_unmeasured_supplier() -> None:
    evidence = source_evidence(METALNOVA, links=[link(SUPPLIER, METALNOVA)])
    unmeasured = source_evidence(QUALIFIED_ONLY, links=[link(SUPPLIER, QUALIFIED_ONLY)])
    trimmed = source_evidence(METALNOVA, links=[link(SUPPLIER, METALNOVA)], part=" ab-7")

    def history(line: RequestedLine) -> dict[UUID, SupplierPerformance]:
        return assessment_performance(line, [SUPPLIER], today=TODAY, policy=POLICY)

    # The buyer changed the line's part number after the draft, or cleared it.
    assert history(requested(evidence, part_number="000102")) == {}
    assert history(requested(evidence, part_number=None)) == {}
    assert history(requested(unmeasured)) == {}
    # Part numbers compare like the imported-history join: trimmed, case-insensitive.
    assert history(requested(trimmed, part_number="AB-7 "))


def test_links_match_by_code_or_by_name_only_when_neither_side_has_a_code() -> None:
    by_code, by_name, recoded, coded = uuid4(), uuid4(), uuid4(), uuid4()
    nameless = ACOS_SUL.model_copy(update={"supplier_code": None, "name": "Fundição  Paulista"})
    line = requested(
        source_evidence(
            METALNOVA,
            nameless,
            links=[
                # Codes compare trimmed and case-insensitively.
                {"supplier_id": str(by_code), "supplier_code": " f001 ", "name": "Other name"},
                {"supplier_id": str(by_name), "supplier_code": None, "name": "fundição paulista"},
                # A link with a code never falls back to the name...
                {"supplier_id": str(recoded), "supplier_code": "F009", "name": "MetalNova"},
                # ...and a name never matches an assessment supplier that has a code.
                {"supplier_id": str(coded), "supplier_code": None, "name": "MetalNova"},
            ],
        )
    )

    result = assessment_performance(
        line, [by_code, by_name, recoded, coded], today=TODAY, policy=POLICY
    )

    assert set(result) == {by_code, by_name}
    assert result[by_code].on_time_rate == D("0.4")
    assert result[by_name].on_time_rate == D(1)


def test_a_supplier_is_never_credited_with_another_ones_history() -> None:
    merged, first, second, partial = uuid4(), uuid4(), uuid4(), uuid4()
    one_to_many = requested(
        source_evidence(
            METALNOVA, ACOS_SUL, links=[link(merged, METALNOVA), link(merged, ACOS_SUL)]
        )
    )
    many_to_one = requested(
        source_evidence(METALNOVA, links=[link(first, METALNOVA), link(second, METALNOVA)])
    )
    with_unmeasured = requested(
        source_evidence(
            METALNOVA,
            QUALIFIED_ONLY,
            links=[link(partial, METALNOVA), link(partial, QUALIFIED_ONLY)],
        )
    )
    unattributed = SupplierPerformance(basis="source_assessment", issues=[BASIS_ISSUE, AMBIGUOUS])

    assert assessment_performance(one_to_many, [merged], today=TODAY, policy=POLICY) == {
        merged: unattributed
    }
    assert assessment_performance(many_to_one, [first, second], today=TODAY, policy=POLICY) == {
        first: unattributed,
        second: unattributed,
    }
    # A linked supplier with nothing measured adds nothing to attribute.
    result = assessment_performance(with_unmeasured, [partial], today=TODAY, policy=POLICY)
    assert result[partial].on_time_rate == D("0.4")


def test_imported_history_in_the_window_wins_and_is_never_mixed() -> None:
    imported, stale, empty, absent = suppliers = [uuid4() for _ in range(4)]
    line = requested(
        source_evidence(
            *(METALNOVA.model_copy(update={"supplier_code": f"F{n}"}) for n in range(4)),
            links=[
                {"supplier_id": str(supplier), "supplier_code": f"F{n}"}
                for n, supplier in enumerate(suppliers)
            ],
        )
    )
    unrelated = requested({})
    observed = SupplierPerformance(
        observations=2,
        delivery_samples=2,
        on_time_rate=D(1),
        latest_observation_on=TODAY - timedelta(days=10),
        evidence_po_line_ids=[uuid4(), uuid4()],
    )
    loaded = {
        imported: {line.id: observed, unrelated.id: SupplierPerformance()},
        stale: {line.id: SupplierPerformance(excluded_stale_observations=3)},
        empty: {line.id: SupplierPerformance()},
    }

    result = with_assessment_history(
        loaded, [line, unrelated], suppliers, today=TODAY, policy=POLICY
    )

    assert result[imported] == loaded[imported]
    for supplier in (stale, empty, absent):
        assert result[supplier] == {line.id: assessed_history("0.4", "0.9", 5, date(2026, 9, 1))}
    assert loaded[empty][line.id] == SupplierPerformance()  # the input is left as it was


def test_rules_consume_assessment_history_like_imported_history() -> None:
    metalnova, acos = uuid4(), uuid4()
    line = requested(
        source_evidence(
            METALNOVA, ACOS_SUL, links=[link(metalnova, METALNOVA), link(acos, ACOS_SUL)]
        )
    )
    assessed = assessment_performance(line, [metalnova, acos], today=TODAY, policy=POLICY)
    measured = {"MetalNova": assessed[metalnova], "Aços Sul": assessed[acos]}
    # The same numbers, as if they came from imported purchase lines.
    imported = {
        name: history.model_copy(update={"basis": "imported_history", "issues": []})
        for name, history in measured.items()
    }
    prices = {"MetalNova": "9.00", "Aços Sul": "10.50"}

    def decide(
        histories: dict[str, SupplierPerformance], policy: ComparisonPolicy
    ) -> tuple[str | None, dict[str, list[str]], list[str]]:
        quotes = [
            replace(offer(line, name, prices[name]), performance={line.id: history})
            for name, history in histories.items()
        ]
        raw = compute_comparison(
            rfq_id=uuid4(), base_currency="EUR", lines=[line], quotes=quotes, rates={}, today=TODAY
        )
        rec = apply_rules(raw, [line], TODAY, policy).recommendations[0]
        return (
            rec.supplier_name,
            {e.supplier_name: e.reasons for e in rec.excluded},
            rec.evidence_gaps,
        )

    insufficient = ["delivery_history_insufficient", "quality_history_insufficient"]
    cases: list[tuple[ComparisonPolicy, str, dict[str, list[str]]]] = [
        (ComparisonPolicy(), "MetalNova", {}),
        (
            ComparisonPolicy(minimum_on_time_rate=D("0.9")),
            "Aços Sul",
            {"MetalNova": ["delivery_history_below_policy"]},
        ),
        (
            ComparisonPolicy(minimum_accepted_quantity_rate=D("0.95")),
            "Aços Sul",
            {"MetalNova": ["quality_history_below_policy"]},
        ),
        (ComparisonPolicy(preference="reliability_first"), "Aços Sul", {}),
        (
            ComparisonPolicy(minimum_history_samples=5, allow_unknown_history=False),
            "MetalNova",
            {"Aços Sul": insufficient},
        ),
    ]
    for policy, winner, excluded in cases:
        from_assessment = decide(measured, policy)
        from_imports = decide(imported, policy)
        assert from_assessment[:2] == from_imports[:2] == (winner, excluded)
        # Only the basis note sets them apart, as an evidence gap of the recommendation.
        assert BASIS_ISSUE in from_assessment[2]
        assert [gap for gap in from_assessment[2] if gap != BASIS_ISSUE] == from_imports[2]
