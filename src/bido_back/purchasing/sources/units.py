"""Quantities and prices in an item's alternative unit, read in its base unit.

An item master or an info record may state an alternative unit and how many base units
one of it holds ("1 BD = 18 L"). A purchase, a price agreement or a stock line written
in that unit is then read in the base unit: quantities times the factor, prices divided
by it, as `CanonicalValue`s with basis `unit_conversion` (the source spellings stay).
Each conversion is a disclosed assumption; disagreeing factors convert nothing.
"""

from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from bido_back.purchasing.sources.contracts import (
    Assumption,
    CanonicalValue,
    Issue,
    Record,
    SourceRef,
)
from bido_back.purchasing.sources.evidence import (
    join_key,
    source_number,
    source_text,
    unit_key,
)

_QUANTITIES: Final = (
    "quantity", "open_quantity", "received_quantity", "rejected_quantity", "available",
    "on_order", "safety_stock", "blocked", "reserved", "total", "quality_inspection",
    "in_transit", "rejected", "min_order_quantity",
)  # fmt: skip
_PRICES: Final = ("unit_price",)
_CONVERTED: Final = frozenset(
    {"purchase", "supplier_item", "inventory", "material_requirement", "delivery_schedule"}
)


def _plain(number: Decimal) -> str:
    text = format(number.normalize(), "f")
    return "0" if text in ("-0", "") else text


def convert_units(records: Sequence[Record]) -> tuple[list[Issue], list[Assumption]]:
    """Convert, in place, records written in an item's alternative unit."""
    bases: dict[str, set[str]] = defaultdict(set)
    spelled: dict[str, str] = {}
    for record in records:
        if record.kind == "item" and (part := join_key(source_text(record, "part_number"))):
            if (unit := unit_key(record)) is not None:
                bases[part].add(unit)
                spelled.setdefault(part, source_text(record, "unit") or unit)
    factors: dict[tuple[str, str], dict[Decimal, list[SourceRef]]] = defaultdict(dict)
    for record in records:
        part = join_key(source_text(record, "part_number"))
        alternative = unit_key(record, "alt_unit")
        factor = source_number(record, "alt_unit_factor", [])
        if part is None or alternative is None or factor is None or factor <= 0:
            continue
        refs = [ref for f in ("alt_unit", "alt_unit_factor") if (ref := record.evidence.get(f))]
        factors[part, alternative].setdefault(factor, []).extend(refs)
    issues: list[Issue] = []
    assumptions: list[Assumption] = []
    for (part, alternative), found in factors.items():
        base = next(iter(bases[part])) if len(bases[part]) == 1 else None
        if base is None or base == alternative:
            continue
        if len(found) > 1:
            issues.append(
                Issue(
                    code="conflicting_unit_conversion",
                    severity="warning",
                    message=f"{part}: the sources state different conversions for "
                    f"{alternative}; nothing in that unit was converted.",
                    evidence=[ref for refs in found.values() for ref in refs][:10],
                )
            )
            continue
        factor, refs = next(iter(found.items()))
        converted = 0
        spelling = alternative
        for record in records:
            if (
                record.kind not in _CONVERTED
                or join_key(source_text(record, "part_number")) != part
                or unit_key(record) != alternative
            ):
                continue
            spelling = source_text(record, "unit") or alternative
            for name in _QUANTITIES:
                value = source_number(record, name, [])
                if value is not None:
                    _set(record, name, value * factor, refs)
            for name in _PRICES:
                value = source_number(record, name, [])
                if value is not None:
                    _set(record, name, value / factor, refs)
            record.canonical["unit"] = CanonicalValue(
                value=base, source=record.values["unit"], basis="unit_conversion", evidence=refs
            )
            converted += 1
        if converted:
            assumptions.append(
                Assumption(
                    code="unit_converted",
                    message=f"{converted} record(s) of {part} in {spelling} were read in the "
                    f"base unit at 1 {spelling} = {_plain(factor)} {spelled.get(part, base)} "
                    "(the source's own conversion); prices are per base unit.",
                    part_numbers=[part],
                    evidence=refs[:4],
                )
            )
    return issues, assumptions


def _set(record: Record, name: str, value: Decimal, refs: list[SourceRef]) -> None:
    record.canonical[name] = CanonicalValue(
        value=_plain(value), source=record.values[name], basis="unit_conversion", evidence=refs
    )
