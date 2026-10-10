"""Can a new order arrive before the line runs short? Lead times against its balance.

Pure code over the assessed line (`planning.assess_plan`, `analysis.assess`); the model
only words the result. The quantity to buy never changes here:

* The projected balance per need date is the free stock plus the open orders as the
  netting counts them, minus the dated demand, without new orders. The first shortage
  date is the first one below the safety stock; the first stockout date, below zero.
* Each usable source's lead time comes from its purchasing agreement (info record),
  else the average of its completed orders of the item, else the source's reported
  supplier lead time, else the material's planned delivery time (`LeadTimeSource`). An
  order placed on the reference date arrives that many calendar days later: it meets
  the line by the first shortage date, meets it consuming safety stock by the first
  stockout date, else does not meet it.
* The best-ranked usable source (`pricing.measure`) is recommended when it meets the
  line; else the fastest source that does; when none does, the stockout is unavoidable:
  the best-ranked source stays recommended if it arrives within the horizon, else the
  fastest. The shortfall below zero before that arrival, the demands behind it and
  production planning to involve are listed.
* Suggested (and so invited by default): the recommended source first, then the usable
  ones arriving by the last purchase need or with an unknown lead time, up to
  `MAX_SUGGESTED`. A source arriving after every need is not suggested, and says so.
* Open orders the MRP list asks to move, due after the first stockout date, or overdue
  become expedite actions, with how much of the shortfall they would cover.
* A line whose known suppliers are all blocked, disqualified or without a current
  agreement or purchase history gets a sourcing proposal limited to suppliers of the
  material's own group in the evidence.
* Every evaluated line gets a one-line reason: why it is an alert (`TimingAlert.reason`)
  or why it is not (`alert_exclusions`): stock covering the demand, no demand in the
  horizon, an order arriving in time. Nothing is left out silently.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Final

from bido_back.purchasing.analysis import MAX_SUGGESTED
from bido_back.purchasing.importers import norm
from bido_back.purchasing.sources.contracts import (
    AffectedDemand,
    AlertExclusion,
    AssessmentLine,
    Assumption,
    DatedQuantity,
    ExpediteAction,
    ExpediteReason,
    InboundEvidence,
    Issue,
    LeadTimeSource,
    ProjectedBalance,
    Record,
    SourcingProposal,
    SupplierEvidence,
    TimingAlert,
    TimingCheck,
)
from bido_back.purchasing.sources.evidence import (
    categorical,
    join_key,
    same_key,
    source_number,
    source_refs,
    source_text,
)
from bido_back.purchasing.sources.status import disqualified

ZERO: Final = Decimal(0)
_SOURCE_LABEL: Final[dict[LeadTimeSource, str]] = {
    "agreement": "info record",
    "history": "average of past orders",
    "reported": "source's supplier lead time",
    "material_master": "material's planned delivery time",
}
# "0000004113/300000030": an MRP dependent requirement's order and the product it feeds.
_PEGGED: Final = re.compile(r"^\s*([A-Za-z0-9-]+)\s*/\s*([A-Za-z0-9-]{6,})\s*$")
MAX_CANDIDATES: Final = 5
MAX_COMPONENTS: Final = 3


def need_schedule(line: AssessmentLine) -> list[DatedQuantity]:
    """The quantity to buy per need date: the netting's purchase steps."""
    steps = [
        DatedQuantity(need_by=step.need_by, quantity=step.purchase_quantity)
        for step in line.netting
        if step.purchase_quantity > 0
    ]
    if steps or line.netting:
        return steps
    need = line.net_requirement or line.calculated_to_buy
    if line.need_by is not None and need is not None and need > 0:
        return [DatedQuantity(need_by=line.need_by, quantity=need)]
    return []


def demand_schedule(line: AssessmentLine) -> list[DatedQuantity]:
    """The gross demand per date, as the requirements give it (not one total)."""
    totals: dict[date, Decimal] = {}
    for contribution in line.demand_contributions:
        totals[contribution.need_by] = totals.get(contribution.need_by, ZERO) + (
            contribution.quantity
        )
    return [DatedQuantity(need_by=day, quantity=totals[day]) for day in sorted(totals)]


