"""Cross-reference source evidence without inventing procurement facts.

An assessment is a read-only result. Reported recommendations stay visible even
when their units or planning assumptions prevent an independent calculation.
Consolidated tables corroborate source tables; they never add another stock or
demand balance. Every issue retains the original evidence coordinates.
"""

import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

from bido_back.purchasing.importers import norm
from bido_back.purchasing.sources.contracts import (
    Assessment,
    AssessmentLine,
    Issue,
    Record,
    SupplierEvidence,
)
from bido_back.purchasing.sources.evidence import (
    add_issue as _issue,
)
from bido_back.purchasing.sources.evidence import (
    normalize_unit as _unit,
)
from bido_back.purchasing.sources.evidence import (
    source_date as _day,
)
from bido_back.purchasing.sources.evidence import (
    source_number as _number,
)
from bido_back.purchasing.sources.evidence import (
    source_refs as _refs,
)
from bido_back.purchasing.sources.evidence import (
    source_text as _text,
)

_ZERO = Decimal(0)
_GROSS = frozenset(
    {
        "gross",
        "gross demand",
        "gross requirement",
        "gross requirements",
        "bruta",
        "necessidade bruta",
        "necessidades brutas",
        "demanda bruta",
    }
)
_NET = frozenset(
    {
        "net",
        "net demand",
        "net requirement",
        "net requirements",
        "liquida",
        "necessidade liquida",
        "necessidades liquidas",
        "demanda liquida",
    }
)
_PLANNING_KINDS = frozenset({"inventory", "material_requirement", "purchase_summary"})


def _currency(record: Record | None, issues: list[Issue]) -> str | None:
    value = _text(record, "currency")
    if value is None or record is None:
        return None
    if not re.fullmatch(r"[A-Za-z]{3}", value):
        _issue(
            issues,
            "invalid_currency",
            "Currency needs an explicit three-letter code.",
            [record],
            "currency",
        )
        return None
    return value.upper()


def _one(records: Sequence[Record], kind: str, issues: list[Issue]) -> Record | None:
    found = [record for record in records if record.kind == kind]
    if len(found) > 1:
        _issue(
            issues,
            f"ambiguous_{kind}",
            f"Multiple {kind.replace('_', ' ')} rows share this item and location; "
            "their scope must be reconciled before combining quantities.",
            found,
        )
        return None
    return found[0] if found else None


def _deduplicate(records: Sequence[Record]) -> list[Record]:
    """A repeated mapping of the same source cells is one piece of evidence.

    Equal values at different coordinates are not deduplicated: they may be
    different movements, snapshots, or demand lines.
    """
    result: list[Record] = []
    seen: set[str] = set()
    for record in records:
        identity = record.model_dump_json()
        if identity not in seen or not record.evidence:
            result.append(record)
        seen.add(identity)
    return result


def _groups(
    records: Sequence[Record], issues: list[Issue]
) -> dict[tuple[str, str | None], list[Record]]:
    planning = [record for record in records if record.kind in _PLANNING_KINDS]
    locations: dict[str, set[str]] = defaultdict(set)
    for record in planning:
        part, location = _text(record, "part_number"), _text(record, "location")
        if part is not None and location is not None:
            locations[part].add(location)
    groups: dict[tuple[str, str | None], list[Record]] = defaultdict(list)
    for record in planning:
        part = _text(record, "part_number")
        if part is None:
            _issue(
                issues, "missing_part_number", "A planning record has no item identifier.", [record]
            )
            continue
        location = _text(record, "location")
        if location is None and len(locations[part]) == 1:
            location = next(iter(locations[part]))
        groups[part, location].append(record)
    return groups


