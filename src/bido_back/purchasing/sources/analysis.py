"""Cross-reference source evidence without inventing procurement facts.

An assessment is a read-only result. Reported recommendations stay visible even
when their units or planning assumptions prevent an independent calculation.
Consolidated tables corroborate source tables; they never add another stock or
demand balance. Every issue retains the original evidence coordinates.
"""

import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from bido_back.purchasing.sources.contracts import (
    Assessment,
    AssessmentLine,
    Assumption,
    Issue,
    Record,
    SupplierEvidence,
)
from bido_back.purchasing.sources.evidence import (
    add_issue as _issue,
)
from bido_back.purchasing.sources.evidence import (
    categorical,
    join_key,
    same_key,
    unit_key,
    unit_price,
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
from bido_back.purchasing.sources.joins import CANCELLED, coalesce, state
from bido_back.purchasing.sources.pricing import (
    ORDER_KEY,
    OrderFacts,
    measure,
    reconcile_order,
    reference_date,
)
from bido_back.rfqs.language import country_code

_ZERO = Decimal(0)
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
    """Planning records per item and location, matched by `join_key`; first spelling shown."""
    planning = [record for record in records if record.kind in _PLANNING_KINDS]
    names: dict[str | None, str] = {}
    locations: dict[str, dict[str, str]] = defaultdict(dict)
    for record in planning:
        part, location = _text(record, "part_number"), _text(record, "location")
        if part is not None:
            names.setdefault(join_key(part), part)
            if location is not None:
                locations[names[join_key(part)]].setdefault(join_key(location) or "", location)
    groups: dict[tuple[str, str | None], list[Record]] = defaultdict(list)
    for record in planning:
        part = _text(record, "part_number")
        if part is None:
            _issue(
                issues, "missing_part_number", "A planning record has no item identifier.", [record]
            )
            continue
        part = names[join_key(part)]
        location = _text(record, "location")
        if location is None and len(locations[part]) == 1:
            location = next(iter(locations[part].values()))
        elif location is not None:
            location = locations[part][join_key(location) or ""]
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


def confidence(line: AssessmentLine, *, blocked: bool = False) -> None:
    """low: an inconsistency was settled; medium: conventions or defaults; else high."""
    if (
        blocked
        or any(i.severity == "blocker" for i in line.issues)
        or any(a.confidence == "low" for a in line.assumptions)
    ):
        line.confidence = "low"
    elif line.assumptions:
        line.confidence = "medium"
    else:
        line.confidence = "high"


def _note(
    part: str, code: str, message: str, alternative: str | None = None, *, low: bool = False
) -> Assumption:
    return Assumption(
        code=code,
        message=message,
        alternative=alternative,
        confidence="low" if low else "medium",
        part_numbers=[part],
    )


def _assume(
    line: AssessmentLine,
    code: str,
    message: str,
    alternative: str | None = None,
    *,
    low: bool = False,
) -> None:
    line.assumptions.append(
        Assumption(
            code=code,
            message=message,
            alternative=alternative,
            confidence="low" if low else "medium",
            part_numbers=[line.part_number],
        )
    )


def _calculate(
    line: AssessmentLine,
    inventory: Record | None,
    requirement: Record | None,
) -> None:
    """The quantity to buy, best estimate: what the sources do not establish is not
    netted (or is read the most plausible way) and becomes a disclosed assumption."""
    issues = line.issues
    if requirement is None:
        _issue(
            issues,
            "missing_material_requirement",
            "No unambiguous direct requirement was found for this item and location.",
            [],
        )
        if line.reported_to_buy is not None and line.reported_to_buy >= 0:
            line.calculated_to_buy = line.reported_to_buy
            _assume(
                line,
                "reported_quantity_used",
                "No requirement could be read for this item: the source's own suggested "
                "purchase quantity is used, not an independent calculation.",
                "Check the source summary; Bido could not recompute it.",
                low=True,
            )
        return
    basis = categorical(requirement, "demand_basis")
    if basis not in ("gross", "net"):
        _issue(
            issues,
            "ambiguous_demand_basis",
            "The source does not establish whether the requirement is gross demand "
            "or an already netted purchase requirement.",
            [requirement],
            "quantity",
            "demand_basis",
            warning=True,
        )
        _assume(
            line,
            "requirement_basis_assumed_gross",
            "The requirement does not say whether it is gross or already netted: it was read "
            "as gross demand and netted against stock and open orders.",
            "If it is already a net purchase requirement, buy exactly the required quantity.",
        )
        basis = "gross"
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
    if line.demand is None:
        return  # Literally no quantity: nothing to estimate.
    if basis == "net":
        line.calculated_to_buy = line.demand
        return
    available, on_order, safety = line.available, line.on_order, line.safety_stock
    if (
        inventory is not None
        and _text(inventory, "unit") is not None
        and _text(requirement, "unit") is not None
        and unit_key(inventory) != unit_key(requirement)
    ):
        # No conversion: balances in another unit are never netted against the demand.
        same = (
            max(line.demand + safety - available - on_order, _ZERO)
            if available is not None and on_order is not None and safety is not None
            else None
        )
        _assume(
            line,
            "stock_not_netted",
            f"Stock is in {_text(inventory, 'unit')} and the requirement in "
            f"{_text(requirement, 'unit')}, with no conversion: stock, open orders and "
            "safety stock were not netted.",
            f"If the stock is in {_text(requirement, 'unit')} too, buy {same.normalize():f}."
            if same is not None
            else "With a conversion, the quantity to buy drops by the stock and open orders.",
            low=True,
        )
        line.calculated_to_buy = line.demand
        return
    if inventory is None:
        _issue(
            issues,
            "missing_inventory",
            "No unambiguous inventory balance was found for this item and location.",
            [],
            warning=True,
        )
    for field in ("available", "on_order", "safety_stock"):
        if getattr(line, field) is None:
            _issue(
                issues,
                f"missing_{field}",
                f"The source does not establish {field.replace('_', ' ')}; zero was not assumed.",
                [inventory] if inventory is not None else [],
                field,
                warning=True,
            )
    if available is None:
        available = _ZERO
        _assume(
            line,
            "stock_not_netted",
            "No usable free-stock balance was found: stock was not subtracted.",
            "Any free stock on hand reduces the quantity to buy by its size.",
            low=True,
        )
    if on_order is None:
        on_order = _ZERO
        _assume(
            line,
            "no_open_orders_known",
            "No open-order quantity was found: no open order was subtracted.",
            "Open orders arriving in time reduce the quantity to buy.",
            low=True,
        )
    if safety is None:
        safety = _ZERO
        _assume(
            line,
            "no_safety_stock",
            "No safety-stock policy was found or agreed: none was kept (0).",
            "A safety stock raises the quantity to buy by its size.",
        )
    if line.inventory_unit is None and inventory is not None:
        _issue(
            issues,
            "missing_inventory_unit",
            "The inventory quantity has no physical unit.",
            [inventory],
            "available",
            "unit",
        )
    eligible_on_order = on_order
    if on_order > 0 and inventory is not None:
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
                warning=True,
            )
            _assume(
                line,
                "inbound_assumed_on_time",
                "The on-order quantity has no confirmed arrival date: it is assumed to arrive "
                "in time.",
                f"If it arrives late, buy {on_order.normalize():f} more.",
                low=True,
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
    line.calculated_to_buy = max(line.demand + safety - available - eligible_on_order, _ZERO)


def _planning_line(part: str, location: str | None, records: Sequence[Record]) -> AssessmentLine:
    issues: list[Issue] = []
    inventory = _one(records, "inventory", issues)
    requirement = _one(records, "material_requirement", issues)
    summary = _one(records, "purchase_summary", issues)
    notes: list[Assumption] = []
    stocks = [r for r in records if r.kind == "inventory"]
    if inventory is None and len(stocks) > 1:
        if len({json.dumps(r.values, sort_keys=True) for r in stocks}) == 1:
            inventory = stocks[0]
            notes.append(
                _note(
                    part,
                    "duplicate_stock_counted_once",
                    f"{len(stocks)} identical stock rows were counted once.",
                    "If they are separate balances, add them up.",
                )
            )
        elif usable := [(q, r) for r in stocks if (q := _number(r, "available", [])) is not None]:
            # Disagreeing balances: the lowest free stock is the cautious reading.
            low_stock, inventory = min(usable, key=lambda pair: pair[0])
            high_stock = max(q for q, _ in usable)
            notes.append(
                _note(
                    part,
                    "stock_rows_disagree",
                    f"{len(stocks)} different stock rows exist for this item and plant: the "
                    f"lowest free stock ({low_stock.normalize():f}) was used.",
                    f"With the highest ({high_stock.normalize():f}), buy up to "
                    f"{(high_stock - low_stock).normalize():f} less.",
                    low=True,
                )
            )
        else:
            notes.append(
                _note(
                    part,
                    "stock_rows_disagree",
                    f"{len(stocks)} different stock "
                    "rows exist for this item and plant: stock was not netted.",
                    "Once the right balance is known, the quantity to buy drops by it.",
                    low=True,
                )
            )
    demands = [r for r in records if r.kind == "material_requirement"]
    combined: tuple[Decimal, date | None] | None = None
    if requirement is None and len(demands) > 1 and len({unit_key(r) for r in demands}) == 1:
        quantities = [_number(r, "quantity", []) for r in demands]
        if None not in quantities:
            requirement = demands[0]
            total = sum((q for q in quantities if q is not None), _ZERO)
            days = [d for r in demands if (d := _day(r, "need_by", [])) is not None]
            combined = (total, min(days, default=None))
            notes.append(
                _note(
                    part,
                    "requirements_added",
                    f"{len(demands)} requirement rows "
                    f"for this item and plant were added up ({total.normalize():f}).",
                    "If they repeat one requirement, the demand is the largest of them.",
                    low=True,
                )
            )
    _reconcile(inventory, requirement, summary, issues)
    stock = inventory or summary
    reported_unit = _text(summary, "unit")
    demand_unit = _text(requirement, "unit")
    inventory_unit = _text(inventory, "unit")
    units = {
        unit_key(record) for record in (summary, requirement, inventory) if _text(record, "unit")
    }
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
    line.assumptions.extend(notes)
    if combined is not None:
        line.demand, line.need_by = combined
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
        return [record for record in records if same_key(_text(record, "supplier_code"), code)]
    if name is None:
        return []
    matches = [record for record in records if _name(_text(record, "name")) == _name(name)]
    codes = {_text(record, "supplier_code") for record in matches}
    return matches if len(codes - {None}) <= 1 else []


def supplier_country(masters: Sequence[Record]) -> str | None:
    """The one country the supplier's master rows name (ISO alpha-2), else None.

    Every written value must be a recognizable country and all must agree.
    """
    written = {value for record in masters if (value := _text(record, "country")) is not None}
    codes = {country_code(value) for value in written}
    if not codes or None in codes or len(codes) != 1:
        return None
    return next(iter(codes))


@dataclass(frozen=True, slots=True)
class PurchasesByCode:
    """Purchases by their exact supplier code, and the supplier names they write, built
    once per assessment: a supplier's identity check is then a lookup, not a scan of
    every purchase."""

    records: Mapping[str, Sequence[Record]]
    # Normalized name -> its last spelling, per code.
    names: Mapping[str, Mapping[str, str]]


def by_supplier_code(purchases: Sequence[Record]) -> PurchasesByCode:
    records: dict[str, list[Record]] = defaultdict(list)
    for record in purchases:
        if (code := _text(record, "supplier_code")) is not None:
            records[code].append(record)
    return PurchasesByCode(
        records=records, names={code: _names(rows) for code, rows in records.items()}
    )


def _names(records: Sequence[Record]) -> dict[str, str]:
    """The supplier names records write: normalized name -> its last spelling."""
    return {
        _name(value): value
        for record in records
        if (value := _text(record, "supplier_name" if record.kind == "purchase" else "name"))
        is not None
    }


def supplier_evidence(
    purchases: Sequence[Record],
    masters: Sequence[Record],
    performance: Sequence[Record],
    all_purchases: PurchasesByCode,
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
    identity_purchases = all_purchases.records.get(code, ()) if code is not None else purchases
    names = _names([*matching, *metrics, *purchases])
    if code is not None:
        names.update(all_purchases.names.get(code, {}))
    conflicted = len(names) > 1
    if conflicted:
        _issue(
            issues,
            "supplier_code_conflict",
            "A supplier identifier maps to multiple source names; no name was selected.",
            [*matching, *metrics, *purchases, *identity_purchases],
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
        signatures = {
            (unit_price(record, []), _text(record, "currency"), unit_key(record))
            for record in newest
        }
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
        last_price=unit_price(latest, issues, warning=True),
        currency=_currency(latest, issues),
        ordered_at=_day(latest, "ordered_at", issues),
        country=supplier_country(matching),
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
    master_facts(result, matching, metrics)
    return result


_SCORES = ("score_overall", "score_price", "score_quality", "score_delivery", "score_service")


def master_facts(
    supplier: SupplierEvidence, masters: Sequence[Record], metrics: Sequence[Record]
) -> None:
    """The supplier master's purchase block and the vendor evaluation's own scores."""
    if any(categorical(record, "purchase_block") == "blocked" for record in masters):
        supplier.purchase_blocked = True
    for field in _SCORES:
        values = {_number(record, field, []) for record in metrics} - {None}
        if len(values) == 1:
            setattr(supplier, field, next(iter(values)))


def _supplier_key(code: str | None, name: str | None) -> str | None:
    if code is not None:
        return f"code:{join_key(code) or code}"
    return f"name:{_name(name)}" if name is not None else None


def _orders(
    purchases: Sequence[Record],
    records: Sequence[Record],
    unit: str | None,
    as_of: date | None,
    issues: list[Issue],
) -> list[OrderFacts]:
    """The item's purchase lines at any location, joined to receipts where keyed."""
    if as_of is None:
        return []
    keyed = [r for r in purchases if all(_text(r, field) for field in ORDER_KEY)]
    conflicts: list[Issue] = []
    lines = [
        *coalesce(keyed, ORDER_KEY, conflicts, "conflicting_purchase_history"),
        *(r for r in purchases if not all(_text(r, field) for field in ORDER_KEY)),
    ]
    # Measured history supplements this assessment; it never blocks its quantities.
    issues.extend(issue.model_copy(update={"severity": "warning"}) for issue in conflicts)
    orders: list[OrderFacts] = []
    for order in lines:
        key = _supplier_key(_text(order, "supplier_code"), _text(order, "supplier_name"))
        ordered_at = _day(order, "ordered_at", [])
        if key is None or ordered_at is None or ordered_at > as_of or state(order) in CANCELLED:
            continue
        orders.append(
            reconcile_order(
                order,
                records,
                supplier=key,
                ordered_at=ordered_at,
                unit=unit,
                as_of=as_of,
                issues=issues,
            )
        )
    return orders


def assess(records: Sequence[Record]) -> Assessment:
    """Produce a source-grounded assessment, never write data or propose an action."""
    unique = _deduplicate(records)
    result = Assessment(record_counts=dict(Counter(record.kind for record in unique)))
    grouped = _groups(unique, result.issues)
    masters = [record for record in unique if record.kind == "supplier"]
    performance = [record for record in unique if record.kind == "supplier_performance"]
    all_purchases = by_supplier_code([record for record in unique if record.kind == "purchase"])
    purchases: dict[str | None, list[Record]] = defaultdict(list)
    for record in unique:
        if record.kind == "purchase" and (part := _text(record, "part_number")) is not None:
            purchases[join_key(part)].append(record)
    # Without a planning scope, history is measured up to the latest date the evidence shows.
    as_of = reference_date(unique)
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
            _assume(
                line,
                "location_unknown",
                "This item's evidence names no plant while the item is at several plants: "
                "it was assessed on its own, without the stock of any plant.",
                "Once the plant is known, that plant's stock and orders are netted.",
                low=True,
            )
        by_supplier: dict[tuple[str | None, str | None], list[Record]] = defaultdict(list)
        for record in purchases[join_key(part)]:
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
            by_supplier[join_key(code), None if code else _name(name)].append(record)
        orders = _orders(purchases[join_key(part)], unique, line.unit, as_of, line.issues)
        suppliers: dict[str, SupplierEvidence] = {}
        for (code, name), rows in by_supplier.items():
            key = _supplier_key(code, name) or ""
            # A completed purchase (closed, fully received) sets the last price when one
            # reconciles; otherwise the latest purchase order does, under its disclosed basis.
            completed = [o.order for o in orders if o.supplier == key and o.completed]
            suppliers[key] = supplier_evidence(
                completed or rows,
                masters,
                performance,
                all_purchases,
                line.issues,
                include_history_evidence=not completed,
            )
            if completed:
                suppliers[key].last_price_basis = "received_order"
        line.suppliers = list(suppliers.values())
        # Supplier performance and the internal price benchmark (an internal reference only).
        measure(line, suppliers, orders, as_of)
        line.ready = line.calculated_to_buy is not None
        confidence(line)
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
