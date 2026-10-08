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
import re
import time
import zipfile
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
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
# A spreadsheet preview is paged: `limit` rows from `offset`, every column up to
# `PREVIEW_COLUMNS` (the rest is marked `truncated_columns`).
PREVIEW_ROWS: Final = 200
PREVIEW_ROWS_MAX: Final = 1_000
PREVIEW_COLUMNS: Final = 200
PREVIEW_CELL_CHARS: Final = 200
PREVIEW_SHEETS: Final = 20
# Bigger files are not read for a preview (download only).
PREVIEW_MAX_BYTES: Final = 25 * 1024 * 1024
# Rows read per sheet at most.
COUNT_ROWS_LIMIT: Final = 1_000_000
PREVIEW_TIMEOUT_SECONDS: Final = 30.0
# Files kept in this process between page requests (their bytes).
FILE_CACHE_ENTRIES: Final = 2

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
    page: "PageRequest | None" = None,
) -> UploadPreview:
    """What to show for the file. Spreadsheets come one page at a time (`page`).

    Raises `UploadNotFound`, and `SheetNotFound` for a `page.sheet` the file does not have.
    """
    page = page or PageRequest()
    row = await owned_upload(db, principal, upload_id)
    ext = extension(row)
    base = UploadPreview(
        id=row.id,
        filename=row.filename,
        size_bytes=row.size_bytes,
        content_type=served_type(row),
        kind="none",
    )
    started = time.monotonic()
    result = await _preview(storage, links, row, ext, base, page)
    logger.info(
        "upload preview",
        extra={
            "upload_id": str(upload_id),
            "kind": result.kind,
            "reason": result.reason,
            "sheets": len(result.sheets),
            "sheet_count": len(result.sheet_names),
            "offset": page.offset,
            "limit": page.limit,
            "paged": page.sheet is not None,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
        },
    )
    return result


