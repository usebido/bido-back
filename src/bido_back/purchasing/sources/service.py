"""Source assessment: authorized extraction, reconciliation and optional evidence snapshots."""

import asyncio
import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.context import ToolContext
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import TemplateUpload
from bido_back.purchasing.sources.analysis import assess, confidence
from bido_back.purchasing.sources.contracts import (
    Assessment,
    Assumption,
    Coverage,
    EvidenceTable,
    Issue,
    PlanningScope,
    Record,
    ScopeRequest,
    ScopeResolution,
    SourceAssessment,
)
from bido_back.purchasing.sources.evidence import same_key
from bido_back.purchasing.sources.mapping import Mapper, map_sources
from bido_back.purchasing.sources.packs import PackResult, read_known
from bido_back.purchasing.sources.packs.base import PackContext
from bido_back.purchasing.sources.planning import assess_plan, planning_context
from bido_back.purchasing.sources.pricing import spend_totals
from bido_back.purchasing.sources.readers import SourceDocument, read_source
from bido_back.purchasing.sources.scope import (
    ResolvedScope,
    as_request,
    candidate_scopes,
    direct_scope,
    has_production_plans,
    mrp_scopes,
    resolve_scope,
    scope_issues,
)
from bido_back.purchasing.sources.selectors import (
    ScopeValidationError,
    apply_mrp_element,
    select_scope,
)
from bido_back.purchasing.sources.snapshots import find_snapshot, save_snapshot
from bido_back.purchasing.sources.timing import alert_exclusions, timing_alerts
from bido_back.purchasing.sources.units import convert_units
from bido_back.storage import BlobStorage

Db = AsyncEngine | async_sessionmaker[AsyncSession]


class SourceAccessError(Exception):
    pass


__all__ = [
    "ScopeValidationError",
    "SourceAccessError",
    "analyze_documents",
    "analyze_uploads",
]


async def analyze_documents(
    documents: Sequence[SourceDocument],
    objective: str,
    context: ToolContext,
    mapper: Mapper,
    *,
    scope: ScopeRequest | PlanningScope | None = None,
    today: date | None = None,
) -> SourceAssessment:
    """Read, map, settle the buyer's planning boundaries, then assess, in one pass.

    Tables of a known ERP report are read by its report pack (no model call); the rest
    go to the source mapper, in parallel. `scope` is the buyer's request; code resolves
    it against the mapped evidence (no second model call). Its selectors (plants, MRP
    element, product) are checked against the values the sources hold first: a value
    they lack raises `ScopeValidationError` naming the valid ones, before anything is
    planned. With production plans and unsettled boundaries, the result holds the
    candidates and the questions to ask, never per-material lines of every area.
    Without them, an MRP list is planned for every required material of each plant.

    Problems never hold the quantities: every line with demand gets a best estimate,
    its assumptions and a confidence (see `planning.assess_plan`); the buyer and the
    approvals downstream decide.
    """
    today = today or date.today()
    tables: list[EvidenceTable] = []
    issues: list[Issue] = []
    unread: list[Coverage] = []
    names = {document.source_id: document.filename for document in documents}
    for document in documents:
        extracted, problems = await asyncio.to_thread(read_source, document)
        tables.extend(extracted)
        issues.extend(problems)
        if not extracted:
            unread.append(
                Coverage(
                    source_id=document.source_id,
                    filename=document.filename,
                    table=document.filename,
                    rows=0,
                )
            )
    known = await asyncio.to_thread(read_known, tables, PackContext(filenames=names, today=today))
    mapped, problems, coverage = await map_sources(
        known.remaining, objective, context, mapper, known=known.records
    )
    # The rest is computation: off the event loop, so a large source does not hold up the
    # other requests this process serves.
    return await asyncio.to_thread(
        _settle, known, mapped, problems, coverage, issues, unread, names, scope, today
    )


