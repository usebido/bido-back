"""What a supplier's offer still lacks: the open items a clarification email asks for.

Deterministic, from the stored proposal (never from the model): the offer's
currency and validity, and for every requested RFQ line a unit price, the
quantity it applies to and a lead time.

- "missing": no value at all (or no offered line for a requested position);
- "unclear": a value the extraction flagged for review (`needs_review` in its
  provenance), which the supplier is asked to confirm.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, cast
from uuid import UUID

from bido_back.agents.catalog.contracts import FollowupField, FollowupItem

Problem = Literal["missing", "unclear"]

# Proposal provenance key -> the field the email asks for.
_HEADER: Final[dict[str, FollowupField]] = {"currency": "currency", "valid_until": "valid_until"}
_LINE: Final[dict[str, FollowupField]] = {
    "unit_price": "unit_price",
    "min_quantity": "quantity",
    "lead_time_days": "lead_time",
}


@dataclass(frozen=True, slots=True)
class RequestedLine:
    id: UUID
    position: int
    description: str
    mpn: str | None

    @property
    def label(self) -> str:
        return f"{self.description} ({self.mpn})" if self.mpn else self.description


@dataclass(frozen=True, slots=True)
class OfferedLine:
    rfq_line_id: UUID | None
    values: Mapping[str, object]  # unit_price, min_quantity, lead_time_days
    fields: Mapping[str, object]  # the line's provenance


def _flagged(provenance: Mapping[str, object], key: str) -> bool:
    entry = provenance.get(key)
    if not isinstance(entry, dict):
        return False
    return cast(dict[str, object], entry).get("needs_review") is True


def open_items(
    *,
    header_values: Mapping[str, object],
    header_fields: Mapping[str, object],
    requested: Sequence[RequestedLine],
    offered: Sequence[OfferedLine],
) -> list[FollowupItem]:
    items: list[FollowupItem] = []
    for key, field in _HEADER.items():
        problem = _problem(header_values.get(key), header_fields, key)
        if problem is not None:
            items.append(FollowupItem(position=None, line=None, field=field, problem=problem))

    for line in sorted(requested, key=lambda r: r.position):
        answers = [o for o in offered if o.rfq_line_id == line.id]
        if not answers:
            items.extend(
                FollowupItem(position=line.position, line=line.label, field=f, problem="missing")
                for f in _LINE.values()
            )
            continue
        for key, field in _LINE.items():
            # A line is fine when at least one offered tier has the value cleanly.
            problems = [_problem(o.values.get(key), o.fields, key) for o in answers]
            if any(p is None for p in problems):
                continue
            line_problem: Problem = "unclear" if "unclear" in problems else "missing"
            items.append(
                FollowupItem(
                    position=line.position, line=line.label, field=field, problem=line_problem
                )
            )
    return items


def _problem(value: object, provenance: Mapping[str, object], key: str) -> Problem | None:
    if value is None:
        return "missing"
    if _flagged(provenance, key):
        return "unclear"
    return None
