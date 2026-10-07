"""Turning an `Offer` into the email a simulated supplier sends.

Code renders every number, and states the terms a buyer needs (currency,
validity, Incoterm, payment, origin, no tooling cost, MOQ per line): a
plain-text table in the body (`quote_format` "body"), or an .xlsx / .pdf
attachment. Numbers follow the supplier's
language (decimal comma for it, de, fr, pt), as a real supplier's would, and
quantities never carry thousands separators so they are never ambiguous.
"""

import io
from datetime import UTC, datetime
from decimal import Decimal
from email.message import EmailMessage
from email.utils import format_datetime
from typing import Any, Final, cast

from bido_back.simulator.offers import NoiseItem, Offer, OfferLine
from bido_back.simulator.personas import Language, QuoteFormat

_LABELS: Final[dict[Language, dict[str, str]]] = {
    "it": {
        "pos": "Pos.",
        "description": "Descrizione",
        "mpn": "Codice",
        "qty": "Quantità",
        "unit": "UM",
        "price": "Prezzo unitario",
        "lead": "Consegna (giorni)",
        "moq": "Lotto minimo",
        "declined": "non quotato",
        "equivalent": "equivalente",
        "valid": "Validità offerta",
        "payment": "Pagamento",
        "origin": "Origine",
        "currency": "Valuta",
        "other": "Altre offerte",
        "department": "Reparto",
        "product": "Prodotto",
        "sheet": "Offerta",
        "reply": "R:",
        "tooling": "Costi attrezzature",
        "tooling_none": "nessuno",
    },
    "de": {
        "pos": "Pos.",
        "description": "Bezeichnung",
        "mpn": "Artikelnr.",
        "qty": "Menge",
        "unit": "ME",
        "price": "Einzelpreis",
        "lead": "Lieferzeit (Tage)",
        "moq": "MOQ",
        "declined": "nicht angeboten",
        "equivalent": "gleichwertig",
        "valid": "Angebot gültig bis",
        "payment": "Zahlung",
        "origin": "Ursprung",
        "currency": "Währung",
        "other": "Weitere Angebote",
        "department": "Abteilung",
        "product": "Produkt",
        "sheet": "Angebot",
        "reply": "AW:",
        "tooling": "Werkzeugkosten",
        "tooling_none": "keine",
    },
    "en": {
        "pos": "Pos",
        "description": "Description",
        "mpn": "Part number",
        "qty": "Qty",
        "unit": "Unit",
        "price": "Unit price",
        "lead": "Lead time (days)",
        "moq": "MOQ",
        "declined": "not quoted",
        "equivalent": "equivalent",
        "valid": "Offer valid until",
        "payment": "Payment",
        "origin": "Origin",
        "currency": "Currency",
        "other": "Other offers",
        "department": "Department",
        "product": "Product",
        "sheet": "Quote",
        "reply": "Re:",
        "tooling": "Tooling cost",
        "tooling_none": "none",
    },
    "fr": {
        "pos": "Pos.",
        "description": "Désignation",
        "mpn": "Référence",
        "qty": "Quantité",
        "unit": "Unité",
        "price": "Prix unitaire",
        "lead": "Délai (jours)",
        "moq": "MOQ",
        "declined": "non chiffré",
        "equivalent": "équivalent",
        "valid": "Offre valable jusqu'au",
        "payment": "Paiement",
        "origin": "Origine",
        "currency": "Devise",
        "other": "Autres offres",
        "department": "Service",
        "product": "Produit",
        "sheet": "Offre",
        "reply": "RE :",
        "tooling": "Frais d'outillage",
        "tooling_none": "aucun",
    },
    "pt": {
        "pos": "Item",
        "description": "Descrição",
        "mpn": "Código",
        "qty": "Quantidade",
        "unit": "Un.",
        "price": "Preço unitário",
        "lead": "Prazo (dias)",
        "moq": "Lote mínimo",
        "declined": "não cotado",
        "equivalent": "equivalente",
        "valid": "Validade da proposta",
        "payment": "Pagamento",
        "origin": "Origem",
        "currency": "Moeda",
        "other": "Outras ofertas",
        "department": "Departamento",
        "product": "Produto",
        "sheet": "Proposta",
        "reply": "RE:",
        "tooling": "Custo de ferramental",
        "tooling_none": "nenhum",
    },
}

_XLSX_TYPE: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def labels(language: Language) -> dict[str, str]:
    return _LABELS[language]


def number(value: Decimal, language: Language) -> str:
    """A decimal as the supplier writes it: `2,3500` (it/de/fr/pt) or `2.3500` (en)."""
    text = format(value, "f")
    return text if language == "en" else text.replace(".", ",")


def quantity(value: Decimal) -> str:
    """Quantities without thousands separators: `5000`, `12.5` stays `12.5`."""
    return format(value.normalize(), "f") if value == value.to_integral() else format(value, "f")


def _mpn_cell(line: OfferLine, text: dict[str, str]) -> str:
    if line.is_alternative and line.offered_mpn:
        return f"{line.offered_mpn} ({text['equivalent']})"
    return line.offered_mpn or line.requested_mpn or ""


def _rows(offer: Offer, language: Language) -> list[list[str]]:
    text = labels(language)
    rows: list[list[str]] = []
    for line in offer.lines:
        if line.declined:
            rows.append(
                [
                    str(line.position),
                    line.description,
                    line.requested_mpn or "",
                    "",
                    line.unit,
                    text["declined"],
                    "",
                    "",
                ]
            )
            continue
        for tier in line.tiers:
            rows.append(
                [
                    str(line.position),
                    line.description,
                    _mpn_cell(line, text),
                    quantity(tier.quantity),
                    line.unit,
                    f"{number(tier.unit_price, language)} {offer.currency}",
                    str(line.lead_time_days) if line.lead_time_days is not None else "",
                    quantity(line.moq) if line.moq is not None else "",
                ]
            )
    return rows


