"""Buyer policy, evidence coverage and landed-cost uncertainty are reproducible."""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

from bido_back.comparison.compute import SupplierQuote, compute_comparison
from bido_back.comparison.policy import (
    ComparisonPolicy,
    PurchaseObservation,
    SupplierPerformance,
    summarize_performance,
)
from bido_back.comparison.rules import apply_rules
from tests.comparison.test_rules import TODAY, offer, requested

D = Decimal


def performance(on_time: str, quality: str, samples: int = 5) -> SupplierPerformance:
    return SupplierPerformance(
        observations=samples,
        delivery_samples=samples,
        quality_samples=samples,
        on_time_rate=D(on_time),
        accepted_quantity_rate=D(quality),
        latest_observation_on=TODAY - timedelta(days=3),
        evidence_po_line_ids=[uuid4() for _ in range(samples)],
    )


def test_buyer_policy_selects_reliability_and_exposes_cheaper_alternative() -> None:
    line = requested()
    cheap = replace(offer(line, "Cheap", "1"), performance={line.id: performance("0.4", "0.9")})
    reliable = replace(offer(line, "Reliable", "1.2"), performance={line.id: performance("1", "1")})
    raw = compute_comparison(
        rfq_id=uuid4(),
        base_currency="EUR",
        lines=[line],
        quotes=[cheap, reliable],
        rates={},
        today=TODAY,
    )
    assert apply_rules(raw, [line], TODAY).recommendations[0].supplier_name == "Cheap"
    result = apply_rules(raw, [line], TODAY, ComparisonPolicy(preference="reliability_first"))
    rec = result.recommendations[0]
    assert rec.supplier_name == "Reliable"
    assert "reliability_first" in rec.policy_reasons
    assert "lowest_effective_price" not in rec.reasons
    assert rec.alternatives[0].supplier_name == "Cheap"
    assert "lower_quoted_cost" in rec.alternatives[0].tradeoffs
    assert rec.performance.delivery_samples == 5
    assert len(rec.performance.evidence_po_line_ids) == 5
    assert rec.proposal_id == reliable.proposal_id


def test_thresholds_and_unknown_history_never_become_perfect_scores() -> None:
    line = requested()
    quotes = [
        replace(offer(line, "Poor", "1"), performance={line.id: performance("0.5", "1")}),
        offer(line, "Unknown", "0.9"),
        replace(offer(line, "One receipt", "0.8"), performance={line.id: performance("1", "1", 1)}),
    ]
    raw = compute_comparison(
        rfq_id=uuid4(),
        base_currency="EUR",
        lines=[line],
        quotes=quotes,
        rates={},
        today=TODAY,
    )
    policy = ComparisonPolicy(minimum_on_time_rate=D("0.9"), allow_unknown_history=False)
    result = apply_rules(raw, [line], TODAY, policy)
    assert result.readiness == "no_feasible_offer"
    rec = result.recommendations[0]
    assert rec.invite_id is None
    excluded = {item.supplier_name: item.reasons for item in rec.excluded}
    assert excluded["Poor"] == ["delivery_history_below_policy"]
    assert "delivery_history_insufficient" in excluded["Unknown"]
    assert "delivery_history_insufficient" in excluded["One receipt"]
    # A single observed success is shown, but it cannot outrank five known poor outcomes.
    ranked = apply_rules(raw, [line], TODAY, ComparisonPolicy(preference="reliability_first"))
    assert ranked.recommendations[0].supplier_name == "Poor"


def test_history_excludes_stale_future_and_incomplete_observations() -> None:
    base = PurchaseObservation(
        po_line_id=uuid4(),
        ordered_at=TODAY - timedelta(days=30),
        quantity=D(10),
        promised_at=TODAY - timedelta(days=10),
        received_at=TODAY - timedelta(days=12),
        received_qty=D(10),
        rejected_qty=D(1),
    )
    partial = replace(base, po_line_id=uuid4(), received_qty=D(5), rejected_qty=D(0))
    unknown = replace(base, po_line_id=uuid4(), received_qty=None)
    stale = replace(
        base, po_line_id=uuid4(), ordered_at=date(2020, 1, 1), received_at=date(2020, 2, 1)
    )
    future = replace(base, po_line_id=uuid4(), ordered_at=TODAY + timedelta(days=2))
    result = summarize_performance(
        [base, partial, unknown, stale, future], today=TODAY, window_days=365
    )
    assert result.observations == 3
    assert result.delivery_samples == 2
    assert result.on_time_rate == D("0.5")
    assert result.quality_samples == 2
    assert result.accepted_quantity_rate == D(14) / D(15)
    assert result.excluded_stale_observations == 1
    assert stale.po_line_id not in result.evidence_po_line_ids
    assert future.po_line_id not in result.evidence_po_line_ids


