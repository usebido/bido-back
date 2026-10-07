"""Comparison math: price breaks per tier, FX, tooling, units, flags, totals. Pure."""

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from bido_back.comparison.compute import (
    FieldInfo,
    QuotedLine,
    Rate,
    RequestedLine,
    SupplierQuote,
    compute_comparison,
    normalize_unit,
    pick_break,
    to_base,
)

TODAY = date(2026, 10, 6)
D = Decimal
RATES = {
    "USD": Rate("USD", D("1.25"), date(2026, 10, 5)),
    "GBP": Rate("GBP", D("0.8"), date(2026, 10, 5)),
}


def requested(*quantities: int, unit: str = "pcs", incoterm: str | None = None) -> RequestedLine:
    return RequestedLine(
        id=uuid4(),
        position=1,
        description="MLCC",
        mpn="GRM188",
        unit=unit,
        quantities=tuple(D(q) for q in quantities),
        incoterm=incoterm,
    )


def quote(
    *lines: QuotedLine,
    currency: str | None = "EUR",
    valid_until: date | None = date(2026, 12, 31),
    incoterm: str | None = "DAP",
    fields: dict[str, FieldInfo] | None = None,
) -> SupplierQuote:
    return SupplierQuote(
        invite_id=uuid4(),
        supplier_id=uuid4(),
        supplier_name="Rhein",
        proposal_id=uuid4(),
        version=2,
        currency=currency,
        valid_until=valid_until,
        incoterm=incoterm,
        lines=lines,
        fields=fields or {},
    )


def brk(line_id: UUID, min_qty: str | None, price: str | None, **kw: object) -> QuotedLine:
    values: dict[str, object] = {
        "rfq_line_id": line_id,
        "offered_mpn": "GRM188",
        "min_quantity": D(min_qty) if min_qty is not None else None,
        "unit_price": D(price) if price is not None else None,
        "unit": "pcs",
        "lead_time_days": 28,
    }
    values.update(kw)
    return QuotedLine(**values)  # pyright: ignore[reportArgumentType]


def test_picks_the_largest_break_not_above_the_tier() -> None:
    line = requested(1000)
    breaks = [brk(line.id, "1", "0.10"), brk(line.id, "1000", "0.08"), brk(line.id, "5000", "0.05")]

    assert pick_break(breaks, D(999)) == (breaks[0], True)
    assert pick_break(breaks, D(1000)) == (breaks[1], True)
    assert pick_break(breaks, D(7000)) == (breaks[2], True)
    assert pick_break(breaks[1:], D(10)) == (breaks[1], False)
    assert pick_break([], D(10)) is None


def test_tiers_totals_and_tooling_amortization() -> None:
    line = requested(1000, 5000)
    q = quote(
        brk(line.id, "1000", "0.08", tooling_cost=D("200")),
        brk(line.id, "5000", "0.05", tooling_cost=D("200")),
    )

    result = compute_comparison(
        rfq_id=uuid4(), base_currency="EUR", lines=[line], quotes=[q], rates=RATES, today=TODAY
    )
    [column] = result.suppliers
    first, second = column.cells
    assert (first.quantity, first.unit_price_base, first.tooling_per_unit_base) == (
        D(1000),
        D("0.080000"),
        D("0.200000"),
    )
    assert first.effective_unit_price_base == D("0.280000")
    assert first.line_total_base == D("280.00")  # 0.08 * 1000 + 200
    assert second.line_total_base == D("450.00")  # 0.05 * 5000 + 200
    assert second.tooling_per_unit_base == D("0.040000")
    assert [(t.tier, t.total_base, t.complete) for t in column.totals] == [
        (0, D("280.00"), True),
        (1, D("450.00"), True),
    ]
    assert first.flags == [] and column.flags == []
    assert column.max_lead_time_days == 28


def test_converts_to_the_base_currency_with_ecb_rates() -> None:
    line = requested(100)
    q = quote(brk(line.id, "1", "1.25", tooling_cost=D("12.5")), currency="usd")

    result = compute_comparison(
        rfq_id=uuid4(), base_currency="EUR", lines=[line], quotes=[q], rates=RATES, today=TODAY
    )
    [cell] = result.suppliers[0].cells
    assert cell.currency == "USD" and cell.unit_price == D("1.25")
    assert cell.unit_price_base == D("1.000000")  # 1.25 USD / 1.25 USD per EUR
    assert cell.line_total_base == D("110.00")  # 100 * 1 + 10
    assert [r.currency for r in result.fx_rates] == ["USD"]


