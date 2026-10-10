"""Reconcile dated stock and purchase evidence by business keys, never row position.

Identifiers are compared with `join_key`, so "000101", "101" and "101.0" (or "Planta A"
and "PLANTA A") are one key across reports. Statuses, movements and units are compared
as canonical terms: the mapper's checked interpretation of a section's spelling, or the
built-in multilingual vocabulary.

A purchase register may carry its own receipt and open-balance facts (received date,
received/rejected and open quantity, expected date). They stand in for separate receipt
or delivery-schedule tables only when those have nothing for the same order line; when
both exist they must agree.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from bido_back.purchasing.sources.contracts import (
    Assumption,
    CanonicalValue,
    InboundEvidence,
    Issue,
    PlanningScope,
    Record,
    SourceRef,
)
from bido_back.purchasing.sources.evidence import (
    add_issue,
    categorical,
    fact_key,
    join_key,
    locate,
    normalize_unit,
    same_key,
    source_date,
    source_number,
    source_refs,
    source_text,
    unit_key,
)

ZERO = Decimal(0)
# Canonical terms (see `vocabulary`); source spellings are interpreted before comparing.
OPEN = frozenset({"open"})
CLOSED = frozenset({"closed"})
CANCELLED = frozenset({"cancelled"})
PENDING = frozenset({"pending"})
# Purchase-register fields that describe the order line's own receipt.
_INLINE_RECEIPT = {
    "po_number": "po_number",
    "po_line": "po_line",
    "location": "location",
    "unit": "unit",
    "received_at": "received_at",
    "received_quantity": "quantity",
    "rejected_quantity": "rejected",
}


def state(record: Record) -> str:
    """The record's canonical status term, or "" when its status is unknown."""
    return categorical(record, "status") or ""


def _at_location(record: Record, location: str) -> bool:
    """The record is at the location, or names none (the caller decides about that)."""
    found = source_text(record, "location")
    return found is None or same_key(found, location)


def coalesce(
    records: Sequence[Record],
    keys: tuple[str, ...],
    issues: list[Issue],
    code: str,
) -> list[Record]:
    """Identical business facts may be repeated across reports; conflicts stay blocked.

    Facts are compared by meaning, not spelling: "000101"/"101", "25"/"25.0" and
    "2026-10-08"/"08/10/2026" agree. The first record's spelling is kept for display.
    """
    grouped: dict[tuple[str, ...], list[Record]] = defaultdict(list)
    unkeyed: list[Record] = []
    for record in records:
        if not all(source_text(record, field) for field in keys):
            unkeyed.append(record)
            continue
        grouped[tuple(fact_key(record, field) for field in keys)].append(record)
    if unkeyed:
        missing = [f for f in keys if any(source_text(r, f) is None for r in unkeyed)]
        add_issue(
            issues,
            "missing_join_key",
            f"{len(unkeyed)} record(s) have no {', '.join(missing)}, part of the business "
            f"key ({', '.join(keys)}) that matches them across reports; they were not used.",
            unkeyed,
            *keys,
            locations=locate(unkeyed, keys, missing),
        )
    result: list[Record] = []
    for group in grouped.values():
        values: dict[str, str] = {}
        evidence: dict[str, SourceRef] = {}
        canonical: dict[str, CanonicalValue] = {}
        providers: dict[str, Record] = {}
        conflict = False
        for index, record in enumerate(group):
            for field, value in record.values.items():
                provider = providers.get(field)
                if provider is None:
                    providers[field] = record
                    values[field] = value
                    if field in record.canonical:
                        canonical[field] = record.canonical[field]
                elif fact_key(provider, field) != fact_key(record, field):
                    conflict = True
            for field, ref in record.evidence.items():
                evidence[field if field not in evidence else f"{field}#{index}"] = ref
        if conflict:
            add_issue(
                issues,
                code,
                f"{len(group)} records with the same business key ({', '.join(keys)}) "
                "disagree; none of them was used.",
                group,
                locations=locate(group, keys),
            )
        else:
            result.append(
                Record(kind=group[0].kind, values=values, evidence=evidence, canonical=canonical)
            )
    return result


@dataclass(frozen=True)
class Stock:
    available: Decimal | None
    unit: str | None
    safety_stock: Decimal | None
    records: list[Record]


