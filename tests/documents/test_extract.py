"""Attachment -> model-ready text, with locator markers and limits."""

from datetime import date

import pytest

from bido_back.documents import Limits, extract_text, marker_name, render_documents
from bido_back.documents.extract import column_letter, format_cell
from tests.documents.fixtures import (
    PdfPage,
    PdfText,
    build_docx,
    build_pdf,
    build_xlsx,
    table_page,
)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# --- PDF ---------------------------------------------------------------------------


def test_pdf_text_is_marked_per_page() -> None:
    data = build_pdf(
        [
            PdfPage(texts=[PdfText(50, 800, "Angebot Nr. 4711"), PdfText(50, 780, "Gesamt")]),
            PdfPage(texts=[PdfText(50, 800, "Lieferzeit: 6 Wochen")]),
        ]
    )

    doc = extract_text("Angebot.pdf", "application/pdf", data)

    assert doc.kind == "pdf"
    assert doc.readable and not doc.truncated
    lines = doc.text.splitlines()
    assert lines[0] == "[Angebot.pdf p.1]"
    assert "Angebot Nr. 4711" in lines
    assert lines.index("[Angebot.pdf p.2]") > lines.index("Angebot Nr. 4711")
    assert lines[-1] == "Lieferzeit: 6 Wochen"


def test_pdf_tables_are_rendered_as_rows_and_not_repeated_as_text() -> None:
    page = table_page(
        ["Part", "Qty", "Price"],
        [["TPS7A4701", "1.000", "0,45 EUR"], ["LM317", "5.000", "0,12 EUR"]],
        title="Quotation Q-1",
    )

    doc = extract_text("quote.pdf", "application/pdf", build_pdf([page]))

    lines = doc.text.splitlines()
    assert lines[:2] == ["[quote.pdf p.1]", "Quotation Q-1"]
    table = lines.index("[quote.pdf p.1 table 1]")
    assert lines[table + 1 :] == [
        "Part | Qty | Price",
        "TPS7A4701 | 1.000 | 0,45 EUR",
        "LM317 | 5.000 | 0,12 EUR",
    ]
    assert doc.text.count("TPS7A4701") == 1


def test_pdf_without_text_layer_needs_vision() -> None:
    doc = extract_text("scan.pdf", "application/pdf", build_pdf([PdfPage(), PdfPage()]))

    assert doc.needs_vision
    assert not doc.readable
    assert doc.text == "[scan.pdf] scanned PDF without a text layer: needs vision, not read"


def test_pdf_page_limit_truncates_with_a_marker() -> None:
    pages = [PdfPage(texts=[PdfText(50, 800, f"page {n}")]) for n in range(1, 4)]

    doc = extract_text(
        "long.pdf", "application/pdf", build_pdf(pages), limits=Limits(max_pdf_pages=2)
    )

    assert doc.truncated
    assert "page 2" in doc.text and "page 3" not in doc.text
    assert doc.text.splitlines()[-1] == "[long.pdf] truncated: pages 3-3 not read"


def test_pdf_character_limit_truncates_with_a_marker() -> None:
    texts = [PdfText(50, 800 - 12 * i, f"line {i:02d} " + "x" * 40) for i in range(40)]

    doc = extract_text(
        "big.pdf",
        "application/pdf",
        build_pdf([PdfPage(texts=texts)]),
        limits=Limits(max_chars=500),
    )

    assert doc.truncated
    assert len(doc.text) < 700
    assert doc.text.endswith("[big.pdf] truncated: the rest of the document was not read")


def test_broken_pdf_is_reported_not_raised() -> None:
    doc = extract_text("broken.pdf", "application/pdf", b"%PDF-1.4\nthis is not a pdf")

    assert doc.skipped == "unreadable"
    assert doc.text == "[broken.pdf] file could not be read"


# --- Excel and CSV -----------------------------------------------------------------


def test_xlsx_cells_carry_sheet_and_cell_references() -> None:
    data = build_xlsx(
        {
            "Prices": [
                ["Part", "Qty", "Unit price", None, "Valid until"],
                ["TPS7A4701", 1000, 0.45, None, date(2026, 12, 31)],
                [],
                ["LM317", 5000.0, 0.12],
            ],
            "Notes": [["Lead time 6 weeks"]],
        },
        formats={"Prices!C2": '#,##0.00 "€"'},
    )

    doc = extract_text("offer.xlsx", XLSX, data)

    assert doc.kind == "sheet" and doc.readable
    assert doc.text.splitlines() == [
        "[offer.xlsx Prices!A1] Part | [offer.xlsx Prices!B1] Qty"
        " | [offer.xlsx Prices!C1] Unit price | [offer.xlsx Prices!E1] Valid until",
        "[offer.xlsx Prices!A2] TPS7A4701 | [offer.xlsx Prices!B2] 1000"
        ' | [offer.xlsx Prices!C2] 0.45 (format: #,##0.00 "€") | [offer.xlsx Prices!E2] 2026-12-31',
        "[offer.xlsx Prices!A4] LM317 | [offer.xlsx Prices!B4] 5000 | [offer.xlsx Prices!C4] 0.12",
        "[offer.xlsx Notes!A1] Lead time 6 weeks",
    ]