def test_cost_basis_freight_and_unknown_tax_are_not_fabricated() -> None:
    line = requested(40)
    original = offer(line, "A", "200")
    quoted_line = replace(
        original.lines[0],
        price_basis_quantity=D(100),
        tooling_cost=D(0),
        freight_cost=D(20),
    )
    quoted = replace(original, lines=(quoted_line,))
    raw = compute_comparison(
        rfq_id=uuid4(),
        base_currency="EUR",
        lines=[line],
        quotes=[quoted],
        rates={},
        today=TODAY,
    )
    cell = raw.suppliers[0].cells[0]
    assert cell.unit_price == D(200)  # Original evidence retained.
    assert cell.unit_price_base == D(2)
    assert cell.line_total_base == D(80)
    assert cell.known_total_base == D(100)
    assert cell.landed_total_base is None
    assert cell.cost_gaps == ["tax_treatment_unknown"]
    result = apply_rules(raw, [line], TODAY, ComparisonPolicy(require_complete_costs=True))
    assert result.readiness == "no_feasible_offer"
    assert "complete_costs_required" in result.recommendations[0].excluded[0].reasons


def test_landed_cost_policy_uses_evidenced_freight_and_returns_ready_package() -> None:
    line = requested(40)
    quotes: list[SupplierQuote] = []
    for name, price, freight in (("Cheap goods", "1", "100"), ("Delivered", "2", "0")):
        initial = offer(line, name, price)
        quotes.append(
            replace(
                initial,
                payment_terms="Net 30",
                performance={line.id: performance("1", "1")},
                lines=(
                    replace(
                        initial.lines[0],
                        tooling_cost=D(0),
                        freight_cost=D(freight),
                        tax_rate_percent=D(0),
                    ),
                ),
            )
        )
    raw = compute_comparison(
        rfq_id=uuid4(),
        base_currency="EUR",
        lines=[line],
        quotes=quotes,
        rates={},
        today=TODAY,
    )
    # Complete evidence makes landed cost the natural basis even under defaults.
    for policy in (ComparisonPolicy(), ComparisonPolicy(require_complete_costs=True)):
        result = apply_rules(raw, [line], TODAY, policy)
        assert result.recommendations[0].supplier_name == "Delivered"
    rec = result.recommendations[0]
    assert rec.supplier_name == "Delivered"
    assert rec.landed_total_base == D(80)
    assert result.readiness == "ready_for_buyer_review"
    assert result.covered_line_tiers == result.requested_line_tiers == 1


def test_bundle_conditions_are_excluded_and_partial_coverage_is_explicit() -> None:
    first, second = requested(), requested()
    ordinary = offer(first, "A", "1")
    bundle = replace(
        offer(second, "B", "0.8"),
        lines=(
            replace(
                offer(second, "B", "0.8").lines[0],
                conditional_terms="Only with the full 3-item bundle",
            ),
        ),
    )
    raw = compute_comparison(
        rfq_id=uuid4(),
        base_currency="EUR",
        lines=[first, second],
        quotes=[ordinary, bundle],
        rates={},
        today=TODAY,
    )
    result = apply_rules(raw, [first, second], TODAY)
    assert result.covered_line_tiers == 1
    assert result.requested_line_tiers == 2
    assert result.readiness == "needs_review"
    excluded = result.recommendations[1].excluded
    assert any("conditional_terms_require_review" in e.reasons for e in excluded)


def test_positive_tax_rate_does_not_invent_taxable_basis() -> None:
    line = requested(10)
    original = offer(line, "A", "10")
    q = replace(
        original,
        lines=(
            replace(
                original.lines[0], tooling_cost=D(0), freight_cost=D(5), tax_rate_percent=D(20)
            ),
        ),
    )
    result = compute_comparison(
        rfq_id=uuid4(), base_currency="EUR", lines=[line], quotes=[q], rates={}, today=TODAY
    )
    cell = result.suppliers[0].cells[0]
    assert cell.known_total_base == D(105)
    assert cell.landed_total_base is None
    assert cell.cost_gaps == ["taxable_basis_unknown"]
