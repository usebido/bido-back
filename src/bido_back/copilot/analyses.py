"""The analyses a copilot conversation already ran: kept whole, reused, explained.

The buyer asks several questions about one analysis ("e o item 100000152, por
quê?"). Its result used to live only in the conversation's history, as the bounded
view the model read, so a follow-up was answered by running the analysis again,
often with a narrower scope (the material code as `product_code`) that found no
demand and contradicted the first answer.

Each `analyze_procurement_sources` result of a conversation is stored here whole
(`copilot_analyses`), keyed by its inputs: the files and the buyer's normalized scope
request. Then, in code:

- The same inputs, or an equivalent request (the same files, with values the stored
  analysis resolved to: its plant, its dates), return the stored result: nothing is
  recomputed and no model reads the files again, until the buyer explicitly asks to
  recompute (`recompute`), which replaces it.
- A narrower request on the same files (a product, plant or MRP element the stored
  analysis did not narrow to) is never stored, so it never replaces the analysis the
  buyer is asking about. When it names an item the stored analysis has, it is not
  run at all: the caller points to that item instead (`narrowed_to_item`).
- `explain_item` reads one item of the latest analysis: its whole code-produced
  record, no model call and no re-planning.
"""

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, Field, JsonValue, ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import Insert, insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import CopilotAnalysis
from bido_back.purchasing.sources.contracts import (
    AlertExclusion,
    AssessmentLine,
    PlanningScope,
    ScopeRequest,
    SourceAssessment,
    SupplierEvidence,
    TimingAlert,
)
from bido_back.purchasing.sources.evidence import join_key

logger = logging.getLogger(__name__)

Db = AsyncEngine | async_sessionmaker[AsyncSession]

# Bumped when what a key covers changes, so older rows are never matched.
KEY_VERSION: Final = 1
# Stored analyses of a conversation compared with a new request, latest first.
RECENT: Final = 10
# Lines an item question returns at most (one part can be planned at several plants).
MAX_ITEMS: Final = 5
# Part numbers listed when an item is not in the analysis.
MAX_PART_NUMBERS: Final = 60
# A product name shorter than this never matches a description by containment.
MIN_NAME_CHARS: Final = 3

_ITEM_FIELDS: Final = ("product_code", "product", "area")
_HORIZON_FIELDS: Final = ("as_of", "horizon_end", "horizon_days")


@dataclass(frozen=True, slots=True)
class StoredAnalysis:
    id: UUID
    created_at: datetime
    upload_ids: tuple[UUID, ...]
    request: ScopeRequest | None
    assessment: SourceAssessment


# --- Keys and comparisons ----------------------------------------------------------------


def _text(value: str | None) -> str | None:
    return " ".join(value.split()).casefold() if value and value.strip() else None


def _code(value: str | None) -> str | None:
    return join_key(value)


def normalized_request(request: ScopeRequest | None) -> dict[str, JsonValue]:
    """The request's set values, spelled one way: case, spacing and defaults ignored."""
    if request is None:
        return {}
    found: dict[str, JsonValue] = {}
    for name in ("product", "area", "location", "mrp_element"):
        value = _text(getattr(request, name))
        if value is not None:
            found[name] = value
    if (code := _code(request.product_code)) is not None:
        found["product_code"] = code
    for name in ("as_of", "horizon_end"):
        day: date | None = getattr(request, name)
        if day is not None:
            found[name] = day.isoformat()
    if request.horizon_days is not None:
        found["horizon_days"] = request.horizon_days
    if request.safety_stock is not None:
        found["safety_stock"] = str(request.safety_stock.normalize())
    if request.production_lead_days:
        found["production_lead_days"] = request.production_lead_days
    plants = request.plants
    if isinstance(plants, list):
        found["plants"] = list[JsonValue](
            sorted({p for p in (_text(plant) for plant in plants) if p})
        )
    elif plants is not None:
        found["plants"] = plants
    return found