def usable(supplier: SupplierEvidence, agreements: bool = False) -> bool:
    """A source the buyer can order from: not blocked or disqualified (by its own code),
    and with a current agreement or a purchase history of the item.

    `agreements`: the item has purchasing agreements (e.g. SAP info records) at the
    plant. Then only a current one makes a source valid: an expired agreement or a
    history without one is price history, not a source of supply.
    """
    if disqualified(supplier):
        return False
    if agreements:
        return supplier.contract_expired is False
    return (
        supplier.orders > 0 or supplier.last_price is not None or supplier.contract_expired is False
    )


def has_agreements(line: AssessmentLine) -> bool:
    """The item's sources at the plant are governed by purchasing agreements."""
    return any(s.contract_expired is not None for s in line.suppliers)


def lead_time(
    supplier: SupplierEvidence, planned_days: Decimal | None
) -> tuple[int, LeadTimeSource] | None:
    options: list[tuple[Decimal | None, LeadTimeSource]] = [
        (supplier.contract_lead_time_days, "agreement"),
        (supplier.avg_lead_time_days, "history"),
        (supplier.lead_time_days, "reported"),
        (planned_days, "material_master"),
    ]
    for days, source in options:
        if days is not None and days > 0:
            return int(days.to_integral_value(rounding=ROUND_CEILING)), source
    return None


def project(
    line: AssessmentLine,
    as_of: date,
    moved: dict[tuple[str, str, str | None], date] | None = None,
) -> list[ProjectedBalance]:
    """The balance per need date without new orders; `moved` re-dates open orders."""
    moved = moved or {}
    arrivals: list[tuple[date, Decimal]] = []
    for order in line.inbound:
        when = moved.get((order.po_number, order.po_line, order.schedule_line))
        if when is None:
            if not order.eligible or order.expected_at is None:
                continue
            when = order.expected_at
        arrivals.append((max(when, as_of), order.quantity))
    days = sorted({c.need_by for c in line.demand_contributions})
    balance = line.available if line.available is not None else ZERO
    previous: date | None = None
    projected: list[ProjectedBalance] = []
    for day in days:
        balance += sum(
            (q for when, q in arrivals if when <= day and (previous is None or when > previous)),
            ZERO,
        )
        balance -= sum((c.quantity for c in line.demand_contributions if c.need_by == day), ZERO)
        projected.append(ProjectedBalance(on=day, balance=balance))
        previous = day
    return projected


@dataclass(frozen=True, slots=True)
class RecordIndex:
    """Lookups over all records that each line would otherwise scan: built once per
    assessment (a large ERP export has tens of thousands of records)."""

    # Item -> its first description (item master, purchase summary or requirement).
    descriptions: dict[str, str]
    # (production order, product) -> the requirements pegged to it: (item, label).
    pegged: dict[tuple[str, str], list[tuple[str, str]]]

    @classmethod
    def build(cls, records: Sequence[Record]) -> "RecordIndex":
        descriptions: dict[str, str] = {}
        pegged: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for record in records:
            code = source_text(record, "part_number")
            key = join_key(code)
            if key is None or code is None:
                continue
            if record.kind in ("item", "purchase_summary", "material_requirement"):
                text = source_text(record, "description")
                if text is not None:
                    descriptions.setdefault(key, text)
            if record.kind == "material_requirement":
                match = _PEGGED.match(source_text(record, "reference") or "")
                order, product = (
                    (join_key(match.group(1)), join_key(match.group(2))) if match else (None, None)
                )
                if order is not None and product is not None:
                    label = f"{code} {source_text(record, 'description') or ''}".strip()
                    pegged.setdefault((order, product), []).append((key, label))
        return cls(descriptions=descriptions, pegged=pegged)