def inventory_at(
    records: Sequence[Record],
    part: str,
    scope: PlanningScope,
    issues: list[Issue],
    *,
    master_safety: Decimal | None = None,
) -> Stock:
    """The free stock at the scope's plant and date, and the safety-stock policy.

    The policy is the buyer's, else the stock list's own, else the material master's
    (`master_safety`, e.g. an MRP view's safety stock).
    """
    candidates = [
        r
        for r in records
        if r.kind == "inventory"
        and same_key(source_text(r, "part_number"), part)
        and _at_location(r, scope.location)
    ]
    warehouses: dict[str, list[tuple[date, Record]]] = defaultdict(list)
    for record in candidates:
        if source_text(record, "location") is None:
            add_issue(
                issues,
                "missing_inventory_location",
                "Stock cannot be assigned to a plant.",
                [record],
            )
            continue
        as_of = source_date(record, "as_of", issues)
        if as_of is None:
            add_issue(issues, "missing_snapshot_date", "Stock needs a snapshot date.", [record])
        elif as_of <= scope.as_of:
            warehouses[join_key(source_text(record, "warehouse")) or ""].append((as_of, record))
    if "" in warehouses and len(warehouses) > 1:
        add_issue(
            issues,
            "overlapping_inventory_scope",
            "Plant totals and warehouse balances cannot be added together.",
            candidates,
        )
    chosen: list[Record] = []
    for rows in warehouses.values():
        latest = max(day for day, _ in rows)
        current = [r for day, r in rows if day == latest]
        chosen.extend(
            coalesce(
                current,
                ("part_number", "location", "as_of"),
                issues,
                "conflicting_inventory_snapshot",
            )
        )
        if latest < scope.as_of:
            add_issue(
                issues,
                "stale_inventory_snapshot",
                "The latest stock snapshot predates the analysis; intervening movements "
                "have not been reconciled.",
                current,
            )
    if not chosen:
        add_issue(
            issues,
            "missing_inventory",
            "No usable stock snapshot exists for this plant.",
            candidates,
        )
    units = {unit_key(r) for r in chosen}
    if None in units or len(units) != 1:
        add_issue(
            issues, "inventory_unit_conflict", "Stock units are missing or incompatible.", chosen
        )
    total_free = ZERO
    valid = bool(chosen)
    safety_values: set[Decimal] = set()
    for record in chosen:
        free = source_number(record, "available", issues)
        total = source_number(record, "total", issues)
        reserved = source_number(record, "reserved", issues)
        blocked = source_number(record, "blocked", issues)
        if free is None and total is not None and reserved is not None and blocked is not None:
            free = total - reserved - blocked
        if free is None:
            valid = False
            add_issue(
                issues,
                "missing_available",
                "Stock needs free quantity or explicit gross, "
                "reserved and blocked quantities; missing quantities are not zero.",
                [record],
            )
        elif free < 0 or (total is not None and free > total):
            valid = False
            add_issue(
                issues, "inventory_balance_conflict", "Stock balances are inconsistent.", [record]
            )
        else:
            total_free += free
        safety = source_number(record, "safety_stock", issues)
        if safety is not None:
            safety_values.add(safety)
    # A buyer's explicit policy overrides a source policy, but remains visible in scope.
    safety = scope.safety_stock
    if safety is None:
        if len(safety_values) == 1:
            safety = next(iter(safety_values))
        elif not safety_values and master_safety is not None:
            safety = master_safety
        elif safety_values:
            # Disagreeing policies: the highest is kept, the conflict stays visible.
            safety = max(safety_values)
            add_issue(
                issues,
                "conflicting_safety_stock",
                "The stock lines state different safety stocks; the highest was used.",
                chosen,
                "safety_stock",
            )
        else:
            add_issue(
                issues,
                "missing_safety_stock",
                "No safety-stock policy is stated.",
                chosen,
                warning=True,
            )
    return Stock(
        total_free if valid else None,
        source_text(chosen[0], "unit") if chosen else None,
        safety,
        chosen,
    )


def _order_key(record: Record) -> tuple[str | None, str | None, str | None]:
    return (
        join_key(source_text(record, "po_number")),
        join_key(source_text(record, "po_line")),
        join_key(source_text(record, "location")),
    )


def _same_line(record: Record, order: Record) -> bool:
    """The record names the order's number and line, at its plant or at no plant."""
    location = source_text(order, "location")
    return (
        same_key(source_text(record, "po_number"), source_text(order, "po_number"))
        and same_key(source_text(record, "po_line"), source_text(order, "po_line"))
        and (
            source_text(record, "location") is None
            or same_key(source_text(record, "location"), location)
        )
    )


