"""Download links and previews for the files a buyer uploaded (`template_uploads`).

An upload belongs to the user who made it, inside their tenant: anyone else
gets `UploadNotFound` (404), the same answer as for an id that does not exist.
The Copilot's attachments are these uploads, and a conversation belongs to
the user who had it, so this is also the rule for a chat's files.

What a file is served as comes from its extension in the storage key (set by
us at upload), never from the content type the browser declared: downloads
are always `attachment`, and only PDF and images are ever `inline`, with
their exact type. A spreadsheet preview is read here, values only: openpyxl
never evaluates formulas, and `data_only=True` shows a formula's last saved
result (or nothing). Nothing read from a file is logged.
"""

import asyncio
import csv
import io
import logging
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath
from typing import Final, Protocol, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import TemplateUpload
from bido_back.storage import BlobNotFound, BlobStorage
from bido_back.storage.links import DownloadLinks
from bido_back.uploads.schemas import FileLinkView, SheetPreview, UploadPreview

logger = logging.getLogger(__name__)

Db = async_sessionmaker[AsyncSession] | AsyncEngine

LINK_TTL: Final = timedelta(minutes=5)
PREVIEW_ROWS: Final = 50
PREVIEW_COLUMNS: Final = 30
PREVIEW_CELL_CHARS: Final = 200
PREVIEW_SHEETS: Final = 20
# Bigger files are not read for a preview (download only).
PREVIEW_MAX_BYTES: Final = 25 * 1024 * 1024
# Rows counted per sheet before giving up on an exact total.
COUNT_ROWS_LIMIT: Final = 1_000_000
PREVIEW_TIMEOUT_SECONDS: Final = 15.0

# Served types, by the extension in the storage key.
CONTENT_TYPES: Final[dict[str, str]] = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "csv": "text/csv; charset=utf-8",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
}
INLINE: Final = frozenset({"pdf", "png", "jpg", "jpeg", "webp"})
TABLES: Final = frozenset({"xlsx", "csv"})
FALLBACK_TYPE: Final = "application/octet-stream"


class UploadNotFound(LookupError):
    """No upload with that id for the caller (another user's, another tenant's, or none)."""


async def owned_upload(db: Db, principal: Principal, upload_id: UUID) -> TemplateUpload:
    async with tenant_session(db, principal.tenant_id) as session:
        row = await session.get(TemplateUpload, upload_id)
    if row is None or row.user_id != principal.user_id:
        raise UploadNotFound(f"upload {upload_id} not found")
    return row


def extension(row: TemplateUpload) -> str:
    return PurePosixPath(row.storage_key).suffix.lstrip(".").lower()


def served_type(row: TemplateUpload) -> str:
    return CONTENT_TYPES.get(extension(row), FALLBACK_TYPE)


async def download_link(
    db: Db, links: DownloadLinks, principal: Principal, upload_id: UUID
) -> FileLinkView:
    row = await owned_upload(db, principal, upload_id)
    content_type = served_type(row)
    link = await links.presign_get(
        row.storage_key,
        filename=row.filename,
        content_type=content_type,
        disposition="attachment",
        expires_in=LINK_TTL,
    )
    logger.info("upload download link", extra={"upload_id": str(upload_id)})
    return FileLinkView(
        url=link.url, expires_at=link.expires_at, filename=row.filename, content_type=content_type
    )


async def preview(
    db: Db,
    storage: BlobStorage,
    links: DownloadLinks,
    principal: Principal,
    upload_id: UUID,
) -> UploadPreview:
    row = await owned_upload(db, principal, upload_id)
    ext = extension(row)
    base = UploadPreview(
        id=row.id,
        filename=row.filename,
        size_bytes=row.size_bytes,
        content_type=served_type(row),
        kind="none",
    )
    result = await _preview(storage, links, row, ext, base)
    logger.info(
        "upload preview",
        extra={
            "upload_id": str(upload_id),
            "kind": result.kind,
            "reason": result.reason,
            "sheets": len(result.sheets),
        },
    )
    return result


