"""Derive component demand from source evidence and net it in date order."""

from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal

from bido_back.purchasing.importers import norm
from bido_back.purchasing.sources.analysis import supplier_evidence
from bido_back.purchasing.sources.contracts import (
    Assessment,
    AssessmentLine,
    DemandContribution,
    Issue,
    NettingStep,
    PlanningCandidate,
    PlanningScope,
    Record,
    SupplierEvidence,
)
from bido_back.purchasing.sources.evidence import (
    add_issue,
    normalize_unit,
    source_date,
    source_number,
    source_refs,
    source_text,
)
from bido_back.purchasing.sources.joins import (
    CANCELLED,
    CLOSED,
    ZERO,
    coalesce,
    incoming_for,
    inventory_at,
    receipts_for,
    state,
)

_ACTIVE_PLANS = frozenset(
    {"planned", "planejada", "planejado", "released", "liberada", "liberado", "open", "em aberto"}
)
_GROSS = frozenset(
    {"gross", "gross demand", "gross requirement", "bruta", "demanda bruta", "necessidade bruta"}
)


def planning_context(records: Sequence[Record]) -> tuple[list[PlanningCandidate], list[date]]:
    """Expose located product/plant/date choices so the copilot can complete a scoped call."""
    grouped: dict[tuple[str, str | None], list[Record]] = defaultdict(list)
    dates: set[date] = set()
    for row in records:
        if row.kind == "production_plan" and (product := source_text(row, "product_code")):
            grouped[product, source_text(row, "location")].append(row)
        elif row.kind == "inventory" and (day := source_date(row, "as_of", [])) is not None:
            dates.add(day)
    candidates: list[PlanningCandidate] = []
    for (product, location), plans in grouped.items():
        masters = [
            r for r in records if r.kind == "item" and source_text(r, "part_number") == product
        ]
        descriptions = {source_text(r, "description") for r in masters} - {None}
        areas = {source_text(r, "area") for r in [*plans, *masters]} - {None}
        candidates.append(
            PlanningCandidate(
                product_code=product,
                location=location,
                description=next(iter(descriptions)) if len(descriptions) == 1 else None,
                area=next(iter(areas)) if len(areas) == 1 else None,
                need_dates=sorted(
                    {d for r in plans if (d := source_date(r, "need_by", [])) is not None}
                ),
                evidence=source_refs([*plans, *masters]),
            )
        )
    return candidates, sorted(dates)


def _master(records: Sequence[Record], part: str, issues: list[Issue]) -> Record | None:
    found = coalesce(
        [r for r in records if r.kind == "item" and source_text(r, "part_number") == part],
        ("part_number",),
        issues,
        "conflicting_item_master",
    )
    return found[0] if len(found) == 1 else None


def _matches_scope(
    record: Record, records: Sequence[Record], scope: PlanningScope, issues: list[Issue]
) -> bool:
    product = source_text(record, "product_code")
    if scope.product_code is not None and product != scope.product_code:
        return False
    if scope.area is not None:
        master = _master(records, product or "", issues)
        area = source_text(record, "area") or source_text(master, "area")
        if area is None and product == scope.product_code:
            add_issue(
                issues,
                "unresolved_product_area",
                "The product's area is not established.",
                [record],
            )
        if area is None or norm(area) != norm(scope.area):
            return False
    if source_text(record, "location") is None:
        add_issue(
            issues,
            "missing_production_location",
            "A relevant production order cannot be assigned to a plant.",
            [record],
        )
        return False
    if source_text(record, "location") != scope.location:
        return False
    return True


def _bom_at(
    records: Sequence[Record], product: str, need: date, scope: PlanningScope, issues: list[Issue]
) -> list[Record]:
    candidates = [
        r
        for r in records
        if r.kind == "bom"
        and source_text(r, "product_code") == product
        and source_text(r, "location") in {None, scope.location}
    ]
    effective: list[Record] = []
    for row in candidates:
        since = source_date(row, "valid_from", issues)
        until = source_date(row, "valid_to", issues)
        if since is None:
            add_issue(
                issues,
                "missing_bom_validity",
                "BOM effectivity is required for dated planning.",
                [row],
            )
            continue
        if since <= need and (until is None or until >= need):
            effective.append(row)
    revisions = {source_text(r, "revision") for r in effective}
    if len(revisions) > 1:
        add_issue(
            issues,
            "ambiguous_bom_revision",
            "Multiple BOM revisions apply on the need date.",
            effective,
        )
        return []
    if not effective:
        add_issue(
            issues,
            "missing_effective_bom",
            "No effective BOM exists for this product/date.",
            candidates,
        )
    return coalesce(effective, ("product_code", "part_number"), issues, "conflicting_bom_component")


