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
  `unit_price * tier + tooling`.
- **Units** are compared, not converted: only identical units (and a few
  spellings of "pieces") count as the same. A mismatch is flagged and the
  line gets no total.
- **Flags** say what a person must look at: alternative part, needs_review,
  low-confidence fields, missing price / lead time / currency / FX rate /
  unit, expired validity, Incoterm different from the requested one, MOQ.

Nothing uncertain is hidden: values are shown as quoted, with their flags.
Supplier totals per tier sum what could be priced and say whether that is
every requested line (`complete`).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from bido_back.compliance.schemas import SupplierComplianceSummary

EUR: Final = "EUR"
# Same threshold as the quote extraction's review rule.
LOW_CONFIDENCE: Final = 0.8

_UNIT_PRICE_PLACES: Final = Decimal("0.000001")
_MONEY_PLACES: Final = Decimal("0.01")
_PIECES: Final = frozenset({"pcs", "pc", "pce", "piece", "pieces", "ea", "each", "stk", "st"})

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


@dataclass(frozen=True, slots=True)
class RequestedLine:
    id: UUID
    position: int
    description: str
    mpn: str | None
    unit: str
    quantities: tuple[Decimal, ...]
    incoterm: str | None = None


@dataclass(frozen=True, slots=True)
class QuotedLine:
    rfq_line_id: UUID | None
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


@dataclass(frozen=True, slots=True)
class Rate:
    currency: str
    per_eur: Decimal
    rate_date: date


# --- Outputs -------------------------------------------------------------------------


class ComparisonLine(BaseModel):
    id: UUID
    position: int
    description: str
    mpn: str | None
    unit: str
    quantities: list[Decimal]
    incoterm: str | None


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
    max_lead_time_days: int | None
    cells: list[Cell]
    totals: list[TierTotal]


class AwaitingSupplier(BaseModel):
    invite_id: UUID
    supplier_id: UUID
    supplier_name: str
    status: str


class RateUsed(BaseModel):
    currency: str
    per_eur: Decimal
    rate_date: date


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
        if info is None:
            continue
        if info.needs_review or (info.confidence is not None and info.confidence < LOW_CONFIDENCE):
            low.append(name)
    return low


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
)
_PROPOSAL_FIELDS: Final = ("currency", "valid_until", "incoterm")


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
    flags: list[Flag] = []
    if chosen.is_alternative:
        flags.append("alternative_part")
    if chosen.needs_review:
        flags.append("needs_review")
    low = _low_fields(chosen.fields, _LINE_FIELDS)
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
    unit_price_base: Decimal | None = None
    tooling_base: Decimal | None = Decimal(0) if chosen.tooling_cost is None else None
    has_amount = chosen.unit_price is not None or chosen.tooling_cost is not None
    if currency is None:
        if has_amount:
            flags.append("missing_currency")
    else:
        if chosen.unit_price is not None:
            unit_price_base = to_base(chosen.unit_price, currency, base_currency, rates)
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
        max_lead_time_days=max(lead_times) if lead_times else None,
        cells=cells,
        totals=totals,
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
