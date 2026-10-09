"""Bido's internal price benchmark and measured supplier performance from source evidence.

Scoped (`planning.assess_plan`) and unscoped (`analysis.assess`) source assessments share
this module. It applies the imported-data analysis's rules and defaults
(`bido_back.purchasing.analysis`) to orders reconciled from uploaded evidence:

* History: the item's purchase-order lines attributed to a supplier, with a positive
  quantity, ordered in the last `lookback_days` (365) up to the snapshot date. Each line
  is joined to its physical receipts by order/item/plant (`joins.receipts_for`); receipts
  after the snapshot date do not count. Cancelled lines are not history.
* Prices come only from comparable completed purchases: closed orders whose reconciled
  receipts equal the ordered quantity, with a positive unit price, an explicit currency
  and the line's physical unit. Open orders and archived quotations never set a price.
  The line's currency is the one most completed purchases were bought in (on a tie, the
  most recent one's); history in another currency is ignored and the line is flagged
  `mixed_currency`; completed purchases left out for their unit, price or currency are
  disclosed (`unpriced_purchase_history`). Amounts in different currencies are never
  added up or converted.
* Benchmark (v1, the imported-data analysis's formulas): the last price (the most recent
  completed purchase; on the same day, the highest order number), the quantity-weighted
  average, the minimum, the trend (last 90 days against before),
  `target = min(last, avg)` and `max = max(last, avg) * (1 + max_over_pct / 100)`.
  Every figure carries its sample size and age: `observations`, `window_days` and
  `last_ordered_at`. Estimated costs are Bido's calculated quantity times the target and
  last prices.
* Thin or stale evidence proposes no target or maximum: with fewer than
  `MIN_OBSERVATIONS` (2) comparable completed purchases in the window, `price` stays None
  and the line says why (`insufficient_price_history`). Purchases ordered before the
  window are not history at all. The suppliers' last prices stay visible either way.
* Supplier performance learns from every reconciled order, including short, late and
  overdue ones. On time: complete and last received on or before the promised date; a
  short delivery fails and an open order past its promised date is late. Orders not yet
  due, without a promised date, closed without any receipt or whose receipts do not
  reconcile stay unknown. Quality: `1 - rejected / received` over receipts that state a
  valid rejected quantity. Lead time: days from the order to its last receipt, once the
  order is fully received or closed.
* Suppliers are ranked by measured quality, on-time rate and average price; unknown
  measures follow measured ones and suppliers that never sold the item (approved sources
  only) come last. Up to 3 suppliers that sold the item are suggested.

A price benchmark is an internal, company-only reference before any supplier contact:
never a quotation, an accepted price or an approved negotiation limit until the buyer
approves one. Its maximum is a historical escalation threshold (above it, a person
decides), not a walk-away price: a walk-away comes from a current alternative the buyer
confirms. Unknown performance stays None; it is never treated as perfect.
"""

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from statistics import median
from typing import Final, Literal

from bido_back.purchasing.analysis import (
    MAX_SUGGESTED,
    Flag,
    Params,
    RatedSupplier,
    delivery_outcomes,
    history_flags,
    latest,
    main_currency,
    measure_on_time,
    measure_quality,
    price_limits,
    price_trend,
    round_price,
    supplier_rank,
    totals_by_currency,
    weighted_avg,
)
from bido_back.purchasing.sources.contracts import (
    AssessmentLine,
    Issue,
    LineFlag,
    PriceBenchmark,
    PriceOutlier,
    Record,
    SourceRef,
    SpendTotal,
    SupplierEvidence,
)
from bido_back.purchasing.sources.evidence import (
    add_issue,
    normalize_unit,
    source_date,
    source_number,
    source_refs,
    source_text,
    unit_key,
    unit_price,
)
from bido_back.purchasing.sources.joins import CLOSED, OPEN, receipts_for, state

