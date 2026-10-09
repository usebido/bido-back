"""What the copilot's model reads of a tool's output.

A tool's output stays whole for the buyer's screen, the events and the stored
snapshots. The model reads a view of it instead, and the conversation's history
replays that view on every later turn, so its size is paid again and again.

Every tool: cell references (one `SourceRef` per copied cell, which code and the
screen use for provenance) are counted, never listed: `"evidence": [...]` becomes
`"evidence_cells": 3`.

A source assessment, besides:

- Empty values are left out (a missing `price` reads as no price), and an
  assumption that concerns every line is read once, in the assessment's
  `assumptions`, not again on each line.
- Its lines and lists are cut to a budget. Lines with something to buy are read in
  full first, the others as one-row summaries (`more_lines`), and what is left out
  is counted (`<list>_left_out`), so the model can say so instead of presenting
  part of a list as all of it.
"""

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Final

from pydantic import BaseModel, JsonValue

from bido_back.purchasing.sources.contracts import SourceAssessment

# The keys every `SourceRef` has.
_REF_KEYS: Final = frozenset({"source_id", "table", "row", "column"})

# Characters of JSON the model reads of an assessment's lines in full, and of the
# one-row summaries of the lines past those.
FULL_LINES_CHARS: Final = 60_000
SUMMARY_LINES_CHARS: Final = 16_000
# A line's fields in its one-row summary.
SUMMARY_FIELDS: Final = (
    "part_number",
    "description",
    "location",
    "unit",
    "need_by",
    "calculated_to_buy",
    "confidence",
    # The dated demand, so a line read in summary is never one total on one date.
    "demand_schedule",
)
# Dates of a summarized line's demand schedule the model reads.
SUMMARY_SCHEDULE_ITEMS: Final = 8
# Items the model reads of each list inside a line, and of the assessment's lists.
LINE_LIST_ITEMS: Final[Mapping[str, int]] = {
    "suppliers": 6,
    "demand_contributions": 12,
    "inbound": 12,
    "netting": 16,
    "issues": 10,
    "assumptions": 16,
    "need_schedule": 16,
    "demand_schedule": 16,
    "expedite": 8,
}
# Items the model reads of each list inside a line's timing check.
TIMING_LIST_ITEMS: Final[Mapping[str, int]] = {"projected": 16, "affected_demand": 12}
ASSESSMENT_LIST_ITEMS: Final[Mapping[str, int]] = {
    "issues": 40,
    "assumptions": 40,
    "planning_candidates": 20,
    "coverage": 40,
    "snapshot_dates": 12,
    "reports": 40,
}
CANDIDATE_NEED_DATES: Final = 6


def _is_ref(value: JsonValue) -> bool:
    return isinstance(value, dict) and _REF_KEYS <= value.keys()


def _refs(value: JsonValue) -> int | None:
    """How many cell references `value` is, or None when it is something else."""
    if _is_ref(value):
        return 1
    if isinstance(value, list) and value and all(_is_ref(item) for item in value):
        return len(value)
    if isinstance(value, dict) and value and all(_is_ref(item) for item in value.values()):
        return len(value)
    return None


def without_cell_refs(value: JsonValue) -> JsonValue:
    """`value` with every cell reference, or list of them, replaced by a count."""
    if isinstance(value, list):
        return [without_cell_refs(item) for item in value]
    if not isinstance(value, dict):
        return value
    view: dict[str, JsonValue] = {}
    for key, item in value.items():
        count = _refs(item)
        if count is not None:
            view[f"{key}_cells"] = count
        elif key == "evidence" and item == []:
            continue
        else:
            view[key] = without_cell_refs(item)
    return view


def cell_free_view(output: BaseModel) -> JsonValue:
    """The default view: the whole output, without cell references."""
    return without_cell_refs(output.model_dump(mode="json"))