def _summary_checks(summary: Record, issues: list[Issue]) -> None:
    available = _number(summary, "available", issues)
    on_order = _number(summary, "on_order", issues)
    demand = _number(summary, "demand", issues)
    safety = _number(summary, "safety_stock", issues)
    projected = _number(summary, "projected", issues, negative=True)
    reported = _number(summary, "reported_to_buy", issues)
    price = _number(summary, "unit_price", issues)
    cost = _number(summary, "reported_estimated_cost", issues)
    if available is not None and on_order is not None and demand is not None:
        expected_projected = available + on_order - demand
        if projected is not None and projected != expected_projected:
            _issue(
                issues,
                "summary_projection_mismatch",
                "Reported projected stock differs from available + on order - demand.",
                [summary],
                "available",
                "on_order",
                "demand",
                "projected",
            )
        if safety is not None and reported is not None:
            expected_buy = max(safety - expected_projected, _ZERO)
            if reported != expected_buy:
                _issue(
                    issues,
                    "summary_purchase_mismatch",
                    "Reported purchases differ from the summary's demand + safety stock "
                    "- available - on order calculation.",
                    [summary],
                    "available",
                    "on_order",
                    "demand",
                    "safety_stock",
                    "reported_to_buy",
                )
    if reported is not None and price is not None and cost is not None:
        expected_cost = (reported * price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) != expected_cost:
            _issue(
                issues,
                "summary_cost_mismatch",
                "Reported estimated cost differs from reported purchases times unit price.",
                [summary],
                "reported_to_buy",
                "unit_price",
                "reported_estimated_cost",
            )


def _reconcile(
    inventory: Record | None,
    requirement: Record | None,
    summary: Record | None,
    issues: list[Issue],
) -> None:
    if inventory is not None:
        total = _number(inventory, "total", issues)
        available = _number(inventory, "available", issues)
        blocked = _number(inventory, "blocked", issues)
        if total is not None and available is not None:
            if available > total or (blocked is not None and available + blocked > total):
                _issue(
                    issues,
                    "inventory_balance_conflict",
                    "Available stock, which excludes blocked stock, exceeds total stock "
                    "alone or when combined with blocked stock. "
                    "The source balance is inconsistent.",
                    [inventory],
                    "total",
                    "available",
                    "blocked",
                )
    if inventory is not None and requirement is not None:
        demand_ref = requirement.evidence.get("quantity")
        stock_refs = [inventory.evidence.get(field) for field in ("available", "on_order")]
        if demand_ref is not None and demand_ref in stock_refs:
            _issue(
                issues,
                "overlapping_stock_demand_evidence",
                "The same source quantity was assigned to both stock and demand; "
                "their business meanings must be resolved before calculating purchases.",
                [inventory, requirement],
                "quantity",
                "available",
                "on_order",
            )
    if summary is None:
        return
    _summary_checks(summary, issues)
    if inventory is not None:
        for field in ("available", "on_order", "safety_stock"):
            actual = _number(inventory, field, issues)
            reported = _number(summary, field, issues)
            if actual is not None and reported is not None and actual != reported:
                _issue(
                    issues,
                    "summary_inventory_mismatch",
                    f"The consolidated {field} differs from the inventory source.",
                    [inventory, summary],
                    field,
                )
    if requirement is not None:
        actual = _number(requirement, "quantity", issues)
        reported = _number(summary, "demand", issues)
        if actual is not None and reported is not None and actual != reported:
            _issue(
                issues,
                "summary_demand_mismatch",
                "The consolidated demand differs from the requirement source.",
                [requirement, summary],
                "quantity",
                "demand",
            )