def _inline_receipt(order: Record) -> Record | None:
    """The order line's own receipt facts, as a receipt whose evidence is the order's cells."""
    if not any(source_text(order, field) for field in ("received_quantity", "received_at")):
        return None
    pairs = [
        (source, target) for source, target in _INLINE_RECEIPT.items() if source in order.values
    ]
    return Record(
        kind="receipt",
        values={target: order.values[source] for source, target in pairs},
        evidence={
            target: order.evidence[source] for source, target in pairs if source in order.evidence
        },
        canonical={
            target: order.canonical[source] for source, target in pairs if source in order.canonical
        },
    )


def receipts_for(
    order: Record,
    records: Sequence[Record],
    as_of: date,
    issues: list[Issue],
) -> tuple[list[Record], Decimal]:
    """Physical receipts of an order line up to `as_of`, and their received quantity.

    Separate receipt records come first; the purchase register's own received
    quantity and date stand in only when no separate receipt names the line, and
    must equal the separate receipts' total when both exist.
    """
    candidates = [r for r in records if r.kind == "receipt" and _same_line(r, order)]
    found = coalesce(
        candidates,
        ("po_number", "po_line", "location", "receipt_id", "movement"),
        issues,
        "conflicting_receipt",
    )
    inline = _inline_receipt(order)
    if inline is not None and candidates:
        reported = source_number(inline, "quantity", issues)
        separate = [source_number(r, "quantity", []) for r in found]
        total = sum((q for q in separate if q is not None), ZERO)
        if reported is not None and (None in separate or reported != total):
            add_issue(
                issues,
                "conflicting_receipt_evidence",
                "The order line's own received quantity does not reconcile with the "
                "separate receipt records.",
                [order, *candidates],
                "received_quantity",
                "quantity",
                "po_number",
                "po_line",
            )
    elif inline is not None:
        quantity = source_number(inline, "quantity", issues)
        if quantity == ZERO:
            return [], ZERO  # Nothing received yet: no receipt date is needed.
        found = [inline]
    eligible: list[Record] = []
    received = ZERO
    for receipt in found:
        day = source_date(receipt, "received_at", issues)
        if day is None:
            add_issue(issues, "missing_receipt_date", "A physical receipt has no date.", [receipt])
            continue
        if day > as_of:
            continue
        if receipt is not inline and categorical(receipt, "movement") != "receipt":
            add_issue(
                issues,
                "unsupported_receipt_movement",
                "Receipt reversals and other "
                "movement types need reconciliation before calculating the outstanding balance.",
                [receipt],
            )
            continue
        quantity = source_number(receipt, "quantity", issues)
        if quantity is None or unit_key(receipt) != unit_key(order):
            add_issue(
                issues,
                "invalid_receipt_quantity",
                "Receipt quantity or unit is not usable.",
                [receipt, order],
            )
            continue
        eligible.append(receipt)
        received += quantity
    ordered = source_number(order, "quantity", issues)
    if ordered is not None and received > ordered:
        add_issue(
            issues,
            "receipt_exceeds_order",
            "Physical receipts exceed the ordered quantity.",
            [order, *eligible],
        )
    return eligible, received


