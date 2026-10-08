"""The company file library: list, search, read and attach a company's files.

A company's files are its buyers' uploads (`template_uploads`, every one the
policy lets the caller read, references excluded) and its suppliers' reply
attachments (`attachments`, with the supplier and RFQ of the email they came
with). Ids are the rows' own ids; the two tables never share one.

Search matches a file's name (substring, case-insensitive) or its content:
the text read once from the file (`file_texts`, see `bido_back.files.index`),
as a `simple` full-text match where every word of the query is a prefix
(`rolam` finds `rolamento`). A content hit carries a short snippet and where it
is (sheet and cell, PDF page). Nothing read from a file is logged.

Attaching a file to a Copilot message needs an upload the caller owns (the
Copilot's own rule); `attach` gives one: the caller's own upload as is, else a
reference row with the same storage key (no copy), made once per file and user.
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import (
    ColumnElement,
    SelectBase,
    String,
    and_,
    case,
    cast,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy import null as sql_null
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import QueryableAttribute

from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import (
    Attachment,
    FileText,
    Invite,
    Message,
    Rfq,
    Supplier,
    TemplateUpload,
    User,
)
from bido_back.files.policy import COMPANY_WIDE_READ, can_read_upload
from bido_back.uploads import files as stored

logger = logging.getLogger(__name__)

Db = AsyncEngine | async_sessionmaker[AsyncSession]

FileKind = Literal["upload", "supplier_attachment"]
FileType = Literal["spreadsheet", "pdf", "doc", "image", "other"]

UPLOAD: Final = "upload"
SUPPLIER_ATTACHMENT: Final = "supplier_attachment"
MAX_LIMIT: Final = 100
MAX_QUERY: Final = 100
# Words of a query used for content search, and their shortest useful length.
MAX_QUERY_WORDS: Final = 8
SNIPPET_CHARS: Final = 160

TYPE_EXTENSIONS: Final[dict[str, tuple[str, ...]]] = {
    "spreadsheet": ("xlsx", "xls", "csv"),
    "pdf": ("pdf",),
    "doc": ("docx", "doc", "txt"),
    "image": ("png", "jpg", "jpeg", "webp"),
}


class FileNotFound(LookupError):
    """No file with that id the caller may read."""


class FileSnippet(BaseModel):
    text: str = Field(description="A short excerpt around the match, markers removed")
    location: str | None = Field(
        default=None, description="Where in the file: a sheet and cell (Prices!C14), a page (p.2)"
    )


class FileItem(BaseModel):
    id: UUID
    kind: FileKind
    filename: str
    content_type: str
    file_type: FileType
    size_bytes: int
    created_at: datetime
    uploaded_by: UUID | None = Field(default=None, description="The buyer who uploaded it")
    uploaded_by_email: str | None = None
    supplier_id: UUID | None = Field(default=None, description="For a supplier attachment")
    supplier_name: str | None = None
    rfq_id: UUID | None = None
    rfq_ref: str | None = None
    indexed: bool = Field(description="Its content is searchable (read once after upload)")
    matched: Literal["filename", "content"] | None = None
    snippet: FileSnippet | None = None


class FileList(BaseModel):
    query: str | None
    total: int
    limit: int
    offset: int
    items: list[FileItem]


class AttachedFile(BaseModel):
    """An upload the caller owns, to put in a Copilot message's `attachment_ids`."""

    upload_id: UUID
    filename: str
    size_bytes: int
    content_type: str


@dataclass(frozen=True, slots=True)
class FileFilters:
    q: str | None = None
    kind: FileKind | None = None
    file_type: FileType | None = None
    created_from: date | None = None
    created_to: date | None = None
    rfq_id: UUID | None = None
    supplier_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class LibraryFile:
    """A file the caller may read, with what serving it needs."""

    kind: FileKind
    id: UUID
    filename: str
    size_bytes: int
    storage_key: str
    content_type: str
    uploaded_by: UUID | None


def cast_type(name: str) -> FileType:
    match name:
        case "spreadsheet" | "pdf" | "doc" | "image":
            return name
        case _:
            return "other"


