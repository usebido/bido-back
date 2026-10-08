"""Onboarding services: state, the sent-mail scan, uploads, templates, done.

Every function takes the caller's `Principal` and works in `tenant_session`
for that tenant (forced RLS). Scans and uploads belong to the user who made
them; templates belong to the tenant (one default per tenant).

Long work runs in the worker: starting a scan or a template build commits a
row, then enqueues its job (`template.scan` / `template.build`, idempotency
key `<type>:<row id>`). If the enqueue fails, the row is marked `failed`
(`enqueue_failed`) so the user can start again, and the error propagates.
"""

import logging
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from typing import Final, cast
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import (
    MailboxConnection,
    Membership,
    RfqTemplate,
    TemplateScan,
    TemplateUpload,
)
from bido_back.onboarding.candidates import display_attachments, display_date, stored
from bido_back.onboarding.jobs import SCAN_LIMIT
from bido_back.onboarding.schemas import (
    CandidateView,
    CreateTemplateRequest,
    DirectUploadView,
    MailboxSummary,
    OnboardingState,
    Provider,
    ScanStatus,
    ScanView,
    TemplateSource,
    TemplateStatus,
    TemplateView,
    UploadView,
)
from bido_back.onboarding.standard import STANDARD_TEMPLATE
from bido_back.storage import BlobStorage
from bido_back.storage.direct import DirectUploads
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import TEMPLATE_BUILD, TEMPLATE_SCAN
from bido_back.worker.jobs import JobQueue

logger = logging.getLogger(__name__)

type Db = AsyncEngine | async_sessionmaker[AsyncSession]

UPLOAD_EXTENSIONS: Final = frozenset({"xlsx", "xls", "csv", "pdf", "docx"})
MAX_UPLOAD_BYTES: Final = 10 * 1024 * 1024
# Direct uploads skip the api and the WAF, so they are not bound by a request
# body limit. The readers downstream still load the whole file into memory.
MAX_DIRECT_UPLOAD_BYTES: Final = 100 * 1024 * 1024
DIRECT_UPLOAD_TTL: Final = timedelta(minutes=15)
# A scan started this recently and still running is reused instead of starting another.
_SCAN_REUSE: Final = timedelta(minutes=10)
_FILENAME_JUNK = re.compile(r"[\x00-\x1f\x7f/\\]")


class OnboardingError(Exception):
    """Base class. Messages carry ids and reasons, never content."""


class NotFound(OnboardingError):
    pass


class InvalidRequest(OnboardingError):
    pass


class Conflict(OnboardingError):
    pass


class NoMailbox(Conflict):
    """The user has no active mailbox connection to scan."""


class UnsupportedUpload(InvalidRequest):
    pass


class UploadTooLarge(InvalidRequest):
    pass


# --- Views ----------------------------------------------------------------------------------


def _template_view(row: RfqTemplate) -> TemplateView:
    refs = cast(dict[str, object], row.source_refs)
    used: object = refs.get("candidate_ids") or refs.get("upload_ids")
    return TemplateView(
        id=row.id,
        source=cast(TemplateSource, row.source),
        status=cast(TemplateStatus, row.status),
        language=row.language,
        greeting=row.greeting,
        columns=[str(c) for c in row.columns],
        footer=row.footer,
        fields=[str(f) for f in row.fields],
        source_count=len(cast(list[object], used)) if isinstance(used, list) else 0,
        is_default=row.is_default,
        error=row.error,
        created_at=row.created_at,
    )


def _scan_view(row: TemplateScan) -> ScanView:
    candidates: list[CandidateView] = []
    for c in stored(row.candidates):
        recipients = c["recipients"]
        candidates.append(
            CandidateView(
                id=c["id"],
                subject=c["subject"],
                to=recipients[0] if recipients else "",
                more=max(len(recipients) - 1, 0),
                date=display_date(c["sent_at"]),
                sent_at=datetime.fromisoformat(c["sent_at"]) if c["sent_at"] else None,
                attachments=display_attachments(c["attachments"]),
                attachment_count=len(c["attachments"]),
                note=c["note"],
                preselected=c["preselected"],
            )
        )
    return ScanView(
        id=row.id,
        status=cast(ScanStatus, row.status),
        checked=row.checked,
        limit=SCAN_LIMIT,
        found=row.found,
        candidates=candidates,
        error=row.error,
        created_at=row.created_at,
    )