# The imported-data analysis's defaults: 365-day window, max 5% over, flags at 5% price
# increase, 80% on time and 98% quality.
PARAMS: Final = Params()
# Fewer comparable completed purchases than this propose no target or maximum price.
MIN_OBSERVATIONS: Final = 2
ORDER_KEY: Final = ("po_number", "po_line", "location")
_CENTS: Final = Decimal("0.01")
# A completed purchase this far (in %) from the median of the item's other price
# references is an outlier; judged only with at least this many other references.
OUTLIER_PCT: Final = Decimal(25)
OUTLIER_MIN_REFERENCES: Final = 2
# An order delivered in at most this share of the median lead time of the item's other
# completed orders is abnormally fast: a possible emergency purchase.
EMERGENCY_LEAD_SHARE: Final = Decimal("0.5")
_DAYS: Final = Decimal("0.1")


@dataclass(frozen=True, slots=True)
class PricePoint:
    """A completed purchase: the only kind of order that may set a price."""

    supplier: str
    currency: str
    ordered_at: date
    quantity: Decimal
    unit_price: Decimal
    order_key: tuple[str, ...]
    evidence: tuple[SourceRef, ...]
    # Source evidence carries no broker/reseller classification.
    supplier_type: str = ""


@dataclass(frozen=True, slots=True)
class Receipted:
    """An order in the shape the shared delivery and quality rules read."""

    quantity: Decimal
    promised_at: date | None
    received_at: date | None
    received_qty: Decimal | None
    rejected_qty: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class OrderFacts:
    """A purchase-order line of the item and what its physical receipts establish."""

    supplier: str
    status: Literal["open", "closed", "unknown"]
    ordered_at: date
    quantity: Decimal | None
    unit_price: Decimal | None
    currency: str | None
    same_unit: bool
    promised_at: date | None
    receipts: tuple[Record, ...]
    # None when the receipts do not reconcile (or the line has no order/item/plant key).
    received: Decimal | None
    # None when some receipt states no valid rejected quantity.
    rejected: Decimal | None
    # The last physical receipt on or before the snapshot date.
    received_at: date | None
    order: Record

    @property
    def completed(self) -> bool:
        """Closed and fully received: a completed purchase."""
        return (
            self.status == "closed"
            and self.quantity is not None
            and self.quantity > 0
            and self.received == self.quantity
        )

    @property
    def price(self) -> PricePoint | None:
        if (
            not self.completed
            or not self.same_unit
            or self.quantity is None
            or self.currency is None
            or self.unit_price is None
            or self.unit_price <= 0
        ):
            return None
        return PricePoint(
            supplier=self.supplier,
            currency=self.currency,
            ordered_at=self.ordered_at,
            quantity=self.quantity,
            unit_price=self.unit_price,
            order_key=tuple(source_text(self.order, field) or "" for field in ORDER_KEY),
            # The price cell; its row identifies the order.
            evidence=tuple(source_refs([self.order], "unit_price")),
        )

    @property
    def delivery(self) -> Receipted | None:
        """The delivery outcome's input; None when the outcome cannot be established."""
        quantity, received = self.quantity, self.received
        if self.promised_at is None or quantity is None or quantity <= 0 or received is None:
            return None
        if received >= quantity or (self.status == "closed" and received > 0):
            return Receipted(quantity, self.promised_at, self.received_at, received)
        if self.status == "open":
            # Not fully received: late once the promised date has passed, unknown before.
            return Receipted(quantity, self.promised_at, None, None)
        return None

    @property
    def quality(self) -> Receipted | None:
        if (
            self.quantity is None
            or self.received is None
            or self.received <= 0
            or self.rejected is None
        ):
            return None
        return Receipted(
            self.quantity, self.promised_at, self.received_at, self.received, self.rejected
        )

    @property
    def lead_time_days(self) -> int | None:
        if (
            self.quantity is None
            or self.received is None
            or self.received <= 0
            or self.received_at is None
        ):
            return None
        if self.received < self.quantity and self.status != "closed":
            return None  # Still being delivered.
        days = (self.received_at - self.ordered_at).days
        return days if days >= 0 else None


