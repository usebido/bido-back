"""Check the buyer's scope selectors against what the sources hold, before planning.

The copilot passes the plants, MRP element and product as the buyer said them. Once
the sources are mapped and before anything is planned, this module settles them
against the distinct values the records hold (`FoundValues`):

- plants: an explicit list is matched plant by plant; `"all"` is every plant found; a
  count equal to the number found is every plant, any other count is a question that
  lists the plants found. One plant selected becomes the request's `location`.
- MRP element: a value of the requirement rows' element column (NecDep, NecInd...),
  never a field: only requirement rows with that element stay demand.
- product: with production orders, a code the data holds (loosely, or inside a longer
  code), or words that name a production product's code or description. Without
  production orders the product is not checked: direct demand and MRP lists are
  assessed as the sources report them, and say so (`scope_not_applied`).

A value the data does not hold is a `ScopeValidationError` naming the valid values: an
empty result is never how a wrong parameter surfaces. Without a plant column at all,
plants are not checked (the sources are one site).
"""

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from bido_back.purchasing.importers import norm
from bido_back.purchasing.sources.contracts import (
    Assumption,
    FoundValues,
    Record,
    ScopeOption,
    ScopeQuestion,
    ScopeRequest,
)
from bido_back.purchasing.sources.evidence import source_date, source_text
from bido_back.purchasing.sources.planning import code_key, same_place
from bido_back.purchasing.sources.scope import FILLER

MAX_PRODUCTS = 50
_DEMAND_KINDS = frozenset({"production_plan", "material_requirement"})


class ScopeValidationError(Exception):
    """A scope value the sources do not hold, with the values they do."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        field: str,
        value: str | None,
        valid_values: Sequence[str],
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.value = value
        self.valid_values = list(valid_values)


@dataclass(frozen=True, slots=True)
class Selection:
    """The request after its selectors were settled against the sources."""

    request: ScopeRequest
    # The plants selected, in the sources' spelling; empty when none was selected.
    plants: list[str]
    found: FoundValues
    # A count of plants that is not the number found: ask which ones.
    question: ScopeQuestion | None = None


def found_values(records: Sequence[Record]) -> FoundValues:
    """The distinct values the records hold for each boundary."""
    plants = _distinct(source_text(r, "location") for r in records)
    elements = _distinct(
        source_text(r, "element_type") for r in records if r.kind == "material_requirement"
    )
    products = _distinct(
        source_text(r, "product_code") for r in records if r.kind == "production_plan"
    ) or _distinct(
        source_text(r, "part_number") for r in records if r.kind == "material_requirement"
    )
    needs = sorted(
        day
        for r in records
        if r.kind in _DEMAND_KINDS and (day := source_date(r, "need_by", [])) is not None
    )
    return FoundValues(
        plants=plants,
        mrp_elements=elements,
        products=products[:MAX_PRODUCTS],
        products_left_out=max(0, len(products) - MAX_PRODUCTS),
        first_need_date=needs[0] if needs else None,
        last_need_date=needs[-1] if needs else None,
    )


def select_scope(records: Sequence[Record], request: ScopeRequest | None) -> Selection:
    """Settle the request's selectors against the sources; raise on a value they lack."""
    found = found_values(records)
    request = request or ScopeRequest()
    if request.mrp_element is not None:
        _check_element(request.mrp_element, found)
    if request.product_code is not None:
        _check_code(request.product_code, records, found)
    elif request.product is not None:
        _check_words(request.product, records, found)
    if request.location is not None and found.plants:
        _check_plant(request.location, "location", found)
    plants: list[str] = []
    question: ScopeQuestion | None = None
    selector = request.plants
    if isinstance(selector, list):
        if found.plants:
            plants = [_check_plant(plant, "plants", found) for plant in selector]
    elif selector == "all":
        plants = list(found.plants)
    elif isinstance(selector, int):
        if selector == len(found.plants):
            plants = list(found.plants)
        else:
            question = ScopeQuestion(
                code="plant_count_mismatch",
                field="plants",
                message=f"You asked for {selector} plant(s); the sources hold "
                f"{len(found.plants)}"
                + (f": {', '.join(found.plants)}" if found.plants else "")
                + ". Which plants should be analyzed?",
                options=[ScopeOption(value=plant, label=plant) for plant in found.plants],
            )
    plants = list(dict.fromkeys(plants))
    if len(plants) == 1 and request.location is None:
        request = request.model_copy(update={"location": plants[0]})
    return Selection(request=request, plants=plants, found=found, question=question)