def _calculate(
    line: AssessmentLine,
    inventory: Record | None,
    requirement: Record | None,
) -> None:
    issues = line.issues
    if requirement is None:
        _issue(
            issues,
            "missing_material_requirement",
            "No unambiguous direct requirement was found for this item and location.",
            [],
        )
        return
    basis = norm(_text(requirement, "demand_basis") or "")
    if basis not in _GROSS | _NET:
        _issue(
            issues,
            "ambiguous_demand_basis",
            "The source does not establish whether the requirement is gross demand "
            "or an already netted purchase requirement.",
            [requirement],
            "quantity",
            "demand_basis",
        )
    if line.demand is None:
        _issue(
            issues,
            "missing_demand_quantity",
            "The required quantity is missing or invalid.",
            [requirement],
            "quantity",
        )
    if line.demand_unit is None:
        _issue(
            issues,
            "missing_demand_unit",
            "The required quantity has no physical unit.",
            [requirement],
            "quantity",
            "unit",
        )
    if line.need_by is None:
        _issue(
            issues,
            "missing_need_date",
            "The requirement has no valid need-by date.",
            [requirement],
            "need_by",
            warning=True,
        )
    if basis in _NET:
        if not any(issue.severity == "blocker" for issue in issues):
            line.calculated_to_buy = line.demand
        return
    if inventory is None:
        _issue(
            issues,
            "missing_inventory",
            "No unambiguous inventory balance was found for this item and location.",
            [],
        )
        return
    for field in ("available", "on_order", "safety_stock"):
        if getattr(line, field) is None:
            _issue(
                issues,
                f"missing_{field}",
                f"The source does not establish {field.replace('_', ' ')}; zero was not assumed.",
                [inventory],
                field,
            )
    if line.inventory_unit is None:
        _issue(
            issues,
            "missing_inventory_unit",
            "The inventory quantity has no physical unit.",
            [inventory],
            "available",
            "unit",
        )
    eligible_on_order = line.on_order
    if line.on_order is not None and line.on_order > 0:
        receipt = _day(inventory, "receipt_date", issues)
        if receipt is None or line.need_by is None:
            _issue(
                issues,
                "unconfirmed_inbound_date",
                "On-order stock has no confirmed receipt date relative to the need-by date; "
                "any recommendation that subtracts it is conditional on timely arrival.",
                [inventory, requirement],
                "on_order",
                "receipt_date",
                "need_by",
            )
        elif receipt > line.need_by:
            eligible_on_order = _ZERO
            _issue(
                issues,
                "late_inbound_stock",
                "On-order stock arrives after the need date and was excluded from coverage.",
                [inventory, requirement],
                "on_order",
                "receipt_date",
                "need_by",
                warning=True,
            )
    if (
        not any(issue.severity == "blocker" for issue in issues)
        and line.demand is not None
        and line.available is not None
        and eligible_on_order is not None
        and line.safety_stock is not None
    ):
        line.calculated_to_buy = max(
            line.demand + line.safety_stock - line.available - eligible_on_order, _ZERO
        )


def _planning_line(part: str, location: str | None, records: Sequence[Record]) -> AssessmentLine:
    issues: list[Issue] = []
    inventory = _one(records, "inventory", issues)
    requirement = _one(records, "material_requirement", issues)
    summary = _one(records, "purchase_summary", issues)
    _reconcile(inventory, requirement, summary, issues)
    stock = inventory or summary
    reported_unit = _text(summary, "unit")
    demand_unit = _text(requirement, "unit")
    inventory_unit = _text(inventory, "unit")
    units = {_unit(value) for value in (reported_unit, demand_unit, inventory_unit) if value}
    if len(units) > 1:
        _issue(
            issues,
            "unit_conflict",
            "The source quantity units disagree; no physical unit conversion was supplied. "
            "Reported purchase quantities remain source claims, not validated quantities.",
            records,
            "unit",
            "quantity",
            "available",
            "reported_to_buy",
        )
    line = AssessmentLine(
        part_number=part,
        description=_text(requirement, "description")
        or _text(inventory, "description")
        or _text(summary, "description"),
        location=location,
        unit=demand_unit or inventory_unit or reported_unit,
        reported_unit=reported_unit,
        demand_unit=demand_unit,
        inventory_unit=inventory_unit,
        need_by=_day(requirement, "need_by", issues),
        demand=_number(requirement, "quantity", issues),
        available=_number(stock, "available", issues),
        on_order=_number(stock, "on_order", issues),
        safety_stock=_number(stock, "safety_stock", issues),
        reported_to_buy=_number(summary, "reported_to_buy", issues),
        reported_estimated_cost=_number(summary, "reported_estimated_cost", issues),
        # A purchase's currency does not establish an inventory valuation's currency.
        currency=_currency(summary, issues),
        evidence=_refs(records),
        issues=issues,
    )
    _calculate(line, inventory, requirement)
    if line.reported_estimated_cost is not None and line.currency is None:
        _issue(
            line.issues,
            "missing_cost_currency",
            "The source estimated cost has no explicit currency; historical purchase "
            "currencies do not establish the valuation currency.",
            [summary] if summary is not None else [],
            "reported_estimated_cost",
            warning=True,
        )
    if (
        line.calculated_to_buy is not None
        and line.reported_to_buy is not None
        and line.calculated_to_buy != line.reported_to_buy
    ):
        _issue(
            line.issues,
            "calculated_purchase_mismatch",
            "The independently calculated purchase quantity differs from the source summary.",
            records,
            "reported_to_buy",
            "quantity",
            "available",
            "on_order",
            "safety_stock",
        )
    return line


