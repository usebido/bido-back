"""Tiny documents built in-process: a hand-written PDF and openpyxl workbooks.

No PDF library is available to write PDFs, so `build_pdf` writes the format by
hand: one Helvetica font, text drawn with `Tj`, and stroked rectangles, which
pdfplumber's line-based table finder turns into table cells.
"""

import io
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast


@dataclass(frozen=True)
class PdfText:
    x: float
    y: float
    text: str


@dataclass(frozen=True)
class PdfRect:
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class PdfPage:
    texts: Sequence[PdfText] = ()
    rects: Sequence[PdfRect] = field(default_factory=tuple[PdfRect, ...])


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _content(page: PdfPage) -> bytes:
    ops: list[str] = []
    for rect in page.rects:
        ops.append(f"{rect.x} {rect.y} {rect.width} {rect.height} re S")
    for item in page.texts:
        ops.append(f"BT /F1 10 Tf {item.x} {item.y} Td ({_escape(item.text)}) Tj ET")
    return "\n".join(ops).encode("latin-1")


def build_pdf(pages: Sequence[PdfPage]) -> bytes:
    """A valid PDF 1.4 file with the given pages (A4, 595x842 points)."""
    objects: list[bytes] = []
    page_ids = [4 + 2 * i for i in range(len(pages))]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for pid, page in zip(page_ids, pages, strict=True):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {pid + 1} 0 R >>".encode()
        )
        stream = _content(page)
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def table_page(header: Sequence[str], rows: Sequence[Sequence[str]], *, title: str = "") -> PdfPage:
    """A page with an optional title line above a ruled table."""
    left, top, width, height = 50.0, 700.0, 120.0, 20.0
    texts: list[PdfText] = [PdfText(left, 760, title)] if title else []
    rects: list[PdfRect] = []
    for r, row in enumerate([header, *rows]):
        y = top - r * height
        for c, value in enumerate(row):
            x = left + c * width
            rects.append(PdfRect(x, y, width, height))
            texts.append(PdfText(x + 4, y + 6, value))
    return PdfPage(texts=texts, rects=rects)


def build_xlsx(
    sheets: dict[str, list[list[object]]], formats: dict[str, str] | None = None
) -> bytes:
    """A workbook with one sheet per entry; `formats` maps `Sheet!C3` to a number format."""
    from openpyxl import Workbook  # pyright: ignore[reportMissingTypeStubs]

    workbook = cast(Any, Workbook())
    workbook.remove(workbook.active)
    for title, rows in sheets.items():
        sheet = workbook.create_sheet(title)
        for row in rows:
            sheet.append(row)
    for ref, number_format in (formats or {}).items():
        title, cell = ref.split("!")
        workbook[title][cell].number_format = number_format
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


_DOCX_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def build_docx(paragraphs: Sequence[str], table: Sequence[Sequence[str]] = ()) -> bytes:
    """A minimal .docx: paragraphs (split into two runs each), then an optional table."""

    def runs(text: str) -> str:
        half = len(text) // 2
        return f"<w:r><w:t>{text[:half]}</w:t></w:r><w:r><w:t>{text[half:]}</w:t></w:r>"

    body = "".join(f"<w:p>{runs(p)}</w:p>" for p in paragraphs)
    if table:
        rows = "".join(
            "<w:tr>" + "".join(f"<w:tc><w:p>{runs(c)}</w:p></w:tc>" for c in row) + "</w:tr>"
            for row in table
        )
        body += f"<w:tbl>{rows}</w:tbl>"
    document = f'<?xml version="1.0"?><w:document {_DOCX_NS}><w:body>{body}</w:body></w:document>'
    out = io.BytesIO()
    import zipfile

    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document)
    return out.getvalue()
