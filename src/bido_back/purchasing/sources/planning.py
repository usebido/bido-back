"""Derive component demand from source evidence and net it in date order."""

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from decimal import Decimal

from bido_back.purchasing.importers import norm
from bido_back.purchasing.sources.analysis import (
    by_supplier_code,
    confidence,
    master_facts,
    supplier_country,
    supplier_evidence,
)
from bido_back.purchasing.sources.contracts import (
    Assessment,
    AssessmentLine,
    Assumption,
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
    categorical,
    join_key,
    normalize_unit,
    row_of,
    same_key,
    source_date,
    source_number,
    source_refs,
    source_text,
    table_of,
    unit_key,
    unit_price,
)
from bido_back.purchasing.sources.joins import (
    CANCELLED,
    CLOSED,
    OPEN,
    ZERO,
    Stock,
    coalesce,
    incoming_for,
    inventory_at,
    state,
)
from bido_back.purchasing.sources.pricing import ORDER_KEY, OrderFacts, measure, reconcile_order
from bido_back.purchasing.sources.timing import check_timing
from bido_back.purchasing.sources.vocabulary import built_in

# Canonical production-order states (see `vocabulary`) that still consume components.
_ACTIVE_PLANS = frozenset({"planned", "released"})


def code_key(value: str) -> str:
    """How identifiers compare: case and spacing aside, other punctuation is significant."""
    return "".join(value.casefold().split())


def same_code(left: str | None, right: str | None) -> bool:
    return left is not None and right is not None and code_key(left) == code_key(right)


def same_place(left: str | None, right: str | None) -> bool:
    """Plants and areas compare without case, accents, spacing or punctuation."""
    return left is not None and right is not None and bool(norm(left)) and norm(left) == norm(right)


def planning_context(records: Sequence[Record]) -> tuple[list[PlanningCandidate], list[date]]:
    """Expose located product/plant/date choices so the copilot can complete a scoped call.

    Spellings of one code or plant that differ only in case or spacing are one candidate.
    """
    grouped: dict[tuple[str, str], list[Record]] = defaultdict(list)
    dates: set[date] = set()
    for row in records:
        if row.kind == "production_plan" and (product := source_text(row, "product_code")):
            grouped[code_key(product), norm(source_text(row, "location") or "")].append(row)
        elif row.kind == "inventory" and (day := source_date(row, "as_of", [])) is not None:
            dates.add(day)
    candidates: list[PlanningCandidate] = []
    for plans in grouped.values():
        product = source_text(plans[0], "product_code") or ""
        location = source_text(plans[0], "location")
        masters = [
            r
            for r in records
            if r.kind == "item" and same_code(source_text(r, "part_number"), product)
        ]
        descriptions = {source_text(r, "description") for r in masters} - {None}
        areas = {
            norm(area): area
            for r in [*plans, *masters]
            if (area := source_text(r, "area")) is not None
        }
        candidates.append(
            PlanningCandidate(
                product_code=product,
                location=location,
                description=next(iter(descriptions)) if len(descriptions) == 1 else None,
                area=next(iter(areas.values())) if len(areas) == 1 else None,
                need_dates=sorted(
                    {d for r in plans if (d := source_date(r, "need_by", [])) is not None}
                ),
                evidence=source_refs([*plans, *masters]),
            )
        )
    return candidates, sorted(dates)


def _master(records: Sequence[Record], part: str, issues: list[Issue]) -> Record | None:
    found = coalesce(
        [r for r in records if r.kind == "item" and same_key(source_text(r, "part_number"), part)],
        ("part_number",),
        issues,
        "conflicting_item_master",
    )
    return found[0] if len(found) == 1 else None


def _part_name(records: Sequence[Record], known: Iterable[str], part: str) -> str:
    """One display spelling per item: an earlier line's, the item master's or the source's."""
    for name in known:
        if same_key(name, part):
            return name
    for row in records:
        spelling = source_text(row, "part_number")
        if row.kind == "item" and spelling is not None and same_key(spelling, part):
            return spelling
    return part