def assessment_view(assessment: SourceAssessment) -> JsonValue:
    """A source assessment as the model reads it (see the module docstring).

    Lines keep their order, and so does every list that is not cut.
    """
    data = _pruned(without_cell_refs(assessment.model_dump(mode="json")))
    if not isinstance(data, dict):
        return data
    general = _general(data.get("assumptions"))
    assumptions = data.pop("assumptions", None)
    if isinstance(assumptions, list):
        # The assumptions of particular lines are read on those lines.
        kept = [a for a in assumptions if _key(a) in general]
        if kept:
            data["assumptions"] = kept
    raw = data.get("lines")
    lines = (
        [_line(line, general) for line in raw if isinstance(line, dict)]
        if isinstance(raw, list)
        else []
    )
    # Lines with something to buy first; the order of the lines read stays theirs.
    priority = sorted(range(len(lines)), key=lambda i: not _to_buy(lines[i]))
    full = _within(priority, [_size(line) for line in lines], FULL_LINES_CHARS)
    rest = [i for i in priority if i not in full]
    summaries = {i: _summary(lines[i]) for i in rest}
    summarized = _within(
        rest, [_size(summaries.get(i, {})) for i in range(len(lines))], SUMMARY_LINES_CHARS
    )
    view: dict[str, JsonValue] = {"lines": [lines[i] for i in sorted(full)]}
    if summarized:
        view["more_lines"] = [summaries[i] for i in sorted(summarized)]
    if len(lines) > len(full) + len(summarized):
        view["lines_left_out"] = len(lines) - len(full) - len(summarized)
    for key, item in _capped(data, ASSESSMENT_LIST_ITEMS).items():
        if key == "lines":
            continue
        if key == "planning_candidates" and isinstance(item, list):
            item = [
                _capped(candidate, {"need_dates": CANDIDATE_NEED_DATES})
                if isinstance(candidate, dict)
                else candidate
                for candidate in item
            ]
        view[key] = item
    return view


def _pruned(value: JsonValue) -> JsonValue:
    """`value` without nulls and empty lists or objects (booleans and zeros stay)."""
    if isinstance(value, list):
        return [_pruned(item) for item in value]
    if not isinstance(value, dict):
        return value
    kept: dict[str, JsonValue] = {}
    for key, item in value.items():
        item = _pruned(item)
        if item is None or item == [] or item == {}:
            continue
        kept[key] = item
    return kept


def _key(assumption: JsonValue) -> tuple[str, str] | None:
    if not isinstance(assumption, dict):
        return None
    code, message = assumption.get("code"), assumption.get("message")
    return (code, message) if isinstance(code, str) and isinstance(message, str) else None


def _general(assumptions: JsonValue) -> set[tuple[str, str]]:
    """The assessment's assumptions that concern every line (no part numbers)."""
    general: set[tuple[str, str]] = set()
    if isinstance(assumptions, list):
        for assumption in assumptions:
            key = _key(assumption)
            if key is not None and isinstance(assumption, dict):
                if not assumption.get("part_numbers"):
                    general.add(key)
    return general


def _line(line: Mapping[str, JsonValue], general: set[tuple[str, str]]) -> dict[str, JsonValue]:
    view = dict(line)
    assumptions = view.get("assumptions")
    if isinstance(assumptions, list):
        own = [a for a in assumptions if _key(a) not in general]
        if own:
            view["assumptions"] = own
        else:
            del view["assumptions"]
    suppliers = view.get("suppliers")
    limit = LINE_LIST_ITEMS["suppliers"]
    if isinstance(suppliers, list) and len(suppliers) > limit:
        # A cut list keeps the recommended and suggested suppliers, and its own order.
        kept = sorted(range(len(suppliers)), key=lambda i: not _suggested(suppliers[i]))[:limit]
        view["suppliers"] = [suppliers[i] for i in sorted(kept)]
        view["suppliers_left_out"] = len(suppliers) - limit
    timing = view.get("timing")
    if isinstance(timing, dict):
        view["timing"] = _capped(timing, TIMING_LIST_ITEMS)
    return _capped(view, LINE_LIST_ITEMS)


def _suggested(supplier: JsonValue) -> bool:
    return isinstance(supplier, dict) and (
        supplier.get("suggested") is True or supplier.get("recommended") is True
    )


def _to_buy(line: Mapping[str, JsonValue]) -> bool:
    quantity = line.get("calculated_to_buy")
    if not isinstance(quantity, str):
        return False
    try:
        return Decimal(quantity) > 0
    except InvalidOperation:
        return False


def _summary(line: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    summary = {name: line[name] for name in SUMMARY_FIELDS if name in line}
    return _capped(summary, {"demand_schedule": SUMMARY_SCHEDULE_ITEMS})


def _capped(value: Mapping[str, JsonValue], limits: Mapping[str, int]) -> dict[str, JsonValue]:
    """A copy of `value` whose limited lists are cut, with what was cut counted."""
    capped: dict[str, JsonValue] = {}
    for key, item in value.items():
        limit = limits.get(key)
        if limit is not None and isinstance(item, list) and len(item) > limit:
            capped[key] = item[:limit]
            capped[f"{key}_left_out"] = len(item) - limit
        else:
            capped[key] = item
    return capped


def _within(order: Sequence[int], sizes: Sequence[int], budget: int) -> set[int]:
    """The leading items of `order` whose sizes fit `budget` together (at least one)."""
    taken: set[int] = set()
    used = 0
    for index in order:
        if taken and used + sizes[index] > budget:
            break
        taken.add(index)
        used += sizes[index]
    return taken


def _size(value: JsonValue) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