async def _active_connection(
    session: AsyncSession, principal: Principal, provider: Provider | None = None
) -> MailboxConnection | None:
    query = select(MailboxConnection).where(
        MailboxConnection.user_id == principal.user_id, MailboxConnection.status == "active"
    )
    if provider is not None:
        query = query.where(MailboxConnection.provider == provider)
    return await session.scalar(query.order_by(MailboxConnection.updated_at.desc()).limit(1))


# --- State ----------------------------------------------------------------------------------


async def get_state(db: Db, principal: Principal) -> OnboardingState:
    async with tenant_session(db, principal.tenant_id) as session:
        onboarded_at = await session.scalar(
            select(Membership.onboarded_at).where(Membership.user_id == principal.user_id)
        )
        connection = await _active_connection(session, principal)
        # The general default; category variants have their own defaults.
        template = await session.scalar(
            select(RfqTemplate).where(RfqTemplate.is_default, RfqTemplate.category.is_(None))
        )
        return OnboardingState(
            onboarded_at=onboarded_at,
            mailbox=(
                MailboxSummary(
                    provider=cast(Provider, connection.provider),
                    email_address=connection.email_address,
                )
                if connection is not None
                else None
            ),
            default_template=_template_view(template) if template is not None else None,
        )


async def complete(db: Db, principal: Principal) -> OnboardingState:
    """Mark onboarding done for the caller in this tenant (the first time only)."""
    async with tenant_session(db, principal.tenant_id) as session:
        await session.execute(
            update(Membership)
            .where(Membership.user_id == principal.user_id, Membership.onboarded_at.is_(None))
            .values(onboarded_at=func.now())
        )
    return await get_state(db, principal)


# --- The sent-mail scan ----------------------------------------------------------------------


async def start_scan(
    db: Db, principal: Principal, jobs: JobQueue, provider: Provider | None = None
) -> ScanView:
    async with tenant_session(db, principal.tenant_id) as session:
        connection = await _active_connection(session, principal, provider)
        if connection is None:
            raise NoMailbox("connect your mailbox before scanning it")
        recent = await session.scalar(
            select(TemplateScan)
            .where(
                TemplateScan.user_id == principal.user_id,
                TemplateScan.mailbox_connection_id == connection.id,
                TemplateScan.status == "running",
                TemplateScan.created_at > datetime.now(UTC) - _SCAN_REUSE,
            )
            .order_by(TemplateScan.created_at.desc())
            .limit(1)
        )
        if recent is not None:
            return _scan_view(recent)
        scan = TemplateScan(
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            mailbox_connection_id=connection.id,
        )
        session.add(scan)
        await session.flush()
        await session.refresh(scan)
        view = _scan_view(scan)
    await _enqueue(db, principal.tenant_id, jobs, TEMPLATE_SCAN, "scan_id", scan.id, TemplateScan)
    logger.info("template scan started tenant=%s scan=%s", principal.tenant_id, scan.id)
    return view


async def get_scan(db: Db, principal: Principal, scan_id: UUID) -> ScanView:
    async with tenant_session(db, principal.tenant_id) as session:
        scan = await session.scalar(
            select(TemplateScan).where(
                TemplateScan.id == scan_id, TemplateScan.user_id == principal.user_id
            )
        )
        if scan is None:
            raise NotFound(f"scan {scan_id} not found")
        return _scan_view(scan)


# --- Uploads --------------------------------------------------------------------------------


def upload_extension(filename: str) -> str:
    return PurePath(filename).suffix.lstrip(".").lower()


def clean_filename(filename: str) -> str:
    name = _FILENAME_JUNK.sub("_", filename).strip()[-200:]
    return name or "file"


async def store_upload(
    db: Db,
    storage: BlobStorage,
    principal: Principal,
    *,
    filename: str,
    content_type: str,
    data: bytes,
) -> UploadView:
    name = clean_filename(filename)
    ext = _accepted_extension(name)
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadTooLarge("files are limited to 10 MB")
    if not data:
        raise InvalidRequest("the file is empty")
    upload_id = uuid4()
    key = _upload_key(principal, upload_id, ext)
    await storage.put(key, data, content_type)
    return await _record_upload(
        db,
        principal,
        upload_id=upload_id,
        filename=name,
        content_type=content_type,
        size_bytes=len(data),
        key=key,
    )


