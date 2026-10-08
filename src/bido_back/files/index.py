"""Reading each company file once, for content search (`file_texts`).

The `files.index` job (`FILES_INDEX`) reads, for one company, the files that
have no `file_texts` row yet: buyers' uploads (references excluded) and
suppliers' reply attachments, oldest first, a batch at a time. Each file is
read with `bido_back.documents.extract.extract_text` (cells, pages and
paragraphs, each with its locator marker; bounded in bytes, cells and
characters) and its row records `indexed` with the text, or `skipped` /
`failed` with a reason. A file is never read twice; a run that leaves files
behind enqueues the next run.

It is enqueued after an upload is recorded, and by a library listing that
shows a file not yet read; each run reads every unread file of the company
(uploads and supplier attachments alike), so the files a company had before
this job existed are read too (the backfill) and a supplier attachment is read
by the next run. Every run is idempotent: the rows it writes are the state it
checks.
Nothing read from a file is logged.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy import ColumnElement, and_, exists, func, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import QueryableAttribute

from bido_back.db import tenant_session
from bido_back.db.models import Attachment, FileText, TemplateUpload
from bido_back.documents.extract import extract_text
from bido_back.storage import BlobNotFound, BlobStorage
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import FILES_INDEX
from bido_back.worker.jobs import JobQueue

logger = logging.getLogger(__name__)

Db = AsyncEngine | async_sessionmaker[AsyncSession]

BATCH: Final = 10
# Files read by one job before it hands over to the next one.
FILES_PER_JOB: Final = 100
JOB_SECONDS: Final = 120.0
READ_TIMEOUT_SECONDS: Final = 60.0
MAX_BYTES: Final = 25 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _Pending:
    kind: str
    id: UUID
    filename: str
    content_type: str
    size_bytes: int
    storage_key: str


def _not_indexed(kind: str, file_id: QueryableAttribute[UUID]) -> ColumnElement[bool]:
    return ~exists().where(and_(FileText.file_kind == kind, FileText.file_id == file_id))


def _pending_query(limit: int):  # noqa: ANN202 - a SQLAlchemy select
    uploads = select(
        literal("upload").label("kind"),
        TemplateUpload.id,
        TemplateUpload.filename,
        TemplateUpload.content_type,
        TemplateUpload.size_bytes,
        TemplateUpload.storage_key,
        TemplateUpload.created_at,
    ).where(TemplateUpload.source_kind.is_(None), _not_indexed("upload", TemplateUpload.id))
    attachments = select(
        literal("supplier_attachment").label("kind"),
        Attachment.id,
        Attachment.filename,
        Attachment.content_type,
        Attachment.size_bytes,
        Attachment.storage_key,
        Attachment.created_at,
    ).where(_not_indexed("supplier_attachment", Attachment.id))
    both = uploads.union_all(attachments).subquery("pending")
    return select(both).order_by(both.c.created_at, both.c.id).limit(limit)


async def pending_count(db: Db, tenant_id: UUID) -> int:
    async with tenant_session(db, tenant_id) as session:
        rows = _pending_query(FILES_PER_JOB + 1).subquery()
        return await session.scalar(select(func.count()).select_from(rows)) or 0


async def _read(storage: BlobStorage, file: _Pending) -> tuple[str, str | None, str]:
    """(status, reason, text) for one file. Never raises for a bad file."""
    if file.size_bytes > MAX_BYTES:
        return "skipped", "too_large", ""
    try:
        data = await storage.get(file.storage_key)
    except BlobNotFound:
        return "failed", "missing", ""
    try:
        document = await asyncio.wait_for(
            asyncio.to_thread(extract_text, file.filename, file.content_type, data),
            timeout=READ_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return "failed", "timeout", ""
    if document.needs_vision:
        return "skipped", "needs_vision", ""
    if document.skipped is not None:
        return "skipped", document.skipped, ""
    # Postgres text cannot hold NUL.
    return "indexed", None, document.text.replace("\x00", " ")


async def index_pending(
    db: Db, storage: BlobStorage, tenant_id: UUID, *, max_files: int = FILES_PER_JOB
) -> tuple[int, bool]:
    """Read up to `max_files` unread files of a company. Returns (read, more_left)."""
    started = time.monotonic()
    done = 0
    while done < max_files and time.monotonic() - started < JOB_SECONDS:
        async with tenant_session(db, tenant_id) as session:
            rows = (await session.execute(_pending_query(min(BATCH, max_files - done)))).all()
        if not rows:
            return done, False
        for row in rows:
            file = _Pending(
                kind=row.kind,
                id=row.id,
                filename=row.filename,
                content_type=row.content_type,
                size_bytes=row.size_bytes,
                storage_key=row.storage_key,
            )
            status, reason, text = await _read(storage, file)
            async with tenant_session(db, tenant_id) as session:
                await session.execute(
                    insert(FileText)
                    .values(
                        tenant_id=tenant_id,
                        file_kind=file.kind,
                        file_id=file.id,
                        status=status,
                        reason=reason,
                        text=text,
                    )
                    .on_conflict_do_nothing(index_elements=["tenant_id", "file_kind", "file_id"])
                )
            logger.info(
                "file indexed",
                extra={
                    "file_id": str(file.id),
                    "file_kind": file.kind,
                    "outcome": status,
                    "reason": reason,
                    "chars": len(text),
                },
            )
            done += 1
    return done, await pending_count(db, tenant_id) > 0


def index_envelope(tenant_id: UUID, key: str) -> Envelope:
    return Envelope(
        type=FILES_INDEX,
        tenant_id=tenant_id,
        idempotency_key=f"files.index:{tenant_id}:{key}"[:200],
        payload={},
    )


async def request_index(jobs: JobQueue | None, tenant_id: UUID, key: str) -> None:
    """Best effort: a lost request is made again by the next upload or library listing."""
    if jobs is None:
        return
    try:
        await jobs.enqueue(index_envelope(tenant_id, key))
    except Exception as exc:  # noqa: BLE001 - indexing must never fail the caller
        logger.warning("files.index enqueue failed: %s", type(exc).__name__)


class FilesIndexHandler:
    """Worker handler for `files.index`: reads a company's unread files, then hands over."""

    def __init__(self, db: Db, storage: BlobStorage, jobs: JobQueue | None) -> None:
        self._db = db
        self._storage = storage
        self._jobs = jobs

    async def __call__(self, envelope: Envelope) -> None:
        done, more = await index_pending(self._db, self._storage, envelope.tenant_id)
        logger.info("files index run", extra={"files": done, "more_left": more})
        if more:
            await request_index(self._jobs, envelope.tenant_id, f"next:{time.time_ns()}")