def check_timing(
    line: AssessmentLine,
    *,
    as_of: date,
    master: Record | None,
    records: Sequence[Record],
    index: RecordIndex | None = None,
) -> None:
    """Fill the line's schedule, arrival per source, timing, expedite and sourcing."""
    schedule = need_schedule(line)
    line.need_schedule = schedule
    line.demand_schedule = demand_schedule(line)
    if not schedule or not line.demand_contributions:
        return
    safety = line.safety_stock or ZERO
    projected = project(line, as_of)
    shortage = next((p.on for p in projected if p.balance < safety), None)
    stockout = next((p.on for p in projected if p.balance < 0), None)
    timing = TimingCheck(
        order_date=as_of,
        safety_stock=safety,
        projected=projected,
        first_shortage_date=shortage,
        first_stockout_date=stockout,
        outcome="unknown",
    )
    line.timing = timing
    planned_days = source_number(master, "planned_delivery_days", [])
    for supplier in line.suppliers:
        found = lead_time(supplier, planned_days)
        if found is None:
            continue
        days, source = found
        arrival = as_of + timedelta(days=days)
        supplier.planned_lead_time_days = days
        supplier.lead_time_source = source
        supplier.estimated_arrival = arrival
        supplier.arrival_status = (
            "meets"
            if shortage is None or arrival <= shortage
            else "meets_consuming_safety_stock"
            if stockout is None or arrival <= stockout
            else "does_not_meet"
        )
        supplier.days_late = max(0, (arrival - shortage).days) if shortage is not None else 0
        supplier.coverable_quantity = sum(
            (need.quantity for need in schedule if need.need_by >= arrival), ZERO
        )
    line.expedite = _expedite(line, projected, as_of, records)
    agreements = has_agreements(line)
    timed = [s for s in line.suppliers if usable(s, agreements) and s.estimated_arrival is not None]
    if timed:
        _recommend(line, timing, timed, agreements, schedule[-1].need_by)
        _uncovered(line, timing, as_of, index or RecordIndex.build(records))
        _disclose(line, timing, schedule[-1].need_by, agreements)
    if not any(usable(s, agreements) for s in line.suppliers):
        line.sourcing = _sourcing(line, master, records)


def _arrival(supplier: SupplierEvidence) -> date:
    return supplier.estimated_arrival or date.max


def comparable_price(supplier: SupplierEvidence) -> Decimal | None:
    """The source's price per base unit to compare sources: its current agreement, else
    its measured average, else its last price."""
    if supplier.contract_price is not None and supplier.contract_expired is False:
        return supplier.contract_price
    return supplier.avg_price if supplier.avg_price is not None else supplier.last_price


def _cheapest(
    sources: Sequence[SupplierEvidence], currency: str | None, ranked: Sequence[SupplierEvidence]
) -> SupplierEvidence:
    """Cheapest per base unit in the line's currency (a price in another currency, or none,
    ranks after); at an equal price the earlier arrival; without a price, the measured
    rank (`ranked`, `pricing.measure`)."""
    order = {id(s): n for n, s in enumerate(ranked)}

    def key(supplier: SupplierEvidence) -> tuple[bool, Decimal, date, int]:
        price = comparable_price(supplier)
        other = supplier.contract_currency or supplier.currency
        known = price is not None and (currency is None or other is None or other == currency)
        return (
            not known,
            price if known and price is not None else ZERO,
            _arrival(supplier) if known else date.min,
            order.get(id(supplier), len(order)),
        )

    return min(sources, key=key)