def _pending(
    order: Record,
    schedules: list[Record],
    ordered: Decimal | None,
    received: Decimal,
    receipts_known: bool,
    part: str,
    unit: str | None,
    related: list[Record],
    issues: list[Issue],
) -> tuple[Decimal, date | None] | None:
    """The order line's pending balance and expected date, or None when unresolved."""
    inline = source_text(order, "open_quantity") is not None
    if len(schedules) == 1:
        schedule = schedules[0]
        if (
            state(schedule) not in PENDING
            or not same_key(source_text(schedule, "part_number"), part)
            or unit_key(schedule) != normalize_unit(unit)
            or _order_key(schedule) != _order_key(order)
        ):
            add_issue(
                issues,
                "incompatible_delivery_schedule",
                "The delivery confirmation "
                "does not identify the same pending order line and physical unit.",
                related,
            )
            return None
        quantity = source_number(schedule, "quantity", issues)
        expected = source_date(schedule, "expected_at", issues)
        if ordered is None or (quantity is not None and quantity != ordered - received):
            add_issue(
                issues,
                "outstanding_quantity_conflict",
                "The pending delivery quantity "
                "does not reconcile with the order and physical receipts.",
                related,
            )
            return None
        if inline and (
            source_number(order, "open_quantity", issues) != quantity
            or (
                source_text(order, "expected_at") is not None
                and source_date(order, "expected_at", issues) != expected
            )
        ):
            add_issue(
                issues,
                "conflicting_pending_evidence",
                "The order line's own open quantity or expected date differs from its "
                "delivery confirmation.",
                related,
            )
            return None
    elif inline:
        # The register states the pending balance itself: no separate schedule is needed.
        quantity = source_number(order, "open_quantity", issues)
        field = "expected_at" if source_text(order, "expected_at") is not None else "promised_at"
        expected = source_date(order, field, issues)
        if (
            ordered is None
            or quantity is None
            or quantity > ordered
            or (receipts_known and quantity != ordered - received)
        ):
            add_issue(
                issues,
                "outstanding_quantity_conflict",
                "The order line's open quantity does not reconcile with the ordered and "
                "physically received quantities.",
                related,
            )
            return None
    else:
        # No receipt row is not proof of zero receipts. Require an explicit pending balance.
        add_issue(
            issues,
            "missing_pending_confirmation",
            "An open order needs an explicit "
            "pending delivery balance; absence of receipts does not prove it is unreceived.",
            related,
        )
        return None
    if quantity is None:
        add_issue(
            issues,
            "missing_pending_quantity",
            "A delivery has no valid pending quantity.",
            related,
        )
        return None
    return quantity, expected


def incoming_for(
    records: Sequence[Record],
    part: str,
    unit: str | None,
    need_by: date,
    scope: PlanningScope,
    issues: list[Issue],
    assumptions: list[Assumption] | None = None,
) -> list[InboundEvidence]:
    """Open order lines' pending balances, eligible when they arrive by `need_by`.

    An open line already past its date is counted as arriving at the snapshot, as MRP
    does, and the assumption says so (`assumptions`, when the caller collects them);
    without that collector it stays excluded.
    """
    candidates = [
        r
        for r in records
        if r.kind == "purchase"
        and same_key(source_text(r, "part_number"), part)
        and _at_location(r, scope.location)
    ]
    # Several delivery schedule lines of one order item are several incoming lines.
    scheduled = [r for r in candidates if source_text(r, "schedule_line") is not None]
    orders = [
        *coalesce(
            [r for r in candidates if source_text(r, "schedule_line") is None],
            ("po_number", "po_line", "location"),
            issues,
            "conflicting_purchase_order",
        ),
        *coalesce(
            scheduled,
            ("po_number", "po_line", "schedule_line", "location"),
            issues,
            "conflicting_purchase_order",
        ),
    ]
    keys = {_order_key(order) for order in orders}
    for schedule in records:
        if (
            schedule.kind == "delivery_schedule"
            and same_key(source_text(schedule, "part_number"), part)
            and _at_location(schedule, scope.location)
            and _order_key(schedule) not in keys
        ):
            add_issue(
                issues,
                "orphan_delivery_schedule",
                "A pending delivery has no uniquely matching purchase order/item/plant.",
                [schedule],
            )
    inbound: list[InboundEvidence] = []
    for order in orders:
        if state(order) in CLOSED | CANCELLED:
            pending = [
                r
                for r in records
                if r.kind == "delivery_schedule"
                and _order_key(r) == _order_key(order)
                and state(r) in PENDING
            ]
            if pending:
                add_issue(
                    issues,
                    "closed_order_has_pending_delivery",
                    "A closed or cancelled order also has pending delivery evidence.",
                    [order, *pending],
                )
            continue
        if state(order) not in OPEN:
            add_issue(
                issues,
                "unknown_order_status",
                "An order's open/closed status is unresolved.",
                [order],
            )
            continue
        ordered_at = source_date(order, "ordered_at", issues)
        if ordered_at is None:
            add_issue(issues, "missing_order_date", "An open order has no order date.", [order])
            continue
        if ordered_at > scope.as_of:
            continue
        if unit_key(order) != normalize_unit(unit):
            add_issue(
                issues,
                "inbound_unit_conflict",
                "The order unit differs from the demand unit.",
                [order],
            )
            continue
        receipts, received = receipts_for(order, records, scope.as_of, issues)
        schedules = coalesce(
            [
                r
                for r in records
                if r.kind == "delivery_schedule"
                and _same_line(r, order)
                and _at_location(r, scope.location)
            ],
            ("po_number", "po_line", "location"),
            issues,
            "conflicting_delivery_schedule",
        )
        related = [order, *receipts, *schedules]
        receipts_known = _inline_receipt(order) is not None or any(
            r.kind == "receipt" and _same_line(r, order) for r in records
        )
        pending = _pending(
            order,
            schedules,
            source_number(order, "quantity", issues),
            received,
            receipts_known,
            part,
            unit,
            related,
            issues,
        )
        if pending is None:
            continue
        quantity, expected = pending
        eligible = expected is not None and scope.as_of <= expected <= need_by
        if assumptions is not None and expected is not None and expected < scope.as_of:
            eligible = True
            number = (
                f"{source_text(order, 'po_number') or ''}/{source_text(order, 'po_line') or ''}"
            )
            if (line := source_text(order, "schedule_line")) is not None:
                number += f"/{line}"
            amount = format(quantity.normalize(), "f")
            assumptions.append(
                Assumption(
                    code="overdue_order_counted",
                    message=f"Order {number} ({amount} {unit or ''}) was due "
                    f"{expected.isoformat()} and is still open: it is counted as arriving "
                    "now, as MRP does.",
                    alternative=f"If it does not arrive, {amount} more must be bought.",
                    confidence="low",
                    part_numbers=[part],
                    evidence=source_refs(related)[:6],
                )
            )
        if not eligible:
            add_issue(
                issues,
                "late_or_unconfirmed_inbound",
                "The delivery is late, overdue or undated and was excluded from coverage.",
                related,
                warning=True,
            )
        inbound.append(
            InboundEvidence(
                po_number=source_text(order, "po_number") or "",
                po_line=source_text(order, "po_line") or "",
                schedule_line=source_text(order, "schedule_line"),
                quantity=quantity,
                expected_at=expected,
                eligible=eligible,
                supplier_code=source_text(order, "supplier_code"),
                reschedule_to=source_date(order, "reschedule_to", []),
                evidence=source_refs(related),
            )
        )
    if assumptions is not None:
        _not_counted(records, candidates, inbound, part, scope, assumptions)
    return inbound