def _production_demand(
    records: Sequence[Record],
    scope: PlanningScope,
    issues: list[Issue],
) -> tuple[dict[str, list[DemandContribution]], dict[str, str]]:
    contributions: dict[str, list[DemandContribution]] = defaultdict(list)
    units: dict[str, str] = {}
    candidates = [
        r
        for r in records
        if r.kind == "production_plan" and _matches_scope(r, records, scope, issues)
    ]
    plans = coalesce(candidates, ("reference", "location"), issues, "conflicting_production_order")
    for plan in plans:
        if state(plan) in CANCELLED | CLOSED:
            continue
        due = source_date(plan, "need_by", issues)
        if due is None:
            add_issue(issues, "missing_plan_date", "A production order needs a date.", [plan])
            continue
        if due < scope.as_of or due > scope.horizon_end:
            continue
        if state(plan) not in _ACTIVE_PLANS:
            add_issue(
                issues,
                "unknown_production_status",
                "The production order's state is unknown.",
                [plan],
            )
            continue
        quantity = source_number(plan, "quantity", issues)
        product = source_text(plan, "product_code")
        if product is None or quantity is None or quantity <= 0:
            add_issue(
                issues,
                "invalid_production_order",
                "A production order needs a product identifier and a positive quantity.",
                [plan],
            )
            continue
        master = _master(records, product, issues)
        unit = source_text(plan, "unit")
        if unit is None or (
            master is not None
            and normalize_unit(unit) != normalize_unit(source_text(master, "unit"))
        ):
            add_issue(
                issues,
                "production_unit_conflict",
                "The plan and product master units are missing or incompatible.",
                [plan, *([master] if master else [])],
            )
            continue
        need = due - timedelta(days=scope.production_lead_days)
        if need < scope.as_of:
            add_issue(
                issues,
                "production_need_before_snapshot",
                "The production lead time puts component demand before the stock snapshot.",
                [plan],
            )
            continue
        bom = _bom_at(records, product, need, scope, issues)
        for row in bom:
            part = source_text(row, "part_number")
            per = source_number(row, "quantity_per", issues)
            component_unit = source_text(row, "unit")
            base = source_number(row, "base_quantity", issues)
            if source_text(row, "base_quantity") is None:
                base = Decimal(1)  # quantity_per's contract is explicitly per one finished unit.
            bom_product_unit = source_text(row, "product_unit")
            if (
                part is None
                or per is None
                or per <= 0
                or component_unit is None
                or base is None
                or base <= 0
            ):
                add_issue(
                    issues,
                    "invalid_bom_component",
                    "A BOM line needs an identified component, "
                    "unit and positive per-product quantity and base.",
                    [row],
                )
                continue
            if bom_product_unit is not None and normalize_unit(bom_product_unit) != normalize_unit(
                unit
            ):
                add_issue(
                    issues,
                    "bom_product_unit_conflict",
                    "The BOM base unit differs from the production unit.",
                    [row, plan],
                )
                continue
            if any(r.kind == "bom" and source_text(r, "product_code") == part for r in records):
                add_issue(
                    issues,
                    "nested_bom_requires_review",
                    "A component is itself an assembly; "
                    "make/buy policy and subassembly stock must be resolved before explosion.",
                    [row],
                )
                continue
            if part in units and normalize_unit(units[part]) != normalize_unit(component_unit):
                add_issue(
                    issues,
                    "component_unit_conflict",
                    "BOMs use different units for a component.",
                    [row],
                )
                continue
            units[part] = component_unit
            contributions[part].append(
                DemandContribution(
                    reference=source_text(plan, "reference") or "",
                    product_code=product,
                    quantity=quantity * per / base,
                    need_by=need,
                    evidence=source_refs([plan, row]),
                )
            )
    if contributions and scope.production_lead_days == 0:
        add_issue(
            issues,
            "component_date_uses_production_date",
            "Component demand uses the production "
            "need date; no manufacturing lead-time offset was supplied.",
            plans,
            warning=True,
        )
    return contributions, units


