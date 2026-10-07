"""Sector rules and the per-line recommendation, on top of a computed comparison.

Deterministic: no model is asked anything here. Rules mark what a buyer of
automotive electronics must check; the recommendation picks, per requested
line and quantity tier, the cheapest offer that nothing blocks.

Rules (`RuleFlag`), per cell unless noted:

- `alternative_needs_approval` (blocking): the supplier offers another part
  than the one requested. A person must accept the alternative first.
- `unauthorized_source` (blocking): the line requires an automotive
  qualification (AEC-Q...) and the supplier is a reseller/broker: counterfeit
  risk. Authorized distributors and manufacturers pass.
- `authorization_unverified`: the same line, from a supplier recorded only
  as `distributor` (authorization not confirmed) or `other`.
- `qualification_unconfirmed`: the line requires a qualification the quote
  does not state. Informational: an exact, authorized part carries its
  manufacturer's qualification.
- `late_for_need_by`: today + lead time is after the line's need-by date.
  Soft: on-time offers are preferred; a late one is recommended only when
  every eligible offer is late.
- `moq_above_quantity`: the supplier's MOQ is above the requested quantity.
  Informational (the buyer may take more).
- `validity_expired` (supplier, blocking): the quote's validity has passed.
- `validity_ends_before_need_by` (supplier): the quote expires before the
  earliest need-by date. Informational: order before it expires.

Blocking for the recommendation, besides the blocking rules: anything that
makes the price untrustworthy or not comparable (no price, no currency or FX
rate, unit mismatch, quantity below every price break, needs review, a
low-confidence field on the offer, a low-confidence currency). Low-confidence
values never decide a recommendation.
"""

import re
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from bido_back.comparison.compute import (
    Cell,
    Comparison,
    ExcludedOffer,
    ReasonCode,
    Recommendation,
    RequestedLine,
    RuleFlag,
    RunnerUp,
    SupplierColumn,
)

_AUTHORIZED: Final = frozenset({"authorized_distributor", "manufacturer"})
_BROKERS: Final = frozenset({"reseller"})
_AEC: Final = re.compile(r"AEC\s*[-_]?\s*Q\s*[-_]?\s*(\d{3})", re.IGNORECASE)

_BLOCKING_FLAGS: Final = frozenset(
    {
        "no_quote_for_line",
        "missing_price",
        "missing_currency",
        "fx_rate_missing",
        "missing_unit",
        "unit_mismatch",
        "below_min_quantity",
        "needs_review",
        "low_confidence",
    }
)
_BLOCKING_RULES: Final[frozenset[RuleFlag]] = frozenset(
    {"alternative_needs_approval", "unauthorized_source", "validity_expired"}
)
_PERCENT: Final = Decimal("0.1")


# --- Qualification -------------------------------------------------------------------


def qualification_codes(text: str | None) -> frozenset[str]:
    """AEC-Q codes in `text`, normalized: "aec q-200" -> {"AEC-Q200"}."""
    if not text:
        return frozenset()
    return frozenset(f"AEC-Q{m.group(1)}" for m in _AEC.finditer(text))


def is_automotive(line: RequestedLine) -> bool:
    return bool(qualification_codes(line.qualification))


def qualification_confirmed(required: str | None, quoted: str | None) -> bool:
    """Whether the quote states every required qualification."""
    if not required or not required.strip():
        return True
    if not quoted:
        return False
    codes = qualification_codes(required)
    if codes:
        return codes <= qualification_codes(quoted)
    # Not an AEC code (e.g. "IATF 16949"): compare letters and digits only.
    return _alnum(required) in _alnum(quoted)


def _alnum(text: str) -> str:
    return "".join(c for c in text.upper() if c.isalnum())


# --- Rules ---------------------------------------------------------------------------