def _not_counted(
    records: Sequence[Record],
    candidates: Sequence[Record],
    inbound: Sequence[InboundEvidence],
    part: str,
    scope: PlanningScope,
    assumptions: list[Assumption],
) -> None:
    """Open orders and pending deliveries the reconciliation could not count, said so."""
    # Counted, or dated after the horizon's needs (rightly no coverage); undated is missed.
    counted = {
        (join_key(r.po_number), join_key(r.po_line), join_key(r.schedule_line))
        for r in inbound
        if r.eligible or r.expected_at is not None
    }
    pending = [
        r
        for r in records
        if r.kind == "delivery_schedule"
        and same_key(source_text(r, "part_number"), part)
        and _at_location(r, scope.location)
        and state(r) in PENDING
    ]
    missed: dict[
        tuple[str | None, str | None, str | None], tuple[Decimal | None, list[Record]]
    ] = {}
    for record in [*candidates, *pending]:
        if record.kind == "purchase" and state(record) in CLOSED | CANCELLED:
            continue
        key = (
            join_key(source_text(record, "po_number")),
            join_key(source_text(record, "po_line")),
            join_key(source_text(record, "schedule_line")),
        )
        if key in counted:
            continue
        amount = source_number(record, "open_quantity", [])
        if amount is None:
            amount = source_number(record, "quantity", [])
        known = missed.get(key)
        missed[key] = (
            known[0] if known is not None and known[0] is not None else amount,
            [*(known[1] if known else []), record],
        )
    if not missed:
        return
    names = [
        f"{rows[0].values.get('po_number', '?')}/{rows[0].values.get('po_line', '?')}"
        for _, rows in missed.values()
    ]
    total = sum((q for q, _ in missed.values() if q is not None), ZERO)
    assumptions.append(
        Assumption(
            code="open_orders_not_counted",
            message=f"Open order(s) {', '.join(names[:6])} could not be reconciled or have no "
            "delivery date (see the issues) and were not subtracted.",
            alternative=f"If they arrive in time, up to {format(total.normalize(), 'f')} less "
            "is needed."
            if total
            else "If they arrive in time, less is needed.",
            confidence="low",
            part_numbers=[part],
            evidence=source_refs([r for _, rows in missed.values() for r in rows])[:10],
        )
    )