def _matches_scope(
    record: Record, records: Sequence[Record], scope: PlanningScope, issues: list[Issue]
) -> bool:
    product = source_text(record, "product_code")
    if scope.product_code is not None and not same_code(product, scope.product_code):
        return False
    if scope.area is not None:
        master = _master(records, product or "", issues)
        area = source_text(record, "area") or source_text(master, "area")
        if area is None and same_code(product, scope.product_code):
            add_issue(
                issues,
                "unresolved_product_area",
                "The product's area is not established.",
                [record],
            )
        if area is None or norm(area) != norm(scope.area):
            return False
    location = source_text(record, "location")
    if location is None:
        add_issue(
            issues,
            "missing_production_location",
            "A relevant production order cannot be assigned to a plant.",
            [record],
        )
        return False
    return same_place(location, scope.location)


def _bom_at(
    records: Sequence[Record], product: str, need: date, scope: PlanningScope, issues: list[Issue]
) -> list[Record]:
    candidates = [
        r
        for r in records
        if r.kind == "bom"
        and same_key(source_text(r, "product_code"), product)
        and (
            source_text(r, "location") is None
            or same_key(source_text(r, "location"), scope.location)
        )
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
        if unit is None or (master is not None and unit_key(plan) != unit_key(master)):
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
            if bom_product_unit is not None and unit_key(row, "product_unit") != unit_key(plan):
                add_issue(
                    issues,
                    "bom_product_unit_conflict",
                    "The BOM base unit differs from the production unit.",
                    [row, plan],
                )
                continue
            if any(
                r.kind == "bom" and same_key(source_text(r, "product_code"), part) for r in records
            ):
                add_issue(
                    issues,
                    "nested_bom_requires_review",
                    "A component is itself an assembly; "
                    "make/buy policy and subassembly stock must be resolved before explosion.",
                    [row],
                )
                continue
            part = _part_name(records, contributions, part)
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
    assumptions: list[Assumption] | None = None,
) -> None:
    """Dated material requirements of the scope. With `assumptions`, an MRP list's
    conventions are read instead of stopping: its requirement elements are gross demand,
    negative requirement quantities are their size, a requirement without a stated basis
    is gross, and one already past due is due at the snapshot (each disclosed)."""
    exploded_parts = set(contributions)
    lenient = assumptions is not None
    notes: dict[str, list[Record]] = defaultdict(list)

    def exploded(part: str) -> bool:
        return any(same_key(part, name) for name in exploded_parts)

    candidates: list[Record] = []
    for row in records:
        if row.kind != "material_requirement" or not same_key(
            source_text(row, "location"), scope.location
        ):
            continue
        part = source_text(row, "part_number")
        if part is None:
            continue
        if categorical(row, "element_type") not in (None, "requirement"):
            continue  # An MRP list's stock, receipts and proposals are not demand.
        product = source_text(row, "product_code")
        if (
            scope.product_code is not None
            and not same_key(product, scope.product_code)
            and not exploded(part)
        ):
            continue
        if scope.product_code is None and not scope.all_materials:
            master = _master(records, part, issues)
            area = source_text(row, "area") or source_text(master, "area")
            if area is None or norm(area) != norm(scope.area or ""):
                continue
        need = source_date(row, "need_by", issues)
        if need is not None and not _in_horizon(need, scope, lenient=lenient):
            continue
        if scope.product_code is not None:
            if not same_key(product, scope.product_code):
                # A direct demand for a BOM component may already include the exploded demand.
                if exploded(part):
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
    for row in _requirements(candidates, issues):
        need = source_date(row, "need_by", issues)
        if need is None:
            add_issue(issues, "missing_need_date", "A material requirement has no date.", [row])
            continue
        if not _in_horizon(need, scope, lenient=lenient):
            continue
        if need < scope.as_of:
            notes["past_due"].append(row)
            need = scope.as_of
        part = source_text(row, "part_number") or ""
        if exploded(part):
            add_issue(
                issues,
                "overlapping_demand_sources",
                "Direct component demand and exploded "
                "production demand overlap; their relationship needs reconciliation.",
                [row],
            )
            continue
        quantity = source_number(row, "quantity", issues, negative=lenient)
        unit = source_text(row, "unit")
        basis = categorical(row, "demand_basis")
        if lenient and quantity is not None and quantity < 0:
            # MRP lists sign requirements negative: the demand is the quantity's size.
            if categorical(row, "sign_convention") != "negative":
                notes["negative"].append(row)
            quantity = -quantity
        if lenient and basis is None and quantity is not None and unit is not None:
            if categorical(row, "element_type") != "requirement":
                notes["basis"].append(row)
            basis = "gross"
        if quantity is None or unit is None or basis != "gross":
            add_issue(
                issues,
                "unresolved_direct_demand",
                "Scoped direct demand needs explicit gross basis, quantity and unit.",
                [row],
            )
            continue
        part = _part_name(records, contributions, part)
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
    if assumptions is not None:
        assumptions.extend(_demand_notes(notes, scope))