def input_key(upload_ids: Sequence[UUID], request: ScopeRequest | None) -> str:
    """What makes two analysis calls the same: the files and the normalized request."""
    return hashlib.sha256(
        json.dumps(
            {
                "version": KEY_VERSION,
                "uploads": sorted({str(upload) for upload in upload_ids}),
                "scope": normalized_request(request),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _resolved(stored: StoredAnalysis) -> PlanningScope | None:
    return stored.assessment.scope


def _item_matches(value: str, stored: StoredAnalysis) -> bool:
    """A product the request names is the stored analysis's own product or area."""
    scope = _resolved(stored)
    asked = normalized_request(stored.request)
    if _text(value) in (asked.get("product"), asked.get("area")):
        return True
    if _code(value) is not None and _code(value) == asked.get("product_code"):
        return True
    if scope is None:
        return False
    return (scope.product_code is not None and _code(value) == _code(scope.product_code)) or (
        scope.area is not None and _text(value) == _text(scope.area)
    )


def _location_matches(value: str, stored: StoredAnalysis) -> bool:
    scope = _resolved(stored)
    if _text(value) == normalized_request(stored.request).get("location"):
        return True
    return scope is not None and (
        _text(value) == _text(scope.location) or _code(value) == _code(scope.location)
    )


def _horizon_matches(request: ScopeRequest, stored: StoredAnalysis) -> bool:
    """Every horizon value the request gives is the stored analysis's own (asked or
    resolved): `horizon_days` 28 and `horizon_end` 2026-11-05 from 2026-10-08 agree."""
    scope = _resolved(stored)
    asked = stored.request or ScopeRequest()
    if request.as_of is not None and request.as_of not in (
        asked.as_of,
        scope.as_of if scope else None,
    ):
        return False
    if request.horizon_end is not None and request.horizon_end not in (
        asked.horizon_end,
        scope.horizon_end if scope else None,
    ):
        return False
    if request.horizon_days is not None:
        days = (scope.horizon_end - scope.as_of).days if scope else None
        if request.horizon_days not in (asked.horizon_days, days):
            return False
    return True


def same_inputs(
    upload_ids: Sequence[UUID], request: ScopeRequest | None, stored: StoredAnalysis
) -> bool:
    """The request asks for the stored analysis again: the same files, and every value it
    gives is the one the stored analysis was asked or resolved; every boundary the stored
    request set is still set (an omitted horizon or product would mean the default)."""
    if set(upload_ids) != set(stored.upload_ids):
        return False
    new = request or ScopeRequest()
    asked = normalized_request(stored.request)
    given = normalized_request(request)
    for name in _ITEM_FIELDS:
        value = getattr(new, name)
        if value is not None and not _item_matches(value, stored):
            return False
    if any(name in asked for name in _ITEM_FIELDS) and not any(
        name in given for name in _ITEM_FIELDS
    ):
        return False
    if new.location is not None and not _location_matches(new.location, stored):
        return False
    if "location" in asked and new.location is None:
        return False
    if not _horizon_matches(new, stored):
        return False
    if any(name in asked for name in _HORIZON_FIELDS) and not any(
        name in given for name in _HORIZON_FIELDS
    ):
        return False
    for name in ("plants", "mrp_element", "safety_stock", "production_lead_days"):
        if given.get(name) != asked.get(name):
            return False
    return True


def narrower(
    upload_ids: Sequence[UUID], request: ScopeRequest | None, stored: StoredAnalysis
) -> bool:
    """The request narrows the stored analysis to part of it: the same files (or some of
    them), and an item of its lines, or a product, area, plant or MRP element the stored
    one did not narrow to. Another product than the stored one is a new scope instead."""
    if request is None or not set(upload_ids) <= set(stored.upload_ids):
        return False
    asked = normalized_request(stored.request)
    for name in _ITEM_FIELDS:
        value = getattr(request, name)
        if value is None or _item_matches(value, stored):
            continue
        if matching_lines(stored.assessment, value):
            return True
        if not any(n in asked for n in _ITEM_FIELDS):
            return True
    if request.location is not None and "location" not in asked and "plants" not in asked:
        if not _location_matches(request.location, stored):
            return True
    if request.plants is not None and "plants" not in asked and "location" not in asked:
        return True
    return request.mrp_element is not None and "mrp_element" not in asked


def requested_item(request: ScopeRequest | None) -> str | None:
    """The item a narrowed request names, as the model passed it."""
    if request is None:
        return None
    return request.product_code or request.product


def matching_lines(assessment: SourceAssessment, material: str) -> list[int]:
    """The indexes of the lines that are `material`: its code first (leading zeros,
    case and spacing ignored), else its description, whole or containing it."""
    by_code = [
        index
        for index, line in enumerate(assessment.lines)
        if _code(line.part_number) is not None and _code(line.part_number) == _code(material)
    ]
    if by_code:
        return by_code
    name = _text(material)
    if name is None:
        return []
    exact = [i for i, line in enumerate(assessment.lines) if _text(line.description) == name]
    if exact or len(name) < MIN_NAME_CHARS:
        return exact
    return [
        index
        for index, line in enumerate(assessment.lines)
        if name in (_text(line.description) or "")
    ]


# --- Storage -------------------------------------------------------------------------------


def _stored(row: CopilotAnalysis) -> StoredAnalysis | None:
    """A stored row, or None when it no longer reads as today's contract."""
    try:
        assessment = SourceAssessment.model_validate(row.assessment)
        request = ScopeRequest.model_validate(row.request) if row.request is not None else None
    except ValidationError:
        return None
    return StoredAnalysis(
        id=row.id,
        created_at=row.created_at,
        upload_ids=tuple(row.upload_ids),
        request=request,
        assessment=assessment,
    )


async def recent(
    db: Db, principal: Principal, conversation_id: UUID, *, limit: int = RECENT
) -> list[StoredAnalysis]:
    """The conversation's stored analyses, the most recently used first."""
    async with tenant_session(db, principal.tenant_id) as session:
        rows = await session.scalars(
            select(CopilotAnalysis)
            .where(
                CopilotAnalysis.user_id == principal.user_id,
                CopilotAnalysis.conversation_id == conversation_id,
            )
            .order_by(CopilotAnalysis.last_used_at.desc(), CopilotAnalysis.created_at.desc())
            .limit(limit)
        )
        return [found for row in rows if (found := _stored(row)) is not None]


async def latest(db: Db, principal: Principal, conversation_id: UUID) -> StoredAnalysis | None:
    found = await recent(db, principal, conversation_id, limit=1)
    return found[0] if found else None


async def find(
    db: Db, principal: Principal, conversation_id: UUID, key: str
) -> StoredAnalysis | None:
    async with tenant_session(db, principal.tenant_id) as session:
        row = await session.scalar(
            select(CopilotAnalysis).where(
                CopilotAnalysis.user_id == principal.user_id,
                CopilotAnalysis.conversation_id == conversation_id,
                CopilotAnalysis.input_key == key,
            )
        )
        return _stored(row) if row is not None else None


async def touch(db: Db, principal: Principal, analysis_id: UUID) -> None:
    """A stored analysis was used again: it is the conversation's latest now. The buyer
    has the result either way, so a failure is logged (ids only), never raised."""
    try:
        async with tenant_session(db, principal.tenant_id) as session:
            await session.execute(
                update(CopilotAnalysis)
                .where(
                    CopilotAnalysis.id == analysis_id,
                    CopilotAnalysis.user_id == principal.user_id,
                )
                .values(last_used_at=func.clock_timestamp())
            )
    except SQLAlchemyError:
        logger.warning(
            "copilot analysis not marked used",
            extra={"tenant_id": str(principal.tenant_id), "analysis_id": str(analysis_id)},
        )


async def store(
    db: Db,
    principal: Principal,
    *,
    conversation_id: UUID,
    run_id: UUID,
    key: str,
    upload_ids: Sequence[UUID],
    request: ScopeRequest | None,
    assessment: SourceAssessment,
) -> None:
    """Keep a computed analysis as the conversation's latest; one with the same inputs
    (a recomputation) is replaced. The buyer has the result either way, so a failure is
    logged (ids only), never raised: follow-ups then find the previous analysis or none."""
    statement = insert(CopilotAnalysis)
    try:
        await _upsert(
            db, principal, statement, conversation_id, run_id, key, upload_ids, request, assessment
        )
    except SQLAlchemyError:
        logger.warning(
            "copilot analysis not stored",
            extra={
                "tenant_id": str(principal.tenant_id),
                "conversation_id": str(conversation_id),
                "run_id": str(run_id),
            },
        )


async def _upsert(
    db: Db,
    principal: Principal,
    statement: Insert,
    conversation_id: UUID,
    run_id: UUID,
    key: str,
    upload_ids: Sequence[UUID],
    request: ScopeRequest | None,
    assessment: SourceAssessment,
) -> None:
    async with tenant_session(db, principal.tenant_id) as session:
        await session.execute(
            statement.values(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                conversation_id=conversation_id,
                run_id=run_id,
                input_key=key,
                upload_ids=sorted(set(upload_ids)),
                request=request.model_dump(mode="json", exclude_unset=True)
                if request is not None
                else None,
                assessment=assessment.model_dump(mode="json"),
                created_at=func.clock_timestamp(),
                last_used_at=func.clock_timestamp(),
            ).on_conflict_do_update(
                constraint="copilot_analyses_input",
                set_={
                    "run_id": statement.excluded.run_id,
                    "upload_ids": statement.excluded.upload_ids,
                    "request": statement.excluded.request,
                    "assessment": statement.excluded.assessment,
                    "created_at": func.clock_timestamp(),
                    "last_used_at": func.clock_timestamp(),
                },
            )
        )


# --- Explaining an item --------------------------------------------------------------------


class AnalysisRef(BaseModel):
    """Which analysis of the conversation the answer reads."""

    analysis_id: UUID
    analyzed_at: datetime
    upload_ids: list[UUID]
    # The buyer's request as the call gave it, and the scope code resolved it to.
    request: ScopeRequest | None = None
    scope: PlanningScope | None = None
    status: Literal["assessed", "needs_review"]
    lines: int


class AnalysisItem(BaseModel):
    """One line of the analysis, as code computed it."""

    # 1-based, in the analysis's line order.
    position: int
    # The whole record: demand by date (`demand_schedule`, `demand_contributions`), stock
    # (`available`, `safety_stock`), open orders and their arrival (`inbound`,
    # `on_order`), the netting by date, the quantity to buy, the timing check (status,
    # shortage and stockout dates, the recommended source and the one it switched
    # from), the suppliers with their lead times and arrival, expedites, assumptions.
    line: AssessmentLine
    # The supplier the analysis chose for this line (`recommended`), when it chose one.
    chosen_supplier: SupplierEvidence | None = None
    # alert: the line is a timing alert (`timing_alert`, with its reason); not_alert: the
    # analysis evaluated it and did not alert (`excluded_from_alerts`, with its reason);
    # unlisted: neither list names it (the exclusions are capped: alerts_excluded_left_out).
    alert_status: Literal["alert", "not_alert", "unlisted"]
    timing_alert: TimingAlert | None = None
    excluded_from_alerts: AlertExclusion | None = None


class AnalysisItemOut(BaseModel):
    found: bool
    note: str
    # None when the conversation has no analysis yet.
    analysis: AnalysisRef | None = None
    items: list[AnalysisItem] = Field(default_factory=list[AnalysisItem])
    items_left_out: int = 0
    # When the item is not in the analysis: the part numbers it has (capped).
    part_numbers: list[str] = Field(default_factory=list[str])
    part_numbers_left_out: int = 0


NO_ANALYSIS: Final = (
    "This conversation has no source analysis yet: nothing to explain. Run "
    "analyze_procurement_sources on the buyer's files first."
)


def _chosen(line: AssessmentLine) -> SupplierEvidence | None:
    for supplier in line.suppliers:
        if supplier.recommended:
            return supplier
    wanted = line.timing.recommended_supplier if line.timing else None
    if wanted is None:
        return None
    for supplier in line.suppliers:
        if wanted in (supplier.name, supplier.supplier_code):
            return supplier
    return None


def _item(assessment: SourceAssessment, index: int) -> AnalysisItem:
    line = assessment.lines[index]
    alert = next(
        (a for a in assessment.timing_alerts if _code(a.part_number) == _code(line.part_number)),
        None,
    )
    excluded = next(
        (e for e in assessment.alerts_excluded if _code(e.part_number) == _code(line.part_number)),
        None,
    )
    status: Literal["alert", "not_alert", "unlisted"] = (
        "alert" if alert is not None else "not_alert" if excluded is not None else "unlisted"
    )
    return AnalysisItem(
        position=index + 1,
        line=line,
        chosen_supplier=_chosen(line),
        alert_status=status,
        timing_alert=alert,
        excluded_from_alerts=excluded,
    )


def explain_item(
    stored: StoredAnalysis | None, *, material: str | None, position: int | None
) -> AnalysisItemOut:
    """An item of the stored analysis, by its code or name, or by its line position."""
    if stored is None:
        return AnalysisItemOut(found=False, note=NO_ANALYSIS)
    assessment = stored.assessment
    ref = AnalysisRef(
        analysis_id=stored.id,
        analyzed_at=stored.created_at,
        upload_ids=list(stored.upload_ids),
        request=stored.request,
        scope=assessment.scope,
        status=assessment.status,
        lines=len(assessment.lines),
    )
    if position is not None:
        indexes = [position - 1] if 1 <= position <= len(assessment.lines) else []
    else:
        indexes = matching_lines(assessment, material or "")
    if not indexes:
        parts = list(dict.fromkeys(line.part_number for line in assessment.lines))
        asked = f"line {position}" if position is not None else "that item"
        return AnalysisItemOut(
            found=False,
            note=f"{asked.capitalize()} is not in this conversation's latest analysis "
            f"({len(assessment.lines)} lines; their part numbers follow). Say so; do not re-run "
            "the analysis for it unless the buyer changes the files, horizon or plants.",
            analysis=ref,
            part_numbers=parts[:MAX_PART_NUMBERS],
            part_numbers_left_out=max(0, len(parts) - MAX_PART_NUMBERS),
        )
    return AnalysisItemOut(
        found=True,
        note="Computed by code in this conversation's latest analysis; nothing was "
        "recomputed. Answer from this record: its dates, quantities, orders, timing, "
        "chosen supplier and alert reason as code wrote them.",
        analysis=ref,
        items=[_item(assessment, index) for index in indexes[:MAX_ITEMS]],
        items_left_out=max(0, len(indexes) - MAX_ITEMS),
    )