def _currency_code(value: str | None) -> str | None:
    if value is None or not re.fullmatch(r"[A-Za-z]{3}", value):
        return None
    return value.upper()


def reconcile_order(
    order: Record,
    records: Sequence[Record],
    *,
    supplier: str,
    ordered_at: date,
    unit: str | None,
    as_of: date,
    issues: list[Issue],
) -> OrderFacts:
    """Join one coalesced order line to its physical receipts on or before `as_of`.

    Receipt problems are reported as warnings and make the order's receipts unknown; they
    never become a fabricated delivery. A line without its order/item/plant key is not
    joined to receipts.
    """
    problems: list[Issue] = []
    receipts: list[Record] = []
    received: Decimal | None = None
    if all(source_text(order, field) for field in ORDER_KEY):
        receipts, received = receipts_for(order, records, as_of, problems)
    quantity = source_number(order, "quantity", problems)
    issues.extend(problem.model_copy(update={"severity": "warning"}) for problem in problems)
    if problems:
        received = None
    rejected: Decimal | None = None if received is None else Decimal(0)
    for receipt in receipts:
        value = source_number(receipt, "rejected", [])
        amount = source_number(receipt, "quantity", [])
        if rejected is None or value is None or amount is None or value > amount:
            rejected = None
            break
        rejected += value
    days = [day for r in receipts if (day := source_date(r, "received_at", [])) is not None]
    status = state(order)
    return OrderFacts(
        supplier=supplier,
        status="closed" if status in CLOSED else "open" if status in OPEN else "unknown",
        ordered_at=ordered_at,
        quantity=quantity,
        unit_price=unit_price(order, []),
        currency=_currency_code(source_text(order, "currency")),
        same_unit=unit_key(order) == normalize_unit(unit),
        promised_at=source_date(order, "promised_at", []),
        receipts=tuple(receipts),
        received=received,
        rejected=rejected,
        received_at=max(days, default=None),
        order=order,
    )


def reference_date(records: Sequence[Record]) -> date | None:
    """The latest date unscoped evidence describes: a stock snapshot, an order or a receipt."""
    fields = {"inventory": "as_of", "purchase": "ordered_at", "receipt": "received_at"}
    days = [
        day
        for record in records
        if record.kind in fields
        and (day := source_date(record, fields[record.kind], [])) is not None
    ]
    return max(days, default=None)


@dataclass(frozen=True, slots=True)
class _CurrencyOf:
    currency: str
    ordered_at: date
    quantity: Decimal = Decimal(0)
    unit_price: Decimal = Decimal(0)
    supplier_type: str = ""


def _line_currency(points: Sequence[PricePoint], history: Sequence[OrderFacts]) -> str | None:
    if points:
        return main_currency(points)
    stated = [_CurrencyOf(o.currency, o.ordered_at) for o in history if o.currency is not None]
    return main_currency(stated) if stated else None


def _label(supplier: SupplierEvidence) -> str | None:
    return supplier.name or supplier.supplier_code


def _extend(target: list[SourceRef], refs: Iterable[SourceRef]) -> None:
    for ref in refs:
        if ref not in target:
            target.append(ref)


def _cost(quantity: Decimal | None, price: Decimal) -> Decimal | None:
    if quantity is None:
        return None
    return (quantity * price).quantize(_CENTS, rounding=ROUND_HALF_UP)