def _recommend(
    line: AssessmentLine,
    timing: TimingCheck,
    timed: Sequence[SupplierEvidence],
    agreements: bool,
    last_need: date,
) -> None:
    """With purchasing agreements (the item's approved sources): the cheapest that arrives
    before the balance falls below the safety stock; else the cheapest that arrives before
    it runs out; else the fastest (an unavoidable stockout).

    Without agreements, the sources are known from purchase history only: the best-ranked
    by measured performance (`pricing.measure`) when it meets the line, else the fastest
    that does; when none does, the best-ranked if it arrives within the horizon, else the
    fastest.
    """
    currency = _line_currency(line)
    meets = [s for s in timed if s.arrival_status == "meets"]
    consuming = [s for s in timed if s.arrival_status == "meets_consuming_safety_stock"]
    preferred = timed[0]
    if not agreements:
        meeting = [*meets, *consuming]
        if preferred.arrival_status != "does_not_meet":
            chosen = preferred
        elif meeting:
            chosen = min(meeting, key=_arrival)
        elif _arrival(preferred) <= last_need:
            chosen = preferred
        else:
            chosen = min(timed, key=_arrival)
    elif meets:
        chosen = _cheapest(meets, currency, timed)
    elif consuming:
        chosen = _cheapest(consuming, currency, timed)
    else:
        chosen = min(
            timed,
            key=lambda s: (
                _arrival(s),
                comparable_price(s) if comparable_price(s) is not None else Decimal("Infinity"),
                s.supplier_code or "",
            ),
        )
    chosen.recommended = True
    timing.recommended_supplier = _name(chosen)
    timing.recommended_arrival = chosen.estimated_arrival
    timing.outcome = (
        "unavoidable_stockout"
        if chosen.arrival_status == "does_not_meet" or chosen.arrival_status is None
        else chosen.arrival_status
    )
    # The source the recommendation moved away from for timing: the best-ranked one, or
    # with agreements the cheapest.
    first_choice = _cheapest(timed, currency, timed) if agreements else preferred
    if first_choice is not chosen:
        timing.switched_from = _name(first_choice)
    if agreements and chosen.arrival_status == "meets" and consuming:
        cheaper = _cheapest(consuming, currency, timed)
        price, other = comparable_price(cheaper), comparable_price(chosen)
        if price is not None and other is not None and price < other:
            unit = line.unit or ""
            line.assumptions.append(
                Assumption(
                    code="cheaper_source_consumes_safety_stock",
                    message=f"{_name(chosen)} is recommended: it arrives "
                    f"{chosen.estimated_arrival} at {_plain(other)} per {unit}, before the "
                    f"balance falls below the safety stock. {_name(cheaper)} is cheaper "
                    f"({_plain(price)} per {unit}) but arrives {cheaper.estimated_arrival}, "
                    "while the safety stock is being consumed.",
                    alternative=f"Buying from {_name(cheaper)} saves on price and consumes "
                    "safety stock until it arrives.",
                    part_numbers=[line.part_number],
                )
            )
    # Recommended first, then the rest by measured rank. A source that does not meet the
    # line is suggested only when it arrives no later than the recommended one.
    line.suppliers.sort(key=lambda s: not s.recommended)
    eligible = [s for s in line.suppliers if usable(s, agreements) and _competes(s, chosen)]
    picked = {id(s) for s in eligible[:MAX_SUGGESTED]}
    for supplier in line.suppliers:
        supplier.suggested = id(supplier) in picked


def _line_currency(line: AssessmentLine) -> str | None:
    if line.price is not None and line.price.currency:
        return line.price.currency
    if line.currency:
        return line.currency
    found = {c for s in line.suppliers if (c := s.contract_currency or s.currency) is not None}
    return next(iter(found)) if len(found) == 1 else None


def _competes(supplier: SupplierEvidence, chosen: SupplierEvidence) -> bool:
    if supplier.recommended or supplier.estimated_arrival is None:
        return True
    if supplier.arrival_status != "does_not_meet":
        return True
    return _arrival(supplier) <= _arrival(chosen)


def late(supplier: SupplierEvidence) -> bool:
    """A usable source left out for timing: invited only when the buyer asks."""
    return (
        not supplier.suggested
        and supplier.estimated_arrival is not None
        and supplier.arrival_status == "does_not_meet"
    )


def _uncovered(line: AssessmentLine, timing: TimingCheck, as_of: date, index: RecordIndex) -> None:
    arrival = timing.recommended_arrival
    if arrival is None or timing.outcome != "unavoidable_stockout":
        return

    def deficit(projected: Sequence[ProjectedBalance]) -> Decimal:
        return max(
            (-p.balance for p in projected if p.on < arrival and p.balance < 0), default=ZERO
        )

    timing.uncovered_quantity = deficit(timing.projected)
    moved = {
        (a.po_number, a.po_line, a.schedule_line): a.requested_date
        for a in line.expedite
        if a.requested_date < a.current_date
    }
    if moved:
        timing.uncovered_after_expedite = deficit(project(line, as_of, moved))
    short = {p.on for p in timing.projected if p.on < arrival and p.balance < 0}
    timing.affected_demand = [
        _affected(c.reference, c.product_code, c.need_by, c.quantity, index, line.part_number)
        for c in line.demand_contributions
        if c.need_by in short
    ]
    timing.involve = ["production_planning"]
    unit = line.unit or ""
    first = min(short)
    after = timing.uncovered_after_expedite
    line.issues.append(
        Issue(
            code="unavoidable_stockout",
            severity="warning",
            message=(
                f"Stockout from {first.isoformat()}: no usable source arrives by then (the "
                f"recommended one, {timing.recommended_supplier}, arrives "
                f"{arrival.isoformat()}). Up to {_plain(timing.uncovered_quantity)} {unit} "
                "below zero before it arrives"
                + (
                    f"; {_plain(after)} {unit} if the expedite actions are honored"
                    if after is not None
                    else ""
                )
                + ". The demand dated then is at risk: involve production planning."
            ),
            evidence=[
                r for c in line.demand_contributions if c.need_by in short for r in c.evidence
            ][:10],
        )
    )