async def _preview(
    storage: BlobStorage,
    links: DownloadLinks,
    row: TemplateUpload,
    ext: str,
    base: UploadPreview,
    page: "PageRequest",
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
        data = await _file_bytes(storage, row.storage_key)
    except BlobNotFound:
        return base.model_copy(update={"reason": "unreadable"})
    if len(data) > PREVIEW_MAX_BYTES:
        return base.model_copy(update={"reason": "too_large"})
    try:
        names, sheets = await asyncio.wait_for(
            _table_pages(row.storage_key, data, ext, row.filename, page),
            timeout=PREVIEW_TIMEOUT_SECONDS,
        )
    except (TimeoutError, UnreadableFile):
        return base.model_copy(update={"reason": "unreadable"})
    return base.model_copy(update={"kind": "table", "sheets": sheets, "sheet_names": names})


# --- Reading a spreadsheet ------------------------------------------------------------------
#
# A preview is paged, and any page costs the same. An .xlsx sheet is XML with one
# `<row>` element per row: the sheet's XML is unzipped once, the byte offset of
# every `<row>` is found (milliseconds even for 20,000 rows), and a page is parsed
# from just its rows' bytes, with openpyxl's own cell parser (shared strings,
# dates, cached formula results; formulas are never evaluated). `offset` counts
# those `<row>` elements; a row with no value shows as an empty row. A .csv is read
# whole (it is quick) and its empty lines are skipped. What a file needs between
# page requests is kept in this process: the bytes, the opened workbook, each
# paged sheet's XML and row offsets, a csv's rows (`_FILES`, `_BOOKS`, `_XML`,
# `_TABLES`, each bounded).


class UnreadableFile(Exception):
    """The bytes are not a readable workbook or text table."""


class SheetNotFound(LookupError):
    """The requested sheet is not in the file."""


@dataclass(frozen=True, slots=True)
class PageRequest:
    """Which rows to show: `limit` rows of `sheet` from `offset`.

    Without a `sheet`, the first `limit` rows of every sheet (an overview).
    """

    sheet: str | None = None
    offset: int = 0
    limit: int = PREVIEW_ROWS


class _Sheet(Protocol):
    @property
    def title(self) -> str: ...

    def iter_rows(self, *, values_only: bool) -> Iterable[tuple[object, ...]]: ...


class _Workbook(Protocol):
    @property
    def worksheets(self) -> list[_Sheet]: ...

    @property
    def epoch(self) -> datetime: ...

    def close(self) -> None: ...


_SEPARATOR: Final = "\x1f"
_ROW_TAG: Final = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?row[\s>/]")
_SHEET_DATA_OPEN: Final = re.compile(rb"<(?:[A-Za-z_][\w.-]*:)?sheetData(?:\s[^>]*)?>")
_SHEET_DATA_CLOSE: Final = re.compile(rb"</(?:[A-Za-z_][\w.-]*:)?sheetData\s*>")
_DIMENSION: Final = re.compile(rb'<(?:[A-Za-z_][\w.-]*:)?dimension\s+ref="[A-Z]+\d+:([A-Z]+)\d+"')
# Unzipped sheet XML kept between page requests, in all.
XML_CACHE_BYTES: Final = 96 * 1024 * 1024
BOOK_CACHE_ENTRIES: Final = 2


@dataclass(frozen=True, slots=True)
class _SheetInfo:
    name: str
    # The sheet's part in the .xlsx archive ("" for a csv).
    path: str


@dataclass(slots=True)
class _Book:
    """A file opened for previews: its sheets, and the workbook (None for a csv)."""

    infos: list[_SheetInfo]
    workbook: _Workbook | None
    table: "_Table | None" = None


@dataclass(frozen=True, slots=True)
class _SheetXml:
    """A sheet's XML and where each `<row>` starts in it."""

    xml: bytes
    starts: list[int]
    # The `<sheetData>` opening tag ends at `head`; `</sheetData>` starts at `tail`.
    head: int
    tail: int
    # From the `<dimension>` element, 0 when the file has none.
    columns: int


@dataclass(slots=True)
class _Table:
    """Every row of a csv that has a value, as text (cells joined by `_SEPARATOR`)."""

    rows: list[str]
    columns: int
    cut: bool

    @property
    def chars(self) -> int:
        return sum(len(r) for r in self.rows) + 64 * len(self.rows)


@dataclass(frozen=True, slots=True)
class _Page:
    rows: list[list[str]]
    total_rows: int
    columns: int
    cut: bool


# Per-process caches, touched only from the event loop; a file's opened workbook and
# sheets are used by one thread at a time (`_LOCKS`).
_FILES: "OrderedDict[str, bytes]" = OrderedDict()
_BOOKS: "OrderedDict[str, _Book]" = OrderedDict()
_LOCKS: dict[str, asyncio.Lock] = {}
_XML: "OrderedDict[tuple[str, str], _SheetXml]" = OrderedDict()


async def _file_bytes(storage: BlobStorage, key: str) -> bytes:
    data = _FILES.get(key)
    if data is not None:
        _FILES.move_to_end(key)
        return data
    data = await storage.get(key)
    if len(data) <= PREVIEW_MAX_BYTES:
        _FILES[key] = data
        while len(_FILES) > FILE_CACHE_ENTRIES:
            _FILES.popitem(last=False)
    return data


def clear_caches() -> None:
    """Forget every cached file and sheet (tests)."""
    _FILES.clear()
    for book in _BOOKS.values():
        _close(book)
    _BOOKS.clear()
    _LOCKS.clear()
    _XML.clear()


async def _table_pages(
    key: str, data: bytes, ext: str, filename: str, page: PageRequest
) -> tuple[list[str], list[SheetPreview]]:
    lock = _LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        book = _BOOKS.get(key)
        if book is None:
            book = await asyncio.to_thread(_open_book, data, ext, filename)
            _BOOKS[key] = book
            while len(_BOOKS) > BOOK_CACHE_ENTRIES:
                old_key, old = _BOOKS.popitem(last=False)
                _LOCKS.pop(old_key, None)
                for cached in [k for k in _XML if k[0] == old_key]:
                    del _XML[cached]
                _close(old)
        else:
            _BOOKS.move_to_end(key)
        names = [i.name for i in book.infos]
        if page.sheet is not None:
            wanted = [i for i in book.infos if i.name == page.sheet]
            if not wanted:
                raise SheetNotFound(page.sheet)
        else:
            wanted = book.infos
        previews: list[SheetPreview] = []
        for info in wanted:
            found = await _page(key, book, info, page.offset, page.limit)
            previews.append(_sheet_preview(info.name, found, page.offset))
        return names, previews


async def _page(key: str, book: _Book, info: _SheetInfo, offset: int, limit: int) -> _Page:
    if book.table is not None:
        return _slice(book.table, offset, limit)
    if book.workbook is None:
        raise UnreadableFile("not a readable file")
    workbook = book.workbook
    cache_key = (key, info.name)
    sheet = _XML.get(cache_key)
    if sheet is None:
        sheet = await asyncio.to_thread(_sheet_xml, workbook, info)
        if len(sheet.xml) <= XML_CACHE_BYTES:
            _XML[cache_key] = sheet
            while sum(len(s.xml) for s in _XML.values()) > XML_CACHE_BYTES:
                _XML.popitem(last=False)
    else:
        _XML.move_to_end(cache_key)
    return await asyncio.to_thread(_xlsx_page, workbook, info, sheet, offset, limit)


def _slice(table: _Table, offset: int, limit: int) -> _Page:
    rows = [r.split(_SEPARATOR) for r in table.rows[offset : offset + limit]]
    return _Page(rows=rows, total_rows=len(table.rows), columns=table.columns, cut=table.cut)


# --- Blocking readers (run in a thread) ---------------------------------------------------------


def read_preview(data: bytes, ext: str, filename: str) -> list[SheetPreview]:
    """The first `PREVIEW_ROWS` rows of every sheet (at most `PREVIEW_SHEETS`)."""
    return read_page(data, ext, filename, None, 0, PREVIEW_ROWS)


def read_page(
    data: bytes, ext: str, filename: str, sheet: str | None, offset: int, limit: int
) -> list[SheetPreview]:
    """`limit` rows from `offset` of `sheet` (or of every sheet), without the caches."""
    book = _open_book(data, ext, filename)
    try:
        infos = [i for i in book.infos if sheet is None or i.name == sheet]
        if not infos:
            raise SheetNotFound(sheet or "")
        previews: list[SheetPreview] = []
        for info in infos:
            if book.table is not None:
                found = _slice(book.table, offset, limit)
            elif book.workbook is not None:
                xml = _sheet_xml(book.workbook, info)
                found = _xlsx_page(book.workbook, info, xml, offset, limit)
            else:
                raise UnreadableFile("not a readable file")
            previews.append(_sheet_preview(info.name, found, offset))
        return previews
    finally:
        _close(book)


def _open_book(data: bytes, ext: str, filename: str) -> _Book:
    if ext == "csv":
        return _Book(
            infos=[_SheetInfo(name=filename, path="")], workbook=None, table=_csv_table(data)
        )
    from openpyxl import load_workbook  # pyright: ignore[reportMissingTypeStubs]

    try:
        workbook = cast(_Workbook, load_workbook(io.BytesIO(data), read_only=True, data_only=True))
    except Exception as exc:  # openpyxl raises several unrelated types on bad input
        raise UnreadableFile("not a readable .xlsx workbook") from exc
    try:
        infos = [
            # openpyxl's own part path (read-only mode): getattr, as it is private.
            _SheetInfo(name=s.title, path=cast(str, getattr(s, "_worksheet_path")))  # noqa: B009
            for s in workbook.worksheets[:PREVIEW_SHEETS]
        ]
    except Exception as exc:
        workbook.close()
        raise UnreadableFile("not a readable .xlsx workbook") from exc
    return _Book(infos=infos, workbook=workbook)


def _close(book: _Book) -> None:
    if book.workbook is not None:
        book.workbook.close()


def _sheet_xml(workbook: _Workbook, info: _SheetInfo) -> _SheetXml:
    try:
        archive = cast(zipfile.ZipFile, getattr(workbook, "_archive"))  # noqa: B009
        xml = archive.read(info.path)
    except Exception as exc:
        raise UnreadableFile("not a readable .xlsx workbook") from exc
    opened = _SHEET_DATA_OPEN.search(xml)
    if opened is None or xml[opened.end() - 2 : opened.end()] == b"/>":
        # No rows: `<sheetData/>` (or no sheetData at all).
        return _SheetXml(xml=b"", starts=[], head=0, tail=0, columns=0)
    closed = _SHEET_DATA_CLOSE.search(xml, opened.end())
    if closed is None:
        raise UnreadableFile("not a readable .xlsx workbook")
    starts = [m.start() for m in _ROW_TAG.finditer(xml, opened.end(), closed.start())]
    dimension = _DIMENSION.search(xml, 0, opened.start())
    columns = _column_number(dimension.group(1).decode()) if dimension else 0
    return _SheetXml(
        xml=xml, starts=starts, head=opened.end(), tail=closed.start(), columns=columns
    )


def _xlsx_page(
    workbook: _Workbook, info: _SheetInfo, sheet: _SheetXml, offset: int, limit: int
) -> _Page:
    """Parse only the rows `offset` to `offset + limit` of the sheet's XML."""
    total = len(sheet.starts)
    if offset >= total:
        return _Page(rows=[], total_rows=total, columns=sheet.columns, cut=False)
    end = offset + limit
    first = sheet.starts[offset]
    last = sheet.starts[end] if end < total else sheet.tail
    part = sheet.xml[: sheet.head] + sheet.xml[first:last] + sheet.xml[sheet.tail :]
    rows: list[list[str]] = []
    cut = False
    widest = 0
    try:
        for cells in _parse_rows(workbook, info, part):
            text = _row_text(cells)
            rows.append(text.cells)
            cut = cut or text.cut
            widest = max(widest, len(text.cells))
    except Exception as exc:
        raise UnreadableFile("not a readable .xlsx workbook") from exc
    return _Page(rows=rows, total_rows=total, columns=max(sheet.columns, widest), cut=cut)


def _parse_rows(workbook: _Workbook, info: _SheetInfo, part: bytes) -> Iterable[list[object]]:
    """The rows of a slice of sheet XML, as values, with openpyxl's cell parser.

    `WorkSheetParser` is what openpyxl's read-only worksheets use; it is private
    to openpyxl (pinned in uv.lock), hence the getattr calls.
    """
    from openpyxl.worksheet._reader import (  # pyright: ignore[reportMissingTypeStubs]
        WorkSheetParser,  # pyright: ignore[reportUnknownVariableType]
    )

    sheet = next(s for s in workbook.worksheets if s.title == info.name)
    parser = WorkSheetParser(  # pyright: ignore[reportUnknownVariableType]
        io.BytesIO(part),
        getattr(sheet, "_shared_strings"),  # noqa: B009
        data_only=True,
        epoch=workbook.epoch,
        date_formats=getattr(workbook, "_date_formats"),  # noqa: B009
        timedelta_formats=getattr(workbook, "_timedelta_formats"),  # noqa: B009
    )
    for _, cells in cast(Iterable[tuple[int, list[dict[str, object]]]], parser.parse()):  # pyright: ignore[reportUnknownMemberType]
        width = max((cast(int, c["column"]) for c in cells), default=0)
        values: list[object] = [None] * min(width, PREVIEW_COLUMNS + 1)
        for cell in cells:
            column = cast(int, cell["column"])
            if column <= len(values):
                values[column - 1] = cell["value"]
        yield values


def _column_number(letters: str) -> int:
    number = 0
    for letter in letters:
        number = number * 26 + (ord(letter) - ord("A") + 1)
    return number


def _csv_table(data: bytes) -> _Table:
    kept: list[str] = []
    widest = 0
    cut = False
    for count, raw in enumerate(_csv_rows(data)):
        if count >= COUNT_ROWS_LIMIT:
            break
        text = _row_text(raw)
        if not text.cells:
            continue
        kept.append(_SEPARATOR.join(c.replace(_SEPARATOR, " ") for c in text.cells))
        widest = max(widest, len(text.cells))
        cut = cut or text.cut
    return _Table(rows=kept, columns=widest, cut=cut)


@dataclass(frozen=True, slots=True)
class _Text:
    """One row as shown: its cells as text (cut), trailing empty cells dropped."""

    cells: list[str]
    cut: bool


def _row_text(raw: Iterable[object]) -> _Text:
    """A row's cells as shown, one past `PREVIEW_COLUMNS` (to see a cut); [] if empty."""
    texts = [_text(value) for value in raw]
    while texts and not texts[-1]:
        texts.pop()
    if not texts:
        return _Text(cells=[], cut=False)
    kept = texts[: PREVIEW_COLUMNS + 1]
    if max(map(len, kept)) <= PREVIEW_CELL_CHARS:
        return _Text(cells=kept, cut=False)
    cut = any(len(t) > PREVIEW_CELL_CHARS for t in kept[:PREVIEW_COLUMNS])
    return _Text(cells=[_cut(t) for t in kept], cut=cut)


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


def _sheet_preview(name: str, found: _Page, offset: int) -> SheetPreview:
    columns = max(found.columns, max((len(r) for r in found.rows), default=0))
    width = min(columns, PREVIEW_COLUMNS)
    padded = [(row + [""] * (width - len(row)))[:width] for row in found.rows]
    total_rows = found.total_rows
    has_more = offset + len(padded) < total_rows
    truncated_columns = columns > PREVIEW_COLUMNS
    return SheetPreview(
        name=name,
        rows=padded,
        total_rows=total_rows,
        total_columns=columns,
        truncated=found.cut or truncated_columns or has_more or offset > 0,
        offset=offset,
        has_more=has_more,
        truncated_columns=truncated_columns,
    )


def _text(value: object) -> str:
    # Most cells are text or numbers: check those first (this runs once per cell).
    kind = type(value)
    if kind is str:
        return cast(str, value).strip()
    if kind is int:
        return str(value)
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
