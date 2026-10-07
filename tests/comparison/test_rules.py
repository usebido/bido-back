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
) -> SupplierQuote:
    return SupplierQuote(
        invite_id=uuid4(),
        supplier_id=uuid4(),
        supplier_name=name,
        proposal_id=uuid4(),
        version=1,
        currency="EUR",
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
            ),
        ),
    )


def compare(line: RequestedLine, *quotes: SupplierQuote) -> Comparison:
    comparison = compute_comparison(
        rfq_id=uuid4(),
        base_currency="EUR",
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