def _name(supplier: SupplierEvidence) -> str:
    return supplier.name or supplier.supplier_code or "a supplier"


def _plain(number: Decimal) -> str:
    text = format(number.normalize(), "f")
    return "0" if text in ("-0", "") else text


def _disclose(
    line: AssessmentLine, timing: TimingCheck, last_need: date, agreements: bool = False
) -> None:
    timed = [s for s in line.suppliers if s.estimated_arrival is not None and usable(s, agreements)]
    parts = [
        f"{_name(s)} {s.planned_lead_time_days} days ({_SOURCE_LABEL[s.lead_time_source]}) "
        f"→ {s.estimated_arrival.isoformat()}"
        for s in timed
        if s.lead_time_source is not None and s.estimated_arrival is not None
    ]
    left_out = [s for s in timed if late(s)]
    message = (
        f"Arrivals assume an order placed on the reference date {timing.order_date.isoformat()} "
        f"plus each source's lead time: {'; '.join(parts)}."
    )
    if timing.switched_from is not None:
        message += (
            f" {timing.switched_from} is not recommended because it arrives too late; "
            f"{timing.recommended_supplier} is."
        )
    if left_out:
        message += (
            f" {', '.join(_name(s) for s in left_out)} would arrive after the line runs out"
            + (
                f" and after the last need ({last_need.isoformat()})"
                if all(_arrival(s) > last_need for s in left_out)
                else ""
            )
            + ": not suggested nor invited by default."
        )
    line.assumptions.append(
        Assumption(
            code="arrival_from_lead_time",
            message=message,
            alternative="Current lead times confirmed by the suppliers may change who can "
            "deliver in time.",
            part_numbers=[line.part_number],
        )
    )


def _affected(
    reference: str,
    product: str | None,
    need_by: date,
    quantity: Decimal,
    index: RecordIndex,
    part: str,
) -> AffectedDemand:
    order = reference
    if (pegged := _PEGGED.match(reference)) is not None:
        order = pegged.group(1)
        product = product or pegged.group(2)
    description = None
    if product is not None:
        description = index.descriptions.get(join_key(product) or "")
    return AffectedDemand(
        reference=order,
        product_code=product,
        product_description=description,
        product_components=_components(index, order, product, part) if product else [],
        need_by=need_by,
        quantity=quantity,
    )


def _components(index: RecordIndex, order: str, product: str, part: str) -> list[str]:
    """Other materials the same production order needs, as the MRP list names them: they
    identify an undescribed product (e.g. a carcass "... BOMBA BC-125")."""
    found: list[str] = []
    own = join_key(part)
    for code, label in index.pegged.get((join_key(order) or "", join_key(product) or ""), []):
        if code == own:
            continue
        if label not in found:
            found.append(label)
        if len(found) >= MAX_COMPONENTS:
            break
    return found