def file_type_of(filename: str) -> FileType:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    for name, extensions in TYPE_EXTENSIONS.items():
        if ext in extensions:
            return cast_type(name)
    return "other"


def query_words(query: str) -> list[str]:
    """The query's words, letters and digits only, for a prefix full-text match."""
    words = re.findall(r"[^\W_]+", query.casefold())
    return [w for w in words if w][:MAX_QUERY_WORDS]


def tsquery_text(words: list[str]) -> str:
    """`w1:* & w2:*`: every word, as a prefix. Words are alphanumeric, so this is safe."""
    return " & ".join(f"{w}:*" for w in words)


def _ext_filter(column: QueryableAttribute[str], file_type: FileType) -> ColumnElement[bool]:
    if file_type == "other":
        known = [e for exts in TYPE_EXTENSIONS.values() for e in exts]
        return and_(*(~func.lower(column).like(f"%.{e}") for e in known))
    return or_(*(func.lower(column).like(f"%.{e}") for e in TYPE_EXTENSIONS[file_type]))


def _like(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _common(
    filters: FileFilters,
    filename: QueryableAttribute[str],
    created_at: QueryableAttribute[datetime],
) -> list[ColumnElement[bool]]:
    """The filters both kinds share, on one table's columns."""
    conditions: list[ColumnElement[bool]] = []
    if filters.file_type is not None:
        conditions.append(_ext_filter(filename, filters.file_type))
    if filters.created_from is not None:
        conditions.append(
            created_at >= datetime.combine(filters.created_from, time.min, tzinfo=UTC)
        )
    if filters.created_to is not None:
        end = datetime.combine(filters.created_to + timedelta(days=1), time.min, tzinfo=UTC)
        conditions.append(created_at < end)
    return conditions


def _rows(principal: Principal, filters: FileFilters) -> SelectBase:
    """Both kinds as one row shape, filtered (the text match is joined in by the caller)."""
    upload_where = [TemplateUpload.source_kind.is_(None)]
    upload_where += _common(filters, TemplateUpload.filename, TemplateUpload.created_at)
    if not COMPANY_WIDE_READ:
        upload_where.append(TemplateUpload.user_id == principal.user_id)
    uploads = (
        select(
            literal(UPLOAD).label("kind"),
            TemplateUpload.id.label("id"),
            TemplateUpload.filename.label("filename"),
            TemplateUpload.size_bytes.label("size_bytes"),
            TemplateUpload.storage_key.label("storage_key"),
            TemplateUpload.created_at.label("created_at"),
            TemplateUpload.user_id.label("uploaded_by"),
            User.email.label("uploaded_by_email"),
            cast(sql_null(), Supplier.id.type).label("supplier_id"),
            cast(sql_null(), String).label("supplier_name"),
            cast(sql_null(), Rfq.id.type).label("rfq_id"),
            cast(sql_null(), String).label("rfq_ref"),
        )
        .outerjoin(User, User.id == TemplateUpload.user_id)
        .where(*upload_where)
    )
    attachment_where = _common(filters, Attachment.filename, Attachment.created_at)
    if filters.rfq_id is not None:
        attachment_where.append(Rfq.id == filters.rfq_id)
    if filters.supplier_id is not None:
        attachment_where.append(Supplier.id == filters.supplier_id)
    attachments = (
        select(
            literal(SUPPLIER_ATTACHMENT).label("kind"),
            Attachment.id.label("id"),
            Attachment.filename.label("filename"),
            Attachment.size_bytes.label("size_bytes"),
            Attachment.storage_key.label("storage_key"),
            Attachment.created_at.label("created_at"),
            cast(sql_null(), User.id.type).label("uploaded_by"),
            cast(sql_null(), String).label("uploaded_by_email"),
            Supplier.id.label("supplier_id"),
            Supplier.name.label("supplier_name"),
            Rfq.id.label("rfq_id"),
            Rfq.ref.label("rfq_ref"),
        )
        .join(Message, Message.id == Attachment.message_id)
        .outerjoin(Invite, Invite.id == Message.invite_id)
        .outerjoin(Rfq, Rfq.id == Invite.rfq_id)
        .outerjoin(Supplier, Supplier.id == Invite.supplier_id)
        .where(*attachment_where)
    )
    # An RFQ or supplier filter only ever matches supplier attachments.
    only_attachments = filters.rfq_id is not None or filters.supplier_id is not None
    if filters.kind == UPLOAD and not only_attachments:
        return uploads
    if filters.kind == SUPPLIER_ATTACHMENT or only_attachments:
        return attachments
    return uploads.union_all(attachments)


async def list_files(
    db: Db,
    principal: Principal,
    filters: FileFilters,
    *,
    limit: int = 50,
    offset: int = 0,
) -> FileList:
    """The company's files, newest first, matching `filters` (name or content for `q`)."""
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)
    q = (filters.q or "").strip()[:MAX_QUERY] or None
    files = _rows(principal, filters).subquery("files")
    texts = FileText.__table__
    joined = files.outerjoin(
        texts, and_(texts.c.file_kind == files.c.kind, texts.c.file_id == files.c.id)
    )
    name_hit: ColumnElement[bool] | None = None
    content_hit: ColumnElement[bool] | None = None
    if q is not None:
        name_hit = files.c.filename.ilike(_like(q), escape="\\")
        words = query_words(q)
        if words:
            content_hit = and_(
                texts.c.status == "indexed",
                texts.c.tsv.op("@@")(func.to_tsquery("simple", tsquery_text(words))),
            )
    matched = (
        case((name_hit, "filename"), (content_hit, "content"), else_=None)
        if name_hit is not None and content_hit is not None
        else case((name_hit, "filename"), else_=None)
        if name_hit is not None
        else literal(None)
    )
    where = (
        or_(name_hit, content_hit) if name_hit is not None and content_hit is not None else name_hit
    )
    base = select(
        files,
        (texts.c.status == "indexed").label("indexed"),
        matched.label("matched"),
    ).select_from(joined)
    if where is not None:
        base = base.where(where)
    async with tenant_session(db, principal.tenant_id) as session:
        total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
        rows = (
            await session.execute(
                base.order_by(files.c.created_at.desc(), files.c.id).limit(limit).offset(offset)
            )
        ).all()
        content_rows = [r for r in rows if r.matched == "content"]
        texts_by_file: dict[tuple[str, UUID], str] = {}
        if content_rows:
            found = await session.execute(
                select(FileText.file_kind, FileText.file_id, FileText.text).where(
                    or_(
                        *(
                            and_(FileText.file_kind == r.kind, FileText.file_id == r.id)
                            for r in content_rows
                        )
                    )
                )
            )
            texts_by_file = {(k, i): t for k, i, t in found.all()}
    items: list[FileItem] = []
    words = query_words(q) if q is not None else []
    for r in rows:
        snippet = None
        if r.matched == "content":
            text = texts_by_file.get((r.kind, r.id))
            snippet = make_snippet(text, words) if text else None
        items.append(
            FileItem(
                id=r.id,
                kind=r.kind,
                filename=r.filename,
                content_type=stored.CONTENT_TYPES.get(
                    _ext(r.filename, r.storage_key), stored.FALLBACK_TYPE
                ),
                file_type=file_type_of(r.filename),
                size_bytes=r.size_bytes,
                created_at=r.created_at,
                uploaded_by=r.uploaded_by,
                uploaded_by_email=r.uploaded_by_email,
                supplier_id=r.supplier_id,
                supplier_name=r.supplier_name,
                rfq_id=r.rfq_id,
                rfq_ref=r.rfq_ref,
                indexed=bool(r.indexed),
                matched=r.matched,
                snippet=snippet,
            )
        )
    logger.info(
        "files listed",
        extra={"total": total, "returned": len(items), "searched": q is not None},
    )
    return FileList(query=q, total=total, limit=limit, offset=offset, items=items)