def _measure_supplier(
    supplier: SupplierEvidence,
    orders: Sequence[OrderFacts],
    points: Sequence[PricePoint],
    as_of: date,
) -> None:
    supplier.orders = len(orders)
    supplier.quantity = (
        sum((o.quantity for o in orders if o.quantity is not None), Decimal(0))
        if all(o.same_unit for o in orders)
        else None
    )
    average = weighted_avg(points)
    supplier.avg_price = round_price(average) if average is not None else None
    supplier.min_price = round_price(min(p.unit_price for p in points)) if points else None
    deliveries = [(o, d) for o in orders if (d := o.delivery) is not None]
    supplier.on_time_rate, supplier.delivery_observations = measure_on_time(
        [d for _, d in deliveries], as_of
    )
    supplier.quality_rate, supplier.quality_observations = measure_quality(
        [q for o in orders if (q := o.quality) is not None], as_of
    )
    leads = [days for o in orders if (days := o.lead_time_days) is not None]
    supplier.avg_lead_time_days = (
        (Decimal(sum(leads)) / len(leads)).quantize(_DAYS, rounding=ROUND_HALF_UP)
        if leads
        else None
    )
    supplier.last_received_at = max(
        (o.received_at for o in orders if o.received_at is not None), default=None
    )
    # Point to what lowered the measures, one cell each (its row identifies the order or
    # receipt): the promised date of late, short or overdue orders and stated rejections.
    failed = [o.order for o, d in deliveries if delivery_outcomes([d], as_of) == [False]]
    rejecting = [
        r
        for o in orders
        if o.quality is not None
        for r in o.receipts
        if (source_number(r, "rejected", []) or Decimal(0)) > 0
    ]
    _extend(supplier.evidence, source_refs(failed, "promised_at"))
    _extend(supplier.evidence, source_refs(rejecting, "rejected"))


def _outliers(
    points: Sequence[PricePoint],
    listed: Sequence[SupplierEvidence],
    currency: str | None,
    suppliers: Mapping[str, SupplierEvidence],
) -> list[tuple[PricePoint, Decimal]]:
    """Completed-purchase prices at least `OUTLIER_PCT` away from the median of the item's
    other price references: the other completed purchases and current agreements in the
    same currency. Needs `OUTLIER_MIN_REFERENCES` other references to judge."""
    agreements = [
        s.contract_price
        for s in listed
        if s.contract_price is not None
        and s.contract_price > 0
        and s.contract_expired is False
        and (s.contract_currency or currency) == currency
    ]
    found: list[tuple[PricePoint, Decimal]] = []
    for point in points:
        others = [p.unit_price for p in points if p is not point] + agreements
        if len(others) < OUTLIER_MIN_REFERENCES or point.supplier not in suppliers:
            continue
        reference = median(others)
        if reference > 0 and abs(point.unit_price / reference - 1) * 100 >= OUTLIER_PCT:
            found.append((point, reference))
    return found


def _flag_outliers(
    line: AssessmentLine,
    points: Sequence[PricePoint],
    orders: Sequence[OrderFacts],
    currency: str | None,
    suppliers: Mapping[str, SupplierEvidence],
    params: Params,
    target: Decimal,
) -> list[PriceOutlier]:
    """Flag outlying completed-purchase prices; the benchmark keeps them (never dropped
    silently), and each flag says what the target and maximum would be without it."""
    # Lead times of every completed order of the item, not only the price window.
    leads = {
        tuple(source_text(o.order, field) or "" for field in ORDER_KEY): o.lead_time_days
        for o in orders
        if o.supplier in suppliers
    }
    found: list[PriceOutlier] = []
    for point, reference in _outliers(points, line.suppliers, currency, suppliers):
        lead = leads.get(point.order_key)
        other_leads = [
            Decimal(days)
            for key, days in leads.items()
            if key != point.order_key and days is not None
        ]
        typical = median(other_leads) if len(other_leads) >= OUTLIER_MIN_REFERENCES else None
        rest = [p for p in points if p is not point]
        rest_average = weighted_avg(rest)
        without: tuple[Decimal, Decimal] | None = None
        if rest and rest_average is not None:
            without = price_limits(
                round_price(latest(rest).unit_price),
                round_price(rest_average),
                params.max_over_pct,
            )
        outlier = PriceOutlier(
            supplier=_label(suppliers[point.supplier]),
            ordered_at=point.ordered_at,
            unit_price=round_price(point.unit_price),
            reference_price=round_price(reference),
            deviation_pct=((point.unit_price / reference - 1) * 100).quantize(_DAYS),
            in_benchmark=True,
            target_without=without[0] if without else None,
            max_without=without[1] if without else None,
            lead_time_days=lead,
            reference_lead_time_days=typical,
            possible_emergency=lead is not None
            and typical is not None
            and Decimal(lead) <= typical * EMERGENCY_LEAD_SHARE,
            evidence=list(point.evidence),
        )
        found.append(outlier)
        effect = (
            "it does not change the target"
            if without is None or without[0] == target
            else f"without it the target would be {without[0]:f}"
        )
        line.issues.append(
            Issue(
                code="price_outlier",
                severity="warning",
                message=f"The {outlier.unit_price:f} {currency} paid to "
                f"{outlier.supplier or 'a supplier'} on {outlier.ordered_at.isoformat()} is "
                f"{outlier.deviation_pct:+f}% from the median of the item's other price "
                f"references ({outlier.reference_price:f}; rule: {OUTLIER_PCT}% or more). It "
                f"stays in the internal average and maximum; {effect}"
                + (
                    f", and without it the maximum would be {without[1]:f}"
                    if without is not None
                    else ""
                )
                + "."
                + (
                    f" It was delivered in {lead} days against a median of {typical:f} for "
                    "the other orders: possibly an emergency purchase (the evidence states no "
                    "cause)."
                    if outlier.possible_emergency and typical is not None
                    else ""
                ),
                evidence=outlier.evidence,
            )
        )
    return found