def _requirements(candidates: Sequence[Record], issues: list[Issue]) -> list[Record]:
    """The requirements of the scope, each once.

    A requirement list states each element once, so inside ONE table every row is its
    own requirement: an MRP list's element data is a label ("Ordem planej.") or blank
    as often as an order number, never a key unique per row. Only a material and plant
    whose requirements come from more than one table can hold the same requirement
    twice; those are merged by business key (reference, part, plant), and a row
    without its reference, or with a conflicting one, stays blocked there.
    """
    groups: dict[tuple[str | None, str | None], list[Record]] = defaultdict(list)
    for row in candidates:
        key = (join_key(source_text(row, "part_number")), join_key(source_text(row, "location")))
        groups[key].append(row)
    result: list[Record] = []
    for rows in groups.values():
        if len({table_of(row) for row in rows}) > 1:
            result.extend(
                coalesce(
                    rows,
                    ("reference", "part_number", "location"),
                    issues,
                    "conflicting_material_requirement",
                )
            )
            continue
        # One table: a row the mapper read twice (a section and a record) counts once.
        seen: set[tuple[str, str, int] | int] = set()
        for row in rows:
            cell = row_of(row)
            if cell not in seen:
                seen.add(cell)
                result.append(row)
    return result


def _in_horizon(need: date, scope: PlanningScope, *, lenient: bool) -> bool:
    """Inside the horizon; leniently, a requirement already past due counts too."""
    return need <= scope.horizon_end and (need >= scope.as_of or lenient)


def _demand_notes(notes: dict[str, list[Record]], scope: PlanningScope) -> list[Assumption]:
    texts = {
        "past_due": (
            "past_due_requirement",
            "requirement(s) dated before {as_of} are still open and counted as due on {as_of}.",
            "If they were already met, the quantities to buy drop by their size.",
            "medium",
        ),
        "negative": (
            "negative_requirement",
            "requirement(s) are written as negative quantities; their size was read as the demand.",
            "If a negative quantity meant a return or a receipt, the demand is lower.",
            "low",
        ),
        "basis": (
            "requirement_basis_assumed_gross",
            "requirement(s) do not say whether they are gross or already netted; they were "
            "read as gross demand and netted against stock and open orders.",
            "If they are already net purchase requirements, buy exactly the listed "
            "quantities instead.",
            "medium",
        ),
    }
    result: list[Assumption] = []
    for key, rows in notes.items():
        code, message, alternative, confidence = texts[key]
        parts = list(dict.fromkeys(p for r in rows if (p := source_text(r, "part_number"))))
        result.append(
            Assumption(
                code=code,
                message=f"{len(rows)} " + message.format(as_of=scope.as_of.isoformat()),
                alternative=alternative,
                confidence="low" if confidence == "low" else "medium",
                part_numbers=parts,
                evidence=source_refs(rows)[:10],
            )
        )
    return result


def _listed_supplier(
    code: str | None,
    masters: Sequence[Record],
    issues: list[Issue],
    code_on_gap: str,
    gap: str,
    performance: Sequence[Record] = (),
) -> SupplierEvidence:
    """A supplier without a completed purchase of the item: identity from the masters."""
    matching = [r for r in masters if same_key(source_text(r, "supplier_code"), code)]
    metrics = [r for r in performance if same_key(source_text(r, "supplier_code"), code)]
    names = {source_text(r, "name") for r in matching} - {None}
    emails = {source_text(r, "email") for r in matching} - {None}
    if len(names) != 1 or len(emails) > 1:
        add_issue(issues, code_on_gap, gap, matching, warning=True)
    supplier = SupplierEvidence(
        supplier_code=code,
        name=next(iter(names)) if len(names) == 1 else None,
        email=next(iter(emails)) if len(emails) == 1 and len(names) == 1 else None,
        country=supplier_country(matching),
        evidence=source_refs(matching),
    )
    master_facts(supplier, matching, metrics)
    return supplier