def cell_rules(
    line: RequestedLine, cell: Cell, supplier_type: str | None, today: date
) -> list[RuleFlag]:
    rules: list[RuleFlag] = []
    if cell.is_alternative:
        rules.append("alternative_needs_approval")
    if is_automotive(line):
        kind = (supplier_type or "").strip().lower()
        if kind in _BROKERS:
            rules.append("unauthorized_source")
        elif kind not in _AUTHORIZED:
            rules.append("authorization_unverified")
    if "no_quote_for_line" not in cell.flags and not qualification_confirmed(
        line.qualification, cell.qualification
    ):
        rules.append("qualification_unconfirmed")
    if (
        line.need_by is not None
        and cell.lead_time_days is not None
        and today + timedelta(days=cell.lead_time_days) > line.need_by
    ):
        rules.append("late_for_need_by")
    if cell.moq is not None and cell.moq > cell.quantity:
        rules.append("moq_above_quantity")
    return rules


def supplier_rules(
    column: SupplierColumn, lines: Sequence[RequestedLine], today: date
) -> list[RuleFlag]:
    rules: list[RuleFlag] = []
    if column.valid_until is not None:
        if column.valid_until < today:
            rules.append("validity_expired")
        else:
            need_bys = [line.need_by for line in lines if line.need_by is not None]
            if need_bys and column.valid_until < min(need_bys):
                rules.append("validity_ends_before_need_by")
    return rules


# --- Recommendation ------------------------------------------------------------------


def blockers(cell: Cell, column: SupplierColumn) -> list[str]:
    """Why this offer cannot be recommended; empty when it can."""
    reasons: list[str] = [f for f in cell.flags if f in _BLOCKING_FLAGS]
    reasons.extend(r for r in column.rules if r in _BLOCKING_RULES)
    reasons.extend(r for r in cell.rules if r in _BLOCKING_RULES)
    if "currency" in column.low_confidence_fields:
        reasons.append("low_confidence_currency")
    if cell.effective_unit_price_base is None and not reasons:
        reasons.append("not_comparable")
    return list(dict.fromkeys(reasons))


def recommend(
    line: RequestedLine,
    tier: int,
    quantity: Decimal,
    offers: Sequence[tuple[SupplierColumn, Cell]],
    base_currency: str,
) -> Recommendation:
    eligible: list[tuple[SupplierColumn, Cell]] = []
    excluded: list[ExcludedOffer] = []
    for column, cell in offers:
        blocked = blockers(cell, column)
        if blocked:
            excluded.append(
                ExcludedOffer(
                    invite_id=column.invite_id, supplier_name=column.supplier_name, reasons=blocked
                )
            )
        else:
            eligible.append((column, cell))

    if not eligible:
        detail = "no offers yet" if not offers else f"{len(excluded)} excluded"
        return Recommendation(
            rfq_line_id=line.id,
            tier=tier,
            quantity=quantity,
            invite_id=None,
            supplier_name=None,
            effective_unit_price_base=None,
            line_total_base=None,
            lead_time_days=None,
            reasons=["no_eligible_offer"],
            justification=f"No offer can be recommended for line {line.position} ({detail}).",
            runner_up=None,
            excluded=excluded,
        )

    on_time = [o for o in eligible if "late_for_need_by" not in o[1].rules]
    pool = on_time or eligible
    ranked = sorted(pool, key=_rank)
    winner_column, winner = ranked[0]
    price = _price(winner)

    reasons: list[ReasonCode] = []
    if len(eligible) == 1:
        reasons.append("only_eligible_offer")
    else:
        reasons.append("lowest_effective_price")
        if len(ranked) > 1 and _price(ranked[1][1]) == price:
            reasons.append("tie_broken_by_lead_time")
    if not on_time and line.need_by is not None:
        reasons.append("no_offer_meets_need_by")

    runner_up: RunnerUp | None = None
    if len(ranked) > 1:
        second_column, second = ranked[1]
        second_price = _price(second)
        delta = (
            ((second_price - price) / price * 100).quantize(_PERCENT) if price > 0 else Decimal(0)
        )
        runner_up = RunnerUp(
            invite_id=second_column.invite_id,
            supplier_name=second_column.supplier_name,
            effective_unit_price_base=second_price,
            price_delta_pct=delta,
        )

    return Recommendation(
        rfq_line_id=line.id,
        tier=tier,
        quantity=quantity,
        invite_id=winner_column.invite_id,
        supplier_name=winner_column.supplier_name,
        effective_unit_price_base=price,
        line_total_base=winner.line_total_base,
        lead_time_days=winner.lead_time_days,
        reasons=reasons,
        justification=_justify(
            line, winner_column, winner, runner_up, reasons, excluded, base_currency
        ),
        runner_up=runner_up,
        excluded=excluded,
    )