def _direct_demand(
    records: Sequence[Record],
    scope: PlanningScope,
    contributions: dict[str, list[DemandContribution]],
    units: dict[str, str],
    issues: list[Issue],
) -> None:
    exploded_parts = set(contributions)
    candidates: list[Record] = []
    for row in records:
        if row.kind != "material_requirement" or source_text(row, "location") != scope.location:
            continue
        part = source_text(row, "part_number")
        if part is None:
            continue
        product = source_text(row, "product_code")
        if (
            scope.product_code is not None
            and product != scope.product_code
            and part not in exploded_parts
        ):
            continue
        if scope.product_code is None:
            master = _master(records, part, issues)
            area = source_text(row, "area") or source_text(master, "area")
            if area is None or norm(area) != norm(scope.area or ""):
                continue
        need = source_date(row, "need_by", issues)
        if need is not None and not scope.as_of <= need <= scope.horizon_end:
            continue
        if scope.product_code is not None:
            if product != scope.product_code:
                # A direct demand for a BOM component may already include the exploded demand.
                if part in exploded_parts:
                    add_issue(
                        issues,
                        "unreconciled_component_requirement",
                        "A component also has "
                        "a direct requirement with no explicit product link; it cannot be "
                        "added to or substituted for the production demand.",
                        [row],
                    )
                continue
        candidates.append(row)
    for row in coalesce(
        candidates,
        ("reference", "part_number", "location"),
        issues,
        "conflicting_material_requirement",
    ):
        need = source_date(row, "need_by", issues)
        if need is None:
            add_issue(issues, "missing_need_date", "A material requirement has no date.", [row])
            continue
        if not scope.as_of <= need <= scope.horizon_end:
            continue
        part = source_text(row, "part_number") or ""
        if part in exploded_parts:
            add_issue(
                issues,
                "overlapping_demand_sources",
                "Direct component demand and exploded "
                "production demand overlap; their relationship needs reconciliation.",
                [row],
            )
            continue
        quantity = source_number(row, "quantity", issues)
        unit = source_text(row, "unit")
        if (
            quantity is None
            or unit is None
            or norm(source_text(row, "demand_basis") or "") not in _GROSS
        ):
            add_issue(
                issues,
                "unresolved_direct_demand",
                "Scoped direct demand needs explicit gross basis, quantity and unit.",
                [row],
            )
            continue
        if part in units and normalize_unit(units[part]) != normalize_unit(unit):
            add_issue(
                issues, "component_unit_conflict", "Direct demands use incompatible units.", [row]
            )
            continue
        units[part] = unit
        contributions[part].append(
            DemandContribution(
                reference=source_text(row, "reference") or "",
                quantity=quantity,
                need_by=need,
                evidence=source_refs([row]),
            )
        )


def _history(
    records: Sequence[Record],
    part: str,
    unit: str | None,
    scope: PlanningScope,
    issues: list[Issue],
) -> list[SupplierEvidence]:
    masters = [r for r in records if r.kind == "supplier"]
    performance = [r for r in records if r.kind == "supplier_performance"]
    grouped: dict[str, list[Record]] = defaultdict(list)
    histories = [
        r
        for r in records
        if r.kind == "purchase"
        and source_text(r, "part_number") == part
        and source_text(r, "location") == scope.location
        and state(r) in CLOSED
    ]
    verified: list[Record] = []
    for order in coalesce(
        histories, ("po_number", "po_line", "location"), issues, "conflicting_purchase_history"
    ):
        ordered_at = source_date(order, "ordered_at", issues)
        if ordered_at is None or ordered_at > scope.as_of:
            continue
        code = source_text(order, "supplier_code")
        if code is None or normalize_unit(source_text(order, "unit")) != normalize_unit(unit):
            add_issue(
                issues,
                "unusable_price_reference",
                "A historical price needs a supplier code and a compatible physical unit.",
                [order],
                warning=True,
            )
            continue
        # Keep errors in historical delivery records visible without fabricating received prices.
        receipt_issues: list[Issue] = []
        receipts, received = receipts_for(order, records, scope.as_of, receipt_issues)
        ordered = source_number(order, "quantity", receipt_issues)
        issues.extend(i.model_copy(update={"severity": "warning"}) for i in receipt_issues)
        if not receipts or receipt_issues or ordered is None or received != ordered:
            add_issue(
                issues,
                "unverified_purchase_receipt",
                "A closed order lacks a reconciled "
                "full receipt and is excluded from completed-purchase price references.",
                [order, *receipts],
                warning=True,
            )
            continue
        verified_order = order.model_copy(deep=True)
        for i, ref in enumerate(source_refs(receipts)):
            verified_order.evidence[f"receipt#{i}"] = ref
        grouped[code].append(verified_order)
        verified.append(verified_order)
    suppliers = [
        supplier_evidence(
            rows, masters, performance, verified, issues, include_history_evidence=False
        )
        for rows in grouped.values()
    ]
    for supplier in suppliers:
        supplier.last_price_basis = "received_order"
    qualifications = coalesce(
        [r for r in records if r.kind == "supplier_item" and source_text(r, "part_number") == part],
        ("part_number", "supplier_code"),
        issues,
        "conflicting_supplier_qualification",
    )
    for qualification in qualifications:
        since = source_date(qualification, "valid_from", issues)
        if since is None or since > scope.as_of:
            continue
        code = source_text(qualification, "supplier_code")
        existing = next((s for s in suppliers if s.supplier_code == code), None)
        if existing is None:
            matching = [r for r in masters if source_text(r, "supplier_code") == code]
            names = {source_text(r, "name") for r in matching} - {None}
            emails = {source_text(r, "email") for r in matching} - {None}
            existing = SupplierEvidence(
                supplier_code=code,
                name=next(iter(names)) if len(names) == 1 else None,
                email=next(iter(emails)) if len(emails) == 1 and len(names) == 1 else None,
                evidence=source_refs(matching),
            )
            if len(names) != 1 or len(emails) > 1:
                add_issue(
                    issues,
                    "unresolved_qualified_supplier",
                    "A qualified supplier's name or contact is not uniquely established.",
                    matching,
                    warning=True,
                )
            suppliers.append(existing)
        existing.qualification_status = source_text(qualification, "status")
        existing.evidence.extend(
            r for r in source_refs([qualification]) if r not in existing.evidence
        )
    return suppliers