def _terms(offer: Offer, language: Language) -> list[str]:
    text = labels(language)
    return [
        f"{text['currency']}: {offer.currency}",
        f"{text['valid']}: {offer.valid_until.isoformat()}",
        f"Incoterm: {offer.incoterm}",
        f"{text['payment']}: {offer.payment_terms}",
        f"{text['tooling']}: {text['tooling_none']}",
        f"{text['origin']}: {offer.origin_country}",
    ]


def _noise_lines(noise: list[NoiseItem], currency: str, language: Language) -> list[str]:
    text = labels(language)
    lines = [f"{text['other']}:"]
    lines.extend(
        f"- {item.department}: {item.product}, {number(item.unit_price, language)} "
        f"{currency}/{item.unit}"
        for item in noise
    )
    return lines


def body_table(offer: Offer, language: Language) -> str:
    """The offer as plain text, one line per line and quantity tier."""
    text = labels(language)
    header = [
        text[key] for key in ("pos", "description", "mpn", "qty", "unit", "price", "lead", "moq")
    ]
    lines = [" | ".join(header)]
    lines.extend(" | ".join(cell for cell in row) for row in _rows(offer, language))
    lines.append("")
    lines.extend(_terms(offer, language))
    return "\n".join(lines)


def xlsx_bytes(offer: Offer, language: Language) -> bytes:
    from openpyxl import Workbook  # pyright: ignore[reportMissingTypeStubs]

    text = labels(language)
    workbook = cast(Any, Workbook())
    sheet = workbook.active
    sheet.title = text["sheet"]
    sheet.append(
        [text[key] for key in ("pos", "description", "mpn", "qty", "unit", "price", "lead", "moq")]
    )
    for line in offer.lines:
        if line.declined:
            sheet.append(
                [
                    line.position,
                    line.description,
                    line.requested_mpn or "",
                    None,
                    line.unit,
                    text["declined"],
                    None,
                    None,
                ]
            )
            continue
        for tier in line.tiers:
            sheet.append(
                [
                    line.position,
                    line.description,
                    _mpn_cell(line, text),
                    float(tier.quantity),
                    line.unit,
                    float(tier.unit_price),
                    line.lead_time_days,
                    float(line.moq) if line.moq is not None else None,
                ]
            )
    sheet.append([])
    for term in _terms(offer, language):
        sheet.append([term])
    if offer.noise:
        other = workbook.create_sheet(text["other"])
        other.append([text["department"], text["product"], text["price"], text["unit"]])
        for item in offer.noise:
            other.append([item.department, item.product, float(item.unit_price), item.unit])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def pdf_bytes(title: str, lines: list[str]) -> bytes:
    """A one-page PDF with a real text layer (Helvetica, WinAnsi)."""
    ops = ["BT", "/F1 9 Tf", "12 TL", "40 800 Td", f"({_pdf_escape(title)}) Tj T*", "T*"]
    ops.extend(f"({_pdf_escape(line)}) Tj T*" for line in lines[:60])
    ops.append("ET")
    stream = "\n".join(ops).encode("cp1252", errors="replace")
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
    for index, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    )
    return out.getvalue()


def _pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def reply_subject(subject: str, language: Language) -> str:
    prefix = labels(language)["reply"]
    stripped = subject.strip()
    return stripped if stripped.lower().startswith(prefix.lower()) else f"{prefix} {stripped}"


def build_reply(
    *,
    offer: Offer,
    language: Language,
    quote_format: QuoteFormat,
    opening: str,
    closing: str,
    supplier_company: str,
    from_name: str,
    from_address: str,
    to_name: str,
    to_address: str,
    subject: str,
    message_id: str,
    in_reply_to: str,
    references: list[str],
    now: datetime | None = None,
) -> bytes:
    """The raw RFC 5322 reply, threaded to the buyer's email."""
    body_parts = [opening.strip(), ""]
    if quote_format == "body":
        body_parts.extend([body_table(offer, language), ""])
    if offer.noise:
        body_parts.extend([*_noise_lines(offer.noise, offer.currency, language), ""])
    body_parts.append(closing.strip())

    reply = EmailMessage()
    reply["From"] = f"{from_name} <{from_address}>"
    reply["To"] = f"{to_name} <{to_address}>" if to_name else to_address
    reply["Subject"] = reply_subject(subject, language)
    reply["Date"] = format_datetime(now or datetime.now(UTC))
    reply["Message-ID"] = message_id
    reply["In-Reply-To"] = in_reply_to
    reply["References"] = " ".join([*references, in_reply_to])
    reply.set_content("\n".join(body_parts))

    safe_name = "".join(c if c.isalnum() else "_" for c in supplier_company)[:40] or "offer"
    if quote_format == "xlsx":
        reply.add_attachment(
            xlsx_bytes(offer, language),
            maintype="application",
            subtype=_XLSX_TYPE.split("/", 1)[1],
            filename=f"{safe_name}_offer_r{offer.round}.xlsx",
        )
    elif quote_format == "pdf":
        text = labels(language)
        rows = [" | ".join(row) for row in _rows(offer, language)]
        lines = [
            " | ".join(
                text[k]
                for k in ("pos", "description", "mpn", "qty", "unit", "price", "lead", "moq")
            ),
            *rows,
            "",
            *_terms(offer, language),
        ]
        reply.add_attachment(
            pdf_bytes(f"{supplier_company} - {text['sheet']}", lines),
            maintype="application",
            subtype="pdf",
            filename=f"{safe_name}_offer_r{offer.round}.pdf",
        )
    return reply.as_bytes()
