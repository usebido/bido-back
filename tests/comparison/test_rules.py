"""Sector rules and the per-line recommendation. Pure."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

from bido_back.comparison.compute import (
    Comparison,
    FieldInfo,
    QuotedLine,
    RequestedLine,
    SupplierQuote,
    compute_comparison,
)
from bido_back.comparison.rules import (
    apply_rules,
    qualification_codes,
    qualification_confirmed,
)

TODAY = date(2026, 10, 7)
D = Decimal


def requested(
    *quantities: int,
    qualification: str | None = None,
    need_by: date | None = None,
    mpn: str = "GCM188R71H104KA57D",
    max_unit_price: str | None = None,
) -> RequestedLine:
    return RequestedLine(
        id=uuid4(),
        position=1,
        description="MLCC 100nF",
        mpn=mpn,
        unit="pcs",
        quantities=tuple(D(q) for q in quantities) or (D(1000),),
        incoterm="DAP",
        need_by=need_by,
        qualification=qualification,
        max_unit_price=D(max_unit_price) if max_unit_price is not None else None,
    )


def offer(
    line: RequestedLine,
    name: str,
    price: str | None,
    *,
    supplier_type: str = "authorized_distributor",
    lead_time_days: int | None = 28,
    valid_until: date | None = date(2026, 12, 31),
    is_alternative: bool = False,
    qualification: str | None = None,
    moq: str | None = None,
    low: tuple[str, ...] = (),
    currency_low: bool = False,
    tooling: str | None = None,
    currency: str = "EUR",
) -> SupplierQuote:
    return SupplierQuote(
        invite_id=uuid4(),
        supplier_id=uuid4(),
        supplier_name=name,
        proposal_id=uuid4(),
        version=1,
        currency=currency,
        valid_until=valid_until,
        incoterm="DAP",
        supplier_type=supplier_type,
        fields={"currency": FieldInfo(confidence=0.5)} if currency_low else {},
        lines=(
            QuotedLine(
                rfq_line_id=line.id,
                offered_mpn="OTHER-PART" if is_alternative else line.mpn,
                is_alternative=is_alternative,
                min_quantity=D(1),
                unit_price=D(price) if price is not None else None,
                unit="pcs",
                moq=D(moq) if moq is not None else None,
                lead_time_days=lead_time_days,
                qualification=qualification,
                fields={name: FieldInfo(confidence=0.4) for name in low},
                tooling_cost=D(tooling) if tooling is not None else None,
            ),
        ),
    )


def compare(line: RequestedLine, *quotes: SupplierQuote, base: str = "EUR") -> Comparison:
    comparison = compute_comparison(
        rfq_id=uuid4(),
        base_currency=base,
        lines=[line],
        quotes=quotes,
        rates={},
        today=TODAY,
    )
    return apply_rules(comparison, [line], TODAY)


def rules_of(comparison: Comparison, name: str) -> list[str]:
    column = next(s for s in comparison.suppliers if s.supplier_name == name)
    return [*column.rules, *column.cells[0].rules]


# --- Qualification ------------------------------------------------------------------


def test_qualification_codes_are_normalized() -> None:
    assert qualification_codes("aec q-200, AEC-Q100 grade 1") == {"AEC-Q200", "AEC-Q100"}
    assert qualification_codes("RoHS") == frozenset()


def test_qualification_confirmation() -> None:
    assert qualification_confirmed(None, None)
    assert qualification_confirmed("AEC-Q200", "qualified per AEC Q200")
    assert not qualification_confirmed("AEC-Q200", "AEC-Q100")
    assert not qualification_confirmed("AEC-Q200", None)
    assert qualification_confirmed("IATF 16949", "plant certified IATF-16949")


# --- Rules ----------------------------------------------------------------------------


def test_automotive_line_rules_by_supplier_type() -> None:
    line = requested(qualification="AEC-Q200")
    result = compare(
        line,
        offer(line, "Authorized", "0.010", qualification="AEC-Q200"),
        offer(line, "Distributor", "0.011", supplier_type="distributor", qualification="AEC-Q200"),
        offer(line, "Broker", "0.005", supplier_type="reseller", qualification="AEC-Q200"),
    )

    assert rules_of(result, "Authorized") == []
    assert rules_of(result, "Distributor") == ["authorization_unverified"]
    assert rules_of(result, "Broker") == ["unauthorized_source"]


def test_non_automotive_line_does_not_check_the_source() -> None:
    line = requested()
    result = compare(line, offer(line, "Broker", "0.005", supplier_type="reseller"))

    assert rules_of(result, "Broker") == []


def test_unconfirmed_qualification_and_alternative() -> None:
    line = requested(qualification="AEC-Q200")
    result = compare(
        line,
        offer(line, "Silent", "0.010"),
        offer(line, "Alt", "0.009", is_alternative=True, qualification="AEC-Q200"),
    )

    assert rules_of(result, "Silent") == ["qualification_unconfirmed"]
    assert rules_of(result, "Alt") == ["alternative_needs_approval"]


def test_need_by_validity_and_moq_rules() -> None:
    line = requested(1000, need_by=date(2026, 11, 30))
    result = compare(
        line,
        offer(line, "Late", "0.01", lead_time_days=70, moq="5000"),
        offer(line, "Expired", "0.01", valid_until=date(2026, 10, 1)),
        offer(line, "Short", "0.01", valid_until=date(2026, 11, 1)),
    )

    assert rules_of(result, "Late") == ["late_for_need_by", "moq_above_quantity"]
    assert rules_of(result, "Expired") == ["validity_expired"]
    assert rules_of(result, "Short") == ["validity_ends_before_need_by"]


# --- Recommendation -------------------------------------------------------------------


def test_recommends_the_cheapest_unblocked_offer_and_explains_exclusions() -> None:
    line = requested(1000, qualification="AEC-Q200")
    result = compare(
        line,
        offer(line, "Rhein", "0.012", qualification="AEC-Q200"),
        offer(line, "Lyon", "0.010", qualification="AEC-Q200"),
        offer(line, "Broker", "0.004", supplier_type="reseller"),
        offer(line, "Alt", "0.005", is_alternative=True),
    )

    [rec] = result.recommendations
    assert rec.supplier_name == "Lyon"
    assert rec.reasons == ["lowest_effective_price"]
    assert rec.runner_up is not None
    assert rec.runner_up.supplier_name == "Rhein"
    assert rec.runner_up.price_delta_pct == D("20.0")
    assert {e.supplier_name: e.reasons for e in rec.excluded} == {
        "Broker": ["unauthorized_source"],
        "Alt": ["alternative_needs_approval"],
    }
    assert rec.justification.startswith("Lyon has the lowest effective unit price, 0.01 EUR")
    assert "20% below Rhein" in rec.justification
    assert "2 other offer(s) excluded" in rec.justification


def test_low_confidence_never_decides() -> None:
    line = requested()
    result = compare(
        line,
        offer(line, "Unsure price", "0.001", low=("unit_price",)),
        offer(line, "Unsure currency", "0.002", currency_low=True),
        offer(line, "Sure", "0.010"),
    )

    [rec] = result.recommendations
    assert rec.supplier_name == "Sure"
    assert rec.reasons == ["only_eligible_offer"]
    assert {e.supplier_name: e.reasons for e in rec.excluded} == {
        "Unsure price": ["low_confidence"],
        "Unsure currency": ["low_confidence_currency"],
    }


def test_tie_broken_by_lead_time() -> None:
    line = requested()
    result = compare(
        line,
        offer(line, "Slow", "0.010", lead_time_days=56),
        offer(line, "Fast", "0.010", lead_time_days=14),
    )

    [rec] = result.recommendations
    assert rec.supplier_name == "Fast"
    assert rec.reasons == ["lowest_effective_price", "tie_broken_by_lead_time"]
    assert "shorter lead time" in rec.justification


def test_on_time_offers_win_over_cheaper_late_ones() -> None:
    line = requested(need_by=date(2026, 11, 30))
    result = compare(
        line,
        offer(line, "Cheap but late", "0.008", lead_time_days=90),
        offer(line, "On time", "0.010", lead_time_days=21),
    )

    [rec] = result.recommendations
    assert rec.supplier_name == "On time"
    assert "no_offer_meets_need_by" not in rec.reasons


def test_when_every_offer_is_late_the_cheapest_is_recommended_and_says_so() -> None:
    line = requested(need_by=date(2026, 10, 20))
    result = compare(
        line,
        offer(line, "A", "0.012", lead_time_days=42),
        offer(line, "B", "0.010", lead_time_days=56),
    )

    [rec] = result.recommendations
    assert rec.supplier_name == "B"
    assert rec.reasons == ["lowest_effective_price", "no_offer_meets_need_by"]
    assert "misses the need-by date 2026-10-20" in rec.justification


def test_no_eligible_offer() -> None:
    line = requested()
    empty = compare(line)
    blocked = compare(line, offer(line, "Expired", "0.01", valid_until=date(2026, 9, 1)))

    assert empty.recommendations[0].invite_id is None
    assert empty.recommendations[0].reasons == ["no_eligible_offer"]
    assert "no offers yet" in empty.recommendations[0].justification
    rec = blocked.recommendations[0]
    assert rec.supplier_name is None
    assert rec.excluded[0].reasons == ["validity_expired"]


def test_one_recommendation_per_line_and_tier() -> None:
    line = requested(1000, 10000)
    result = compare(line, offer(line, "Lyon", "0.010"))

    assert [(r.tier, r.quantity) for r in result.recommendations] == [(0, D(1000)), (1, D(10000))]


def test_a_broker_never_wins_against_an_authorized_offer() -> None:
    line = requested()
    result = compare(
        line,
        offer(line, "Broker", "0.004", supplier_type="reseller"),
        offer(line, "Lyon", "0.010"),
        offer(line, "Maker", "0.012", supplier_type="manufacturer"),
    )

    [rec] = result.recommendations
    assert rec.supplier_name == "Lyon"
    assert rec.advisories == []
    assert {e.supplier_name: e.reasons for e in rec.excluded} == {
        "Broker": ["reseller_not_preferred"]
    }
    # Still in the comparison, with its price.
    assert any(s.supplier_name == "Broker" for s in result.suppliers)


def test_a_broker_can_win_against_unverified_distributors() -> None:
    # Only authorized distributors and manufacturers push brokers out.
    line = requested()
    result = compare(
        line,
        offer(line, "Broker", "0.004", supplier_type="reseller"),
        offer(line, "Somebody", "0.010", supplier_type="distributor"),
    )

    [rec] = result.recommendations
    assert rec.supplier_name == "Broker"
    assert rec.advisories == ["only_unauthorized_offers"]


def test_a_broker_alone_wins_and_says_so() -> None:
    line = requested()
    result = compare(line, offer(line, "Broker", "0.004", supplier_type="reseller"))

    [rec] = result.recommendations
    assert rec.supplier_name == "Broker"
    assert rec.reasons == ["only_eligible_offer"]
    assert rec.advisories == ["only_unauthorized_offers"]
    assert "Only unauthorized resellers offered this line" in rec.justification


# --- The buyer's maximum is a unit price ------------------------------------------------


def test_rfq9_one_off_charges_above_the_maximum_are_a_warning_not_a_blocker() -> None:
    # RFQ-9 (2026-10-13): maximum 3.05 per piece, 2,000 pieces at 2.90 plus tooling 1,250:
    # the effective price is 3.525, above the maximum; the unit price is within it.
    line = requested(2000, max_unit_price="3.05")
    result = compare(
        line, offer(line, "Torino", "2.90", tooling="1250", currency="BRL"), base="BRL"
    )

    [rec] = result.recommendations
    assert rec.supplier_name == "Torino"
    assert (rec.unit_price_base, rec.tooling_base, rec.effective_unit_price_base) == (
        D("2.90"),
        D("1250.00"),
        D("3.525"),
    )
    assert "effective_above_max" in rec.advisories
    assert rec.readiness == "needs_review"
    assert "with one-off charges the effective price per unit is above it" in rec.justification
    cell = result.suppliers[0].cells[0]
    assert (cell.buyer_limit_issues, cell.buyer_limit_warnings) == ([], ["effective_above_max"])


def test_a_unit_price_above_the_maximum_is_never_recommended() -> None:
    line = requested(2000, max_unit_price="3.05")
    result = compare(line, offer(line, "Torino", "3.10", tooling="1250"))

    [rec] = result.recommendations
    assert rec.invite_id is None and rec.readiness == "no_feasible_offer"
    assert rec.excluded[0].reasons == ["above_buyer_maximum"]


def test_without_one_off_charges_the_maximum_reads_as_before() -> None:
    line = requested(2000, max_unit_price="3.05")
    within = compare(line, offer(line, "Torino", "2.90"))
    above = compare(line, offer(line, "Torino", "3.06"))

    [rec] = within.recommendations
    assert (rec.supplier_name, rec.unit_price_base, rec.tooling_base) == (
        "Torino",
        D("2.90"),
        None,
    )
    assert "effective_above_max" not in rec.advisories
    assert within.suppliers[0].cells[0].buyer_limit_warnings == []
    assert above.recommendations[0].excluded[0].reasons == ["above_buyer_maximum"]


def test_offers_are_still_ranked_by_their_effective_price() -> None:
    # Cheaper per piece, dearer with its tooling: the other offer wins the ranking.
    line = requested(2000, max_unit_price="3.05")
    result = compare(
        line,
        offer(line, "Torino", "2.90", tooling="1250"),
        offer(line, "Milano", "3.00"),
    )

    [rec] = result.recommendations
    assert (rec.supplier_name, rec.effective_unit_price_base) == ("Milano", D("3.00"))
    assert "effective_above_max" not in rec.advisories
    assert [a.supplier_name for a in rec.alternatives] == ["Torino"]