async def start_direct_upload(
    direct: DirectUploads,
    principal: Principal,
    *,
    filename: str,
    content_type: str,
    size_bytes: int,
) -> DirectUploadView:
    """Sign a form for the browser to upload one file straight to storage.

    Nothing is recorded yet: the upload exists once `complete_direct_upload`
    finds the stored object.
    """
    ext = _accepted_extension(clean_filename(filename))
    if size_bytes > MAX_DIRECT_UPLOAD_BYTES:
        raise UploadTooLarge("files are limited to 100 MB")
    upload_id = uuid4()
    form = await direct.presign_post(
        _upload_key(principal, upload_id, ext),
        content_type=content_type,
        max_bytes=size_bytes,
        expires_in=DIRECT_UPLOAD_TTL,
    )
    return DirectUploadView(
        upload_id=upload_id,
        url=form.url,
        fields=form.fields,
        expires_at=form.expires_at,
        max_bytes=size_bytes,
    )


async def complete_direct_upload(
    db: Db,
    direct: DirectUploads,
    principal: Principal,
    upload_id: UUID,
    *,
    filename: str,
) -> UploadView:
    """Record a direct upload once its object is in storage. Repeating it is harmless."""
    name = clean_filename(filename)
    ext = _accepted_extension(name)
    async with tenant_session(db, principal.tenant_id) as session:
        existing = await session.get(TemplateUpload, upload_id)
        if existing is not None:
            if existing.user_id != principal.user_id:
                raise NotFound(f"upload {upload_id} not found")
            return _upload_view(existing)
    key = _upload_key(principal, upload_id, ext)
    stored_object = await direct.stat(key)
    if stored_object is None:
        raise NotFound(f"upload {upload_id} not found")
    if stored_object.size_bytes > MAX_DIRECT_UPLOAD_BYTES:
        raise UploadTooLarge("files are limited to 100 MB")
    if stored_object.size_bytes == 0:
        raise InvalidRequest("the file is empty")
    return await _record_upload(
        db,
        principal,
        upload_id=upload_id,
        filename=name,
        content_type=stored_object.content_type or "application/octet-stream",
        size_bytes=stored_object.size_bytes,
        key=key,
    )


def _accepted_extension(name: str) -> str:
    ext = upload_extension(name)
    if ext not in UPLOAD_EXTENSIONS:
        raise UnsupportedUpload("accepted files: xlsx, xls, csv, pdf, docx")
    return ext


def _upload_key(principal: Principal, upload_id: UUID, ext: str) -> str:
    return f"onboarding/{principal.tenant_id}/{upload_id}.{ext}"


async def _record_upload(
    db: Db,
    principal: Principal,
    *,
    upload_id: UUID,
    filename: str,
    content_type: str,
    size_bytes: int,
    key: str,
) -> UploadView:
    async with tenant_session(db, principal.tenant_id) as session:
        row = TemplateUpload(
            id=upload_id,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            storage_key=key,
        )
        session.add(row)
        await session.flush()
    logger.info("template upload stored tenant=%s upload=%s", principal.tenant_id, upload_id)
    return _upload_view(row)


def _upload_view(row: TemplateUpload) -> UploadView:
    return UploadView(
        id=row.id,
        filename=row.filename,
        size_bytes=row.size_bytes,
        ext=upload_extension(row.filename),
    )


# --- Templates ------------------------------------------------------------------------------


