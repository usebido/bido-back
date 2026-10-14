"""The quote comparison for one RFQ, as pure functions over plain values.

For every supplier (the latest proposal version of its invite), every
requested line and every requested quantity tier:

- **Price break.** The supplier's lines for that requested line are its price
  breaks. Exact-part breaks are preferred; alternatives are used only when
  there is no exact one (and then flagged). The break used is the one with the
  largest `min_quantity <= tier`. A tier below every break gets no total
  (`below_min_quantity`); the smallest break is still shown.
- **Currency.** Amounts are converted to the base currency with ECB rates
  (units of currency per 1 EUR, EUR as the pivot); a missing currency or rate
  leaves the base amounts empty and flagged.
- **Tooling** (one-off NRE) is amortized over the tier quantity:
  `tooling_per_unit = tooling / tier`, and the line total is
  `unit_price * tier + tooling`. The effective unit price
  (`unit_price + tooling_per_unit`) ranks offers; the buyer's maximum is a UNIT
  price and is compared with the unit price only (`above_buyer_maximum`). When
  the unit price is within the maximum and the effective price is above it, the
  cell says so (`buyer_limit_warnings: effective_above_max`): a warning for the
  buyer, never a blocker.
- **Units** are compared, not converted: only identical units (and a few
  spellings of "pieces") count as the same. A mismatch is flagged and the
  line gets no total.
- **Flags** say what a person must look at: alternative part, needs_review,
  low-confidence fields, missing price / lead time / currency / FX rate /
  unit, expired validity, Incoterm different from the requested one, MOQ.

Nothing uncertain is hidden: values are shown as quoted, with their flags.
A value a person corrected or confirmed (`bido_back.corrections`) is shown as
corrected, listed in `corrected_fields`, and never counts as low confidence.
Supplier totals per tier sum what could be priced and say whether that is
every requested line (`complete`).

Sector rules (`rules` on cells and suppliers) and the per-line recommendation
are added on top by `bido_back.comparison.rules`.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, Field, JsonValue

from bido_back.comparison.policy import ComparisonPolicy, SupplierPerformance
from bido_back.compliance.schemas import SupplierComplianceSummary

EUR: Final = "EUR"
# Same threshold as the quote extraction's review rule.
LOW_CONFIDENCE: Final = 0.8

_UNIT_PRICE_PLACES: Final = Decimal("0.000001")
_MONEY_PLACES: Final = Decimal("0.01")
# "Pieces" as suppliers write it (quote extraction keeps the unit as written).
_PIECES: Final = frozenset(
    {"pcs", "pc", "pce", "piece", "pieces", "ea", "each", "unit", "units",
     "stk", "st", "stück", "stueck", "stck",
     "pz", "pezzi", "pezzo",
     "pièce", "pièces", "unité", "unités",
     "un", "und", "unid", "peça", "peças", "peca", "pecas",
     "ud", "uds", "unidad", "unidades"}
)  # fmt: skip

Flag = Literal[
    "alternative_part",
    "needs_review",
    "low_confidence",
    "missing_price",
    "missing_lead_time",
    "missing_currency",
    "fx_rate_missing",
    "missing_unit",
    "unit_mismatch",
    "below_min_quantity",
    "below_moq",
    "min_quantity_assumed",
    "incoterm_differs",
    "missing_incoterm",
    "validity_expired",
    "missing_validity",
    "no_quote_for_line",
]


# --- Inputs --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldInfo:
    """What the extraction recorded about one value."""

    confidence: float | None = None
    needs_review: bool = False
    # A person corrected or confirmed the value.
    corrected: bool = False


@dataclass(frozen=True, slots=True)
class RequestedLine:
    id: UUID
    position: int
    description: str
    mpn: str | None
    unit: str
    quantities: tuple[Decimal, ...]
    incoterm: str | None = None
    need_by: date | None = None
    # Required qualification as the buyer wrote it, e.g. "AEC-Q200".
    qualification: str | None = None
    part_number: str | None = None
    max_unit_price: Decimal | None = None
    max_lead_time_days: int | None = None
    source_evidence: Mapping[str, JsonValue] = field(default_factory=dict[str, JsonValue])


@dataclass(frozen=True, slots=True)
class QuotedLine:
    rfq_line_id: UUID | None
    # The proposal line's id (to correct it); None in tests that do not need it.
    id: UUID | None = None
    offered_mpn: str | None = None
    is_alternative: bool = False
    min_quantity: Decimal | None = None
    unit_price: Decimal | None = None
    unit: str | None = None
    tooling_cost: Decimal | None = None
    moq: Decimal | None = None
    lead_time_days: int | None = None
    needs_review: bool = False
    fields: Mapping[str, FieldInfo] = field(default_factory=dict[str, FieldInfo])
    # Line-level validation issues (`_review.issues`); None when not recorded.
    review_issues: tuple[str, ...] | None = None
    # Qualification the supplier states for this offer (grounded text), if any.
    qualification: str | None = None
    price_basis_quantity: Decimal | None = None
    freight_cost: Decimal | None = None
    tax_rate_percent: Decimal | None = None
    conditional_terms: str | None = None
    source_message_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class SupplierQuote:
    invite_id: UUID
    supplier_id: UUID
    supplier_name: str
    proposal_id: UUID
    version: int
    currency: str | None
    valid_until: date | None
    incoterm: str | None
    lines: tuple[QuotedLine, ...]
    fields: Mapping[str, FieldInfo] = field(default_factory=dict[str, FieldInfo])
    # suppliers.type: authorized_distributor, distributor, reseller, manufacturer...
    supplier_type: str | None = None
    # As extracted from the proposal header (free text, e.g. "30 days net").
    payment_terms: str | None = None
    performance: Mapping[UUID, SupplierPerformance] = field(
        default_factory=dict[UUID, SupplierPerformance]
    )
    negotiation_holds: tuple[str, ...] = ()
    correction_revision: int = 0
    # How the proposal reached Bido: the supplier's email, or a document the buyer
    # registered from the Copilot (`messages.origin`).
    origin: str = "email"


@dataclass(frozen=True, slots=True)
class Rate:
    currency: str
    per_eur: Decimal
    rate_date: date


# --- Outputs -------------------------------------------------------------------------

# Sector rules (see `bido_back.comparison.rules`). Kept apart from `Flag` so
# the API adds a field instead of growing an existing enum.
RuleFlag = Literal[
    "alternative_needs_approval",
    "qualification_unconfirmed",
    "unauthorized_source",
    "authorization_unverified",
    "late_for_need_by",
    "moq_above_quantity",
    "validity_expired",
    "validity_ends_before_need_by",
]


class ComparisonLine(BaseModel):
    id: UUID
    position: int
    description: str
    mpn: str | None
    unit: str
    quantities: list[Decimal]
    incoterm: str | None
    need_by: date | None = None
    qualification: str | None = None
    source_evidence: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])


class Cell(BaseModel):
    rfq_line_id: UUID
    tier: int
    quantity: Decimal
    offered_mpn: str | None
    is_alternative: bool
    # The price break used (its minimum quantity), as quoted.
    break_min_quantity: Decimal | None
    unit: str | None
    currency: str | None
    unit_price: Decimal | None
    tooling_cost: Decimal | None
    moq: Decimal | None
    lead_time_days: int | None
    unit_price_base: Decimal | None
    tooling_per_unit_base: Decimal | None
    effective_unit_price_base: Decimal | None
    line_total_base: Decimal | None
    flags: list[Flag]
    low_confidence_fields: list[str]
    proposal_line_id: UUID | None = None
    corrected_fields: list[str] = Field(default_factory=list[str])
    qualification: str | None = None
    rules: list[RuleFlag] = Field(default_factory=list[RuleFlag])
    price_basis_quantity: Decimal | None = None
    freight_cost: Decimal | None = None
    tax_rate_percent: Decimal | None = None
    conditional_terms: str | None = None
    # Known subtotal is not a landed cost; missing components stay explicit.
    known_total_base: Decimal | None = None
    landed_total_base: Decimal | None = None
    cost_gaps: list[str] = Field(default_factory=list[str])
    # The buyer's limits this offer breaks (blocking for the recommendation): the UNIT
    # price above the maximum (`above_buyer_maximum`), the lead time.
    buyer_limit_issues: list[str] = Field(default_factory=list[str])
    evidence_message_ids: list[UUID] = Field(default_factory=list[UUID])
    performance: SupplierPerformance = Field(default_factory=SupplierPerformance)
    # The one-off tooling charge in the base currency (not per unit).
    tooling_base: Decimal | None = None
    # What the buyer should know about its limits without it blocking anything:
    # `effective_above_max` (the unit price is within the maximum, the effective price
    # with one-off charges is above it).
    buyer_limit_warnings: list[str] = Field(default_factory=list[str])


class TierTotal(BaseModel):
    tier: int
    total_base: Decimal | None
    lines_priced: int
    lines_requested: int
    complete: bool


class SupplierColumn(BaseModel):
    invite_id: UUID
    supplier_id: UUID
    supplier_name: str
    proposal_id: UUID
    proposal_version: int
    currency: str | None
    valid_until: date | None
    incoterm: str | None
    flags: list[Flag]
    low_confidence_fields: list[str]
    corrected_fields: list[str] = Field(default_factory=list[str])
    max_lead_time_days: int | None
    cells: list[Cell]
    totals: list[TierTotal]
    supplier_type: str | None = None
    rules: list[RuleFlag] = Field(default_factory=list[RuleFlag])
    # The latest proposal's payment terms, as extracted (free text).
    payment_terms: str | None = None
    negotiation_holds: list[str] = Field(default_factory=list[str])
    correction_revision: int = 0
    origin: Literal["email", "copilot_upload"] = Field(
        default="email",
        description="How the latest proposal reached Bido: the supplier's email reply, or a "
        "document the buyer attached in the Copilot and registered (copilot_upload)",
    )


class AwaitingSupplier(BaseModel):
    invite_id: UUID
    supplier_id: UUID
    supplier_name: str
    status: str


class RateUsed(BaseModel):
    currency: str
    per_eur: Decimal
    rate_date: date


ReasonCode = Literal[
    "lowest_effective_price",
    "tie_broken_by_lead_time",
    "only_eligible_offer",
    "no_offer_meets_need_by",
    "no_eligible_offer",
]


class ExcludedOffer(BaseModel):
    invite_id: UUID
    supplier_name: str
    # Blocking flags and rules, e.g. "alternative_needs_approval", "low_confidence".
    reasons: list[str]


class RunnerUp(BaseModel):
    invite_id: UUID
    supplier_name: str
    effective_unit_price_base: Decimal
    # How much more expensive than the winner, in percent.
    price_delta_pct: Decimal


class DecisionOffer(BaseModel):
    supplier_id: UUID
    proposal_id: UUID
    proposal_version: int
    proposal_line_id: UUID | None = None
    offered_mpn: str | None = None
    unit: str | None = None
    quoted_currency: str | None = None
    quoted_unit_price: Decimal | None = None
    price_basis_quantity: Decimal | None = None
    valid_until: date | None = None
    payment_terms: str | None = None
    incoterm: str | None = None
    known_total_base: Decimal | None = None
    landed_total_base: Decimal | None = None
    evidence_message_ids: list[UUID] = Field(default_factory=list[UUID])


class OfferAlternative(BaseModel):
    invite_id: UUID
    supplier_name: str
    effective_unit_price_base: Decimal
    landed_total_base: Decimal | None = None
    lead_time_days: int | None = None
    performance: SupplierPerformance = Field(default_factory=SupplierPerformance)
    tradeoffs: list[str] = Field(default_factory=list[str])
    offer: DecisionOffer | None = None


class Recommendation(BaseModel):
    rfq_line_id: UUID
    tier: int
    quantity: Decimal
    # None when no offer can be recommended (see `excluded`).
    invite_id: UUID | None
    supplier_name: str | None
    effective_unit_price_base: Decimal | None
    line_total_base: Decimal | None
    lead_time_days: int | None
    reasons: list[ReasonCode]
    # One sentence built from the numbers above, in English; never model output.
    justification: str
    runner_up: RunnerUp | None
    excluded: list[ExcludedOffer]
    # Warnings about the recommended offer, e.g. "only_unauthorized_offers": the
    # winner is a reseller because no authorized distributor or manufacturer
    # offered the line (brokers never win against them). A free list rather
    # than new `reasons` values, so existing clients keep validating.
    advisories: list[str] = Field(default_factory=list[str])
    policy_reasons: list[str] = Field(default_factory=list[str])
    source_evidence: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    offer: DecisionOffer | None = None
    evidence_gaps: list[str] = Field(default_factory=list[str])
    alternatives: list[OfferAlternative] = Field(default_factory=list[OfferAlternative])
    performance: SupplierPerformance = Field(default_factory=SupplierPerformance)
    proposal_id: UUID | None = None
    proposal_line_id: UUID | None = None
    evidence_message_ids: list[UUID] = Field(default_factory=list[UUID])
    landed_total_base: Decimal | None = None
    readiness: Literal["ready_for_buyer_review", "needs_review", "no_feasible_offer"] = (
        "needs_review"
    )
    # The recommended offer's unit price (what the buyer's target and maximum apply to)
    # and its one-off tooling charge, both in the base currency; the effective price
    # above is the unit price plus the tooling per unit.
    unit_price_base: Decimal | None = None
    tooling_base: Decimal | None = None


class Comparison(BaseModel):
    rfq_id: UUID
    base_currency: str
    computed_on: date
    lines: list[ComparisonLine]
    suppliers: list[SupplierColumn]
    awaiting: list[AwaitingSupplier]
    fx_rates: list[RateUsed]
    # Per invited supplier, filled by the API from bido_back.compliance.
    compliance: list[SupplierComplianceSummary] = Field(
        default_factory=list[SupplierComplianceSummary]
    )
    recommendations: list[Recommendation] = Field(default_factory=list[Recommendation])
    policy: ComparisonPolicy = Field(default_factory=ComparisonPolicy)
    readiness: Literal["ready_for_buyer_review", "needs_review", "no_feasible_offer"] = (
        "needs_review"
    )
    covered_line_tiers: int = 0
    requested_line_tiers: int = 0


# --- Pure helpers ----------------------------------------------------------------------


def normalize_unit(unit: str | None) -> str | None:
    if unit is None:
        return None
    value = unit.strip().lower().rstrip(".")
    if not value:
        return None
    return "pcs" if value in _PIECES else value


def same_unit(a: str | None, b: str | None) -> bool:
    return normalize_unit(a) == normalize_unit(b)


def _norm_code(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip().upper()
    return stripped or None


def to_base(amount: Decimal, currency: str, base: str, rates: Mapping[str, Rate]) -> Decimal | None:
    """Convert with EUR as the pivot; None when a needed rate is missing."""
    currency, base = currency.upper(), base.upper()
    if currency == base:
        return amount
    if currency == EUR:
        eur = amount
    else:
        rate = rates.get(currency)
        if rate is None or rate.per_eur <= 0:
            return None
        eur = amount / rate.per_eur
    if base == EUR:
        return eur
    base_rate = rates.get(base)
    if base_rate is None or base_rate.per_eur <= 0:
        return None
    return eur * base_rate.per_eur


def pick_break(breaks: Sequence[QuotedLine], quantity: Decimal) -> tuple[QuotedLine, bool] | None:
    """The price break for `quantity`: largest min_quantity <= quantity.

    Returns (break, applies). When no break applies (quantity below every
    minimum) the smallest break is returned with applies=False. A missing
    min_quantity counts as 0. Ties prefer a break that has a price.
    """
    if not breaks:
        return None

    def minimum(b: QuotedLine) -> Decimal:
        return b.min_quantity if b.min_quantity is not None else Decimal(0)

    applicable = [b for b in breaks if minimum(b) <= quantity]
    if not applicable:
        return min(breaks, key=minimum), False
    best = max(minimum(b) for b in applicable)
    tied = [b for b in applicable if minimum(b) == best]
    priced = [b for b in tied if b.unit_price is not None]
    return (priced or tied)[0], True


def _low_fields(fields: Mapping[str, FieldInfo], names: Sequence[str]) -> list[str]:
    low: list[str] = []
    for name in names:
        info = fields.get(name)
        if info is None or info.corrected:
            continue
        if info.needs_review or (info.confidence is not None and info.confidence < LOW_CONFIDENCE):
            low.append(name)
    return low


def _corrected(fields: Mapping[str, FieldInfo], names: Sequence[str]) -> list[str]:
    return [name for name in names if (info := fields.get(name)) is not None and info.corrected]


def _q(value: Decimal | None, places: Decimal) -> Decimal | None:
    return value.quantize(places) if value is not None else None


_LINE_FIELDS: Final = (
    "offered_mpn",
    "min_quantity",
    "unit_price",
    "unit",
    "tooling_cost",
    "moq",
    "lead_time_days",
    "price_basis_quantity",
    "freight_cost",
    "tax_rate_percent",
    "conditional_terms",
)
_PROPOSAL_FIELDS: Final = ("currency", "valid_until", "incoterm")
# Fields that do not count against an offer when the RFQ line requests no part number.
_UNREQUESTED_WITHOUT_MPN: Final = ("offered_mpn",)


def _needs_review(line: QuotedLine, ignored: Sequence[str]) -> bool:
    """The line's review flag, recomputed without the `ignored` fields when possible.

    Without the recorded line issues the stored flag stands: an unknown issue may be
    the reason for it.
    """
    if not line.needs_review or not ignored or line.review_issues is None:
        return line.needs_review
    if line.review_issues:
        return True
    return any(
        info.needs_review and not info.corrected
        for name, info in line.fields.items()
        if name not in ignored
    )


# What `corrected_fields` reports: every field a person can correct.
_CORRECTABLE_LINE_FIELDS: Final = (
    "offered_mpn",
    "is_alternative",
    "rfq_line_id",
    "min_quantity",
    "unit_price",
    "unit",
    "tooling_cost",
    "moq",
    "lead_time_days",
    "origin_country",
    "qualification",
)
_CORRECTABLE_PROPOSAL_FIELDS: Final = (*_PROPOSAL_FIELDS, "payment_terms")


def compute_cell(
    line: RequestedLine,
    tier: int,
    quantity: Decimal,
    quote: SupplierQuote,
    base_currency: str,
    rates: Mapping[str, Rate],
) -> Cell:
    candidates = [q for q in quote.lines if q.rfq_line_id == line.id]
    exact = [q for q in candidates if not q.is_alternative]
    breaks = exact or candidates
    picked = pick_break(breaks, quantity)
    if picked is None:
        return Cell(
            rfq_line_id=line.id,
            tier=tier,
            quantity=quantity,
            offered_mpn=None,
            is_alternative=False,
            break_min_quantity=None,
            unit=None,
            currency=quote.currency,
            unit_price=None,
            tooling_cost=None,
            moq=None,
            lead_time_days=None,
            unit_price_base=None,
            tooling_per_unit_base=None,
            effective_unit_price_base=None,
            line_total_base=None,
            flags=["no_quote_for_line", "missing_price"],
            low_confidence_fields=[],
        )

    chosen, applies = picked
    # A part number the buyer did not ask for cannot make an offer doubtful.
    ignored = () if line.mpn else _UNREQUESTED_WITHOUT_MPN
    flags: list[Flag] = []
    if chosen.is_alternative:
        flags.append("alternative_part")
    if _needs_review(chosen, ignored):
        flags.append("needs_review")
    low = _low_fields(chosen.fields, [n for n in _LINE_FIELDS if n not in ignored])
    if low:
        flags.append("low_confidence")
    if chosen.unit_price is None:
        flags.append("missing_price")
    if chosen.lead_time_days is None:
        flags.append("missing_lead_time")
    if not applies:
        flags.append("below_min_quantity")
    if chosen.min_quantity is None and len(breaks) > 1:
        flags.append("min_quantity_assumed")
    if chosen.moq is not None and quantity < chosen.moq:
        flags.append("below_moq")

    unit_ok = True
    if chosen.unit is None:
        flags.append("missing_unit")
    elif not same_unit(chosen.unit, line.unit):
        flags.append("unit_mismatch")
        unit_ok = False

    requested_incoterm = _norm_code(line.incoterm)
    if requested_incoterm is not None:
        quoted_incoterm = _norm_code(quote.incoterm)
        if quoted_incoterm is None:
            flags.append("missing_incoterm")
        elif quoted_incoterm != requested_incoterm:
            flags.append("incoterm_differs")

    currency = _norm_code(quote.currency)
    denominator = chosen.price_basis_quantity or Decimal(1)
    basis_valid = chosen.price_basis_quantity is None or chosen.price_basis_quantity > 0
    if not basis_valid:
        flags.append("needs_review")
    unit_price_base: Decimal | None = None
    tooling_base: Decimal | None = Decimal(0) if chosen.tooling_cost is None else None
    has_amount = chosen.unit_price is not None or chosen.tooling_cost is not None
    if currency is None:
        if has_amount:
            flags.append("missing_currency")
    else:
        if chosen.unit_price is not None:
            unit_price_base = (
                to_base(chosen.unit_price / denominator, currency, base_currency, rates)
                if basis_valid
                else None
            )
        if chosen.tooling_cost is not None:
            tooling_base = to_base(chosen.tooling_cost, currency, base_currency, rates)
        if has_amount and (
            (chosen.unit_price is not None and unit_price_base is None)
            or (chosen.tooling_cost is not None and tooling_base is None)
        ):
            flags.append("fx_rate_missing")

    tooling_per_unit = (
        tooling_base / quantity if tooling_base is not None and quantity > 0 else None
    )
    effective = (
        unit_price_base + tooling_per_unit
        if unit_price_base is not None and tooling_per_unit is not None
        else None
    )
    total = (
        unit_price_base * quantity + tooling_base
        if unit_price_base is not None and tooling_base is not None and applies and unit_ok
        else None
    )

    cost_gaps: list[str] = []
    if chosen.tooling_cost is None:
        cost_gaps.append("tooling_cost_unknown")
    if chosen.freight_cost is None:
        cost_gaps.append("freight_cost_unknown")
    if chosen.tax_rate_percent is None:
        cost_gaps.append("tax_treatment_unknown")
    if chosen.conditional_terms:
        cost_gaps.append("conditional_terms_require_review")
    freight_base = (
        to_base(chosen.freight_cost, currency, base_currency, rates)
        if currency is not None and chosen.freight_cost is not None
        else None
    )
    known_total = total
    if known_total is not None and freight_base is not None:
        known_total += freight_base
    # Tax rate alone does not establish the taxable base (freight/tooling can
    # differ by jurisdiction). A positive rate requires an evidenced tax amount.
    if chosen.tax_rate_percent is not None and chosen.tax_rate_percent != 0:
        cost_gaps.append("taxable_basis_unknown")
    if chosen.freight_cost is not None and freight_base is None:
        cost_gaps.append("freight_fx_missing")
    if low:
        cost_gaps.append("cost_evidence_needs_review")
    landed_total = known_total if not cost_gaps and unit_ok and applies else None
    buyer_limit_issues: list[str] = []
    buyer_limit_warnings: list[str] = []
    # The maximum is a unit price: one-off charges never decide it, they are a warning.
    if line.max_unit_price is not None and unit_price_base is not None:
        if unit_price_base > line.max_unit_price:
            buyer_limit_issues.append("above_buyer_maximum")
        elif effective is not None and effective > line.max_unit_price:
            buyer_limit_warnings.append("effective_above_max")
    if line.max_lead_time_days is not None:
        if chosen.lead_time_days is None:
            buyer_limit_issues.append("buyer_lead_time_unconfirmed")
        elif chosen.lead_time_days > line.max_lead_time_days:
            buyer_limit_issues.append("above_buyer_lead_time_limit")

    return Cell(
        rfq_line_id=line.id,
        tier=tier,
        quantity=quantity,
        offered_mpn=chosen.offered_mpn,
        is_alternative=chosen.is_alternative,
        break_min_quantity=chosen.min_quantity,
        unit=chosen.unit,
        currency=currency,
        unit_price=chosen.unit_price,
        tooling_cost=chosen.tooling_cost,
        moq=chosen.moq,
        lead_time_days=chosen.lead_time_days,
        unit_price_base=_q(unit_price_base, _UNIT_PRICE_PLACES),
        tooling_per_unit_base=_q(tooling_per_unit, _UNIT_PRICE_PLACES),
        effective_unit_price_base=_q(effective, _UNIT_PRICE_PLACES),
        line_total_base=_q(total, _MONEY_PLACES),
        flags=flags,
        low_confidence_fields=low,
        proposal_line_id=chosen.id,
        corrected_fields=_corrected(chosen.fields, _CORRECTABLE_LINE_FIELDS),
        qualification=chosen.qualification,
        price_basis_quantity=chosen.price_basis_quantity,
        freight_cost=chosen.freight_cost,
        tax_rate_percent=chosen.tax_rate_percent,
        conditional_terms=chosen.conditional_terms,
        known_total_base=_q(known_total, _MONEY_PLACES),
        landed_total_base=_q(landed_total, _MONEY_PLACES),
        cost_gaps=cost_gaps,
        buyer_limit_issues=buyer_limit_issues,
        evidence_message_ids=list(chosen.source_message_ids),
        performance=quote.performance.get(line.id, SupplierPerformance()),
        tooling_base=_q(tooling_base, _MONEY_PLACES) if chosen.tooling_cost is not None else None,
        buyer_limit_warnings=buyer_limit_warnings,
    )


def compute_supplier(
    lines: Sequence[RequestedLine],
    quote: SupplierQuote,
    base_currency: str,
    rates: Mapping[str, Rate],
    today: date,
) -> SupplierColumn:
    flags: list[Flag] = []
    if quote.valid_until is None:
        flags.append("missing_validity")
    elif quote.valid_until < today:
        flags.append("validity_expired")
    if _norm_code(quote.currency) is None:
        flags.append("missing_currency")
    low = _low_fields(quote.fields, _PROPOSAL_FIELDS)
    if low:
        flags.append("low_confidence")

    cells = [
        compute_cell(line, tier, quantity, quote, base_currency, rates)
        for line in lines
        for tier, quantity in enumerate(line.quantities)
    ]
    if any("needs_review" in c.flags for c in cells):
        flags.append("needs_review")

    tiers = max((len(line.quantities) for line in lines), default=0)
    totals: list[TierTotal] = []
    for tier in range(tiers):
        tier_cells = [c for c in cells if c.tier == tier]
        priced = [c.line_total_base for c in tier_cells if c.line_total_base is not None]
        totals.append(
            TierTotal(
                tier=tier,
                total_base=_q(sum(priced, Decimal(0)), _MONEY_PLACES) if priced else None,
                lines_priced=len(priced),
                lines_requested=len(lines),
                complete=len(priced) == len(lines),
            )
        )

    lead_times = [c.lead_time_days for c in cells if c.lead_time_days is not None]
    return SupplierColumn(
        invite_id=quote.invite_id,
        supplier_id=quote.supplier_id,
        supplier_name=quote.supplier_name,
        proposal_id=quote.proposal_id,
        proposal_version=quote.version,
        currency=_norm_code(quote.currency),
        valid_until=quote.valid_until,
        incoterm=quote.incoterm,
        flags=flags,
        low_confidence_fields=low,
        corrected_fields=_corrected(quote.fields, _CORRECTABLE_PROPOSAL_FIELDS),
        max_lead_time_days=max(lead_times) if lead_times else None,
        cells=cells,
        totals=totals,
        supplier_type=quote.supplier_type,
        payment_terms=quote.payment_terms,
        negotiation_holds=list(quote.negotiation_holds),
        correction_revision=quote.correction_revision,
        origin="copilot_upload" if quote.origin == "copilot_upload" else "email",
    )


def compute_comparison(
    *,
    rfq_id: UUID,
    base_currency: str,
    lines: Sequence[RequestedLine],
    quotes: Sequence[SupplierQuote],
    rates: Mapping[str, Rate],
    today: date,
    awaiting: Sequence[AwaitingSupplier] = (),
) -> Comparison:
    ordered = sorted(lines, key=lambda line: line.position)
    used: set[str] = set()
    for quote in quotes:
        currency = _norm_code(quote.currency)
        if currency is not None:
            used.add(currency)
    if base_currency.upper() != EUR:
        used.add(base_currency.upper())
    return Comparison(
        rfq_id=rfq_id,
        base_currency=base_currency.upper(),
        computed_on=today,
        lines=[
            ComparisonLine(
                id=line.id,
                position=line.position,
                description=line.description,
                mpn=line.mpn,
                unit=line.unit,
                quantities=list(line.quantities),
                incoterm=line.incoterm,
                need_by=line.need_by,
                qualification=line.qualification,
                source_evidence=dict(line.source_evidence),
            )
            for line in ordered
        ],
        suppliers=[
            compute_supplier(ordered, quote, base_currency, rates, today) for quote in quotes
        ],
        awaiting=list(awaiting),
        fx_rates=[
            RateUsed(currency=r.currency, per_eur=r.per_eur, rate_date=r.rate_date)
            for code in sorted(used)
            if (r := rates.get(code)) is not None
        ],
    )


def best_first_tier_total(comparison: Comparison) -> Decimal | None:
    """The lowest complete first-tier total among the suppliers, in the base currency."""
    totals = [
        s.totals[0].total_base
        for s in comparison.suppliers
        if s.totals and s.totals[0].complete and s.totals[0].total_base is not None
    ]
    return min(totals) if totals else None