def _expedite(
    line: AssessmentLine,
    projected: Sequence[ProjectedBalance],
    as_of: date,
    records: Sequence[Record],
) -> list[ExpediteAction]:
    stockout = next((p.on for p in projected if p.balance < 0), None)
    safety = line.safety_stock or ZERO
    actions: list[ExpediteAction] = []
    for order in line.inbound:
        current = order.expected_at
        if current is None:
            continue
        requested: date
        reason: ExpediteReason
        if order.reschedule_to is not None and order.reschedule_to != current:
            requested = order.reschedule_to
            reason = "mrp_reschedule_in" if requested < current else "mrp_reschedule_out"
        elif order.eligible and current < as_of:
            requested, reason = as_of, "overdue"
        elif stockout is not None and current > stockout:
            requested, reason = stockout, "due_after_shortage"
        else:
            continue
        would_cover = ZERO
        if requested < current:
            # The deepest shortfall below the safety stock while it is still on its way.
            deficit = max(
                (safety - p.balance for p in projected if requested <= p.on < current),
                default=ZERO,
            )
            would_cover = min(order.quantity, max(deficit, ZERO))
        if reason == "due_after_shortage" and would_cover <= 0:
            continue
        actions.append(
            ExpediteAction(
                po_number=order.po_number,
                po_line=order.po_line,
                schedule_line=order.schedule_line,
                supplier_code=order.supplier_code,
                supplier_name=_supplier_name(line, order, records),
                quantity=order.quantity,
                current_date=current,
                requested_date=requested,
                reason=reason,
                would_cover=would_cover,
                evidence=order.evidence[:6],
            )
        )
    return actions


def _supplier_name(
    line: AssessmentLine, order: InboundEvidence, records: Sequence[Record]
) -> str | None:
    known = next(
        (s for s in line.suppliers if same_key(s.supplier_code, order.supplier_code)), None
    )
    if known is not None and known.name is not None:
        return known.name
    return _master_name(records, order.supplier_code)


def _master_name(records: Sequence[Record], code: str | None) -> str | None:
    if code is None:
        return None
    return next(
        (
            name
            for r in records
            if r.kind == "supplier"
            and same_key(source_text(r, "supplier_code"), code)
            and (name := source_text(r, "name")) is not None
        ),
        None,
    )


def _why_not(supplier: SupplierEvidence) -> str:
    if supplier.purchase_blocked:
        return "blocked for purchasing"
    if disqualified(supplier):
        return f"status {supplier.qualification_status}"
    if supplier.contract_expired:
        until = supplier.contract_valid_to.isoformat() if supplier.contract_valid_to else ""
        return f"agreement expired {until}, no purchase history".replace("  ", " ")
    return "no current agreement or purchase history"


def _group(records: Sequence[Record], part: str) -> str | None:
    """The material's merchandise group, from its master or stock list."""
    for record in records:
        if record.kind in ("item", "inventory") and same_key(
            source_text(record, "part_number"), part
        ):
            group = source_text(record, "material_group")
            if group is not None:
                return group
    return None


def _sourcing(
    line: AssessmentLine, master: Record | None, records: Sequence[Record]
) -> SourcingProposal:
    """Only suppliers of the material's own merchandise group in the evidence: never
    suppliers of other categories."""
    excluded = [f"{_name(s)}: {_why_not(s)}" for s in line.suppliers]
    known = {join for s in line.suppliers if (join := norm(s.supplier_code or ""))}
    group = _group(records, line.part_number)
    candidates: list[str] = []
    if group is not None:
        parts = {
            norm(part)
            for r in records
            if r.kind in ("item", "inventory")
            and norm(source_text(r, "material_group") or "") == norm(group)
            and (part := source_text(r, "part_number")) is not None
            and not same_key(part, line.part_number)
        }
        blocked = {
            norm(code)
            for r in records
            if r.kind == "supplier"
            and categorical(r, "purchase_block") == "blocked"
            and (code := source_text(r, "supplier_code")) is not None
        }
        for record in records:
            if record.kind not in ("supplier_item", "purchase"):
                continue
            if norm(source_text(record, "part_number") or "") not in parts:
                continue
            code = source_text(record, "supplier_code")
            if code is None or norm(code) in known or norm(code) in blocked:
                continue
            name = _master_name(records, code) or code
            if name not in candidates:
                candidates.append(name)
            if len(candidates) >= MAX_CANDIDATES:
                break
    where = (
        f"merchandise group {group}" if group is not None else "the material's merchandise group"
    )
    line.issues.append(
        Issue(
            code="no_usable_supplier",
            severity="warning",
            message=(
                "No known supplier of this item can be used ("
                + ("; ".join(excluded) or "none in the evidence")
                + "). "
                + (
                    f"Other suppliers of {where} in the evidence: {', '.join(candidates)}. "
                    if candidates
                    else f"No other supplier of {where} is in the evidence. "
                )
                + "Find a source before buying: an RFQ to new suppliers the buyer names."
            ),
            evidence=source_refs([master])[:4] if master is not None else [],
        )
    )
    return SourcingProposal(excluded=excluded, material_group=group, candidates=candidates)


