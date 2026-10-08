"""The purchase analysis: what to buy for a build, how much, from whom, at what price.

Pure code, no database and no model: `analyze()` takes the bill of materials,
stock and purchase history already loaded and returns one line per item. The
LLM only explains the result (the copilot); every number comes from here.

Formulas (per item of the product's BOM):

* `required = qty_per_unit * quantity` (units of the product to build).
* `to_buy = max(0, required * (1 + safety_stock_pct / 100) - on_hand - on_order)`,
  rounded UP: to whole units for discrete units (pcs, un, pz, stk, ...), to
  3 decimals otherwise (kg, m). With dated demand, supply is consumed once in
  chronological order and rounding surplus carries into the next requirement.
* `on_order`: the stock's on-order quantity when the stock export gives one
  (it already counts open purchase orders, so they are not added again).
  An aggregate without an arrival date cannot cover dated demand. Otherwise
  the open purchase-order lines due in time: not yet received
  (no receipt date), promised on or before `need_by` (any date without one),
  and not stale (promised at most `open_po_stale_days` ago). Each counts its
  ordered minus received quantity; the line is flagged `on_order_from_pos`. A
  line without a promised date is not counted (nobody knows when it arrives),
  nor is a partly received line with a receipt date (it may be closed short).
* History window: purchase-order lines ordered in the last `lookback_days`
  (365 by default), in the item's currency: the one most of its lines in the
  window were bought in (on a tie, one a non-broker sold in, then the most
  recent purchase's). Lines in other currencies are ignored and the item is
  flagged `mixed_currency`: one recent broker purchase in EUR does not hide a
  year of purchases in BRL.
* `last_price`: unit price of the most recent purchase. `avg_price`: the
  quantity-weighted average. `min_price`: the lowest.
* `trend_pct = (avg of the last 90 days - avg before that) / avg before * 100`;
  None unless both periods have purchases.
* `target_price = min(last_price, avg_price)`: aim at the better of the
  recent price and the year's average (a recent increase is not accepted as
  the new normal). `max_price = max(last_price, avg_price) * (1 +
  max_over_pct / 100)` (5% by default): the most the buyer should accept
  without escalating.
* Per supplier: `on_time_rate` measures complete, timely receipts and overdue
  open lines; partial receipts are failures, missing receipt quantities unknown.
  `quality_rate = 1 - rejected / received` over validated, dated receipts.
* Suppliers are ranked: brokers/resellers last, then by measured quality, on-time
  rate and average price. Unknown performance follows measured performance.
  Up to 3 are suggested,
  brokers only when nobody else ever sold the item.

Currencies: every line keeps the currency of its item's purchases (above). The
summary's totals are per currency (`totals`): amounts in different currencies
are never added up or converted here (historical prices paid in BRL and in EUR
are not comparable at today's rate). `currency` / `total_at_*` are set only
when every line to buy is in one currency.

Flags per line: `no_history`, `single_source`, `price_up` (trend above
`price_up_pct`), `supplier_late` (on-time below `late_threshold`),
`quality_issue` (quality below `quality_threshold`), `broker_source`,
`mixed_currency`, `covered_by_stock` (nothing to buy).
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Final, Literal
from uuid import UUID

PRICE_PLACES: Final = Decimal("0.0001")
RATE_PLACES: Final = Decimal("0.0001")
PCT_PLACES: Final = Decimal("0.01")
QTY_PLACES: Final = Decimal("0.001")
RECENT_DAYS: Final = 90
MAX_SUGGESTED: Final = 3
DISCRETE_UNITS: Final = frozenset(
    {"pcs", "pc", "piece", "pieces", "un", "und", "unid", "unit", "units", "ea", "each",
     "pz", "pezzi", "pezzo", "stk", "stück", "peca", "pecas", "peça", "peças", "set", "kit"}
)  # fmt: skip
BROKER_TYPES: Final = frozenset({"reseller", "broker"})

FlagCode = Literal[
    "no_history",
    "single_source",
    "price_up",
    "supplier_late",
    "quality_issue",
    "broker_source",
    "mixed_currency",
    "covered_by_stock",
    "on_order_from_pos",
]


@dataclass(frozen=True, slots=True)
class Params:
    safety_stock_pct: Decimal = Decimal(0)
    lookback_days: int = 365
    max_over_pct: Decimal = Decimal(5)
    price_up_pct: Decimal = Decimal(5)
    late_threshold: Decimal = Decimal("0.8")
    quality_threshold: Decimal = Decimal("0.98")
    # An open line promised longer ago than this and still not received is stale.
    open_po_stale_days: int = 60
    location: str | None = None


@dataclass(frozen=True, slots=True)
class ItemInfo:
    id: UUID
    part_number: str
    description: str
    mpn: str | None
    manufacturer: str | None
    category: str | None
    unit: str


@dataclass(frozen=True, slots=True)
class BomEntry:
    item: ItemInfo
    qty_per_unit: Decimal


@dataclass(frozen=True, slots=True)
class Stock:
    # Eligible, available quantities; reservations have already been deducted.
    on_hand: Decimal = Decimal(0)
    on_order: Decimal = Decimal(0)
    on_order_due: date | None = None
    known: bool = True
    issues: tuple[str, ...] = ()
    gross_on_hand: Decimal = Decimal(0)
    reserved_qty: Decimal = Decimal(0)
    blocked_qty: Decimal = Decimal(0)
    # Separate dated receipts for multiple locations; never add the same PO again.
    inbound: tuple[tuple[date | None, Decimal], ...] = ()


@dataclass(frozen=True, slots=True)
class Demand:
    quantity: Decimal
    need_by: date | None


@dataclass(frozen=True, slots=True)
class DemandAllocation:
    need_by: date | None
    required: Decimal
    gross_required: Decimal
    stock_used: Decimal
    inbound_used: Decimal
    to_buy: Decimal
    planned_supply_used: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class Calculation:
    stock_known: bool = True
    stock_issues: tuple[str, ...] = ()
    gross_on_hand: Decimal = Decimal(0)
    reserved_qty: Decimal = Decimal(0)
    blocked_qty: Decimal = Decimal(0)
    allocations: tuple[DemandAllocation, ...] = ()
    excluded_inbound: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class Purchase:
    item_id: UUID
    supplier_id: UUID
    supplier_name: str
    supplier_type: str
    currency: str
    ordered_at: date
    quantity: Decimal
    unit_price: Decimal
    promised_at: date | None = None
    received_at: date | None = None
    received_qty: Decimal | None = None
    rejected_qty: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class SupplierStats:
    supplier_id: UUID
    name: str
    type: str
    orders: int
    quantity: Decimal
    last_price: Decimal
    last_ordered_at: date
    avg_price: Decimal
    min_price: Decimal
    on_time_rate: Decimal | None
    quality_rate: Decimal | None
    suggested: bool = False
    delivery_observations: int = 0
    quality_observations: int = 0
    last_received_at: date | None = None


@dataclass(frozen=True, slots=True)
class Flag:
    code: FlagCode
    supplier: str | None = None
    value: Decimal | None = None


@dataclass(frozen=True, slots=True)
class LineResult:
    position: int
    item: ItemInfo
    required: Decimal
    on_hand: Decimal
    on_order: Decimal
    to_buy: Decimal
    need_by: date | None
    currency: str | None
    last_price: Decimal | None
    avg_price: Decimal | None
    min_price: Decimal | None
    trend_pct: Decimal | None
    target_price: Decimal | None
    max_price: Decimal | None
    suppliers: tuple[SupplierStats, ...] = ()
    flags: tuple[Flag, ...] = ()
    calculation: Calculation = field(default_factory=Calculation)

    @property
    def suggested(self) -> tuple[SupplierStats, ...]:
        return tuple(s for s in self.suppliers if s.suggested)


@dataclass(frozen=True, slots=True)
class CurrencyTotal:
    currency: str
    items_to_buy: int
    total_at_target: Decimal
    total_at_last_price: Decimal


@dataclass(frozen=True, slots=True)
class Summary:
    items: int
    items_to_buy: int
    currency: str | None
    total_at_target: Decimal | None
    total_at_last_price: Decimal | None
    flagged_items: int
    totals: tuple[CurrencyTotal, ...] = ()


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    product: str
    quantity: Decimal
    need_by: date | None
    lines: tuple[LineResult, ...]
    summary: Summary
    params: Params = field(default_factory=Params)


# --- Helpers ---------------------------------------------------------------------------


def round_up_quantity(quantity: Decimal, unit: str) -> Decimal:
    if quantity <= 0:
        return Decimal(0)
    if unit.strip().lower() in DISCRETE_UNITS:
        return quantity.to_integral_value(rounding=ROUND_CEILING)
    return quantity.quantize(QTY_PLACES, rounding=ROUND_CEILING)


def _price(value: Decimal) -> Decimal:
    return value.quantize(PRICE_PLACES, rounding=ROUND_HALF_UP)


def _weighted_avg(rows: Sequence[Purchase]) -> Decimal | None:
    quantity = sum((p.quantity for p in rows), Decimal(0))
    if quantity <= 0:
        return None
    return sum((p.quantity * p.unit_price for p in rows), Decimal(0)) / quantity


def _main_currency(rows: Sequence[Purchase]) -> str:
    """Most lines; on a tie, a currency some non-broker sold in; then the latest."""
    counts: dict[str, int] = defaultdict(int)
    regular: set[str] = set()
    for row in rows:
        counts[row.currency] += 1
        if row.supplier_type not in BROKER_TYPES:
            regular.add(row.currency)
    most = max(counts.values())
    tied = [r for r in rows if counts[r.currency] == most]
    return _latest([r for r in tied if r.currency in regular] or tied).currency


def _latest(rows: Sequence[Purchase]) -> Purchase:
    # Stable: among purchases on the same day, the last one listed wins.
    best = rows[0]
    for row in rows[1:]:
        if row.ordered_at >= best.ordered_at:
            best = row
    return best


def _delivery_outcomes(rows: Sequence[Purchase], today: date) -> list[bool]:
    outcomes: list[bool] = []
    for row in rows:
        if row.promised_at is None:
            continue
        arrived = row.received_at is not None and row.received_at <= today
        if not arrived and row.promised_at >= today:
            continue
        if arrived and row.received_qty is None:
            continue
        outcomes.append(
            bool(
                arrived
                and row.received_qty is not None
                and row.received_qty >= row.quantity
                and row.received_at is not None
                and row.received_at <= row.promised_at
            )
        )
    return outcomes


def _supplier_stats(rows: Sequence[Purchase], today: date) -> SupplierStats:
    latest = _latest(rows)
    avg = _weighted_avg(rows)
    assert avg is not None  # rows have positive quantities
    delivery = _delivery_outcomes(rows, today)
    quality = [
        p
        for p in rows
        if p.received_at is not None
        and p.received_at <= today
        and p.received_qty is not None
        and p.received_qty > 0
        and 0 <= p.rejected_qty <= p.received_qty
    ]
    received = sum((p.received_qty for p in quality if p.received_qty is not None), Decimal(0))
    rejected = sum((p.rejected_qty for p in quality), Decimal(0))
    return SupplierStats(
        supplier_id=latest.supplier_id,
        name=latest.supplier_name,
        type=latest.supplier_type,
        orders=len(rows),
        quantity=sum((p.quantity for p in rows), Decimal(0)),
        last_price=_price(latest.unit_price),
        last_ordered_at=latest.ordered_at,
        avg_price=_price(avg),
        min_price=_price(min(p.unit_price for p in rows)),
        on_time_rate=(Decimal(sum(delivery)) / len(delivery)).quantize(RATE_PLACES)
        if delivery
        else None,
        quality_rate=(1 - rejected / received).quantize(RATE_PLACES) if received else None,
        delivery_observations=len(delivery),
        quality_observations=len(quality),
        last_received_at=max(
            (p.received_at for p in rows if p.received_at and p.received_at <= today), default=None
        ),
    )


def _rank_key(stats: SupplierStats) -> tuple[bool, bool, Decimal, bool, Decimal, Decimal, str]:
    return (
        stats.type in BROKER_TYPES,
        stats.quality_rate is None,
        -(stats.quality_rate if stats.quality_rate is not None else Decimal(0)),
        stats.on_time_rate is None,
        -(stats.on_time_rate if stats.on_time_rate is not None else Decimal(0)),
        stats.avg_price,
        str(stats.supplier_id),
    )


def _ranked(stats: list[SupplierStats]) -> tuple[SupplierStats, ...]:
    ordered = sorted(stats, key=_rank_key)
    regular = [s for s in ordered if s.type not in BROKER_TYPES]
    pool = regular or ordered
    chosen = {s.supplier_id for s in pool[:MAX_SUGGESTED]}
    return tuple(replace(s, suggested=s.supplier_id in chosen) for s in ordered)


def _trend(rows: Sequence[Purchase], today: date) -> Decimal | None:
    cutoff = today - timedelta(days=RECENT_DAYS)
    recent = _weighted_avg([p for p in rows if p.ordered_at > cutoff])
    older = _weighted_avg([p for p in rows if p.ordered_at <= cutoff])
    if recent is None or older is None or older == 0:
        return None
    return ((recent - older) / older * 100).quantize(PCT_PLACES, rounding=ROUND_HALF_UP)


# --- The analysis ----------------------------------------------------------------------


def analyze(
    *,
    product: str,
    quantity: Decimal,
    need_by: date | None,
    bom: Sequence[BomEntry],
    stock: Mapping[UUID, Stock],
    purchases: Sequence[Purchase],
    today: date,
    params: Params | None = None,
    demands: Sequence[Demand] | None = None,
) -> AnalysisResult:
    params = params or Params()
    window_start = today - timedelta(days=params.lookback_days)
    by_item: dict[UUID, list[Purchase]] = defaultdict(list)
    for purchase in purchases:
        if window_start <= purchase.ordered_at <= today and purchase.quantity > 0:
            by_item[purchase.item_id].append(purchase)

    lines: list[LineResult] = []
    for position, entry in enumerate(bom, start=1):
        item = entry.item
        held = stock.get(item.id, Stock(known=False, issues=("stock_unknown",)))
        scoped = sorted(
            demands or (Demand(quantity, need_by),), key=lambda d: d.need_by or date.max
        )
        allocations, from_pos, inbound_total, issues, excluded_inbound = _allocate(
            entry, scoped, held, purchases, today, params
        )
        required = sum((a.required for a in allocations), Decimal(0))
        to_buy = sum((a.to_buy for a in allocations), Decimal(0))
        shortage_dates = [a.need_by for a in allocations if a.to_buy > 0 and a.need_by]
        line_need_by = min(shortage_dates) if shortage_dates else need_by
        held = replace(held, on_order=inbound_total)
        line = _line(
            position, item, required, held, to_buy, line_need_by, by_item[item.id], today, params
        )
        if from_pos:
            line = replace(line, flags=(*line.flags, Flag("on_order_from_pos", value=from_pos)))
        lines.append(
            replace(
                line,
                calculation=Calculation(
                    stock_known=held.known,
                    stock_issues=issues,
                    gross_on_hand=held.gross_on_hand or held.on_hand,
                    reserved_qty=held.reserved_qty,
                    blocked_qty=held.blocked_qty,
                    allocations=allocations,
                    excluded_inbound=excluded_inbound,
                ),
            )
        )

    return AnalysisResult(
        product=product,
        quantity=quantity,
        need_by=need_by,
        lines=tuple(lines),
        summary=_summary(lines),
        params=params,
    )


def _allocate(
    entry: BomEntry,
    demands: Sequence[Demand],
    held: Stock,
    purchases: Sequence[Purchase],
    today: date,
    params: Params,
) -> tuple[tuple[DemandAllocation, ...], Decimal, Decimal, tuple[str, ...], Decimal]:
    """Consume each stock/receipt once in demand order; late supply cannot erase a shortage."""
    if any(d.quantity <= 0 for d in demands):
        raise ValueError("demand quantities must be positive")
    issues = list(held.issues)
    if held.on_order == 0 and any(
        p.item_id == entry.item.id
        and p.received_at is not None
        and p.received_qty is not None
        and p.received_qty < p.quantity
        and p.promised_at is not None
        and p.promised_at >= today - timedelta(days=params.open_po_stale_days)
        for p in purchases
    ):
        issues.append("partial_receipt_status_unknown")
    from_pos = Decimal(0)
    receipts = list(held.inbound or ((held.on_order_due, held.on_order),))
    if held.on_order == 0 and not held.inbound:
        # Imported order history has no destination. It cannot establish availability
        # at an explicitly selected warehouse.
        if params.location is not None:
            if any(p.item_id == entry.item.id and p.received_at is None for p in purchases):
                issues.append("inbound_location_unknown")
            receipts = []
        else:
            stale = today - timedelta(days=params.open_po_stale_days)
            receipts = [
                (p.promised_at, max(p.quantity - (p.received_qty or Decimal(0)), Decimal(0)))
                for p in purchases
                if p.item_id == entry.item.id
                and p.received_at is None
                and p.promised_at is not None
                and p.promised_at >= stale
                and p.ordered_at <= today
            ]
            from_pos = sum(
                (
                    qty
                    for due, qty in receipts
                    if demands[-1].need_by is None or due <= demands[-1].need_by
                ),
                Decimal(0),
            )
    elif any(due is None and qty > 0 for due, qty in receipts):
        issues.append("inbound_date_unknown")
    excluded_inbound = Decimal(0)
    if any(d.need_by is not None for d in demands):
        excluded_inbound = sum((qty for due, qty in receipts if due is None), Decimal(0))
        receipts = [(due, qty) for due, qty in receipts if due is not None]
    receipts.sort(key=lambda receipt: receipt[0] or date.min)
    inbound_total = sum(
        (
            qty
            for due, qty in receipts
            if due is None or demands[-1].need_by is None or due <= demands[-1].need_by
        ),
        Decimal(0),
    )
    excluded_inbound += sum((qty for _, qty in receipts), Decimal(0)) - inbound_total
    remaining_stock = held.on_hand
    remaining_inbound = Decimal(0)
    purchased_surplus = Decimal(0)
    cursor = 0
    result: list[DemandAllocation] = []
    for demand in demands:
        while cursor < len(receipts):
            due, qty = receipts[cursor]
            if due is not None and demand.need_by is not None and due > demand.need_by:
                break
            remaining_inbound += qty
            cursor += 1
        required = entry.qty_per_unit * demand.quantity
        gross = required * (1 + params.safety_stock_pct / 100)
        stock_used = min(remaining_stock, gross)
        remaining_stock -= stock_used
        inbound_used = min(remaining_inbound, gross - stock_used)
        remaining_inbound -= inbound_used
        surplus_used = min(purchased_surplus, gross - stock_used - inbound_used)
        purchased_surplus -= surplus_used
        shortage = gross - stock_used - inbound_used - surplus_used
        to_buy = round_up_quantity(shortage, entry.item.unit)
        purchased_surplus += to_buy - shortage
        result.append(
            DemandAllocation(
                need_by=demand.need_by,
                required=required,
                gross_required=gross,
                stock_used=stock_used,
                inbound_used=inbound_used,
                to_buy=to_buy,
                planned_supply_used=surplus_used,
            )
        )
    return tuple(result), from_pos, inbound_total, tuple(dict.fromkeys(issues)), excluded_inbound


def open_on_order(
    purchases: Iterable[Purchase],
    item_id: UUID,
    need_by: date | None,
    today: date,
    params: Params,
) -> Decimal:
    """What open purchase-order lines will still deliver of an item in time (see the top)."""
    stale = today - timedelta(days=params.open_po_stale_days)
    total = Decimal(0)
    for p in purchases:
        if (
            p.item_id != item_id
            or p.received_at is not None
            or p.promised_at is None
            or p.ordered_at > today
            or params.location is not None
        ):
            continue
        if p.promised_at < stale or (need_by is not None and p.promised_at > need_by):
            continue
        total += max(p.quantity - (p.received_qty or Decimal(0)), Decimal(0))
    return total


def _line(
    position: int,
    item: ItemInfo,
    required: Decimal,
    held: Stock,
    to_buy: Decimal,
    need_by: date | None,
    history: list[Purchase],
    today: date,
    params: Params,
) -> LineResult:
    flags: list[Flag] = []
    if to_buy == 0:
        flags.append(Flag("covered_by_stock"))
    if not history:
        flags.append(Flag("no_history"))
        return LineResult(
            position=position,
            item=item,
            required=required,
            on_hand=held.on_hand,
            on_order=held.on_order,
            to_buy=to_buy,
            need_by=need_by,
            currency=None,
            last_price=None,
            avg_price=None,
            min_price=None,
            trend_pct=None,
            target_price=None,
            max_price=None,
            flags=tuple(flags),
        )

    currency = _main_currency(history)
    rows = [p for p in history if p.currency == currency]
    if len(rows) < len(history):
        flags.append(Flag("mixed_currency"))

    per_supplier: dict[UUID, list[Purchase]] = defaultdict(list)
    for row in rows:
        per_supplier[row.supplier_id].append(row)
    suppliers = _ranked([_supplier_stats(group, today) for group in per_supplier.values()])

    last = _price(_latest(rows).unit_price)
    avg_raw = _weighted_avg(rows)
    assert avg_raw is not None
    avg = _price(avg_raw)
    trend = _trend(rows, today)
    target = min(last, avg)
    maximum = _price(max(last, avg) * (1 + params.max_over_pct / 100))

    if len(suppliers) == 1:
        flags.append(Flag("single_source", supplier=suppliers[0].name))
    if trend is not None and trend > params.price_up_pct:
        flags.append(Flag("price_up", value=trend))
    for stats in suppliers:
        if stats.on_time_rate is not None and stats.on_time_rate < params.late_threshold:
            flags.append(Flag("supplier_late", supplier=stats.name, value=stats.on_time_rate))
        if stats.quality_rate is not None and stats.quality_rate < params.quality_threshold:
            flags.append(Flag("quality_issue", supplier=stats.name, value=stats.quality_rate))
        if stats.type in BROKER_TYPES:
            flags.append(Flag("broker_source", supplier=stats.name))

    return LineResult(
        position=position,
        item=item,
        required=required,
        on_hand=held.on_hand,
        on_order=held.on_order,
        to_buy=to_buy,
        need_by=need_by,
        currency=currency,
        last_price=last,
        avg_price=avg,
        min_price=_price(min(p.unit_price for p in rows)),
        trend_pct=trend,
        target_price=target,
        max_price=maximum,
        suppliers=suppliers,
        flags=tuple(flags),
    )


def currency_totals(
    lines: Iterable[tuple[str | None, Decimal, Decimal | None, Decimal | None]],
) -> tuple[CurrencyTotal, ...]:
    """Spend per currency of (currency, to_buy, target_price, last_price) lines to buy.

    Lines without a currency (no purchase history) have no price: not counted.
    """
    grouped: dict[str, list[tuple[Decimal, Decimal | None, Decimal | None]]] = defaultdict(list)
    for currency, to_buy, target, last in lines:
        if currency is not None and to_buy > 0:
            grouped[currency].append((to_buy, target, last))
    cents = Decimal("0.01")
    return tuple(
        CurrencyTotal(
            currency=currency,
            items_to_buy=len(rows),
            total_at_target=sum((q * t for q, t, _ in rows if t is not None), Decimal(0)).quantize(
                cents, rounding=ROUND_HALF_UP
            ),
            total_at_last_price=sum(
                (q * p for q, _, p in rows if p is not None), Decimal(0)
            ).quantize(cents, rounding=ROUND_HALF_UP),
        )
        for currency, rows in sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    )


def _summary(lines: Sequence[LineResult]) -> Summary:
    buying = [line for line in lines if line.to_buy > 0]
    totals = currency_totals(
        (line.currency, line.to_buy, line.target_price, line.last_price) for line in buying
    )
    single = totals[0] if len(totals) == 1 else None
    return Summary(
        items=len(lines),
        items_to_buy=len(buying),
        currency=single.currency if single else None,
        total_at_target=single.total_at_target if single else None,
        total_at_last_price=single.total_at_last_price if single else None,
        flagged_items=sum(
            1 for line in lines if any(f.code != "covered_by_stock" for f in line.flags)
        ),
        totals=totals,
    )