def _ext(filename: str, storage_key: str) -> str:
    for name in (storage_key, filename):
        if "." in name:
            ext = name.rsplit(".", 1)[-1].lower()
            if ext in stored.CONTENT_TYPES:
                return ext
    return ""


_MARKER: Final = re.compile(r"\[([^\[\]\n]{1,200})\]")


def make_snippet(text: str, words: list[str]) -> FileSnippet | None:
    """The excerpt around the first query word in `text`, and the marker it falls under."""
    folded = text.casefold()
    positions = [p for p in (folded.find(w) for w in words) if p >= 0]
    if not positions:
        return None
    at = min(positions)
    location = None
    for marker in _MARKER.finditer(text, 0, at + 1):
        location = _location(marker.group(1))
    start = max(0, at - SNIPPET_CHARS // 2)
    end = min(len(text), at + SNIPPET_CHARS // 2)
    excerpt = _MARKER.sub(" ", text[start:end])
    excerpt = " ".join(excerpt.split())
    if start > 0:
        excerpt = "…" + excerpt
    if end < len(text):
        excerpt += "…"
    return FileSnippet(text=excerpt, location=location)


def _location(marker: str) -> str | None:
    """`offer.xlsx Prices!C14` -> `Prices!C14`; `quote.pdf p.2` -> `p.2`; a bare name -> None."""
    parts = marker.rsplit(" ", 2)
    for i in range(len(parts) - 1, 0, -1):
        tail = " ".join(parts[i:])
        if re.fullmatch(r"(?:\S+!)?[A-Z]{1,3}\d+|p\.\d+(?: table \d+)?", tail):
            return tail
    return None


async def get_file(db: Db, principal: Principal, file_id: UUID) -> LibraryFile:
    """A file the caller may read, by id (an upload or a supplier attachment)."""
    async with tenant_session(db, principal.tenant_id) as session:
        upload = await session.get(TemplateUpload, file_id)
        if upload is not None and can_read_upload(upload.user_id, principal):
            return LibraryFile(
                kind=UPLOAD,
                id=upload.id,
                filename=upload.filename,
                size_bytes=upload.size_bytes,
                storage_key=upload.storage_key,
                content_type=stored.served_type(stored.file_ref(upload)),
                uploaded_by=upload.user_id,
            )
        attachment = await session.get(Attachment, file_id)
    if attachment is not None:
        return LibraryFile(
            kind=SUPPLIER_ATTACHMENT,
            id=attachment.id,
            filename=attachment.filename,
            size_bytes=attachment.size_bytes,
            storage_key=attachment.storage_key,
            content_type=stored.served_type(stored.file_ref(attachment)),
            uploaded_by=None,
        )
    raise FileNotFound(f"file {file_id} not found")


async def attach(db: Db, principal: Principal, file_id: UUID) -> AttachedFile:
    """An upload id the caller owns for this file, to attach it to a Copilot message."""
    found = await get_file(db, principal, file_id)
    if found.kind == UPLOAD and found.uploaded_by == principal.user_id:
        return AttachedFile(
            upload_id=found.id,
            filename=found.filename,
            size_bytes=found.size_bytes,
            content_type=found.content_type,
        )
    async with tenant_session(db, principal.tenant_id) as session:
        existing = await session.scalar(
            select(TemplateUpload).where(
                TemplateUpload.user_id == principal.user_id,
                TemplateUpload.source_kind == found.kind,
                TemplateUpload.source_id == found.id,
            )
        )
        if existing is None:
            existing = TemplateUpload(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                filename=found.filename,
                content_type=found.content_type,
                size_bytes=found.size_bytes,
                storage_key=found.storage_key,
                source_kind=found.kind,
                source_id=found.id,
            )
            session.add(existing)
            await session.flush()
        upload_id = existing.id
    logger.info(
        "file attached from library",
        extra={"file_id": str(found.id), "file_kind": found.kind, "upload_id": str(upload_id)},
    )
    return AttachedFile(
        upload_id=upload_id,
        filename=found.filename,
        size_bytes=found.size_bytes,
        content_type=found.content_type,
    )