def _price(cell: Cell) -> Decimal:
    assert cell.effective_unit_price_base is not None  # eligible cells always have one
    return cell.effective_unit_price_base


def _rank(offer: tuple[SupplierColumn, Cell]) -> tuple[Decimal, int, str]:
    column, cell = offer
    lead = cell.lead_time_days if cell.lead_time_days is not None else 10**6
    return (_price(cell), lead, column.supplier_name)


def _justify(
    line: RequestedLine,
    column: SupplierColumn,
    cell: Cell,
    runner_up: RunnerUp | None,
    reasons: Sequence[ReasonCode],
    excluded: Sequence[ExcludedOffer],
    base_currency: str,
) -> str:
    price = f"{_fmt(_price(cell))} {base_currency}"
    if "only_eligible_offer" in reasons:
        text = f"{column.supplier_name} is the only eligible offer, at {price} per unit"
    else:
        text = f"{column.supplier_name} has the lowest effective unit price, {price}"
        if runner_up is not None:
            if runner_up.price_delta_pct > 0:
                text += (
                    f", {_fmt(runner_up.price_delta_pct)}% below {runner_up.supplier_name}"
                    f" ({_fmt(runner_up.effective_unit_price_base)} {base_currency})"
                )
            elif "tie_broken_by_lead_time" in reasons:
                text += f", same as {runner_up.supplier_name} but with the shorter lead time"
    if cell.lead_time_days is not None:
        text += f"; lead time {cell.lead_time_days} days"
    if "no_offer_meets_need_by" in reasons and line.need_by is not None:
        text += f", which misses the need-by date {line.need_by.isoformat()} (no offer meets it)"
    elif line.need_by is not None and cell.lead_time_days is not None:
        text += f", in time for {line.need_by.isoformat()}"
    text += "."
    if excluded:
        text += f" {len(excluded)} other offer(s) excluded."
    return text


def _fmt(value: Decimal) -> str:
    return format(value.normalize(), "f")


# --- Entry point -----------------------------------------------------------------------


def apply_rules(comparison: Comparison, lines: Sequence[RequestedLine], today: date) -> Comparison:
    """The comparison with `rules` on suppliers and cells, and `recommendations`."""
    by_id = {line.id: line for line in lines}
    suppliers: list[SupplierColumn] = []
    for column in comparison.suppliers:
        cells = [
            cell.model_copy(
                update={
                    "rules": cell_rules(by_id[cell.rfq_line_id], cell, column.supplier_type, today)
                }
            )
            if cell.rfq_line_id in by_id
            else cell
            for cell in column.cells
        ]
        suppliers.append(
            column.model_copy(
                update={"cells": cells, "rules": supplier_rules(column, lines, today)}
            )
        )

    recommendations: list[Recommendation] = []
    for line in sorted(lines, key=lambda line: line.position):
        for tier, quantity in enumerate(line.quantities):
            offers = [
                (column, cell)
                for column in suppliers
                for cell in column.cells
                if cell.rfq_line_id == line.id and cell.tier == tier
            ]
            recommendations.append(
                recommend(line, tier, quantity, offers, comparison.base_currency)
            )
    return comparison.model_copy(
        update={"suppliers": suppliers, "recommendations": recommendations}
    )
