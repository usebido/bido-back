"""The purchase order PDF the supplier receives: pure code, no database."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

from bido_back.awards.document import po_document_html, po_document_text, po_pdf, po_pdf_filename
from bido_back.awards.render import PoDocument, PoLine, content_hash
from bido_back.rfqs.document import DocumentBranding


def doc(**changes: object) -> PoDocument:
    base = PoDocument(
        ref="PO-7",
        rfq_ref="RFQ-3",
        issued_on=date(2026, 10, 7),
        buyer_company="Voltwerk",
        supplier_name="Rhein Elektronik GmbH",
        currency="EUR",
        incoterm="DAP Leipzig",
        payment_terms="30 Tage netto",
        total=Decimal("1650.00"),
        lines=(
            PoLine(
                position=1,
                description="MLCC 100nF",
                mpn="GRM188R71H104KA93D",
                quantity=Decimal(4000),
                unit="pcs",
                unit_price=Decimal("0.1"),
                line_total=Decimal("400.00"),
                need_by=date(2026, 11, 20),
                lead_time_days=21,
                part_number="VW-1001",
                delivery_location="Leipzig, Werk 2",
            ),
            PoLine(
                position=2,
                description="Connector <40 pin>",
                mpn=None,
                quantity=Decimal(500),
                unit="pcs",
                unit_price=Decimal("2.5"),
                line_total=Decimal("1250.00"),
                need_by=None,
                lead_time_days=28,
                delivery_location="Leipzig, Werk 2",
            ),
        ),
        language="de",
        branding=DocumentBranding(
            company_name="Voltwerk",
            legal_name="Voltwerk GmbH",
            tax_id="DE123456789",
            address="Industriestr. 1, 04109 Leipzig",
        ),
        supplier_contact="sales@rhein.example.test",
        quote_version=2,
        quote_date=date(2026, 10, 1),
        approved_by="Anna Becker",
    )
    return replace(base, **changes)  # pyright: ignore[reportArgumentType]


def test_the_pdf_says_who_orders_what_from_whom_on_which_terms() -> None:
    text = po_document_text(doc())
    for expected in (
        "Bestellung",
        "PO-7",
        "Voltwerk GmbH",
        "USt-IdNr. DE123456789",
        "Industriestr. 1, 04109 Leipzig",
        "Rhein Elektronik GmbH",
        "z. Hd. sales@rhein.example.test",
        "RFQ-3, Version 2 vom 01.10.2026",
        "Leipzig, Werk 2",
        "Artikelnr. VW-1001",
        "Herstellernr. GRM188R71H104KA93D",
        "0,1",
        "20.11.2026",
        "28 Tage nach Bestellung",
        "1.650,00 EUR",
        "DAP Leipzig",
        "30 Tage netto",
        "Freigegeben von: Anna Becker",
        "Geben Sie PO-7",
    ):
        assert expected in text, expected
    assert "Connector <40 pin>" in text  # unescaped in the text, escaped in the markup
    assert "&lt;40 pin&gt;" in po_document_html(doc())


def test_the_pdf_renders_and_falls_back_to_english() -> None:
    assert po_pdf(doc()).startswith(b"%PDF")
    assert po_pdf_filename(doc()) == "PO-7.pdf"
    english = po_document_text(doc(language="zh", branding=None))
    assert "Purchase order" in english and "Approved by: Anna Becker" in english
    assert "Voltwerk" in english  # the company name heads it without branding


def test_the_hash_covers_what_the_pdf_shows() -> None:
    base = content_hash(doc())
    assert content_hash(doc()) == base
    assert content_hash(doc(approved_by="Someone else")) != base
    assert content_hash(doc(supplier_contact="other@rhein.example.test")) != base
    assert content_hash(doc(language="en")) != base
    branding = doc().branding
    assert branding is not None
    assert content_hash(doc(branding=branding.model_copy(update={"tax_id": "DE9"}))) != base
    line = replace(doc().lines[0], delivery_location="Dresden")
    assert content_hash(doc(lines=(line, doc().lines[1]))) != base
