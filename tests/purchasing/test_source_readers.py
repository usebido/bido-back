"""Source readers preserve evidence and disclose extraction limits without model calls."""

from datetime import date, datetime

import pytest

from bido_back.purchasing.sources import readers
from bido_back.purchasing.sources.readers import SourceDocument, read_source
from tests.documents.fixtures import PdfPage, PdfText, build_pdf, build_xlsx

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_workbook_reader_keeps_every_sheet_and_formatted_identifiers() -> None:
    source = SourceDocument(
        source_id="upload-1",
        filename="procurement.xlsx",
        content_type=XLSX,
        data=build_xlsx(
            {
                "Suppliers": [["Supplier code", "Name"], [123, "Example Supplier"]],
                "Requirements": [["Material", "Quantity", "Unit"], [10, 25, "KG"]],
                "Notes": [["Prepared by purchasing"], [], ["Do not include blocked stock"]],
            },
            formats={"Suppliers!A2": "000000", "Requirements!A2": "00000000"},
        ),
    )

    tables, issues = read_source(source)

    assert issues == []
    assert [table.name for table in tables] == ["Suppliers", "Requirements", "Notes"]
    assert {table.source_id for table in tables} == {"upload-1"}
    assert tables[0].rows[1] == ["000123", "Example Supplier"]
    assert tables[1].rows[1] == ["00000010", "25", "KG"]
    assert tables[2].rows == [["Prepared by purchasing"], [None], ["Do not include blocked stock"]]


def test_csv_reader_preserves_codes_decimal_spelling_and_all_rows() -> None:
    data = b"Fornecedor;Valor;Moeda\n000123;1.234,50;BRL\n123;10,00;BRL\n"

    tables, issues = read_source(SourceDocument("csv-1", "history.csv", "text/csv", data))

    assert issues == []
    [table] = tables
    assert table.layout == "table"
    assert table.rows == [
        ["Fornecedor", "Valor", "Moeda"],
        ["000123", "1.234,50", "BRL"],
        ["123", "10,00", "BRL"],
    ]


def test_text_reader_keeps_lines_for_grounded_slices() -> None:
    source = SourceDocument(
        "text-1", "request.txt", "text/plain", b"Material P-01\nNeed 45 EA by 2026-10-31"
    )

    tables, issues = read_source(source)

    assert issues == []
    [table] = tables
    assert table.layout == "text"
    assert table.rows[-2:] == [["Material P-01"], ["Need 45 EA by 2026-10-31"]]
    assert table.source_id == "text-1"


def test_formula_cache_is_disclosed_and_missing_results_are_not_invented() -> None:
    source = SourceDocument(
        "formula-1",
        "needs.xlsx",
        XLSX,
        build_xlsx({"Needs": [["Material", "Need"], ["P-01", "=10+5"]]}),
    )

    tables, issues = read_source(source)

    assert tables[0].rows[1] == ["P-01", None]
    assert [(issue.code, issue.severity) for issue in issues] == [("formula_cache", "blocker")]


def test_native_excel_dates_are_canonical_calendar_dates() -> None:
    source = SourceDocument(
        "dates",
        "dates.xlsx",
        XLSX,
        build_xlsx(
            {
                "Requirements": [
                    ["Material", "Need by", "Ordered at"],
                    ["P-01", date(2026, 10, 31), datetime(2026, 10, 8, 12, 30)],
                ]
            }
        ),
    )

    tables, issues = read_source(source)

    assert issues == []
    assert tables[0].rows[1] == ["P-01", "2026-10-31", "2026-10-08"]


@pytest.mark.parametrize(
    ("filename", "content_type", "data", "code"),
    [
        ("scan.pdf", "application/pdf", build_pdf([PdfPage()]), "needs_vision"),
        ("photo.png", "image/png", b"unavailable image data", "needs_vision"),
        ("archive.bin", "application/octet-stream", b"\x00\x01\x02", "unreadable"),
        ("broken.xlsx", XLSX, b"not a workbook", "unreadable"),
    ],
)
def test_unreadable_sources_never_become_successful_tables(
    filename: str, content_type: str, data: bytes, code: str
) -> None:
    tables, issues = read_source(SourceDocument("source-1", filename, content_type, data))

    assert tables == []
    assert [(issue.code, issue.severity) for issue in issues] == [(code, "blocker")]


def test_partial_pdf_coverage_is_explicit() -> None:
    pages = [PdfPage(texts=[PdfText(50, 800, f"Material page {i}")]) for i in range(31)]
    source = SourceDocument("pdf-1", "long.pdf", "application/pdf", build_pdf(pages))

    tables, issues = read_source(source)

    assert len(tables) == 1
    assert any("Material page 29" in (row[0] or "") for row in tables[0].rows)
    assert not any("Material page 30" in (row[0] or "") for row in tables[0].rows)
    assert [(issue.code, issue.severity) for issue in issues] == [("partial_coverage", "blocker")]


def test_mixed_pdf_exposes_readable_evidence_and_blocks_claims_of_complete_coverage() -> None:
    source = SourceDocument(
        "mixed-pdf",
        "mixed.pdf",
        "application/pdf",
        build_pdf(
            [
                PdfPage(texts=[PdfText(50, 800, "Material P-01: 25 EA")]),
                PdfPage(image=True),
            ]
        ),
    )

    tables, issues = read_source(source)

    assert len(tables) == 1
    assert any(row == ["Material P-01: 25 EA"] for row in tables[0].rows)
    assert [(issue.code, issue.severity) for issue in issues] == [("partial_coverage", "blocker")]


def test_byte_limit_rejects_source_without_echoing_its_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(readers, "MAX_SOURCE_BYTES", 8)
    tables, issues = read_source(
        SourceDocument("too-large", "input.txt", "text/plain", b"private source content")
    )

    assert tables == []
    assert issues[0].code == "source_limit"
    assert "private" not in issues[0].message


@pytest.mark.parametrize("limit", ["MAX_SOURCE_CELLS", "MAX_SOURCE_CHARS", "MAX_SOURCE_ROWS"])
def test_workbook_limits_do_not_return_a_silently_partial_workbook(
    monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    monkeypatch.setattr(readers, limit, 2)
    source = SourceDocument(
        "limited", "input.xlsx", XLSX, build_xlsx({"S": [["Material"], ["P-01"], ["P-02"]]})
    )

    tables, issues = read_source(source)

    assert tables == []
    assert issues and all(issue.severity == "blocker" for issue in issues)


def test_text_truncation_returns_explicit_partial_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readers, "MAX_SOURCE_CHARS", 12)
    tables, issues = read_source(
        SourceDocument("text-limit", "input.txt", "text/plain", b"first line\nsecond line")
    )

    assert tables
    assert any(row == ["first line"] for row in tables[0].rows)
    assert [issue.code for issue in issues] == ["partial_coverage"]


@pytest.mark.parametrize(
    ("filename", "content_type", "data"),
    [
        ("input.csv", "text/csv", b"Material,Quantity\nP-01,25\nP-02,50\n"),
        ("input.txt", "text/plain", b"Material P-01\nQuantity 25"),
    ],
)
def test_row_limit_applies_to_csv_and_text_evidence(
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    content_type: str,
    data: bytes,
) -> None:
    monkeypatch.setattr(readers, "MAX_SOURCE_ROWS", 2)

    tables, issues = read_source(SourceDocument("row-limit", filename, content_type, data))

    assert tables == []
    assert [(issue.code, issue.severity) for issue in issues] == [("source_limit", "blocker")]