def apply_mrp_element(
    records: Sequence[Record], element: str | None
) -> tuple[list[Record], Assumption | None]:
    """Keep only the requirement rows of that MRP element; say what was left out."""
    if element is None:
        return list(records), None
    kept: list[Record] = []
    dropped: Counter[str] = Counter()
    for record in records:
        value = source_text(record, "element_type")
        if (
            record.kind == "material_requirement"
            and value is not None
            and not _same(value, element)
        ):
            dropped[value] += 1
            continue
        kept.append(record)
    if not dropped:
        return kept, None
    others = ", ".join(f"{value} ({count})" for value, count in sorted(dropped.items()))
    return kept, Assumption(
        code="mrp_element_filter",
        message=f"Only requirement rows with MRP element '{element}' count as demand: "
        f"{sum(dropped.values())} requirement row(s) of other elements were left out "
        f"({others}).",
        alternative="Leave the MRP element out of the scope to count every requirement.",
    )


def _check_plant(given: str, field: str, found: FoundValues) -> str:
    matches = _matching_plants(given, found)
    if not matches:
        raise ScopeValidationError(
            f"No source names plant '{given}'. Plants in the sources: {', '.join(found.plants)}.",
            code="unknown_plant",
            field=field,
            value=given,
            valid_values=found.plants,
        )
    return matches[0]


def _matching_plants(given: str, found: FoundValues) -> list[str]:
    """The plant the buyer named, as the sources spell it; "planta 1000" names 1000."""
    exact = [plant for plant in found.plants if same_place(plant, given)]
    if exact:
        return exact
    tokens = set(norm(given).split())
    return [plant for plant in found.plants if set(norm(plant).split()) <= tokens]


def _check_element(given: str, found: FoundValues) -> None:
    if any(_same(value, given) for value in found.mrp_elements):
        return
    raise ScopeValidationError(
        f"'{given}' is not an MRP element of the requirement rows in the sources"
        + (
            f". Elements found: {', '.join(found.mrp_elements)}."
            if found.mrp_elements
            else ": the sources have no MRP element column."
        ),
        code="unknown_mrp_element",
        field="mrp_element",
        value=given,
        valid_values=found.mrp_elements,
    )


def _check_code(given: str, records: Sequence[Record], found: FoundValues) -> None:
    codes = _production_codes(records)
    if not codes:
        return
    key = _loose(given)
    if any(key == _loose(code) or key in _loose(code) for code in codes):
        return
    raise ScopeValidationError(
        f"No production order or requirement is for product code '{given}'. "
        f"{_products_message(records, found)}",
        code="unknown_product",
        field="product_code",
        value=given,
        valid_values=found.products,
    )


def _check_words(given: str, records: Sequence[Record], found: FoundValues) -> None:
    codes = _production_codes(records)
    if not codes:
        return
    words = [word for word in norm(given).split() if word not in FILLER]
    if not words:
        return
    loose = "".join(words)
    for code in codes:
        text = norm(f"{code} {_description(records, code) or ''}").split()
        if loose in _loose(code) or any(_same_word(w, t) for w in words for t in text):
            return
    raise ScopeValidationError(
        f"No production order or requirement matches '{given}'. "
        f"{_products_message(records, found)}",
        code="unknown_product",
        field="product",
        value=given,
        valid_values=found.products,
    )


def _products_message(records: Sequence[Record], found: FoundValues) -> str:
    labels = [
        f"{code} ({description})" if (description := _description(records, code)) else code
        for code in found.products
    ]
    return (
        "Products in the sources: "
        + ", ".join(labels)
        + (f" and {found.products_left_out} more." if found.products_left_out else ".")
    )


def _production_codes(records: Sequence[Record]) -> list[str]:
    return _distinct(source_text(r, "product_code") for r in records if r.kind == "production_plan")


def _loose(code: str) -> str:
    """Codes as the buyer may type them: MTR-100, mtr 100 and MTR100 are the same."""
    return "".join(norm(code).split())


def _description(records: Sequence[Record], code: str) -> str | None:
    for record in records:
        if record.kind not in ("item", "material_requirement"):
            continue
        if code_key(source_text(record, "part_number") or "") != code_key(code):
            continue
        description = source_text(record, "description")
        if description is not None:
            return description
    return None


def _distinct(values: Iterable[str | None]) -> list[str]:
    """Distinct values, case, accents and spacing aside, each as most rows spell it."""
    spellings: dict[str, Counter[str]] = defaultdict(Counter)
    for value in values:
        if value is not None and norm(value):
            spellings[norm(value)][value] += 1
    return sorted(
        min(counts, key=lambda value: (-counts[value], value)) for counts in spellings.values()
    )


def _same(left: str, right: str) -> bool:
    return bool(norm(left)) and norm(left) == norm(right)


def _same_word(left: str, right: str) -> bool:
    """Singular and plural are one word: motor/motores, bomba/bombas."""
    return (
        left == right or left in (right + "s", right + "es") or right in (left + "s", left + "es")
    )


__all__ = [
    "MAX_PRODUCTS",
    "ScopeValidationError",
    "Selection",
    "apply_mrp_element",
    "found_values",
    "select_scope",
]
