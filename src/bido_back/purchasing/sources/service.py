"""Source assessment: authorized extraction, reconciliation and optional evidence snapshots."""

import asyncio
import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.context import ToolContext
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import TemplateUpload
from bido_back.purchasing.sources.analysis import assess
from bido_back.purchasing.sources.contracts import (
    Assessment,
    Coverage,
    EvidenceTable,
    Issue,
    PlanningScope,
    ScopeRequest,
    SourceAssessment,
)
from bido_back.purchasing.sources.mapping import Mapper, map_sources
from bido_back.purchasing.sources.planning import assess_plan, planning_context
from bido_back.purchasing.sources.pricing import spend_totals
from bido_back.purchasing.sources.readers import SourceDocument, read_source
from bido_back.purchasing.sources.scope import (
    ResolvedScope,
    as_request,
    direct_scope,
    has_production_plans,
    resolve_scope,
    scope_issues,
)
from bido_back.purchasing.sources.snapshots import find_snapshot, save_snapshot
from bido_back.storage import BlobStorage

Db = AsyncEngine | async_sessionmaker[AsyncSession]


class SourceAccessError(Exception):
    pass


async def analyze_documents(
    documents: Sequence[SourceDocument],
    objective: str,
    context: ToolContext,
    mapper: Mapper,
    *,
    scope: ScopeRequest | PlanningScope | None = None,
) -> SourceAssessment:
    """Read, map, settle the buyer's planning boundaries, then assess, in one pass.

    `scope` is the buyer's request; code resolves it against the mapped evidence (no
    second model call). With production plans and unsettled boundaries, the result holds
    the candidates and the questions to ask, never per-material lines of every area.
    """
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
    records, problems, coverage = await map_sources(tables, objective, context, mapper)
    candidates, snapshot_dates = planning_context(records)
    request = as_request(scope)
    planned = has_production_plans(records)
    resolved: ResolvedScope | None = None
    if planned:
        resolved = resolve_scope(records, request or ScopeRequest())
    elif request is not None:
        resolved = direct_scope(records, request)
    planning = resolved.scope if resolved is not None else None
    if planning is not None:
        result = assess_plan(records, planning)
    elif planned:
        # Unsettled boundaries: no lines for every material of every area and snapshot.
        result = Assessment(record_counts=dict(Counter(record.kind for record in records)))
    else:
        result = assess(records)
    if resolved is not None:
        result.issues[:0] = scope_issues(resolved, planned=planned)
    result.issues.extend([*issues, *problems])
    if not result.lines and (planning is not None or not planned):
        result.issues.append(
            Issue(
                code="no_material_demand",
                severity="blocker",
                message="No direct material demand or reported purchase needs could be assessed. "
                "Check uncovered content; a BOM is needed only for a product-build calculation.",
            )
        )
    for row in coverage:
        row.filename = names[row.source_id]
    all_coverage = [*coverage, *unread]
    blocked = any(issue.severity == "blocker" for issue in result.issues)
    if blocked:
        for line in result.lines:
            line.ready = False
    return SourceAssessment(
        **result.model_dump(),
        scope=planning,
        scope_resolution=resolved.resolution if resolved is not None else None,
        planning_candidates=candidates,
        snapshot_dates=snapshot_dates,
        coverage=all_coverage,
        status=(
            "assessed"
            if result.lines
            and not blocked
            and all(line.ready for line in result.lines)
            and all(row.inspected for row in all_coverage)
            else "needs_review"
        ),
        totals=spend_totals(result.lines),
    )


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
                "version": 3,
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