def _settle(
    known: PackResult,
    mapped: Sequence[Record],
    problems: Sequence[Issue],
    coverage: Sequence[Coverage],
    issues: Sequence[Issue],
    unread: Sequence[Coverage],
    names: Mapping[str, str],
    scope: ScopeRequest | PlanningScope | None,
    today: date,
) -> SourceAssessment:
    """Units, the buyer's boundaries and the assessment of the read records."""
    records = [*known.records, *mapped]
    unit_issues, conversions = convert_units(records)
    problems = [*known.issues, *problems, *unit_issues]
    coverage = [*known.coverage, *coverage]
    for row in coverage:
        row.filename = names[row.source_id]
    all_coverage = [*coverage, *unread]
    # The buyer's selectors against what the sources hold, before anything is planned.
    selection = select_scope(records, as_request(scope))
    request = selection.request if scope is not None else None
    records, element_note = apply_mrp_element(records, selection.request.mrp_element)
    candidates, snapshot_dates = planning_context(records)
    if selection.question is not None:
        # A plant count the sources do not have: ask which plants, plan nothing yet.
        return SourceAssessment(
            issues=[
                Issue(
                    code="plant_selection_open",
                    severity="blocker",
                    message=selection.question.message
                    + " Ask the buyer scope_resolution.questions and repeat the analysis "
                    "with their answer (scope.plants); nothing is planned until then.",
                ),
                *issues,
                *problems,
            ],
            record_counts=dict(Counter(record.kind for record in records)),
            scope_resolution=ScopeResolution(
                request=selection.request,
                questions=[selection.question],
                filters=list(selection.filters),
            ),
            planning_candidates=candidates,
            snapshot_dates=snapshot_dates,
            coverage=all_coverage,
            reports=known.reports,
            found=selection.found,
        )
    planned = has_production_plans(records)
    resolved: ResolvedScope | None = None
    lists: list[ResolvedScope] = []
    if planned:
        resolved = resolve_scope(records, request or ScopeRequest())
    elif request is not None:
        resolved = direct_scope(records, request)
    left: list[str] = []
    if planned and resolved is not None and resolved.scope is None:
        # Open boundaries never hold the plan: each candidate is planned on its own.
        lists, left = candidate_scopes(
            records, request or ScopeRequest(), today, plants=selection.plants
        )
        if lists:
            resolved = lists[0]
    elif not planned and (resolved is None or resolved.scope is None):
        lists = mrp_scopes(records, request, today, plants=selection.plants)
        if lists:
            resolved = lists[0]
    planning = resolved.scope if resolved is not None else None
    if lists:
        result = Assessment(record_counts=dict(Counter(record.kind for record in records)))
        for each in lists:
            if each.scope is None:
                continue
            found = assess_plan(records, each.scope)
            if len(lists) > 1:
                # Each scope's own settled values belong to its lines only.
                for line in found.lines:
                    line.assumptions.extend(
                        Assumption(
                            code=a.code,
                            message=a.message,
                            evidence=a.evidence,
                            part_numbers=[line.part_number],
                        )
                        for a in each.resolution.assumptions
                        if a.code != "planned_each_candidate"
                    )
            if planned and len(lists) > 1:
                label = f"{each.scope.product_code} at {each.scope.location}"
                for line in found.lines:
                    line.assumptions.append(
                        Assumption(
                            code="candidate_plan",
                            message=f"Part of the plan for {label}, one of {len(lists)} "
                            "product/plant candidates planned separately.",
                            alternative=f"If {label} is not wanted, leave this line out.",
                            part_numbers=[line.part_number],
                        )
                    )
            result.lines.extend(found.lines)
            result.issues.extend(found.issues)
            result.assumptions.extend(found.assumptions)
            if each is not resolved:
                result.issues[:0] = scope_issues(each, planned=False)
    elif planning is not None:
        result = assess_plan(records, planning)
    elif planned:
        # Unsettled boundaries: no lines for every material of every area and snapshot.
        result = Assessment(record_counts=dict(Counter(record.kind for record in records)))
    else:
        result = assess(records)
    if resolved is not None:
        result.issues[:0] = scope_issues(resolved, planned=planned)
    result.issues.extend([*issues, *problems])
    settled = [
        Assumption(code=a.code, message=a.message, evidence=a.evidence)
        for each in (
            (lists if len(lists) == 1 else []) or ([resolved] if resolved and not lists else [])
        )
        for a in each.resolution.assumptions
        if a.code != "planned_each_candidate"
    ]
    if planned and len(lists) > 1:
        labels = [f"{e.scope.product_code} at {e.scope.location}" for e in lists if e.scope]
        settled.append(
            Assumption(
                code="planned_each_candidate",
                message=f"The request does not settle one product and plant: {len(labels)} "
                f"candidates were planned separately ({'; '.join(labels)}).",
                alternative="If you meant only one, ignore the other candidates' lines.",
            )
        )
    result.assumptions[:0] = [
        *settled,
        *known.assumptions,
        *conversions,
        *([element_note] if element_note is not None else []),
    ]
    if left:
        result.assumptions.append(
            Assumption(
                code="candidates_not_planned",
                message="Also with production orders, not planned in this answer: "
                + "; ".join(left[:10])
                + ".",
                alternative="Ask to plan any of them.",
            )
        )
    if not result.lines and (planning is not None or not planned):
        result.issues.append(
            Issue(
                code="no_material_demand",
                severity="blocker",
                message="No direct material demand or reported purchase needs could be assessed. "
                "Check uncovered content; a BOM is needed only for a product-build calculation.",
            )
        )
    _disclose(result, all_coverage)
    excluded, excluded_left_out = alert_exclusions(result.lines)
    resolution = resolved.resolution if resolved is not None else None
    if resolution is not None and selection.filters:
        resolution = resolution.model_copy(update={"filters": list(selection.filters)})
    # The assessment's own objects, not a dump and a re-validation: on a large source the
    # copy would nearly double the memory this call needs.
    return SourceAssessment(
        **dict(result),
        scope=planning,
        scope_resolution=resolution,
        planning_candidates=candidates,
        snapshot_dates=snapshot_dates,
        coverage=all_coverage,
        reports=known.reports,
        status="assessed"
        if result.lines and all(line.calculated_to_buy is not None for line in result.lines)
        else "needs_review",
        totals=spend_totals(result.lines),
        timing_alerts=timing_alerts(result.lines),
        alerts_excluded=excluded,
        alerts_excluded_left_out=excluded_left_out,
        found=selection.found,
    )