def test_to_base_pivots_through_eur() -> None:
    assert to_base(D("10"), "USD", "GBP", RATES) == D("6.4")  # 10 / 1.25 * 0.8
    assert to_base(D("10"), "EUR", "EUR", {}) == D("10")
    assert to_base(D("10"), "JPY", "EUR", RATES) is None


def test_missing_currency_or_rate_leaves_base_empty_and_flagged() -> None:
    line = requested(10)
    no_rate = quote(brk(line.id, "1", "100"), currency="JPY")
    no_currency = quote(brk(line.id, "1", "100"), currency=None)

    result = compute_comparison(
        rfq_id=uuid4(),
        base_currency="EUR",
        lines=[line],
        quotes=[no_rate, no_currency],
        rates=RATES,
        today=TODAY,
    )
    a, b = (s.cells[0] for s in result.suppliers)
    assert a.unit_price == D("100") and a.unit_price_base is None and "fx_rate_missing" in a.flags
    assert b.unit_price_base is None and "missing_currency" in b.flags
    assert "missing_currency" in result.suppliers[1].flags
    assert result.suppliers[0].totals[0].total_base is None
    assert result.suppliers[0].totals[0].complete is False


def test_unit_mismatch_is_flagged_and_not_totalled() -> None:
    line = requested(1000)
    reel = quote(brk(line.id, "1", "40", unit="reel"))
    pieces = quote(brk(line.id, "1", "0.04", unit="Stk."))

    result = compute_comparison(
        rfq_id=uuid4(),
        base_currency="EUR",
        lines=[line],
        quotes=[reel, pieces],
        rates=RATES,
        today=TODAY,
    )
    mismatch, same = (s.cells[0] for s in result.suppliers)
    assert "unit_mismatch" in mismatch.flags
    assert mismatch.unit_price_base == D("40.000000")  # shown, never hidden
    assert mismatch.line_total_base is None
    assert "unit_mismatch" not in same.flags and same.line_total_base == D("40.00")
    assert normalize_unit(" PCS ") == normalize_unit("each") == "pcs"


def test_flags_alternative_review_low_confidence_lead_time_moq_and_minimums() -> None:
    line = requested(100, 5000, incoterm="DAP")
    q = quote(
        brk(
            line.id,
            "500",
            "0.10",
            offered_mpn="CL10B104",
            is_alternative=True,
            needs_review=True,
            lead_time_days=None,
            moq=D("1000"),
            fields={"unit_price": FieldInfo(confidence=0.55)},
        ),
        incoterm="EXW",
        valid_until=date(2026, 9, 30),
        fields={
            "currency": FieldInfo(confidence=0.95),
            "valid_until": FieldInfo(needs_review=True),
        },
    )

    result = compute_comparison(
        rfq_id=uuid4(), base_currency="EUR", lines=[line], quotes=[q], rates=RATES, today=TODAY
    )
    [column] = result.suppliers
    below, above = column.cells
    assert set(below.flags) >= {
        "alternative_part",
        "needs_review",
        "low_confidence",
        "missing_lead_time",
        "below_min_quantity",
        "below_moq",
        "incoterm_differs",
    }
    assert below.unit_price == D("0.10") and below.line_total_base is None
    assert below.low_confidence_fields == ["unit_price"]
    assert "below_min_quantity" not in above.flags and "below_moq" not in above.flags
    assert above.line_total_base == D("500.00")  # flagged values still add up
    assert set(column.flags) == {"validity_expired", "low_confidence", "needs_review"}
    assert column.low_confidence_fields == ["valid_until"]
    assert [t.complete for t in column.totals] == [False, True]


def test_exact_part_is_preferred_over_an_alternative() -> None:
    line = requested(100)
    q = quote(
        brk(line.id, "1", "0.05", offered_mpn="ALT", is_alternative=True),
        brk(line.id, "1", "0.09"),
    )
    result = compute_comparison(
        rfq_id=uuid4(), base_currency="EUR", lines=[line], quotes=[q], rates=RATES, today=TODAY
    )
    [cell] = result.suppliers[0].cells
    assert cell.unit_price == D("0.09") and not cell.is_alternative


@pytest.mark.parametrize(
    ("quoted", "flag"), [(None, "missing_incoterm"), ("dap", None), ("FCA", "incoterm_differs")]
)
def test_incoterm_compared_with_the_requested_one(quoted: str | None, flag: str | None) -> None:
    line = requested(10, incoterm="DAP")
    result = compute_comparison(
        rfq_id=uuid4(),
        base_currency="EUR",
        lines=[line],
        quotes=[quote(brk(line.id, "1", "1"), incoterm=quoted)],
        rates=RATES,
        today=TODAY,
    )
    flags = result.suppliers[0].cells[0].flags
    assert {f for f in flags if "incoterm" in f} == ({flag} if flag else set())