async def create_template(
    db: Db, principal: Principal, jobs: JobQueue, request: CreateTemplateRequest
) -> TemplateView:
    refs: dict[str, JsonValue]
    async with tenant_session(db, principal.tenant_id) as session:
        if request.source == "none":
            row = _standard_row(principal)
            session.add(row)
            await session.flush()
            await session.refresh(row)
            return _template_view(row)
        if request.source == "search":
            refs = await _search_refs(session, principal, request)
        else:
            refs = await _upload_refs(session, principal, request.upload_ids)
        row = RfqTemplate(
            tenant_id=principal.tenant_id,
            created_by=principal.user_id,
            source=request.source,
            status="building",
            source_refs=refs,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        view = _template_view(row)
    await _enqueue(
        db, principal.tenant_id, jobs, TEMPLATE_BUILD, "template_id", row.id, RfqTemplate
    )
    logger.info("template build started tenant=%s template=%s", principal.tenant_id, row.id)
    return view


def _standard_row(principal: Principal) -> RfqTemplate:
    return RfqTemplate(
        tenant_id=principal.tenant_id,
        created_by=principal.user_id,
        source="none",
        status="ready",
        language=STANDARD_TEMPLATE.language,
        greeting=STANDARD_TEMPLATE.greeting,
        columns=list(STANDARD_TEMPLATE.columns),
        footer=STANDARD_TEMPLATE.footer,
        fields=list(STANDARD_TEMPLATE.fields),
        source_refs={},
    )


async def _search_refs(
    session: AsyncSession, principal: Principal, request: CreateTemplateRequest
) -> dict[str, JsonValue]:
    if request.scan_id is None or not request.candidate_ids:
        raise InvalidRequest("source=search needs scan_id and at least one candidate id")
    scan = await session.scalar(
        select(TemplateScan).where(
            TemplateScan.id == request.scan_id, TemplateScan.user_id == principal.user_id
        )
    )
    if scan is None:
        raise NotFound(f"scan {request.scan_id} not found")
    if scan.status != "done":
        raise Conflict(f"scan {scan.id} is {scan.status}")
    known = {c["id"] for c in stored(scan.candidates)}
    picked = list(dict.fromkeys(request.candidate_ids))
    unknown = [c for c in picked if c not in known]
    if unknown:
        raise InvalidRequest(f"unknown candidate ids: {', '.join(unknown[:5])}")
    candidate_ids: list[JsonValue] = list(picked)
    return {"scan_id": str(scan.id), "candidate_ids": candidate_ids}


async def _upload_refs(
    session: AsyncSession, principal: Principal, upload_ids: Sequence[UUID]
) -> dict[str, JsonValue]:
    wanted = list(dict.fromkeys(upload_ids))
    if not wanted:
        raise InvalidRequest("source=upload needs at least one upload id")
    found = set(
        await session.scalars(
            select(TemplateUpload.id).where(
                TemplateUpload.id.in_(wanted), TemplateUpload.user_id == principal.user_id
            )
        )
    )
    missing = [str(u) for u in wanted if u not in found]
    if missing:
        raise NotFound(f"uploads not found: {', '.join(missing[:5])}")
    ids: list[JsonValue] = [str(u) for u in wanted]
    return {"upload_ids": ids}


async def get_template(db: Db, principal: Principal, template_id: UUID) -> TemplateView:
    async with tenant_session(db, principal.tenant_id) as session:
        row = await session.scalar(select(RfqTemplate).where(RfqTemplate.id == template_id))
        if row is None:
            raise NotFound(f"template {template_id} not found")
        return _template_view(row)


async def use_template(db: Db, principal: Principal, template_id: UUID) -> TemplateView:
    """Make a ready template the default for its category (the previous default stops being one)."""
    async with tenant_session(db, principal.tenant_id) as session:
        row = await session.scalar(
            select(RfqTemplate).where(RfqTemplate.id == template_id).with_for_update()
        )
        if row is None:
            raise NotFound(f"template {template_id} not found")
        if row.status != "ready":
            raise Conflict(f"template {template_id} is {row.status}")
        if not row.is_default:
            await session.execute(
                update(RfqTemplate)
                .where(
                    RfqTemplate.is_default,
                    RfqTemplate.id != template_id,
                    RfqTemplate.category.is_not_distinct_from(row.category),
                )
                .values(is_default=False, updated_at=func.now())
            )
            # The partial unique index must see the old default gone first.
            await session.flush()
            row.is_default = True
            row.updated_at = datetime.now(UTC)
            await session.flush()
        logger.info("template made default tenant=%s template=%s", principal.tenant_id, template_id)
        return _template_view(row)


# --- Jobs -----------------------------------------------------------------------------------


async def _enqueue(
    db: Db,
    tenant_id: UUID,
    jobs: JobQueue,
    job_type: str,
    key: str,
    row_id: UUID,
    model: type[TemplateScan] | type[RfqTemplate],
) -> None:
    try:
        await jobs.enqueue(
            Envelope(
                type=job_type,
                tenant_id=tenant_id,
                idempotency_key=f"{job_type}:{row_id}",
                payload={key: str(row_id)},
            )
        )
    except Exception:
        logger.exception("%s %s not enqueued", job_type, row_id)
        async with tenant_session(db, tenant_id) as session:
            await session.execute(
                update(model)
                .where(model.id == row_id)
                .values(status="failed", error="enqueue_failed", updated_at=func.now())
            )
        raise