def test_xlsx_cell_limit_truncates_with_a_marker() -> None:
    rows: list[list[object]] = [[f"r{r}c{c}" for c in range(5)] for r in range(10)]

    doc = extract_text("big.xlsx", XLSX, build_xlsx({"S": rows}), limits=Limits(max_cells=12))

    assert doc.truncated
    assert "[big.xlsx S!E2] r1c4" in doc.text
    assert "r2c0" not in doc.text
    assert doc.text.endswith("[big.xlsx] truncated: the rest of the workbook was not read")


def test_xlsx_is_detected_by_content_even_without_a_known_name() -> None:
    data = build_xlsx({"S": [["x"]]})

    doc = extract_text("download", "application/octet-stream", data)

    assert doc.kind == "sheet"
    assert doc.text == "[download S!A1] x"


def test_csv_with_semicolons_keeps_the_written_numbers() -> None:
    data = "Teil;Menge;Preis\nTPS7A4701;1.000;0,45 €\n".encode()

    doc = extract_text("angebot.csv", "text/csv", data)

    assert doc.kind == "csv"
    assert doc.text.splitlines() == [
        "[angebot.csv A1] Teil | [angebot.csv B1] Menge | [angebot.csv C1] Preis",
        "[angebot.csv A2] TPS7A4701 | [angebot.csv B2] 1.000 | [angebot.csv C2] 0,45 €",
    ]


# --- Text, images, unsupported -------------------------------------------------------


def test_plain_text_and_html() -> None:
    plain = extract_text("notes.txt", "text/plain", "Preis: 0,45 €\r\nMOQ 1000".encode("cp1252"))
    html = extract_text(
        "quote.html",
        "text/html",
        b"<html><head><style>p{}</style></head><body><p>Price</p>"
        b"<table><tr><td>A</td><td>0.45</td></tr></table></body></html>",
    )

    assert plain.text.splitlines() == ["[notes.txt]", "Preis: 0,45 €", "MOQ 1000"]
    assert html.text.splitlines() == ["[quote.html]", "Price", "| A | 0.45"]


def test_attached_email_body_is_read() -> None:
    raw = (
        b"From: sales@example.test\r\nSubject: Quote 42\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\nUnit price 0.45 USD\r\n"
    )

    doc = extract_text("fwd.eml", "message/rfc822", raw)

    assert doc.text.splitlines() == ["[fwd.eml]", "Subject: Quote 42", "Unit price 0.45 USD"]


def test_images_need_vision_and_are_listed() -> None:
    image = extract_text("photo.jpg", "image/jpeg", b"\xff\xd8\xff\xe0")
    text = extract_text("notes.txt", "text/plain", b"hello")

    assert image.needs_vision and image.kind == "image"
    rendered = render_documents([text, image])
    assert rendered.splitlines()[-1] == "[not read: needs vision] photo.jpg"
    assert "[notes.txt]\nhello" in rendered


@pytest.mark.parametrize(
    ("filename", "content_type", "data", "reason"),
    [
        ("old.xls", "application/vnd.ms-excel", b"\xd0\xcf\x11\xe0binary", "unsupported_format"),
        ("big.txt", "text/plain", b"x" * 2_000, "too_large"),
    ],
)
def test_unreadable_files_are_marked(
    filename: str, content_type: str, data: bytes, reason: str
) -> None:
    doc = extract_text(filename, content_type, data, limits=Limits(max_bytes=1_000))

    assert doc.skipped == reason
    assert doc.text.startswith(f"[{filename}] ")


def test_marker_names_cannot_break_markers() -> None:
    assert marker_name("../../etc/[evil]\nname.pdf") == "_evil_name.pdf"
    assert marker_name("C:\\Users\\x\\quote.pdf") == "quote.pdf"
    assert marker_name("") == "attachment"


def test_cell_formatting_helpers() -> None:
    assert [column_letter(n) for n in (1, 26, 27, 52, 703)] == ["A", "Z", "AA", "AZ", "AAA"]
    assert format_cell(0.1 + 0.2) == "0.30000000000000004"
    assert format_cell(1e-7) == "0.0000001"
    assert format_cell(12.0) == "12"
    assert format_cell(True) == "TRUE"
    assert format_cell("  a \n b ") == "a b"


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_docx_paragraphs_and_tables_are_read() -> None:
    data = build_docx(
        ["Prezados, segue solicitação de cotação.", "Favor informar prazo de entrega."],
        table=[["Item", "Qtd.", "Un."], ["Rolamento 6205", "200", "pç"]],
    )
    doc = extract_text("pedido.docx", DOCX, data)

    assert doc.kind == "docx"
    assert doc.readable
    assert doc.text.splitlines() == [
        "[pedido.docx]",
        "Prezados, segue solicitação de cotação.",
        "Favor informar prazo de entrega.",
        "Item | Qtd. | Un.",
        "Rolamento 6205 | 200 | pç",
    ]


def test_broken_docx_is_reported_not_raised() -> None:
    doc = extract_text("pedido.docx", DOCX, b"PK\x03\x04 not really a zip")
    assert doc.skipped == "unreadable"