def test_lines_without_a_quote_make_totals_incomplete() -> None:
    a, b = requested(10), requested(10)
    result = compute_comparison(
        rfq_id=uuid4(),
        base_currency="EUR",
        lines=[a, b],
        quotes=[quote(brk(a.id, "1", "2"))],
        rates=RATES,
        today=TODAY,
    )
    [column] = result.suppliers
    missing = next(c for c in column.cells if c.rfq_line_id == b.id)
    assert missing.flags == ["no_quote_for_line", "missing_price"]
    [total] = column.totals
    assert (total.total_base, total.lines_priced, total.lines_requested, total.complete) == (
        D("20.00"),
        1,
        2,
        False,
    )


def test_corrected_values_are_marked_and_never_low_confidence() -> None:
    line = requested(1000)
    line_id = uuid4()
    q = quote(
        brk(
            line.id,
            "1",
            "0.10",
            id=line_id,
            fields={
                "unit_price": FieldInfo(confidence=0.4, needs_review=True, corrected=True),
                "lead_time_days": FieldInfo(confidence=0.5),
            },
        ),
        fields={"currency": FieldInfo(confidence=0.3, corrected=True)},
    )

    result = compute_comparison(
        rfq_id=uuid4(), base_currency="EUR", lines=[line], quotes=[q], rates=RATES, today=TODAY
    )
    [column] = result.suppliers
    [cell] = column.cells

    assert cell.proposal_line_id == line_id
    assert cell.corrected_fields == ["unit_price"]
    assert cell.low_confidence_fields == ["lead_time_days"]
    assert column.corrected_fields == ["currency"]
    assert "low_confidence" not in column.flags


def _line_without_mpn() -> RequestedLine:
    return RequestedLine(
        id=uuid4(),
        position=1,
        description="Copper wire",
        mpn=None,
        unit="pcs",
        quantities=(D(100),),
    )


def test_unrequested_part_number_never_makes_an_offer_doubtful() -> None:
    line = _line_without_mpn()
    q = quote(
        brk(
            line.id,
            "1",
            "0.10",
            offered_mpn="CU-0.8",
            needs_review=True,
            review_issues=(),
            fields={
                "offered_mpn": FieldInfo(confidence=0.4, needs_review=True),
                "unit_price": FieldInfo(confidence=0.95),
            },
        )
    )

    [column] = compute_comparison(
        rfq_id=uuid4(), base_currency="EUR", lines=[line], quotes=[q], rates=RATES, today=TODAY
    ).suppliers
    [cell] = column.cells
    assert "needs_review" not in cell.flags and "low_confidence" not in cell.flags
    assert cell.low_confidence_fields == []
    assert cell.line_total_base == D("10.00")


def test_other_doubts_still_count_when_no_part_number_was_requested() -> None:
    line = _line_without_mpn()
    doubtful_price = brk(
        line.id,
        "1",
        "0.10",
        needs_review=True,
        review_issues=(),
        fields={
            "offered_mpn": FieldInfo(confidence=0.4, needs_review=True),
            "unit_price": FieldInfo(confidence=0.5, needs_review=True),
        },
    )
    line_issue = brk(
        line.id,
        "1",
        "0.10",
        needs_review=True,
        review_issues=("currency_unverified",),
        fields={"offered_mpn": FieldInfo(confidence=0.4, needs_review=True)},
    )
    unknown_issues = brk(
        line.id,
        "1",
        "0.10",
        needs_review=True,
        fields={"offered_mpn": FieldInfo(confidence=0.4, needs_review=True)},
    )

    for offer, low in ((doubtful_price, ["unit_price"]), (line_issue, []), (unknown_issues, [])):
        [column] = compute_comparison(
            rfq_id=uuid4(),
            base_currency="EUR",
            lines=[line],
            quotes=[quote(offer)],
            rates=RATES,
            today=TODAY,
        ).suppliers
        [cell] = column.cells
        assert "needs_review" in cell.flags
        assert cell.low_confidence_fields == low


def test_requested_part_number_keeps_its_confidence_check() -> None:
    line = requested(100)
    q = quote(
        brk(
            line.id,
            "1",
            "0.10",
            needs_review=True,
            review_issues=(),
            fields={"offered_mpn": FieldInfo(confidence=0.4, needs_review=True)},
        )
    )

    [column] = compute_comparison(
        rfq_id=uuid4(), base_currency="EUR", lines=[line], quotes=[q], rates=RATES, today=TODAY
    ).suppliers
    [cell] = column.cells
    assert {"needs_review", "low_confidence"} <= set(cell.flags)
    assert cell.low_confidence_fields == ["offered_mpn"]