_ORDER: Final = {"unavoidable_stockout": 0, "meets_consuming_safety_stock": 1, "unknown": 2}
# Excluded lines listed with their reason, at most; the rest are counted.
MAX_EXCLUSIONS: Final = 60


def timing_alerts(lines: Sequence[AssessmentLine]) -> list[TimingAlert]:
    """The lines that run short before a new order arrives, most urgent first."""
    alerts: list[TimingAlert] = []
    for line in lines:
        timing = line.timing
        if timing is None or timing.first_shortage_date is None or timing.outcome == "meets":
            continue
        alerts.append(
            TimingAlert(
                part_number=line.part_number,
                description=line.description,
                outcome=timing.outcome,
                summary=_summary(line, timing),
                actions=_actions(line, timing),
                reason=_alert_reason(line, timing),
            )
        )
    alerts.sort(key=lambda a: _ORDER[a.outcome])
    return alerts


def alert_exclusions(lines: Sequence[AssessmentLine]) -> tuple[list[AlertExclusion], int]:
    """Every line that is not an alert, each with its one-line reason, in the lines'
    order; the first `MAX_EXCLUSIONS` and how many more there are."""
    excluded = [
        AlertExclusion(part_number=line.part_number, description=line.description, reason=reason)
        for line in lines
        if (reason := _exclusion_reason(line)) is not None
    ]
    return excluded[:MAX_EXCLUSIONS], max(0, len(excluded) - MAX_EXCLUSIONS)


def _alert_reason(line: AssessmentLine, timing: TimingCheck) -> str:
    unit = line.unit or ""
    arrival = (
        f"{timing.recommended_supplier} arrives {timing.recommended_arrival}"
        if timing.recommended_arrival is not None
        else "no usable supplier has a known lead time"
    )
    if timing.outcome == "unavoidable_stockout":
        return (
            f"projected balance falls below zero on {timing.first_stockout_date} "
            f"(up to {_plain(timing.uncovered_quantity)} {unit} short) and the earliest "
            f"order arrives later: {arrival}."
        )
    if timing.outcome == "meets_consuming_safety_stock":
        return (
            f"projected balance falls below the safety stock {_plain(timing.safety_stock)} "
            f"{unit} on {timing.first_shortage_date} and stays above zero until {arrival}."
        )
    return (
        f"projected balance falls below the safety stock on {timing.first_shortage_date}; "
        f"{arrival}, so the arrival could not be checked."
    )


def _exclusion_reason(line: AssessmentLine) -> str | None:
    """Why the line is not an alert; None when it is one."""
    unit = line.unit or ""
    timing = line.timing
    if timing is not None and timing.first_shortage_date is not None and timing.outcome != "meets":
        return None
    if timing is not None and timing.outcome == "meets":
        return (
            f"{timing.recommended_supplier} arrives {timing.recommended_arrival}, before the "
            f"first shortage on {timing.first_shortage_date}."
        )
    if timing is not None:
        lowest = min(timing.projected, key=lambda p: p.balance, default=None)
        return (
            "projected balance never falls below the safety stock "
            f"{_plain(timing.safety_stock)} {unit}"
            + (
                f" (lowest {_plain(lowest.balance)} {unit} on {lowest.on})"
                if lowest is not None
                else ""
            )
            + " within the horizon."
        )
    if not line.demand_contributions and (line.demand is None or line.demand == 0):
        return "no demand within the horizon."
    if line.demand_contributions and not need_schedule(line):
        available = _plain(line.available) if line.available is not None else "unknown"
        incoming = f" + {_plain(line.on_order)} {unit} on order" if line.on_order else ""
        last = line.demand_schedule[-1].need_by if line.demand_schedule else line.need_by
        return (
            f"stock {available} {unit}{incoming} covers the demand "
            f"{_plain(sum((c.quantity for c in line.demand_contributions), ZERO))} {unit}"
            + (f" through {last}" if last is not None else "")
            + ": nothing to buy."
        )
    if line.calculated_to_buy is None:
        return "the quantity could not be netted (see the line's issues): no timing check."
    return (
        "no dated demand to project (the need is assessed as the source reports it): "
        "no timing check."
    )


