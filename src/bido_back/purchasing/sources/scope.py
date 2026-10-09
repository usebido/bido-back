"""Resolve the buyer's planning boundaries against the mapped evidence, in code.

The copilot passes the boundaries as the buyer stated them (`ScopeRequest`): a product's
name or code, a plant as they wrote it, dates or a number of days, a safety-stock policy.
After mapping and before planning, inside the same call, this module settles them into
the `PlanningScope` the planner needs:

- product: its code compared without case, spacing or punctuation; else a production
  product's code written inside the buyer's words (a code inside a longer one loses);
  else the only production product whose master description holds all their words;
  else the only production product whose code holds what the buyer wrote (an ERP that
  names products by description: "BP-200" for "BP-200 Bomba centrifuga");
- area and plant: compared without case, accents, spacing or punctuation with the areas
  and plants of the demand; a product planned at a single plant uses it; sources that
  name no plant at all are one site: the plant the buyer named, if any, applies to
  every row, and no plant is asked for;
- reference date: when absent, the latest stock snapshot of that plant, never an older one;
- horizon: `horizon_end`, or `as_of + horizon_days`. Without either, the candidate
  orders and their dates are offered: future plans are never all included silently.

Ambiguity never guesses: it becomes a question with options taken from the evidence.
Values code settled on its own are assumptions, returned for the copilot to disclose.
Sources without production plans keep the unscoped assessment unless the request is
already a complete scope.
"""

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from bido_back.purchasing.importers import norm
from bido_back.purchasing.sources.contracts import (
    Issue,
    PlanningScope,
    Record,
    ScopeAssumption,
    ScopeField,
    ScopeOption,
    ScopeProvenance,
    ScopeQuestion,
    ScopeRequest,
    ScopeResolution,
    SourceRef,
)
from bido_back.purchasing.sources.evidence import source_date, source_refs, source_text
from bido_back.purchasing.sources.joins import CANCELLED, CLOSED, single_site, state
from bido_back.purchasing.sources.planning import code_key, same_code, same_place