def _name(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _supplier_matches(
    records: Sequence[Record],
    code: str | None,
    name: str | None,
) -> list[Record]:
    if code is not None:
        return [record for record in records if _text(record, "supplier_code") == code]
    if name is None:
        return []
    matches = [record for record in records if _name(_text(record, "name")) == _name(name)]
    codes = {_text(record, "supplier_code") for record in matches}
    return matches if len(codes - {None}) <= 1 else []


def supplier_evidence(
    purchases: Sequence[Record],
    masters: Sequence[Record],
    performance: Sequence[Record],
    all_purchases: Sequence[Record],
    issues: list[Issue],
    *,
    include_history_evidence: bool = True,
) -> SupplierEvidence:
    first = purchases[0]
    code = _text(first, "supplier_code")
    name = _text(first, "supplier_name")
    matching = _supplier_matches(masters, code, name)
    # A unique master identifier may link name-only purchase history to ratings.
    if code is None:
        codes = {_text(record, "supplier_code") for record in matching} - {None}
        if len(codes) == 1:
            code = next(iter(codes))
    metrics = _supplier_matches(performance, code, name)
    # Identity is shared across items, while latest prices remain item-specific.
    identity_purchases = (
        [record for record in all_purchases if _text(record, "supplier_code") == code]
        if code is not None
        else list(purchases)
    )
    identities = [*matching, *metrics, *purchases, *identity_purchases]
    names = {
        _name(value): value
        for record in identities
        if (value := _text(record, "supplier_name" if record.kind == "purchase" else "name"))
        is not None
    }
    conflicted = len(names) > 1
    if conflicted:
        _issue(
            issues,
            "supplier_code_conflict",
            "A supplier identifier maps to multiple source names; no name was selected.",
            identities,
            "supplier_code",
            "name",
            "supplier_name",
        )
        name = None
    elif names:
        name = next(iter(names.values()))
    elif code is not None and name is None:
        _issue(
            issues,
            "unresolved_supplier_code",
            "A purchase supplier identifier has no matching name in the supplied sources.",
            purchases,
            "supplier_code",
            warning=True,
        )
    emails = {_text(record, "email") for record in matching} - {None}
    if len(emails) > 1:
        _issue(
            issues,
            "multiple_supplier_contacts",
            "The supplier has multiple source email addresses; no contact was selected.",
            matching,
            "email",
            warning=True,
        )
    dated = [(record, _day(record, "ordered_at", issues)) for record in purchases]
    latest: Record | None = None
    if all(day is not None for _, day in dated):
        last_day = max(day for _, day in dated if day is not None)
        newest = [record for record, day in dated if day == last_day]
        signatures = {(_text(record, "unit_price"), _text(record, "currency")) for record in newest}
        if len(signatures) == 1:
            latest = newest[0]
        else:
            _issue(
                issues,
                "ambiguous_last_price",
                "Different prices or currencies share the latest purchase date.",
                newest,
                "unit_price",
                "currency",
                "ordered_at",
                warning=True,
            )
    else:
        _issue(
            issues,
            "undated_purchase_history",
            "Some purchase dates are missing; a latest purchase price cannot be established.",
            purchases,
            "ordered_at",
            warning=True,
        )
    result = SupplierEvidence(
        supplier_code=code,
        name=name,
        email=next(iter(emails)) if len(emails) == 1 and not conflicted else None,
        last_price=_number(latest, "unit_price", issues, warning=True),
        currency=_currency(latest, issues),
        ordered_at=_day(latest, "ordered_at", issues),
        evidence=_refs(
            [
                *(purchases if include_history_evidence else [latest or first]),
                *matching,
                *metrics,
            ]
        ),
    )
    for field in ("otif_pct", "quality_pct", "lead_time_days"):
        values = {_number(record, field, issues, warning=True) for record in metrics} - {None}
        if len(values) > 1:
            _issue(
                issues,
                "supplier_performance_conflict",
                f"Supplier sources disagree on {field}; no value was selected.",
                metrics,
                field,
                warning=True,
            )
            continue
        if values:
            value = next(iter(values))
            if field.endswith("_pct") and value is not None and value > 100:
                _issue(
                    issues,
                    "invalid_supplier_percentage",
                    f"The source {field} is outside 0–100 percent.",
                    metrics,
                    field,
                    warning=True,
                )
            else:
                setattr(result, field, value)
    return result


def assess(records: Sequence[Record]) -> Assessment:
    """Produce a source-grounded assessment, never write data or propose an action."""
    unique = _deduplicate(records)
    result = Assessment(record_counts=dict(Counter(record.kind for record in unique)))
    grouped = _groups(unique, result.issues)
    masters = [record for record in unique if record.kind == "supplier"]
    performance = [record for record in unique if record.kind == "supplier_performance"]
    all_purchases = [record for record in unique if record.kind == "purchase"]
    purchases: dict[str, list[Record]] = defaultdict(list)
    for record in unique:
        if record.kind == "purchase" and (part := _text(record, "part_number")) is not None:
            purchases[part].append(record)
    for (part, location), group in grouped.items():
        line = _planning_line(part, location, group)
        if location is None and any(key[0] == part and key[1] is not None for key in grouped):
            _issue(
                line.issues,
                "ambiguous_location",
                "This item occurs at several locations; the unlocated source cannot be "
                "assigned to one without evidence.",
                group,
                "part_number",
                "location",
            )
            line.calculated_to_buy = None
        by_supplier: dict[tuple[str | None, str | None], list[Record]] = defaultdict(list)
        for record in purchases[part]:
            code = _text(record, "supplier_code")
            name = _text(record, "supplier_name")
            if code is None and name is None:
                _issue(
                    line.issues,
                    "missing_purchase_supplier",
                    "A purchase has neither a supplier identifier nor name.",
                    [record],
                    warning=True,
                )
                continue
            by_supplier[code, None if code else _name(name)].append(record)
        line.suppliers = [
            supplier_evidence(rows, masters, performance, all_purchases, line.issues)
            for rows in by_supplier.values()
        ]
        line.ready = line.calculated_to_buy is not None and not any(
            issue.severity == "blocker" for issue in line.issues
        )
        # Validation can encounter the same source field in several comparisons.
        seen: set[str] = set()
        distinct: list[Issue] = []
        for issue in line.issues:
            identity = issue.model_dump_json()
            if identity not in seen:
                distinct.append(issue)
            seen.add(identity)
        line.issues = distinct
        result.lines.append(line)
    return result
