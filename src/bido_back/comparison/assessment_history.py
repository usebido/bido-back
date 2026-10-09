"""Supplier history measured by the source assessment an RFQ line was drafted from.

A line drafted from a source assessment keeps, in `source_evidence`, the
assessed line (`"line"`, an `AssessmentLine` with each supplier's measured
statistics from the buyer's analyzed file) and, per assessment supplier, the
tenant supplier it was resolved to (`"supplier_links"`: `supplier_id`,
`supplier_code`, `name`, `email`). When an invited supplier has no imported
purchase observation for the line in the policy window, its comparison history
is built from those statistics (`basis="source_assessment"`). Imported history
with an observation in the window always wins; the two are never mixed.

Matching is explicit only. A link counts when its `supplier_id` is a supplier
invited to the RFQ (so never another tenant's) and it names exactly one
assessment supplier of the line: by supplier code, or by name only when neither
side has a code. Names are never matched against the tenant's suppliers. The
assessed part number must still be the requested line's. Anything missing,
malformed or unlinked contributes nothing: lines without links keep the
imported-history behavior. One tenant supplier linked to several measured
assessment suppliers, or one assessment supplier linked to several tenant
suppliers, is not attributed (`source_assessment_supplier_ambiguous`).

The measured `SupplierEvidence` fields follow the legacy purchase analysis
(`bido_back.purchasing.analysis`), whose rules match `summarize_performance`:

- `delivery_observations` / `on_time_rate` are `delivery_samples` /
  `on_time_rate`: complete and timely receipts succeed; partial receipts and
  overdue open lines fail; a dated receipt without quantity is not a sample.
- `quality_observations` / `quality_rate` (`1 - rejected / received` over
  validated, dated receipts) are `quality_samples` / `accepted_quantity_rate`
  (`accepted / received`): the same quantity-weighted fraction.
- `observations` is the largest of `orders`, `delivery_observations` and
  `quality_observations`; `latest_observation_on` is the later of
  `last_received_at` and the latest order date (`ordered_at`).

Differences, because the assessment is an aggregate taken at another date:

- It measured over its own lookback window, ending on its snapshot date, not
  on the comparison date: lines overdue since then and later receipts are not
  in it.
- The policy window applies to the aggregate as a whole: when its latest
  observation is older than `history_window_days` all of it is excluded as
  stale; otherwise older observations inside the assessment's window cannot be
  separated, and `oldest_observation_on` stays unknown.
- Its rates cover reconciled orders in the line's price currency and may come
  rounded (the legacy analysis keeps 4 places); invalid receipt quantities are
  left out there instead of being reported as
  `invalid_received_quality_quantities`.
- No imported PO line backs it: `evidence_po_line_ids` stays empty and the
  evidence is the assessed line in `source_evidence`, with its cell references.
  `basis` discloses this; it is not an evidence gap and does not change
  readiness, since Bido computed it from the buyer's own reconciled orders and
  receipts, the facts an import would store. Undated or future-dated
  statistics are not used (`source_assessment_history_undated`,
  `source_assessment_history_after_today`).
- Supplier percentages the source itself reports (`otif_pct`, `quality_pct`)
  are not observations of the item and are not used.

A rate without samples, or samples without a rate, stays unknown: never perfect.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from bido_back.comparison.compute import RequestedLine
from bido_back.comparison.policy import ComparisonPolicy, SupplierPerformance

UNDATED: Final = "source_assessment_history_undated"
AFTER_TODAY: Final = "source_assessment_history_after_today"
AMBIGUOUS: Final = "source_assessment_supplier_ambiguous"


class _Measured(BaseModel):
    """The measured `SupplierEvidence` fields read here; everything else is ignored."""

    model_config = ConfigDict(extra="ignore")

    ordered_at: date | None = None
    orders: int = Field(default=0, ge=0, strict=True)
    on_time_rate: Decimal | None = Field(default=None, ge=0, le=1)
    quality_rate: Decimal | None = Field(default=None, ge=0, le=1)
    delivery_observations: int = Field(default=0, ge=0, strict=True)
    quality_observations: int = Field(default=0, ge=0, strict=True)
    last_received_at: date | None = None

    @property
    def observed(self) -> int:
        return max(self.orders, self.delivery_observations, self.quality_observations)


class _Link(BaseModel):
    model_config = ConfigDict(extra="ignore")

    supplier_id: UUID
    supplier_code: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class _Assessed:
    code: str | None
    name: str | None
    # None when the entry's statistics are malformed.
    measured: _Measured | None


def _code(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip().upper() or None


def _name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join(value.casefold().split()) or None


def _parse[M: BaseModel](model: type[M], raw: object) -> M | None:
    if not isinstance(raw, dict):
        return None
    try:
        return model.model_validate(raw)
    except ValidationError:
        return None


def _is_uuid(value: JsonValue) -> bool:
    if not isinstance(value, str):
        return False
    try:
        UUID(value)
    except ValueError:
        return False
    return True


def _evidence(line: RequestedLine) -> tuple[list[_Assessed], list[_Link]] | None:
    """The assessed suppliers of the line and their links, when it has both."""
    evidence = line.source_evidence
    assessed = evidence.get("line")
    links = evidence.get("supplier_links")
    if not _is_uuid(evidence.get("assessment_id")):
        return None
    if not isinstance(assessed, dict) or not isinstance(links, list):
        return None
    part = _code(assessed.get("part_number"))
    suppliers = assessed.get("suppliers")
    if part is None or part != _code(line.part_number) or not isinstance(suppliers, list):
        return None
    return (
        [
            _Assessed(
                code=_code(entry.get("supplier_code")),
                name=_name(entry.get("name")),
                measured=_parse(_Measured, entry),
            )
            for entry in suppliers
            if isinstance(entry, dict)
        ],
        [link for raw in links if (link := _parse(_Link, raw)) is not None],
    )


def _match(link: _Link, suppliers: Sequence[_Assessed]) -> int | None:
    """The one assessed supplier a link names: by code, or by name when neither has a code."""
    code, name = _code(link.supplier_code), _name(link.name)
    if code is not None:
        found = [i for i, s in enumerate(suppliers) if s.code == code]
    elif name is not None:
        found = [i for i, s in enumerate(suppliers) if s.code is None and s.name == name]
    else:
        return None
    return found[0] if len(found) == 1 else None


def _performance(
    measured: _Measured, *, today: date, policy: ComparisonPolicy
) -> SupplierPerformance:
    dates = [d for d in (measured.last_received_at, measured.ordered_at) if d is not None]
    latest = max(dates, default=None)
    if latest is None or latest > today:
        return SupplierPerformance(
            basis="source_assessment", issues=[UNDATED if latest is None else AFTER_TODAY]
        )
    if latest < today - timedelta(days=policy.history_window_days):
        return SupplierPerformance(
            basis="source_assessment", excluded_stale_observations=measured.observed
        )
    delivery = measured.delivery_observations if measured.on_time_rate is not None else 0
    quality = measured.quality_observations if measured.quality_rate is not None else 0
    return SupplierPerformance(
        basis="source_assessment",
        observations=measured.observed,
        delivery_samples=delivery,
        on_time_rate=measured.on_time_rate if delivery else None,
        quality_samples=quality,
        accepted_quantity_rate=measured.quality_rate if quality else None,
        latest_observation_on=latest,
    )


def assessment_performance(
    line: RequestedLine,
    supplier_ids: Iterable[UUID],
    *,
    today: date,
    policy: ComparisonPolicy,
) -> dict[UUID, SupplierPerformance]:
    """The line's assessment history for each given supplier linked to measured statistics."""
    parsed = _evidence(line)
    if parsed is None:
        return {}
    suppliers, links = parsed
    claims: dict[int, set[UUID]] = {}
    for link in links:
        index = _match(link, suppliers)
        if index is not None:
            claims.setdefault(index, set()).add(link.supplier_id)
    result: dict[UUID, SupplierPerformance] = {}
    for supplier_id in dict.fromkeys(supplier_ids):
        linked = [
            (measured, len(owners) > 1)
            for index, owners in claims.items()
            if supplier_id in owners
            and (measured := suppliers[index].measured) is not None
            and measured.observed > 0
        ]
        if len(linked) == 1 and not linked[0][1]:
            result[supplier_id] = _performance(linked[0][0], today=today, policy=policy)
        elif linked:
            result[supplier_id] = SupplierPerformance(basis="source_assessment", issues=[AMBIGUOUS])
    return result


def with_assessment_history(
    imported: Mapping[UUID, Mapping[UUID, SupplierPerformance]],
    lines: Sequence[RequestedLine],
    supplier_ids: Sequence[UUID],
    *,
    today: date,
    policy: ComparisonPolicy,
) -> dict[UUID, dict[UUID, SupplierPerformance]]:
    """Per supplier and line: imported history with an observation in the policy window,
    else the line's assessment history when there is one, else the imported result."""
    result = {supplier_id: dict(by_line) for supplier_id, by_line in imported.items()}
    for line in lines:
        assessed = assessment_performance(line, supplier_ids, today=today, policy=policy)
        for supplier_id, performance in assessed.items():
            by_line = result.setdefault(supplier_id, {})
            current = by_line.get(line.id)
            if current is None or current.observations == 0:
                by_line[line.id] = performance
    return result