def _net(line: AssessmentLine) -> None:
    if line.available is None or line.safety_stock is None:
        return
    balance, purchased = line.available, ZERO
    dates = sorted({c.need_by for c in line.demand_contributions})
    previous: date | None = None
    for day in dates:
        demand = sum((c.quantity for c in line.demand_contributions if c.need_by == day), ZERO)
        incoming = sum(
            (
                r.quantity
                for r in line.inbound
                if r.eligible
                and r.expected_at is not None
                and r.expected_at <= day
                and (previous is None or r.expected_at > previous)
            ),
            ZERO,
        )
        balance += incoming - demand
        buy = max(ZERO, line.safety_stock - balance)
        balance += buy
        purchased += buy
        line.netting.append(
            NettingStep(
                need_by=day,
                demand=demand,
                incoming=incoming,
                purchase_quantity=buy,
                projected_balance=balance,
            )
        )
        previous = day
    line.calculated_to_buy = purchased


def assess_plan(records: Sequence[Record], scope: PlanningScope) -> Assessment:
    """Read-only, tenant-independent pure computation over already-authorized evidence."""
    result = Assessment(record_counts=dict(Counter(r.kind for r in records)))
    demands, units = _production_demand(records, scope, result.issues)
    _direct_demand(records, scope, demands, units, result.issues)
    for part, contributions in sorted(demands.items()):
        issues: list[Issue] = []
        master = _master(records, part, issues)
        stock = inventory_at(records, part, scope, issues)
        unit = units[part]
        if normalize_unit(stock.unit) != normalize_unit(unit) or (
            master is not None
            and normalize_unit(source_text(master, "unit")) != normalize_unit(unit)
        ):
            add_issue(
                issues,
                "unit_conflict",
                "Demand, stock and material master units disagree.",
                [*stock.records, *([master] if master else [])],
            )
        first_need = min(c.need_by for c in contributions)
        last_need = max(c.need_by for c in contributions)
        inbound = incoming_for(records, part, unit, last_need, scope, issues)
        line = AssessmentLine(
            part_number=part,
            description=source_text(master, "description"),
            location=scope.location,
            unit=unit,
            demand_unit=unit,
            inventory_unit=stock.unit,
            need_by=first_need,
            demand=sum((c.quantity for c in contributions), ZERO),
            available=stock.available,
            on_order=sum((r.quantity for r in inbound if r.eligible), ZERO),
            safety_stock=stock.safety_stock,
            demand_contributions=contributions,
            inbound=inbound,
            evidence=source_refs([*stock.records, *([master] if master else [])]),
            issues=issues,
        )
        for contribution in contributions:
            line.evidence.extend(r for r in contribution.evidence if r not in line.evidence)
        for receipt in inbound:
            line.evidence.extend(r for r in receipt.evidence if r not in line.evidence)
        # A present order register with no matching open lines establishes zero incoming.
        if not any(r.kind == "purchase" for r in records):
            add_issue(
                line.issues,
                "missing_order_register",
                "No order register was mapped; zero outstanding purchases cannot be established.",
                [],
            )
        line.suppliers = _history(records, part, unit, scope, line.issues)
        if not any(i.severity == "blocker" for i in [*result.issues, *line.issues]):
            _net(line)
        line.ready = line.calculated_to_buy is not None
        result.lines.append(line)
    if not demands:
        add_issue(
            result.issues,
            "no_scoped_demand",
            "No production or material demand was established "
            "inside the requested product/area, plant and date horizon.",
            [],
        )
    return result