def _history(
    records: Sequence[Record],
    part: str,
    unit: str | None,
    scope: PlanningScope,
    issues: list[Issue],
) -> tuple[list[SupplierEvidence], list[OrderFacts]]:
    """Suppliers of the item at the plant and its orders reconciled with their receipts.

    Only completed purchases (closed, fully received) set a supplier's last price; every
    reconciled order, open or closed, is delivery and quality evidence for `measure`.
    """
    masters = [r for r in records if r.kind == "supplier"]
    performance = [r for r in records if r.kind == "supplier_performance"]
    grouped: dict[str, list[Record]] = defaultdict(list)
    candidates = [
        r
        for r in records
        if r.kind == "purchase"
        and same_key(source_text(r, "part_number"), part)
        and same_key(source_text(r, "location"), scope.location)
    ]
    histories = [r for r in candidates if state(r) in CLOSED]
    verified: list[Record] = []
    orders: list[OrderFacts] = []
    for order in coalesce(histories, ORDER_KEY, issues, "conflicting_purchase_history"):
        ordered_at = source_date(order, "ordered_at", issues)
        if ordered_at is None or ordered_at > scope.as_of:
            continue
        code = source_text(order, "supplier_code")
        if code is None or unit_key(order) != normalize_unit(unit):
            add_issue(
                issues,
                "unusable_price_reference",
                "A historical price needs a supplier code and a compatible physical unit.",
                [order],
                warning=True,
            )
            if code is not None:  # Still a delivery: performance does not depend on the unit.
                orders.append(
                    reconcile_order(
                        order,
                        records,
                        supplier=code,
                        ordered_at=ordered_at,
                        unit=unit,
                        as_of=scope.as_of,
                        issues=[],
                    )
                )
            continue
        # Keep errors in historical delivery records visible without fabricating received prices.
        facts = reconcile_order(
            order,
            records,
            supplier=code,
            ordered_at=ordered_at,
            unit=unit,
            as_of=scope.as_of,
            issues=issues,
        )
        orders.append(facts)
        if not facts.completed:
            add_issue(
                issues,
                "unverified_purchase_receipt",
                "A closed order lacks a reconciled "
                "full receipt and is excluded from completed-purchase price references.",
                [order, *facts.receipts],
                warning=True,
            )
            continue
        verified_order = order.model_copy(deep=True)
        for i, ref in enumerate(source_refs(facts.receipts)):
            verified_order.evidence[f"receipt#{i}"] = ref
        grouped[join_key(code) or code].append(verified_order)
        verified.append(verified_order)
    # `incoming_for` reports open lines' problems; here they are delivery evidence only.
    for order in coalesce(
        [r for r in candidates if state(r) in OPEN], ORDER_KEY, [], "conflicting_purchase_order"
    ):
        ordered_at = source_date(order, "ordered_at", [])
        code = source_text(order, "supplier_code")
        if ordered_at is not None and ordered_at <= scope.as_of and code is not None:
            orders.append(
                reconcile_order(
                    order,
                    records,
                    supplier=code,
                    ordered_at=ordered_at,
                    unit=unit,
                    as_of=scope.as_of,
                    issues=[],
                )
            )
    by_code = by_supplier_code(verified)
    suppliers = [
        supplier_evidence(
            rows, masters, performance, by_code, issues, include_history_evidence=False
        )
        for rows in grouped.values()
    ]
    for supplier in suppliers:
        supplier.last_price_basis = "received_order"
    for code in dict.fromkeys(order.supplier for order in orders):
        # Orders, but no completed purchase: no price, yet measured.
        if (join_key(code) or code) not in grouped:
            suppliers.append(
                _listed_supplier(
                    code,
                    masters,
                    issues,
                    "unresolved_supplier_code",
                    "A supplier's name or contact is not uniquely established.",
                    performance,
                )
            )
    qualifications = coalesce(
        [
            r
            for r in records
            if r.kind == "supplier_item" and same_key(source_text(r, "part_number"), part)
        ],
        ("part_number", "supplier_code"),
        issues,
        "conflicting_supplier_qualification",
    )
    for qualification in qualifications:
        since = source_date(qualification, "valid_from", issues)
        if since is None or since > scope.as_of:
            continue
        code = source_text(qualification, "supplier_code")
        existing = next((s for s in suppliers if same_key(s.supplier_code, code)), None)
        if existing is None:
            existing = _listed_supplier(
                code,
                masters,
                issues,
                "unresolved_qualified_supplier",
                "A qualified supplier's name or contact is not uniquely established.",
                performance,
            )
            suppliers.append(existing)
        # The source's own spelling when it is a status word, else what it implies (an
        # ERP info record's number implies an approved source).
        raw = source_text(qualification, "status")
        existing.qualification_status = (
            raw
            if built_in("supplier_item", "status", raw) is not None
            else categorical(qualification, "status") or raw
        )
        _agreement(existing, qualification, scope.as_of)
        existing.evidence.extend(
            r for r in source_refs([qualification]) if r not in existing.evidence
        )
    return suppliers, orders