def _rank_key(
    supplier: SupplierEvidence,
) -> tuple[bool, bool, bool, Decimal, bool, Decimal, bool, Decimal, str, str]:
    rank = supplier_rank(
        broker=False,
        quality_rate=supplier.quality_rate,
        on_time_rate=supplier.on_time_rate,
        avg_price=supplier.avg_price,
    )
    return (supplier.orders == 0, *rank, supplier.supplier_code or "", supplier.name or "")


def _line_flag(flag: Flag) -> LineFlag:
    code = flag.code
    if code == "on_order_from_pos":  # Stock netting of imported analyses, not history.
        raise ValueError(code)
    return LineFlag(code=code, supplier=flag.supplier, value=flag.value)


def measure(
    line: AssessmentLine,
    suppliers: Mapping[str, SupplierEvidence],
    orders: Sequence[OrderFacts],
    as_of: date | None,
    params: Params = PARAMS,
) -> None:
    """Fill the line's measured supplier statistics, ranking, price benchmark and flags.

    `suppliers` maps each order's supplier key to its evidence in `line.suppliers`. Call
    it once the quantity is calculated: estimated costs and `covered_by_stock` use it.
    The benchmark is an internal reference, never a quotation or an approved limit; its
    maximum is a historical escalation threshold, not a walk-away price. Below
    `MIN_OBSERVATIONS` comparable completed purchases there is no benchmark at all.
    """
    history: list[OrderFacts] = []
    if as_of is not None:
        start = as_of - timedelta(days=params.lookback_days)
        history = [
            o
            for o in orders
            if o.supplier in suppliers
            and start <= o.ordered_at <= as_of
            and o.quantity is not None
            and o.quantity > 0
        ]
    priced = [p for o in history if (p := o.price) is not None]
    currency = _line_currency(priced, history)
    current = [o for o in history if currency is None or o.currency in {currency, None}]
    # Deterministic, whatever the row order: by date, then order number.
    points = sorted(
        (p for p in priced if p.currency == currency), key=lambda p: (p.ordered_at, p.order_key)
    )
    grouped: dict[str, list[OrderFacts]] = defaultdict(list)
    for order in current:
        grouped[order.supplier].append(order)
    if as_of is not None:
        for key, group in grouped.items():
            _measure_supplier(
                suppliers[key], group, [p for p in points if p.supplier == key], as_of
            )
    line.suppliers.sort(key=_rank_key)
    # Identity, not equality: two suppliers may carry identical evidence.
    # A supplier the buyer's master blocks for purchasing is never suggested.
    chosen = {
        id(s)
        for s in [s for s in line.suppliers if s.orders > 0 and not s.purchase_blocked][
            :MAX_SUGGESTED
        ]
    }
    for supplier in line.suppliers:
        supplier.suggested = id(supplier) in chosen

    line.price = None
    if history and len(points) < MIN_OBSERVATIONS:
        add_issue(
            line.issues,
            "insufficient_price_history",
            f"Fewer than {MIN_OBSERVATIONS} comparable completed purchases (received in full, "
            "same unit and currency) in the lookback window: no internal target or maximum "
            "price is proposed. Suppliers' last prices stay visible.",
            [o.order for o in current if (p := o.price) is not None and p.currency == currency],
            "unit_price",
            warning=True,
        )
    if len(points) >= MIN_OBSERVATIONS and as_of is not None:
        last = latest(points)
        average = weighted_avg(points)
        assert average is not None  # Price points have positive quantities.
        last_price, avg_price = round_price(last.unit_price), round_price(average)
        target, maximum = price_limits(last_price, avg_price, params.max_over_pct)
        evidence: list[SourceRef] = []
        for point in points:
            _extend(evidence, point.evidence)
        found = _flag_outliers(line, points, orders, currency, suppliers, params, target)
        line.price = PriceBenchmark(
            currency=last.currency,
            observations=len(points),
            window_days=params.lookback_days,
            last_price=last_price,
            last_supplier=_label(suppliers[last.supplier]),
            last_ordered_at=last.ordered_at,
            avg_price=avg_price,
            min_price=round_price(min(p.unit_price for p in points)),
            trend_pct=price_trend(points, as_of),
            target_price=target,
            max_price=maximum,
            estimated_cost_at_target=_cost(line.calculated_to_buy, target),
            estimated_cost_at_last=_cost(line.calculated_to_buy, last_price),
            outliers=found,
            evidence=evidence,
        )

    flags: list[LineFlag] = []
    if line.calculated_to_buy is not None and line.calculated_to_buy == 0:
        flags.append(LineFlag(code="covered_by_stock"))
    if not history:
        flags.append(LineFlag(code="no_history"))
    else:
        rated = [
            RatedSupplier(_label(s), s.on_time_rate, s.quality_rate)
            for s in line.suppliers
            if s.orders > 0
        ]
        trend = line.price.trend_pct if line.price is not None else None
        flags.extend(
            _line_flag(flag)
            for flag in history_flags(
                rated,
                mixed_currency=len(current) < len(history),
                trend_pct=trend,
                params=params,
            )
        )
    line.flags = flags

    unpriced = [o.order for o in current if o.completed and o.price is None]
    if unpriced:
        add_issue(
            line.issues,
            "unpriced_purchase_history",
            "Completed purchases without a positive unit price, an explicit currency or the "
            "line's physical unit are not comparable and are left out of the internal price "
            "benchmark.",
            unpriced,
            "unit_price",
            "currency",
            "unit",
            warning=True,
        )


def spend_totals(lines: Iterable[AssessmentLine]) -> list[SpendTotal]:
    """Spend per currency of the lines to buy: the sum of their estimated costs.

    Only lines with a calculated quantity to buy and a price benchmark count. Amounts in
    different currencies are never added up or converted. These are internal estimates,
    not quotations.
    """
    amounts: list[tuple[str, Decimal, Decimal]] = []
    for line in lines:
        price = line.price
        if price is None or line.calculated_to_buy is None or line.calculated_to_buy <= 0:
            continue
        if price.estimated_cost_at_target is None or price.estimated_cost_at_last is None:
            continue
        amounts.append(
            (price.currency, price.estimated_cost_at_target, price.estimated_cost_at_last)
        )
    return [
        SpendTotal(
            currency=total.currency,
            lines=total.items_to_buy,
            total_at_target=total.total_at_target,
            total_at_last_price=total.total_at_last_price,
        )
        for total in totals_by_currency(amounts)
    ]