MAX_OPTIONS = 12
MAX_REFS = 10
# Assumptions that settle a boundary from located evidence alone (no buyer words).
_FROM_EVIDENCE = frozenset(
    {
        "product_from_description",
        "product_from_partial_code",
        "location_single_plant",
        "as_of_latest_snapshot",
    }
)
# Words that name no product: articles, prepositions and "product" itself.
_FILLER = frozenset(
    {
        "a", "o", "os", "as", "um", "uma", "de", "do", "da", "dos", "das", "no", "na", "em",
        "para", "pra", "com", "e", "the", "an", "of", "for", "to", "in", "and", "with", "el",
        "la", "los", "las", "del", "y", "il", "lo", "gli", "le", "di", "della", "per", "der",
        "die", "den", "dem", "des", "und", "fur", "von", "les", "du", "et", "produto",
        "produtos", "product", "products", "produkt", "prodotto", "producto", "item", "itens",
        "items",
    }
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class ResolvedScope:
    scope: PlanningScope | None
    resolution: ScopeResolution


@dataclass(frozen=True, slots=True)
class _Demand:
    """A production order (code = finished product) or a material requirement (= part)."""

    record: Record
    code: str
    area: str | None
    location: str | None
    need_by: date | None


@dataclass(frozen=True, slots=True)
class _Product:
    code: str
    descriptions: list[str]
    areas: list[str]
    plans: list[_Demand]


def has_production_plans(records: Sequence[Record]) -> bool:
    """Production orders that can still create demand: a product, not cancelled or closed."""
    return any(
        record.kind == "production_plan"
        and source_text(record, "product_code") is not None
        and state(record) not in CANCELLED | CLOSED
        for record in records
    )


def as_request(scope: ScopeRequest | PlanningScope | None) -> ScopeRequest | None:
    """A plain request; an explicit planning scope is a complete one.

    Only the fields the caller set are copied, so an unset lead time stays a default.
    """
    if scope is None:
        return None
    return ScopeRequest.model_validate(scope.model_dump(exclude_unset=True))


def resolve_scope(records: Sequence[Record], request: ScopeRequest) -> ResolvedScope:
    """Settle the request against evidence that includes production plans."""
    return _Resolver(records, request).run()


def direct_scope(records: Sequence[Record], request: ScopeRequest) -> ResolvedScope:
    """Without production plans, only an already complete request becomes a scope."""
    end = request.horizon_end
    assumptions: list[ScopeAssumption] = []
    if end is None and request.as_of is not None and request.horizon_days is not None:
        end = request.as_of + timedelta(days=request.horizon_days)
        assumptions.append(_horizon_assumption(request.as_of, request.horizon_days, end))
    location = request.location
    if location is not None:
        location = _plant_spellings(records).get(norm(location), location)
    if (
        (request.product_code is None and request.area is None)
        or (location is None and not single_site(records))
        or request.as_of is None
        or end is None
    ):
        return ResolvedScope(None, ScopeResolution(request=request))
    scope = PlanningScope(
        product_code=request.product_code,
        area=request.area,
        location=location,
        as_of=request.as_of,
        horizon_end=end,
        safety_stock=request.safety_stock,
        production_lead_days=request.production_lead_days,
    )
    return ResolvedScope(
        scope,
        ScopeResolution(
            request=request,
            assumptions=assumptions,
            provenance=_provenance(request, scope, assumptions, {}),
        ),
    )


def scope_issues(resolved: ResolvedScope, *, planned: bool) -> list[Issue]:
    """Assumptions are warnings; boundaries production planning still needs are a blocker."""
    issues = [
        Issue(code=a.code, severity="warning", message=a.message, evidence=a.evidence)
        for a in resolved.resolution.assumptions
    ]
    if resolved.scope is not None:
        return issues
    if planned:
        fields = list(dict.fromkeys(q.field for q in resolved.resolution.questions))
        issues.append(
            Issue(
                code="missing_planning_scope",
                severity="blocker",
                message=(f"Open planning boundaries: {', '.join(fields)}. " if fields else "")
                + "Production and BOM evidence need a product or area, a plant, a reference "
                "date and a horizon: ask the buyer scope_resolution.questions and repeat the "
                "analysis with their answers. No purchase quantity is calculated until then.",
            )
        )
    elif resolved.resolution.request != ScopeRequest():
        issues.append(
            Issue(
                code="scope_not_applied",
                severity="warning",
                message="The sources have no production plan: direct requirements are "
                "assessed as the sources report them, for every plant and date and with the "
                "sources' safety stock; the requested boundaries were not applied.",
            )
        )
    return issues


class _Resolver:
    def __init__(self, records: Sequence[Record], request: ScopeRequest) -> None:
        self.records = records
        self.request = request
        self.masters = [r for r in records if r.kind == "item"]
        # Joins downstream compare plants exactly: settle on the spelling most records use.
        self.spellings = _plant_spellings(records)
        # No source names a plant: one site, so no plant is asked for or matched.
        self.single_site = single_site(records)
        self.demands = [d for r in records if (d := self._demand(r)) is not None]
        self.assumptions: list[ScopeAssumption] = []
        self.questions: list[ScopeQuestion] = []
        # The cells the buyer's own words matched, per field.
        self.matched: dict[ScopeField, list[SourceRef]] = {}

    def run(self) -> ResolvedScope:
        request = self.request
        plans = [d for d in self.demands if d.record.kind == "production_plan"]
        products = self._products(plans)
        product = self._product(products)
        area = self._area(product)
        if request.product_code is None and request.product is None and request.area is None:
            self._ask(
                "missing_product",
                "product_code",
                "Which product (or area) should be planned? These products have production orders.",
                [_product_option(p) for p in products],
            )
        if product is not None:
            subject, relevant = product.code, product.plans
        elif area is not None:
            subject = f"area {area}"
            relevant = [d for d in self.demands if same_place(d.area, area)]
        else:
            subject, relevant = "the planned products", plans
        location = self._location(subject, relevant)
        as_of = self._as_of(location)
        horizon_end = self._horizon(relevant, location, as_of)
        resolution = ScopeResolution(
            request=request, assumptions=self.assumptions, questions=self.questions
        )
        if (
            self.questions
            or (product is None and area is None)
            or (location is None and not self.single_site)
            or as_of is None
            or horizon_end is None
        ):
            return ResolvedScope(None, resolution)
        scope = PlanningScope(
            product_code=product.code if product is not None else None,
            area=area,
            location=location,
            as_of=as_of,
            horizon_end=horizon_end,
            safety_stock=request.safety_stock,
            production_lead_days=request.production_lead_days,
        )
        resolution.provenance = _provenance(request, scope, self.assumptions, self.matched)
        return ResolvedScope(scope, resolution)

    def _demand(self, record: Record) -> _Demand | None:
        if record.kind == "production_plan":
            code = source_text(record, "product_code")
        elif record.kind == "material_requirement":
            code = source_text(record, "part_number")
        else:
            return None
        if code is None or state(record) in CANCELLED | CLOSED:
            return None
        return _Demand(
            record=record,
            code=code,
            area=source_text(record, "area") or _only(self._master_areas(code)),
            location=self._plant(source_text(record, "location")),
            need_by=source_date(record, "need_by", []),
        )

    def _plant(self, location: str | None) -> str | None:
        return None if location is None else self.spellings.get(norm(location), location)

    def _master_areas(self, code: str) -> list[str]:
        return _places(
            source_text(m, "area")
            for m in self.masters
            if same_code(source_text(m, "part_number"), code)
        )

    def _products(self, plans: Sequence[_Demand]) -> list[_Product]:
        grouped: dict[str, list[_Demand]] = defaultdict(list)
        for plan in plans:
            grouped[code_key(plan.code)].append(plan)
        products: list[_Product] = []
        for group in grouped.values():
            code = _spelling(d.code for d in group)
            descriptions = {
                description
                for m in self.masters
                if same_code(source_text(m, "part_number"), code)
                and (description := source_text(m, "description")) is not None
            }
            products.append(
                _Product(
                    code=code,
                    descriptions=sorted(descriptions),
                    areas=_places(d.area for d in group),
                    plans=group,
                )
            )
        return sorted(products, key=lambda p: code_key(p.code))

    def _product(self, products: Sequence[_Product]) -> _Product | None:
        given, words = self.request.product_code, self.request.product
        if given is not None:
            found = [p for p in products if _loose(p.code) == _loose(given)]
            if len(found) == 1:
                self.matched["product_code"] = _product_refs(found[0])
                return found[0]
            if not found:
                # The ERP names products by description ("BP-200 Bomba centrifuga") and the
                # buyer wrote the code part: the only product whose code spells it is theirs.
                found = [p for p in products if _spans(_loose(given), norm(p.code).split())]
                if len(found) == 1:
                    self._assume_partial_code(given, found[0])
                    return found[0]
            message = (
                f"'{given}' matches several production products."
                if found
                else f"No production order is for product '{given}'."
            )
        elif words is not None:
            found = _named_in(words, products)
            if len(found) == 1:
                self.matched["product_code"] = _product_refs(found[0])
                return found[0]
            if not found:
                found = [p for p in products if any(_describes(words, d) for d in p.descriptions)]
                if len(found) == 1:
                    self._assume(
                        "product_from_description",
                        "product_code",
                        found[0].code,
                        f"'{words}' was read as product {found[0].code} "
                        f"({' / '.join(found[0].descriptions)}), the only production product "
                        "whose description matches.",
                        _product_refs(found[0]),
                    )
                    return found[0]
            if not found:
                found = [p for p in products if _describes(words, p.code)]
                if len(found) == 1:
                    self._assume_partial_code(words, found[0])
                    return found[0]
            message = (
                f"'{words}' matches several production products."
                if found
                else f"No production order matches '{words}'."
            )
        else:
            return None
        self._ask(
            "ambiguous_product" if found else "unknown_product",
            "product_code",
            message,
            [_product_option(p) for p in (found or products)],
        )
        return None

    def _assume_partial_code(self, given: str, product: _Product) -> None:
        self._assume(
            "product_from_partial_code",
            "product_code",
            product.code,
            f"'{given}' was read as product {product.code}, the only production product "
            "whose code contains it.",
            _product_refs(product),
        )

    def _area(self, product: _Product | None) -> str | None:
        given = self.request.area
        if given is None:
            return None
        if product is not None:
            found = next((a for a in product.areas if same_place(a, given)), None)
            if found is None:
                self._assume(
                    "area_not_applied",
                    "area",
                    None,
                    f"Product {product.code} is planned under "
                    f"{', '.join(product.areas) or 'no area'} in the sources, not '{given}': "
                    "the area is not used as a filter.",
                    _product_refs(product),
                )
            return found
        areas = _places(d.area for d in self.demands)
        found = next((a for a in areas if same_place(a, given)), None)
        if found is not None:
            self.matched["area"] = self._refs(
                [d.record for d in self.demands if same_place(d.area, found)], "area"
            )
        else:
            self._ask(
                "unknown_area",
                "area",
                f"No production order or requirement is in area '{given}'.",
                [
                    ScopeOption(
                        value=area,
                        label=area,
                        evidence=self._refs(
                            [d.record for d in self.demands if same_place(d.area, area)], "area"
                        ),
                    )
                    for area in areas
                ],
            )
        return found

    def _location(self, subject: str, relevant: Sequence[_Demand]) -> str | None:
        plants = _places(d.location for d in relevant)
        given = self.request.location
        if self.single_site:
            if given is None:
                self._assume(
                    "location_single_site",
                    "location",
                    None,
                    "No source names a plant: the data is read as one site, and every "
                    "order, stock position and purchase belongs to it.",
                    [],
                )
                return None
            self._assume(
                "location_applied_to_all",
                "location",
                given,
                f"No source names a plant: '{given}' is taken as the site of every order, "
                "stock position and purchase.",
                [],
            )
            return given
        if given is not None:
            found = next((p for p in plants if same_place(p, given)), None)
            if found is None:
                self._ask(
                    "unknown_location",
                    "location",
                    f"No order or requirement for {subject} is at '{given}'.",
                    self._plant_options(relevant, plants),
                )
            else:
                self.matched["location"] = self._refs(
                    [d.record for d in relevant if same_place(d.location, found)], "location"
                )
            return found
        if len(plants) == 1:
            self._assume(
                "location_single_plant",
                "location",
                plants[0],
                f"No plant was given: every order for {subject} is at {plants[0]}.",
                self._refs([d.record for d in relevant], "location"),
            )
            return plants[0]
        if plants:
            message = f"Several plants have orders for {subject}; which plant?"
        else:
            message = "No order or requirement names a plant; which plant?"
            plants = _places(
                self._plant(source_text(r, "location"))
                for r in self.records
                if r.kind == "inventory"
            )
        self._ask("missing_location", "location", message, self._plant_options(relevant, plants))
        return None

    def _plant_options(self, relevant: Sequence[_Demand], plants: list[str]) -> list[ScopeOption]:
        options: list[ScopeOption] = []
        for plant in plants:
            here = [d for d in relevant if same_place(d.location, plant)]
            details = "; ".join(_demand_label(d, place=False) for d in _by_date(here))
            options.append(
                ScopeOption(
                    value=plant,
                    label=f"{plant}: {details}" if details else plant,
                    evidence=self._refs([d.record for d in here], "location"),
                )
            )
        return options

    def _as_of(self, location: str | None) -> date | None:
        if self.request.as_of is not None:
            return self.request.as_of
        if location is None and not self.single_site:
            return None  # Settled once the plant is known.
        where = f" of {location}" if location is not None else ""
        stock: list[tuple[date, Record]] = []
        for row in self.records:
            if row.kind == "inventory" and (
                self.single_site or same_place(source_text(row, "location"), location)
            ):
                day = source_date(row, "as_of", [])
                if day is not None:
                    stock.append((day, row))
        if not stock:
            dates = sorted(
                {
                    day
                    for row in self.records
                    if row.kind == "inventory"
                    and (day := source_date(row, "as_of", [])) is not None
                },
                reverse=True,
            )
            self._ask(
                "missing_as_of",
                "as_of",
                f"No stock snapshot{where} gives the reference (export) date; "
                "which date should the analysis use?",
                [ScopeOption(value=d.isoformat(), label=f"stock snapshot {d}") for d in dates],
            )
            return None
        latest = max(day for day, _ in stock)
        older = sorted({day.isoformat() for day, _ in stock if day < latest})
        self._assume(
            "as_of_latest_snapshot",
            "as_of",
            latest.isoformat(),
            f"No reference date was given: {latest}, the latest stock snapshot{where}, "
            "is used as the export date"
            + (f"; older snapshots ({', '.join(older)}) are not added to it." if older else "."),
            self._refs([row for day, row in stock if day == latest], "as_of"),
        )
        return latest

    def _horizon(
        self, relevant: Sequence[_Demand], location: str | None, as_of: date | None
    ) -> date | None:
        end, days = self.request.horizon_end, self.request.horizon_days
        if end is not None:
            if as_of is not None and end < as_of:
                self._ask(
                    "invalid_horizon",
                    "horizon_end",
                    f"The horizon end {end} is before the reference date {as_of}.",
                    self._need_options(relevant, location, as_of),
                )
                return None
            if days is not None and as_of is not None and as_of + timedelta(days=days) != end:
                computed = as_of + timedelta(days=days)
                self._ask(
                    "conflicting_horizon",
                    "horizon_end",
                    f"{as_of} + {days} days is {computed}, not {end}: which horizon end?",
                    [
                        ScopeOption(value=computed.isoformat(), label=f"{as_of} + {days} days"),
                        ScopeOption(value=end.isoformat(), label=f"{end} as given"),
                    ],
                )
                return None
            return end
        if days is not None:
            if as_of is None:
                return None  # Settled once the reference date is known.
            end = as_of + timedelta(days=days)
            self.assumptions.append(_horizon_assumption(as_of, days, end))
            return end
        start = f" from {as_of}" if as_of is not None else ""
        self._ask(
            "missing_horizon",
            "horizon_end",
            f"Until which need date should demand be included? A date includes every order "
            f"due{start} up to it; plans after it are left out.",
            self._need_options(relevant, location, as_of),
        )
        return None

    def _need_options(
        self, relevant: Sequence[_Demand], location: str | None, as_of: date | None
    ) -> list[ScopeOption]:
        dated: dict[date, list[_Demand]] = defaultdict(list)
        for demand in relevant:
            if (
                demand.need_by is not None
                and (location is None or same_place(demand.location, location))
                and (as_of is None or demand.need_by >= as_of)
            ):
                dated[demand.need_by].append(demand)
        return [
            ScopeOption(
                value=day.isoformat(),
                label=f"{day}: " + "; ".join(_demand_label(d) for d in dated[day]),
                evidence=self._refs([d.record for d in dated[day]], "need_by"),
            )
            for day in sorted(dated)
        ]

    def _refs(self, records: Sequence[Record], *fields: str) -> list[SourceRef]:
        return source_refs(records, *fields)[:MAX_REFS]

    def _assume(
        self,
        code: str,
        field: ScopeField,
        value: str | None,
        message: str,
        evidence: list[SourceRef],
    ) -> None:
        self.assumptions.append(
            ScopeAssumption(
                code=code, field=field, value=value, message=message, evidence=evidence[:MAX_REFS]
            )
        )

    def _ask(self, code: str, field: ScopeField, message: str, options: list[ScopeOption]) -> None:
        self.questions.append(
            ScopeQuestion(code=code, field=field, message=message, options=options[:MAX_OPTIONS])
        )


def _provenance(
    request: ScopeRequest,
    scope: PlanningScope,
    assumptions: Sequence[ScopeAssumption],
    matched: Mapping[ScopeField, list[SourceRef]],
) -> list[ScopeProvenance]:
    """Where each value of the resolved scope came from: buyer, evidence or default."""
    settled = {a.field: a.evidence for a in assumptions if a.code in _FROM_EVIDENCE}
    stated: dict[ScopeField, bool] = {
        "product_code": request.product_code is not None or request.product is not None,
        "area": request.area is not None,
        "location": request.location is not None,
        "as_of": request.as_of is not None,
        "horizon_end": request.horizon_end is not None or request.horizon_days is not None,
        "safety_stock": request.safety_stock is not None,
        "production_lead_days": "production_lead_days" in request.model_fields_set,
    }
    result: list[ScopeProvenance] = []
    for field, given in stated.items():
        # An empty safety stock is a policy too: the sources' own.
        if getattr(scope, field) is None and field != "safety_stock":
            continue
        if field in settled:
            result.append(ScopeProvenance(field=field, source="evidence", evidence=settled[field]))
        elif given:
            result.append(
                ScopeProvenance(field=field, source="buyer", evidence=list(matched.get(field, [])))
            )
        else:
            result.append(ScopeProvenance(field=field, source="default"))
    return result


def _horizon_assumption(as_of: date, days: int, end: date) -> ScopeAssumption:
    return ScopeAssumption(
        code="horizon_from_days",
        field="horizon_end",
        value=end.isoformat(),
        message=f"The horizon runs from {as_of} to {end} ({days} days after it, inclusive).",
    )


def _plant_spellings(records: Sequence[Record]) -> dict[str, str]:
    """Each plant as most records of every kind spell it (case, accents and spacing aside)."""
    counts = _spelling_counts(source_text(record, "location") for record in records)
    return {key: _spelling(spellings.elements()) for key, spellings in counts.items()}


def _spelling_counts(values: Iterable[str | None]) -> dict[str, Counter[str]]:
    spellings: dict[str, Counter[str]] = defaultdict(Counter)
    for value in values:
        if value is not None and norm(value):
            spellings[norm(value)][value] += 1
    return spellings


def _places(values: Iterable[str | None]) -> list[str]:
    """Distinct plants or areas; each in the spelling most of its records use."""
    return sorted(_spelling(counts.elements()) for counts in _spelling_counts(values).values())


def _spelling(values: Iterable[str]) -> str:
    counts = Counter(values)
    return min(counts, key=lambda value: (-counts[value], value))


def _only(values: Sequence[str]) -> str | None:
    return values[0] if len(values) == 1 else None


def _loose(code: str) -> str:
    """Codes as the buyer may type them: MTR-100, mtr 100 and MTR100 are the same."""
    return "".join(norm(code).split())


def _named_in(text: str, products: Sequence[_Product]) -> list[_Product]:
    """Products whose code is written in the text; a code inside a longer one loses."""
    tokens = norm(text).split()
    spans = {p.code: _spans(_loose(p.code), tokens) for p in products}
    found = {code: places for code, places in spans.items() if places}

    def inside_another(code: str) -> bool:
        return all(
            any(
                start2 <= start and end <= end2 and (start2, end2) != (start, end)
                for other, places in found.items()
                if other != code
                for start2, end2 in places
            )
            for start, end in found[code]
        )

    return [p for p in products if p.code in found and not inside_another(p.code)]


def _spans(key: str, tokens: Sequence[str]) -> list[tuple[int, int]]:
    """Where consecutive tokens spell the code (`mtr 100` and `mtr100` both spell MTR-100)."""
    found: list[tuple[int, int]] = []
    if not key:
        return found
    for start in range(len(tokens)):
        joined = ""
        for end in range(start, len(tokens)):
            joined += tokens[end]
            if joined == key:
                found.append((start, end + 1))
                break
            if not key.startswith(joined):
                break
    return found


def _describes(text: str, description: str) -> bool:
    """Every word the buyer used (fillers aside) appears in the description."""
    wanted = _words(text)
    named = _words(description)
    return bool(wanted) and all(any(_same_word(w, n) for n in named) for w in wanted)


def _words(text: str) -> list[str]:
    return [w for w in norm(text).split() if w not in _FILLER]


def _same_word(left: str, right: str) -> bool:
    """Singular and plural are one word: motor/motores, bomba/bombas."""
    return (
        left == right or left in (right + "s", right + "es") or right in (left + "s", left + "es")
    )


def _by_date(demands: Sequence[_Demand]) -> list[_Demand]:
    return sorted(demands, key=lambda d: (d.need_by or date.max, d.code))


def _demand_label(demand: _Demand, *, code: bool = True, place: bool = True) -> str:
    record = demand.record
    parts = [
        source_text(record, "reference"),
        demand.code if code else None,
        source_text(record, "quantity"),
        source_text(record, "unit"),
        f"due {demand.need_by}" if demand.need_by is not None else None,
        f"at {demand.location}" if place and demand.location is not None else None,
    ]
    return " ".join(part for part in parts if part)


def _product_refs(product: _Product) -> list[SourceRef]:
    return source_refs([d.record for d in product.plans], "product_code")[:MAX_REFS]


def _product_option(product: _Product) -> ScopeOption:
    details = [" / ".join(product.descriptions)] if product.descriptions else []
    if product.areas:
        details.append(f"area {', '.join(product.areas)}")
    details.extend(_demand_label(d, code=False) for d in _by_date(product.plans))
    return ScopeOption(
        value=product.code,
        label=f"{product.code} ({'; '.join(details)})" if details else product.code,
        evidence=_product_refs(product),
    )