def _agreement(supplier: SupplierEvidence, link: Record, as_of: date) -> None:
    """An info record's price per base unit, validity, lead time and minimum quantity."""
    price = unit_price(link, [])
    if price is not None and price > 0:
        supplier.contract_price = price
        supplier.contract_currency = (source_text(link, "currency") or "").upper() or None
    until = source_date(link, "valid_to", [])
    if until is not None:
        supplier.contract_valid_to = until
        supplier.contract_expired = until < as_of
    supplier.contract_lead_time_days = source_number(link, "lead_time_days", [])
    supplier.min_order_quantity = source_number(link, "min_order_quantity", [])


def _net(line: AssessmentLine, available: Decimal, safety: Decimal) -> None:
    """Date-ordered netting: each need date's shortfall below safety stock is bought."""
    balance, purchased = available, ZERO
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
        buy = max(ZERO, safety - balance)
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


def lot_size(line: AssessmentLine, master: Record | None) -> Assumption | None:
    """The material master's minimum lot and rounding value applied to a positive need."""
    need = line.calculated_to_buy
    if master is None or need is None or need <= 0:
        return None
    minimum = source_number(master, "min_lot_size", [])
    rounding = source_number(master, "rounding", [])
    quantity = need
    if minimum is not None and minimum > quantity:
        quantity = minimum
    if rounding is not None and rounding > 0 and quantity % rounding:
        quantity = (quantity // rounding + 1) * rounding
    if quantity == need:
        return None
    line.net_requirement, line.calculated_to_buy = need, quantity
    unit = line.unit or ""
    rules = [
        f"minimum lot {_plain(minimum)}" if minimum is not None and minimum > 0 else "",
        f"rounding value {_plain(rounding)}" if rounding is not None and rounding > 0 else "",
    ]
    return Assumption(
        code="lot_size_applied",
        message=f"The net need of {_plain(need)} {unit} was raised to {_plain(quantity)} {unit} "
        f"by the material master's {' and '.join(r for r in rules if r)}.",
        alternative=f"Without the lot-sizing rule, buy {_plain(need)} {unit}.",
        part_numbers=[line.part_number],
        evidence=source_refs([master], "min_lot_size", "rounding"),
    )


def _plain(number: Decimal | None) -> str:
    if number is None:
        return ""
    text = format(number.normalize(), "f")
    return "0" if text in ("-0", "") else text


def assess_plan(records: Sequence[Record], scope: PlanningScope) -> Assessment:
    """Read-only, tenant-independent pure computation over already-authorized evidence.

    Every material with demand gets a best-estimate quantity: a missing stock balance or
    safety-stock policy is not netted (and says so), problems lower the line's
    confidence instead of holding it, and every assumption is listed on the line.
    """
    result = Assessment(record_counts=dict(Counter(r.kind for r in records)))
    found: list[Assumption] = []
    demands, units = _production_demand(records, scope, result.issues)
    _direct_demand(records, scope, demands, units, result.issues, found)
    for part, contributions in sorted(demands.items()):
        issues: list[Issue] = []
        assumptions: list[Assumption] = [a for a in found if _concerns(a, part)]
        master = _master(records, part, issues)
        master_safety = source_number(master, "safety_stock", [])
        stock = inventory_at(records, part, scope, issues, master_safety=master_safety)
        if any(i.code == "stale_inventory_snapshot" for i in issues):
            days = sorted({d for r in stock.records if (d := source_date(r, "as_of", []))})
            assumptions.append(
                Assumption(
                    code="older_stock_snapshot_used",
                    message=f"The latest usable stock snapshot is from "
                    f"{', '.join(d.isoformat() for d in days)}, before the reference date "
                    f"{scope.as_of.isoformat()}: its balance was used.",
                    alternative="Stock movements since then are not reflected.",
                    confidence="low",
                    part_numbers=[part],
                    evidence=source_refs(stock.records, "as_of")[:6],
                )
            )
        unit = units[part]
        if normalize_unit(stock.unit) != normalize_unit(unit) or (
            master is not None
            and source_text(master, "unit") is not None
            and unit_key(master) != normalize_unit(unit)
        ):
            add_issue(
                issues,
                "unit_conflict",
                "Demand, stock and material master units disagree.",
                [*stock.records, *([master] if master else [])],
            )
            if stock.unit is not None and normalize_unit(stock.unit) != normalize_unit(unit):
                # No conversion: stock in another unit is never netted against the demand.
                stock = Stock(None, stock.unit, stock.safety_stock, stock.records)
        first_need = min(c.need_by for c in contributions)
        last_need = max(c.need_by for c in contributions)
        inbound = incoming_for(records, part, unit, last_need, scope, issues, assumptions)
        line = AssessmentLine(
            part_number=part,
            description=source_text(master, "description") or _described(records, part),
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
            assumptions=assumptions,
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
                warning=True,
            )
            line.assumptions.append(
                Assumption(
                    code="no_open_orders_known",
                    message="No purchase order list was given: no open order was subtracted.",
                    alternative="Open orders arriving in time reduce the quantity to buy.",
                    confidence="low",
                    part_numbers=[part],
                )
            )
        line.suppliers, orders = _history(records, part, unit, scope, line.issues)
        _best_estimate(line, stock)
        if (sized := lot_size(line, master)) is not None:
            line.assumptions.append(sized)
        line.ready = line.calculated_to_buy is not None
        # Supplier performance and the internal price benchmark (an internal reference only).
        measure(
            line,
            {s.supplier_code: s for s in line.suppliers if s.supplier_code is not None},
            orders,
            scope.as_of,
        )
        # When new orders would arrive against the dated balance: computed after the
        # ranking, which it may override for timing; the quantity to buy is unchanged.
        check_timing(line, as_of=scope.as_of, master=master, records=records)
        confidence(line, blocked=any(i.severity == "blocker" for i in result.issues))
        result.lines.append(line)
    result.assumptions.extend(a for a in found if not a.part_numbers)
    if not demands:
        add_issue(
            result.issues,
            "no_scoped_demand",
            "No production or material demand was established "
            "inside the requested product/area, plant and date horizon.",
            [],
        )
    return result


def _described(records: Sequence[Record], part: str) -> str | None:
    """A requirement's own description of the item, when no item master names it."""
    for record in records:
        if record.kind == "material_requirement" and same_key(
            source_text(record, "part_number"), part
        ):
            text = source_text(record, "description")
            if text is not None:
                return text
    return None


def _concerns(assumption: Assumption, part: str) -> bool:
    return any(same_key(p, part) for p in assumption.part_numbers)


def _best_estimate(line: AssessmentLine, stock: Stock) -> None:
    """Net with what is established; what is not is left out and said so."""
    available, safety = line.available, line.safety_stock
    part = line.part_number
    if available is None:
        available = ZERO
        line.assumptions.append(
            Assumption(
                code="stock_not_netted",
                message=(
                    f"Stock is in {stock.unit} and the demand in {line.unit}, with no "
                    "conversion: stock was not subtracted."
                    if stock.unit is not None
                    and normalize_unit(stock.unit) != normalize_unit(line.unit)
                    else "No usable free-stock balance was found: stock was not subtracted."
                ),
                alternative="Any free stock on hand reduces the quantity to buy by its size.",
                confidence="low",
                part_numbers=[part],
            )
        )
    if safety is None:
        safety = ZERO
        line.assumptions.append(
            Assumption(
                code="no_safety_stock",
                message="No safety-stock policy was found or agreed: none was kept (0).",
                alternative="A safety stock raises the quantity to buy by its size.",
                part_numbers=[part],
            )
        )
    _net(line, available, safety)