async def _preview(
    storage: BlobStorage,
    links: DownloadLinks,
    row: TemplateUpload,
    ext: str,
    base: UploadPreview,
) -> UploadPreview:
    if ext in INLINE:
        link = await links.presign_get(
            row.storage_key,
            filename=row.filename,
            content_type=base.content_type,
            disposition="inline",
            expires_in=LINK_TTL,
        )
        return base.model_copy(
            update={"kind": "inline", "url": link.url, "expires_at": link.expires_at}
        )
    if ext not in TABLES:
        return base.model_copy(update={"reason": "unsupported"})
    if row.size_bytes > PREVIEW_MAX_BYTES:
        return base.model_copy(update={"reason": "too_large"})
    try:
        data = await storage.get(row.storage_key)
    except BlobNotFound:
        return base.model_copy(update={"reason": "unreadable"})
    if len(data) > PREVIEW_MAX_BYTES:
        return base.model_copy(update={"reason": "too_large"})
    try:
        sheets = await asyncio.wait_for(
            asyncio.to_thread(read_preview, data, ext, row.filename),
            timeout=PREVIEW_TIMEOUT_SECONDS,
        )
    except (TimeoutError, UnreadableFile):
        return base.model_copy(update={"reason": "unreadable"})
    return base.model_copy(update={"kind": "table", "sheets": sheets})


# --- Reading a spreadsheet ------------------------------------------------------------------


class UnreadableFile(Exception):
    """The bytes are not a readable workbook or text table."""


class _Sheet(Protocol):
    @property
    def title(self) -> str: ...

    def iter_rows(self, *, values_only: bool) -> Iterable[tuple[object, ...]]: ...


class _Workbook(Protocol):
    @property
    def worksheets(self) -> list[_Sheet]: ...

    def close(self) -> None: ...


def read_preview(data: bytes, ext: str, filename: str) -> list[SheetPreview]:
    """The first rows and columns of every sheet (at most `PREVIEW_SHEETS`)."""
    if ext == "csv":
        return [_sheet_preview(filename, _csv_rows(data))]
    from openpyxl import load_workbook  # pyright: ignore[reportMissingTypeStubs]

    try:
        workbook = cast(_Workbook, load_workbook(io.BytesIO(data), read_only=True, data_only=True))
    except Exception as exc:  # openpyxl raises several unrelated types on bad input
        raise UnreadableFile("not a readable .xlsx workbook") from exc
    try:
        return [
            _sheet_preview(sheet.title, sheet.iter_rows(values_only=True))
            for sheet in workbook.worksheets[:PREVIEW_SHEETS]
        ]
    except UnreadableFile:
        raise
    except Exception as exc:
        raise UnreadableFile("not a readable .xlsx workbook") from exc
    finally:
        workbook.close()


def _csv_rows(data: bytes) -> list[list[object]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(
            text[:4096], delimiters=",;\t"
        )
    except csv.Error:
        dialect = csv.excel
    rows: list[list[object]] = []
    for count, record in enumerate(csv.reader(io.StringIO(text), dialect)):
        if count >= COUNT_ROWS_LIMIT:
            break
        rows.append(list(record))
    return rows


def _sheet_preview(name: str, rows: Iterable[Iterable[object]]) -> SheetPreview:
    kept: list[list[str]] = []
    total_rows = 0
    total_columns = 0
    cells_cut = False
    for count, raw in enumerate(rows):
        if count >= COUNT_ROWS_LIMIT:
            break
        texts = [_text(value) for value in raw]
        while texts and not texts[-1]:
            texts.pop()
        if not texts:
            continue
        total_rows += 1
        total_columns = max(total_columns, len(texts))
        if len(kept) < PREVIEW_ROWS:
            row = texts[:PREVIEW_COLUMNS]
            cells_cut = cells_cut or any(len(t) > PREVIEW_CELL_CHARS for t in row)
            kept.append([_cut(t) for t in row])
    width = min(total_columns, PREVIEW_COLUMNS)
    padded = [row + [""] * (width - len(row)) for row in kept]
    return SheetPreview(
        name=name,
        rows=padded,
        total_rows=total_rows,
        total_columns=total_columns,
        truncated=cells_cut or total_rows > PREVIEW_ROWS or total_columns > PREVIEW_COLUMNS,
    )


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    if isinstance(value, datetime):
        if (value.hour, value.minute, value.second, value.microsecond) == (0, 0, 0, 0):
            return value.date().isoformat()
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip()


def _cut(text: str) -> str:
    if len(text) <= PREVIEW_CELL_CHARS:
        return text
    return text[: PREVIEW_CELL_CHARS - 1] + "…"
