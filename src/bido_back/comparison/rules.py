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

The buyer's limits: an offer whose UNIT price is above the buyer's maximum is
never recommended (`above_buyer_maximum`). Eligible offers are RANKED by the
effective unit price (unit price plus tooling per unit) or the landed cost: what
the line really costs. When the recommended offer's unit price is within the
maximum but its effective price is above it, the recommendation says so
(advisory `effective_above_max`, readiness `needs_review`, and a sentence in the
justification): the buyer decides knowingly.
"""

import re
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from bido_back.comparison.compute import (
    Cell,
    Comparison,
    DecisionOffer,
    ExcludedOffer,
    OfferAlternative,
    ReasonCode,
    Recommendation,
    RequestedLine,
    RuleFlag,
    RunnerUp,
    SupplierColumn,
)
from bido_back.comparison.policy import ComparisonPolicy, evidence_issues

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
    if cell.conditional_terms:
        reasons.append("conditional_terms_require_review")
    reasons.extend(column.negotiation_holds)
    reasons.extend(cell.buyer_limit_issues)
    return list(dict.fromkeys(reasons))


def policy_blockers(cell: Cell, column: SupplierColumn, policy: ComparisonPolicy) -> list[str]:
    reasons = blockers(cell, column)
    history = cell.performance
    unknown = evidence_issues(history, policy)
    if not policy.allow_unknown_history:
        reasons.extend(i for i in unknown if i.endswith("_insufficient"))
    if (
        policy.minimum_on_time_rate is not None
        and "delivery_history_insufficient" not in unknown
        and history.on_time_rate is not None
        and history.on_time_rate < policy.minimum_on_time_rate
    ):
        reasons.append("delivery_history_below_policy")
    if (
        policy.minimum_accepted_quantity_rate is not None
        and "quality_history_insufficient" not in unknown
        and history.accepted_quantity_rate is not None
        and history.accepted_quantity_rate < policy.minimum_accepted_quantity_rate
    ):
        reasons.append("quality_history_below_policy")
    if policy.require_on_time_delivery:
        if "late_for_need_by" in cell.rules:
            reasons.append("delivery_after_required_date")
        if cell.lead_time_days is None:
            reasons.append("delivery_date_unconfirmed")
    if policy.require_complete_costs and cell.landed_total_base is None:
        reasons.append("complete_costs_required")
    return list(dict.fromkeys(reasons))


def recommend(
    line: RequestedLine,
    tier: int,
    quantity: Decimal,
    offers: Sequence[tuple[SupplierColumn, Cell]],
    base_currency: str,
    policy: ComparisonPolicy | None = None,
) -> Recommendation:
    policy = policy or ComparisonPolicy()
    eligible: list[tuple[SupplierColumn, Cell]] = []
    excluded: list[ExcludedOffer] = []
    for column, cell in offers:
        blocked = policy_blockers(cell, column, policy)
        if blocked:
            excluded.append(
                ExcludedOffer(
                    invite_id=column.invite_id, supplier_name=column.supplier_name, reasons=blocked
                )
            )
        else:
            eligible.append((column, cell))

    # Brokers never win against an authorized distributor or a manufacturer:
    # they stay in the comparison, excluded from the recommendation.
    authorized = [o for o in eligible if _kind(o[0]) in _AUTHORIZED]
    if authorized:
        for column, _cell in eligible:
            if _kind(column) in _BROKERS:
                excluded.append(
                    ExcludedOffer(
                        invite_id=column.invite_id,
                        supplier_name=column.supplier_name,
                        reasons=["reseller_not_preferred"],
                    )
                )
        eligible = [o for o in eligible if _kind(o[0]) not in _BROKERS]

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
            readiness="no_feasible_offer",
            source_evidence=dict(line.source_evidence),
        )

    on_time = [
        o
        for o in eligible
        if "late_for_need_by" not in o[1].rules
        and (line.need_by is None or o[1].lead_time_days is not None)
    ]
    pool = on_time or eligible
    # When every eligible alternative has complete cost evidence, compare
    # landed costs even without a hard completeness requirement.
    rank_policy = (
        policy.model_copy(update={"require_complete_costs": True})
        if all(cell.landed_total_base is not None for _, cell in eligible)
        else policy
    )
    ranked = sorted(pool, key=lambda offer: _policy_rank(offer, rank_policy))
    winner_column, winner = ranked[0]
    price = _price(winner)

    reasons: list[ReasonCode] = []
    if len(eligible) == 1:
        reasons.append("only_eligible_offer")
    elif policy.preference == "cost_first" and not rank_policy.require_complete_costs:
        reasons.append("lowest_effective_price")
        if len(ranked) > 1 and _price(ranked[1][1]) == price:
            reasons.append("tie_broken_by_lead_time")
    if not on_time and line.need_by is not None:
        reasons.append("no_offer_meets_need_by")
    # A broker wins only when no authorized distributor or manufacturer offered.
    advisories = ["only_unauthorized_offers"] if _kind(winner_column) in _BROKERS else []
    if "effective_above_max" in winner.buyer_limit_warnings:
        # Within the maximum per unit, above it with the one-off charges: said, not hidden.
        advisories.append("effective_above_max")

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

    gaps = list(winner.cost_gaps)
    gaps.extend(evidence_issues(winner.performance, policy))
    gaps.extend(
        f
        for f in winner.flags
        if f in ("missing_lead_time", "missing_incoterm", "incoterm_differs")
    )
    if winner_column.valid_until is None:
        gaps.append("validity_unconfirmed")
    if winner_column.payment_terms is None:
        gaps.append("payment_terms_unconfirmed")
    if "late_for_need_by" in winner.rules:
        gaps.append("delivery_after_required_date")
    gaps.extend(
        r
        for r in winner.rules
        if r in ("qualification_unconfirmed", "authorization_unverified", "moq_above_quantity")
    )
    policy_reasons = [policy.preference]
    if rank_policy.require_complete_costs:
        policy_reasons.append("compared_landed_costs")
    else:
        policy_reasons.append("compared_goods_and_tooling_only")
    if on_time and len(on_time) < len(eligible):
        policy_reasons.append("on_time_offers_preferred")
    alternatives = [
        OfferAlternative(
            invite_id=col.invite_id,
            supplier_name=col.supplier_name,
            effective_unit_price_base=_price(cell),
            landed_total_base=cell.landed_total_base,
            lead_time_days=cell.lead_time_days,
            performance=cell.performance,
            tradeoffs=_tradeoffs(winner, cell, policy),
            offer=_decision_offer(col, cell),
        )
        for col, cell in sorted(eligible, key=lambda offer: _policy_rank(offer, rank_policy))
        if col.invite_id != winner_column.invite_id
    ]
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
            line,
            winner_column,
            winner,
            runner_up,
            reasons,
            excluded,
            base_currency,
            advisories,
            rank_policy,
        ),
        runner_up=runner_up,
        excluded=excluded,
        advisories=advisories,
        policy_reasons=policy_reasons,
        source_evidence=dict(line.source_evidence),
        offer=_decision_offer(winner_column, winner),
        evidence_gaps=list(dict.fromkeys(gaps)),
        alternatives=alternatives,
        performance=winner.performance,
        proposal_id=winner_column.proposal_id,
        proposal_line_id=winner.proposal_line_id,
        evidence_message_ids=winner.evidence_message_ids,
        landed_total_base=winner.landed_total_base,
        readiness="needs_review"
        if gaps or "effective_above_max" in advisories
        else "ready_for_buyer_review",
        unit_price_base=winner.unit_price_base,
        tooling_base=winner.tooling_base,
    )


def _decision_offer(column: SupplierColumn, cell: Cell) -> DecisionOffer:
    return DecisionOffer(
        supplier_id=column.supplier_id,
        proposal_id=column.proposal_id,
        proposal_version=column.proposal_version,
        proposal_line_id=cell.proposal_line_id,
        offered_mpn=cell.offered_mpn,
        unit=cell.unit,
        quoted_currency=cell.currency,
        quoted_unit_price=cell.unit_price,
        price_basis_quantity=cell.price_basis_quantity,
        valid_until=column.valid_until,
        payment_terms=column.payment_terms,
        incoterm=column.incoterm,
        known_total_base=cell.known_total_base,
        landed_total_base=cell.landed_total_base,
        evidence_message_ids=cell.evidence_message_ids,
    )


def _kind(column: SupplierColumn) -> str:
    return (column.supplier_type or "").strip().lower()


def _price(cell: Cell) -> Decimal:
    assert cell.effective_unit_price_base is not None  # eligible cells always have one
    return cell.effective_unit_price_base


def _rank(offer: tuple[SupplierColumn, Cell]) -> tuple[Decimal, int, str]:
    column, cell = offer
    lead = cell.lead_time_days if cell.lead_time_days is not None else 10**6
    return (_price(cell), lead, column.supplier_name)


def _policy_rank(
    offer: tuple[SupplierColumn, Cell], policy: ComparisonPolicy
) -> tuple[Decimal, Decimal, Decimal, int, str]:
    column, cell = offer
    price, lead, name = _rank(offer)
    if policy.require_complete_costs and cell.landed_total_base is not None and cell.quantity > 0:
        price = cell.landed_total_base / cell.quantity
    if policy.preference == "cost_first":
        return Decimal(0), Decimal(0), price, lead, name
    history = cell.performance
    on_time = (
        history.on_time_rate if history.delivery_samples >= policy.minimum_history_samples else None
    )
    quality = (
        history.accepted_quantity_rate
        if history.quality_samples >= policy.minimum_history_samples
        else None
    )
    # A missing metric sorts below every measured value, including zero.
    return (
        -on_time if on_time is not None else Decimal(1),
        -quality if quality is not None else Decimal(1),
        price,
        lead,
        column.supplier_name,
    )


def _tradeoffs(winner: Cell, other: Cell, policy: ComparisonPolicy) -> list[str]:
    result = list(other.cost_gaps)
    result.extend(evidence_issues(other.performance, policy))
    if _price(other) < _price(winner):
        result.append("lower_quoted_cost")
    elif _price(other) > _price(winner):
        result.append("higher_quoted_cost")
    if other.lead_time_days is not None and winner.lead_time_days is not None:
        if other.lead_time_days < winner.lead_time_days:
            result.append("shorter_lead_time")
        elif other.lead_time_days > winner.lead_time_days:
            result.append("longer_lead_time")
    result.extend(other.rules)
    return list(dict.fromkeys(result))


def _justify(
    line: RequestedLine,
    column: SupplierColumn,
    cell: Cell,
    runner_up: RunnerUp | None,
    reasons: Sequence[ReasonCode],
    excluded: Sequence[ExcludedOffer],
    base_currency: str,
    advisories: Sequence[str] = (),
    policy: ComparisonPolicy | None = None,
) -> str:
    price = f"{_fmt(_price(cell))} {base_currency}"
    if policy is not None and policy.preference == "reliability_first":
        text = (
            f"{column.supplier_name} ranks first under the buyer's reliability-first policy"
            f" (on-time history, accepted-quantity history, then cost), at {price} per unit"
        )
    elif policy is not None and policy.require_complete_costs:
        text = (
            f"{column.supplier_name} has the lowest evidenced landed cost under the buyer's policy"
        )
    elif "only_eligible_offer" in reasons:
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
    if (
        line.need_by is not None
        and "late_for_need_by" not in cell.rules
        and cell.lead_time_days is not None
    ):
        text += " among offers meeting the delivery requirement"
    if cell.lead_time_days is not None:
        text += f"; lead time {cell.lead_time_days} days"
    if "no_offer_meets_need_by" in reasons and line.need_by is not None:
        text += f", which misses the need-by date {line.need_by.isoformat()} (no offer meets it)"
    elif line.need_by is not None and cell.lead_time_days is not None:
        text += f", in time for {line.need_by.isoformat()}"
    text += "."
    if "only_unauthorized_offers" in advisories:
        text += (
            " Only unauthorized resellers offered this line: check the parts' origin and"
            " authenticity before buying."
        )
    if "effective_above_max" in advisories:
        text += (
            " Its unit price is within your maximum, but with one-off charges the effective"
            " price per unit is above it."
        )
    if excluded:
        text += f" {len(excluded)} other offer(s) excluded."
    return text


def _fmt(value: Decimal) -> str:
    return format(value.normalize(), "f")


# --- Entry point -----------------------------------------------------------------------


def apply_rules(
    comparison: Comparison,
    lines: Sequence[RequestedLine],
    today: date,
    policy: ComparisonPolicy | None = None,
) -> Comparison:
    """The comparison with `rules` on suppliers and cells, and `recommendations`."""
    policy = policy or comparison.policy
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
                recommend(line, tier, quantity, offers, comparison.base_currency, policy)
            )
    covered = sum(r.invite_id is not None for r in recommendations)
    readiness = (
        "no_feasible_offer"
        if not covered
        else "ready_for_buyer_review"
        if recommendations
        and all(r.readiness == "ready_for_buyer_review" for r in recommendations)
        and not comparison.awaiting
        else "needs_review"
    )
    return comparison.model_copy(
        update={
            "suppliers": suppliers,
            "recommendations": recommendations,
            "policy": policy,
            "readiness": readiness,
            "covered_line_tiers": covered,
            "requested_line_tiers": len(recommendations),
        }
    )
