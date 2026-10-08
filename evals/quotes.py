"""Synthetic supplier replies for the quote-extraction scenario, with their truth.

Cases: a German PDF + spreadsheet (lead time in a "weeks" column, tooling
"keine"), an Italian reply with prices in the body, an English reply mixing
another division's offers (extra offers to capture, not to price), and a
decline. Domains are reserved test domains only.
"""

import io
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, cast

from bido_back.agents.catalog.contracts import RfqEmailLine


@dataclass(frozen=True, slots=True)
class LineTruth:
    position: int
    min_quantity: Decimal
    unit_price: Decimal
    lead_time_days: int | None = None
    tooling_zero: bool = False


@dataclass(frozen=True, slots=True)
class QuoteCase:
    name: str
    description: str
    language: str  # ISO 639-1, for the decimal separator
    subject: str
    body: str
    documents: tuple[tuple[str, str, bytes], ...]
    is_quote: bool
    declined: bool
    currency: str | None
    lines: tuple[LineTruth, ...] = ()
    min_extra_offers: int = 0
    # True when every truth value is in the e-mail body (scriptable in mock mode).
    body_only: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple[str, ...])


REQUESTED: tuple[RfqEmailLine, ...] = (
    RfqEmailLine(position=1, description="NTC thermistor 10k 0603", mpn="NTC-10K-3950",
                 manufacturer="Sensor Demo", quantities=[Decimal(10000)], unit="pcs"),
    RfqEmailLine(position=2, description="Thermal pad 2 mm 300x300", mpn="TP-2-300",
                 manufacturer="Thermal Demo", quantities=[Decimal(400)], unit="pcs"),
)  # fmt: skip


def _pdf(lines: list[str]) -> bytes:
    """A one-page PDF with a real text layer (latin-1 text only)."""
    ops = ["BT", "/F1 10 Tf", "14 TL", "50 780 Td"]
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"({escaped}) Tj T*")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out = io.BytesIO(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    )
    return out.getvalue()


def _xlsx(title: str, rows: list[list[object]]) -> bytes:
    from openpyxl import Workbook  # pyright: ignore[reportMissingTypeStubs]

    workbook = cast(Any, Workbook())
    sheet = workbook.active
    sheet.title = title
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def cases(today: date) -> list[QuoteCase]:
    valid = today + timedelta(days=60)
    de_valid = valid.strftime("%d.%m.%Y")
    it_valid = valid.strftime("%d/%m/%Y")
    return [
        QuoteCase(
            name="de_pdf_xlsx_weeks_keine",
            description="German reply: header terms in a PDF, prices in an xlsx with decimal "
            "commas, lead time in a 'Lieferzeit (Wochen)' column, tooling 'keine'",
            language="de",
            subject="AW: [RFQ-1] Thermistoren und Waermeleitpads",
            body="Sehr geehrte Damen und Herren,\nanbei unser Angebot zu RFQ-1.\n"
            "Mit freundlichen Gruessen\nJonas Weber",
            documents=(
                (
                    "Angebot_4711.pdf",
                    "application/pdf",
                    _pdf(
                        [
                            "Elektron Demo GmbH - Angebot Nr. 4711",
                            "Ihre Anfrage: RFQ-1",
                            f"Gueltig bis: {de_valid}   Lieferbedingung: DAP Leipzig",
                            "Zahlung: 30 Tage netto   Waehrung: EUR",
                            "Preise siehe beigefuegte Tabelle Angebot.xlsx.",
                        ]
                    ),
                ),
                (
                    "Angebot.xlsx",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    _xlsx(
                        "Angebot",
                        [
                            [
                                "Pos",
                                "Artikel",
                                "Hersteller-Nr.",
                                "Menge",
                                "Einheit",
                                "Preis/Stk (EUR)",
                                "Lieferzeit (Wochen)",
                            ],
                            [1, "NTC Thermistor 10k", "NTC-10K-3950", 10000, "Stk", "0,0420", 6],
                            [2, "Waermeleitpad 2 mm", "TP-2-300", 400, "Stk", "7,6500", 4],
                            ["Werkzeugkosten: keine. Ursprungsland: DE."],
                        ],
                    ),
                ),
            ),  # fmt: skip
            is_quote=True,
            declined=False,
            currency="EUR",
            lines=(
                LineTruth(1, Decimal(10000), Decimal("0.0420"), 42, tooling_zero=True),
                LineTruth(2, Decimal(400), Decimal("7.6500"), 28, tooling_zero=True),
            ),
        ),
        QuoteCase(
            name="it_body_prices",
            description="Italian reply with prices, lead time in weeks and validity in the body",
            language="it",
            subject="R: [RFQ-1] Richiesta di offerta termistori e pad termici",
            body=(
                "Buongiorno,\ndi seguito la nostra offerta per la RFQ-1.\n"
                "Pos. 1 - Termistore NTC 10k NTC-10K-3950: 10.000 pz a 0,039 EUR cad., "
                "consegna 5 settimane.\n"
                "Pos. 2 - Pad termico 2 mm TP-2-300: 400 pz a 7,90 EUR cad., "
                "consegna 3 settimane.\n"
                f"Validita offerta fino al {it_valid}. Resa DAP Leipzig. Pagamento 60 giorni d.f.\n"
                "Cordiali saluti\nGiulia Rossi - Termico Demo S.r.l."
            ),
            documents=(),
            is_quote=True,
            declined=False,
            currency="EUR",
            lines=(
                LineTruth(1, Decimal(10000), Decimal("0.039"), 35),
                LineTruth(2, Decimal(400), Decimal("7.90"), 21),
            ),
            body_only=True,
        ),
        QuoteCase(
            name="en_noisy_other_divisions",
            description="English reply that also pushes another division's products and a "
            "volume discount: requested lines priced, the rest captured as extra offers",
            language="en",
            subject="RE: [RFQ-1] Thermistors and thermal pads",
            body=(
                "Hi Anna,\nthanks for RFQ-1. Our offer:\n"
                "Line 1: NTC thermistor NTC-10K-3950, 10,000 pcs at EUR 0.041 each, "
                "lead time 30 days.\n"
                "Line 2: thermal pad TP-2-300, 400 pcs at EUR 7.70 each, lead time 20 days.\n"
                f"Prices valid until {valid.isoformat()}, FCA Milan, payment 30 days net.\n\n"
                "While you are here - our Industrial Drives division has a special this month: "
                "servo drive SD-200 at EUR 199 per unit (min. 10 units).\n"
                "Also: order 20,000 thermistors and we give 5% off the whole order.\n\n"
                "Best regards,\nMark Lee - Components Demo Ltd"
            ),
            documents=(),
            is_quote=True,
            declined=False,
            currency="EUR",
            lines=(
                LineTruth(1, Decimal(10000), Decimal("0.041"), 30),
                LineTruth(2, Decimal(400), Decimal("7.70"), 20),
            ),
            min_extra_offers=1,
            body_only=True,
        ),
        QuoteCase(
            name="en_decline",
            description="A polite decline with no prices",
            language="en",
            subject="RE: [RFQ-1] Thermistors and thermal pads",
            body=(
                "Hello,\nunfortunately we cannot quote these items: they are outside our "
                "product range.\nKind regards,\nSam - Distributor Demo"
            ),
            documents=(),
            is_quote=False,
            declined=True,
            currency=None,
            body_only=True,
        ),
    ]
