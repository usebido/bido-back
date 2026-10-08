"""Reconcile dated stock and purchase evidence by business keys, never row position."""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from bido_back.purchasing.importers import norm
from bido_back.purchasing.sources.contracts import (
    InboundEvidence,
    Issue,
    PlanningScope,
    Record,
    SourceRef,
)
from bido_back.purchasing.sources.evidence import (
    add_issue,
    normalize_unit,
    source_date,
    source_number,
    source_refs,
    source_text,
)

ZERO = Decimal(0)
OPEN = frozenset({"open", "em aberto", "aberto", "partially received", "parcial"})
CLOSED = frozenset({"closed", "encerrado", "encerrada", "fechado", "recebido", "received"})
CANCELLED = frozenset({"cancelled", "canceled", "cancelado", "cancelada"})
PENDING = frozenset({"not received", "nao recebido", "pending", "pendente", "em aberto"})


def state(record: Record) -> str:
    return norm(source_text(record, "status") or "")


def coalesce(
    records: Sequence[Record],
    keys: tuple[str, ...],
    issues: list[Issue],
    code: str,
) -> list[Record]:
    """Identical business facts may be repeated across reports; conflicts stay blocked."""
    grouped: dict[tuple[str, ...], list[Record]] = defaultdict(list)
    for record in records:
        key = tuple(source_text(record, field) or "" for field in keys)
        if not all(key):
            add_issue(
                issues,
                "missing_join_key",
                "A record lacks an explicit business key.",
                [record],
                *keys,
            )
            continue
        grouped[key].append(record)
    result: list[Record] = []
    for group in grouped.values():
        values: dict[str, str] = {}
        evidence: dict[str, SourceRef] = {}
        conflict = False
        for index, record in enumerate(group):
            for field, value in record.values.items():
                if field in values and values[field] != value:
                    conflict = True
                values[field] = value
            for field, ref in record.evidence.items():
                evidence[field if field not in evidence else f"{field}#{index}"] = ref
        if conflict:
            add_issue(issues, code, "Records with the same business key disagree.", group)
        else:
            result.append(Record(kind=group[0].kind, values=values, evidence=evidence))
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
) -> Stock:
    candidates = [
        r
        for r in records
        if r.kind == "inventory"
        and source_text(r, "part_number") == part
        and source_text(r, "location") in {scope.location, None}
    ]
    warehouses: dict[str, list[tuple[date, Record]]] = defaultdict(list)
    for record in candidates:
        if source_text(record, "location") != scope.location:
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
            warehouses[source_text(record, "warehouse") or ""].append((as_of, record))
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
    units = {normalize_unit(source_text(r, "unit")) for r in chosen}
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
        else:
            add_issue(
                issues, "missing_safety_stock", "An explicit safety-stock policy is needed.", chosen
            )
    return Stock(
        total_free if valid else None,
        source_text(chosen[0], "unit") if chosen else None,
        safety,
        chosen,
    )


def _order_key(record: Record) -> tuple[str | None, str | None, str | None]:
    return (
        source_text(record, "po_number"),
        source_text(record, "po_line"),
        source_text(record, "location"),
    )


def receipts_for(
    order: Record,
    records: Sequence[Record],
    as_of: date,
    issues: list[Issue],
) -> tuple[list[Record], Decimal]:
    candidates = [
        r
        for r in records
        if r.kind == "receipt"
        and source_text(r, "po_number") == source_text(order, "po_number")
        and source_text(r, "po_line") == source_text(order, "po_line")
        and source_text(r, "location") in {None, source_text(order, "location")}
    ]
    found = coalesce(
        candidates,
        ("po_number", "po_line", "location", "receipt_id", "movement"),
        issues,
        "conflicting_receipt",
    )
    eligible: list[Record] = []
    received = ZERO
    for receipt in found:
        day = source_date(receipt, "received_at", issues)
        if day is None:
            add_issue(issues, "missing_receipt_date", "A physical receipt has no date.", [receipt])
            continue
        if day > as_of:
            continue
        if norm(source_text(receipt, "movement") or "") not in {"101", "receipt", "entrada"}:
            add_issue(
                issues,
                "unsupported_receipt_movement",
                "Receipt reversals and other "
                "movement types need reconciliation before calculating the outstanding balance.",
                [receipt],
            )
            continue
        quantity = source_number(receipt, "quantity", issues)
        if quantity is None or normalize_unit(source_text(receipt, "unit")) != normalize_unit(
            source_text(order, "unit")
        ):
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


def incoming_for(
    records: Sequence[Record],
    part: str,
    unit: str | None,
    need_by: date,
    scope: PlanningScope,
    issues: list[Issue],
) -> list[InboundEvidence]:
    candidates = [
        r
        for r in records
        if r.kind == "purchase"
        and source_text(r, "part_number") == part
        and source_text(r, "location") in {scope.location, None}
    ]
    orders = coalesce(
        candidates, ("po_number", "po_line", "location"), issues, "conflicting_purchase_order"
    )
    keys = {_order_key(order) for order in orders}
    for schedule in records:
        if (
            schedule.kind == "delivery_schedule"
            and source_text(schedule, "part_number") == part
            and source_text(schedule, "location") in {scope.location, None}
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
        if normalize_unit(source_text(order, "unit")) != normalize_unit(unit):
            add_issue(
                issues,
                "inbound_unit_conflict",
                "The order unit differs from the demand unit.",
                [order],
            )
            continue
        receipts, received = receipts_for(order, records, scope.as_of, issues)
        schedules = [
            r
            for r in records
            if r.kind == "delivery_schedule"
            and source_text(r, "po_number") == source_text(order, "po_number")
            and source_text(r, "po_line") == source_text(order, "po_line")
            and source_text(r, "location") in {None, scope.location}
        ]
        schedules = coalesce(
            schedules, ("po_number", "po_line", "location"), issues, "conflicting_delivery_schedule"
        )
        ordered = source_number(order, "quantity", issues)
        quantity: Decimal | None = None
        expected: date | None = None
        related = [order, *receipts, *schedules]
        if len(schedules) == 1:
            schedule = schedules[0]
            if (
                state(schedule) not in PENDING
                or source_text(schedule, "part_number") != part
                or normalize_unit(source_text(schedule, "unit")) != normalize_unit(unit)
                or _order_key(schedule) != _order_key(order)
            ):
                add_issue(
                    issues,
                    "incompatible_delivery_schedule",
                    "The delivery confirmation "
                    "does not identify the same pending order line and physical unit.",
                    related,
                )
                continue
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
                continue
        else:
            # No receipt row is not proof of zero receipts. Require an explicit pending balance.
            add_issue(
                issues,
                "missing_pending_confirmation",
                "An open order needs an explicit "
                "pending delivery balance; absence of receipts does not prove it is unreceived.",
                related,
            )
            continue
        if quantity is None:
            add_issue(
                issues,
                "missing_pending_quantity",
                "A delivery has no valid pending quantity.",
                related,
            )
            continue
        eligible = expected is not None and scope.as_of <= expected <= need_by
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
                quantity=quantity,
                expected_at=expected,
                eligible=eligible,
                evidence=source_refs(related),
            )
        )
    return inbound