def _summary(line: AssessmentLine, timing: TimingCheck) -> str:
    unit = line.unit or ""
    shortage = timing.first_shortage_date
    if timing.outcome == "meets_consuming_safety_stock" and timing.recommended_arrival:
        before = [p.balance for p in timing.projected if p.on < timing.recommended_arrival]
        lowest = min(before, default=timing.safety_stock)
        gap = max(timing.safety_stock - lowest, ZERO)
        return (
            f"Consumes safety stock from {shortage} until the purchase from "
            f"{timing.recommended_supplier} arrives on {timing.recommended_arrival}: the "
            f"balance falls to {_plain(lowest)} {unit} against a safety stock of "
            f"{_plain(timing.safety_stock)} ({_plain(gap)} {unit} below it)."
        )
    if timing.outcome == "unavoidable_stockout":
        after = timing.uncovered_after_expedite
        orders = "; ".join(
            f"order {a.reference} ({_plain(a.quantity)} {unit} on {a.need_by}"
            + (f", product {a.product_code}" if a.product_code else "")
            + (
                f" {a.product_description}"
                if a.product_description
                else f", which also needs {a.product_components[0]}"
                if a.product_components
                else ""
            )
            + ")"
            for a in timing.affected_demand
        )
        return (
            f"Unavoidable stockout from {timing.first_stockout_date}: no new order placed on "
            f"{timing.order_date} arrives before then. Up to {_plain(timing.uncovered_quantity)}"
            f" {unit} short before {timing.recommended_supplier} arrives on "
            f"{timing.recommended_arrival}"
            + (
                f"; {_plain(after)} {unit} if the expedites are honored"
                + (" (mitigated)" if after == 0 else "")
                if after is not None
                else ""
            )
            + "."
            + (f" At risk: {orders}." if orders else "")
            + " Involve production planning (PCP) now to resequence or reschedule them."
        )
    return (
        f"Runs below its safety stock from {shortage}; no usable supplier has a known lead "
        "time, so the arrival could not be checked."
    )


def _actions(line: AssessmentLine, timing: TimingCheck) -> list[str]:
    unit = line.unit or ""
    actions: list[str] = []
    for action in line.expedite:
        who = action.supplier_name or action.supplier_code or "the supplier"
        number = f"{action.po_number}/{action.po_line}"
        if action.schedule_line is not None:
            number += f" (schedule line {action.schedule_line})"
        if action.reason == "overdue":
            actions.append(
                f"Chase overdue PO {number} ({who}, "
                f"{_plain(action.quantity)} {unit}, due {action.current_date})."
            )
            continue
        actions.append(
            f"Ask {who} to move PO {number} "
            f"({_plain(action.quantity)} {unit}) from {action.current_date} to "
            f"{action.requested_date}"
            + (" (the MRP asks for it)" if action.reason.startswith("mrp_") else "")
            + (f": covers {_plain(action.would_cover)} {unit}." if action.would_cover else ".")
        )
    if timing.involve:
        actions.append("Involve production planning (PCP) for the orders at risk.")
    if timing.recommended_supplier is not None:
        chosen = next((s for s in line.suppliers if s.recommended), None)
        days = chosen.planned_lead_time_days if chosen is not None else None
        actions.append(
            f"Buy from {timing.recommended_supplier}"
            + (f" ({days} days, arriving {timing.recommended_arrival})" if days else "")
            + (
                f"; {timing.switched_from} is not recommended: it arrives too late."
                if timing.switched_from
                else "."
            )
        )
    elif line.sourcing is not None:
        actions.append("Find a supplier first: no known supplier can be used.")
    return actions


__all__ = [
    "alert_exclusions",
    "check_timing",
    "demand_schedule",
    "late",
    "need_schedule",
    "project",
    "timing_alerts",
    "usable",
]