def _disclose(result: Assessment, coverage: Sequence[Coverage]) -> None:
    """Each line lists the assessment-wide assumptions that concern it, and problems
    elsewhere in the evidence lower every line's confidence instead of holding it."""
    general = [a for a in result.assumptions if not a.part_numbers]
    unread = [row for row in coverage if not row.inspected]
    if unread:
        general.append(
            Assumption(
                code="sources_not_fully_read",
                message=f"{len(unread)} table(s) could not be fully read: "
                + ", ".join(f"{row.filename} ({row.table})" for row in unread[:5])
                + ". Their content is not in the quantities.",
                confidence="low",
            )
        )
        result.assumptions.append(general[-1])
    blocked = any(issue.severity == "blocker" for issue in result.issues)
    for line in result.lines:
        known = {(a.code, a.message) for a in line.assumptions}
        for assumption in result.assumptions:
            if (assumption.code, assumption.message) in known:
                continue
            if not assumption.part_numbers or any(
                same_key(part, line.part_number) for part in assumption.part_numbers
            ):
                line.assumptions.append(assumption)
                known.add((assumption.code, assumption.message))
        line.ready = line.calculated_to_buy is not None
        confidence(line, blocked=blocked or bool(unread))


async def analyze_uploads(
    db: Db,
    storage: BlobStorage,
    principal: Principal,
    upload_ids: Sequence[UUID],
    objective: str,
    context: ToolContext,
    mapper: Mapper,
    *,
    persist: bool = False,
    scope: ScopeRequest | PlanningScope | None = None,
) -> SourceAssessment:
    request = as_request(scope)
    documents: list[SourceDocument] = []
    # Check ALL IDs before fetching any content. The tenant helper and user ownership
    # complement the tool's server-derived conversation attachment allowlist.
    async with tenant_session(db, principal.tenant_id) as session:
        uploads = list(
            await session.scalars(
                select(TemplateUpload).where(
                    TemplateUpload.id.in_(set(upload_ids)),
                    TemplateUpload.user_id == principal.user_id,
                )
            )
        )
        if len(uploads) != len(set(upload_ids)):
            raise SourceAccessError("one or more attachments are not available to this buyer")
        metadata = [(u.id, u.filename, u.content_type, u.storage_key) for u in uploads]
    for upload_id, filename, content_type, key in metadata:
        documents.append(
            SourceDocument(
                source_id=str(upload_id),
                filename=filename,
                content_type=content_type,
                data=await storage.get(key),
            )
        )
    # Source bytes and contract version prevent reuse of stale evidence. The key holds the
    # buyer's request, not the resolved scope: saving the same request finds the snapshot.
    source_key = hashlib.sha256(
        json.dumps(
            {
                "version": 5,
                "objective": objective,
                "scope": request.model_dump(mode="json") if request is not None else None,
                "sources": sorted(
                    (doc.source_id, hashlib.sha256(doc.data).hexdigest()) for doc in documents
                ),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if persist:
        existing = await find_snapshot(db, principal, source_key)
        if existing is not None:
            return existing
    assessment = await analyze_documents(documents, objective, context, mapper, scope=request)
    if not persist:
        return assessment
    return await save_snapshot(
        db,
        principal,
        source_key=source_key,
        upload_ids=sorted(set(upload_ids)),
        objective=objective,
        assessment=assessment,
    )
