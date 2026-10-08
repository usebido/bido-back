"""The RFQ document: one PDF per supplier, attached to that supplier's RFQ email.

It follows the RFQ sheet the buyer sees in the app (bido-front `PdfPreview`):
the Bido wordmark and "Request for quotation", the RFQ title, number and date,
the addressee and the delivery place, a band with the reply-by and need-by
dates, the item table (description with part no. / MPN under it, quantity,
unit, need-by), the terms and what to include in the quote.

One rule the app's sheet does not have: a supplier's PDF is addressed to that
supplier ONLY. The app lists every invited supplier under "To" for the buyer;
the PDF never names another invitee.

`RfqDocument` is everything the PDF shows, as data. `prepare_send` puts it in
the approved send arguments, so the approval covers the PDF; at send time the
PDF is rendered again from those approved arguments (never from the live RFQ),
and the send refuses when the RFQ no longer matches them (`stale_reasons`).

Rendering is HTML + CSS through WeasyPrint. Every value is HTML-escaped, and
the fetcher allows `data:` URIs only (the embedded logo): the PDF never reaches
the network or the file system.
"""

import base64
import html
import re
import unicodedata
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from functools import cache
from importlib import resources
from typing import Final

from pydantic import BaseModel, ConfigDict, Field
from weasyprint import HTML  # pyright: ignore[reportMissingTypeStubs]
from weasyprint.urls import URLFetcher  # pyright: ignore[reportMissingTypeStubs]

from bido_back.db.models import Rfq, RfqLine
from bido_back.templates.render import RenderInput, asks, format_date, format_quantity

PDF_CONTENT_TYPE: Final = "application/pdf"

# Fixed labels per language (English when the supplier's language has none).
_LABELS: Final[dict[str, dict[str, str]]] = {
    "en": {
        "title": "Request for quotation",
        "number": "Number",
        "date": "Date",
        "to": "To",
        "deliver_to": "Deliver to",
        "reply_by": "Reply by",
        "need_by": "Need-by",
        "item": "Item",
        "qty": "Qty",
        "unit": "Unit",
        "part_no": "Part no.",
        "mpn": "MPN",
        "incoterm": "Incoterm",
        "payment_terms": "Payment terms",
        "validity": "Quote validity",
        "days": "{days} days",
        "currency": "Currency",
        "include": "Please include in your quote",
        "reference": "Please keep {ref} in the subject of your reply.",
        "from": "From",
    },
    "pt": {
        "title": "Solicitação de cotação",
        "number": "Número",
        "date": "Data",
        "to": "Para",
        "deliver_to": "Entregar em",
        "reply_by": "Responder até",
        "need_by": "Necessário em",
        "item": "Item",
        "qty": "Qtd.",
        "unit": "Un.",
        "part_no": "Código",
        "mpn": "Cód. fabricante",
        "incoterm": "Incoterm",
        "payment_terms": "Condição de pagamento",
        "validity": "Validade da proposta",
        "days": "{days} dias",
        "currency": "Moeda",
        "include": "Por favor, informe na sua proposta",
        "reference": "Mantenha {ref} no assunto da sua resposta.",
        "from": "De",
    },
    "de": {
        "title": "Angebotsanfrage",
        "number": "Nummer",
        "date": "Datum",
        "to": "An",
        "deliver_to": "Lieferadresse",
        "reply_by": "Antwort bis",
        "need_by": "Liefertermin",
        "item": "Artikel",
        "qty": "Menge",
        "unit": "Einheit",
        "part_no": "Artikelnr.",
        "mpn": "Herstellernr.",
        "incoterm": "Incoterm",
        "payment_terms": "Zahlungsbedingungen",
        "validity": "Angebotsgültigkeit",
        "days": "{days} Tage",
        "currency": "Währung",
        "include": "Bitte geben Sie in Ihrem Angebot an",
        "reference": "Bitte behalten Sie {ref} im Betreff Ihrer Antwort.",
        "from": "Von",
    },
    "fr": {
        "title": "Demande de prix",
        "number": "Numéro",
        "date": "Date",
        "to": "À",
        "deliver_to": "Livrer à",
        "reply_by": "Réponse avant le",
        "need_by": "Date souhaitée",
        "item": "Article",
        "qty": "Qté",
        "unit": "Unité",
        "part_no": "Référence",
        "mpn": "Réf. fabricant",
        "incoterm": "Incoterm",
        "payment_terms": "Conditions de paiement",
        "validity": "Validité de l'offre",
        "days": "{days} jours",
        "currency": "Devise",
        "include": "Merci d'indiquer dans votre offre",
        "reference": "Merci de garder {ref} dans l'objet de votre réponse.",
        "from": "De",
    },
    "es": {
        "title": "Solicitud de cotización",
        "number": "Número",
        "date": "Fecha",
        "to": "Para",
        "deliver_to": "Entregar en",
        "reply_by": "Responder antes del",
        "need_by": "Fecha requerida",
        "item": "Artículo",
        "qty": "Cant.",
        "unit": "Unidad",
        "part_no": "Código",
        "mpn": "Cód. fabricante",
        "incoterm": "Incoterm",
        "payment_terms": "Condiciones de pago",
        "validity": "Validez de la oferta",
        "days": "{days} días",
        "currency": "Moneda",
        "include": "Por favor, indique en su oferta",
        "reference": "Mantenga {ref} en el asunto de su respuesta.",
        "from": "De",
    },
    "it": {
        "title": "Richiesta di offerta",
        "number": "Numero",
        "date": "Data",
        "to": "A",
        "deliver_to": "Consegna a",
        "reply_by": "Rispondere entro",
        "need_by": "Data richiesta",
        "item": "Articolo",
        "qty": "Q.tà",
        "unit": "Unità",
        "part_no": "Codice",
        "mpn": "Codice produttore",
        "incoterm": "Incoterm",
        "payment_terms": "Condizioni di pagamento",
        "validity": "Validità dell'offerta",
        "days": "{days} giorni",
        "currency": "Valuta",
        "include": "Si prega di indicare nella vostra offerta",
        "reference": "Mantenete {ref} nell'oggetto della risposta.",
        "from": "Da",
    },
}


def labels(language: str) -> dict[str, str]:
    return _LABELS.get(language, _LABELS["en"])


# --- What the PDF shows -----------------------------------------------------------------


class DocumentLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    position: int
    description: str
    part_number: str | None = None
    mpn: str | None = None
    manufacturer: str | None = None
    # As printed ("7500", "2.5"), so the approved arguments are plain JSON.
    quantities: list[str] = Field(min_length=1)
    unit: str
    need_by: date | None = None
    incoterm: str | None = None
    delivery_location: str | None = None


class RfqDocument(BaseModel):
    """Everything one supplier's PDF shows. Part of the approved send arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    language: str
    rfq_ref: str
    title: str
    issued_on: date
    buyer_company: str
    buyer_name: str
    # The addressee: this supplier only.
    supplier_name: str
    reply_by: date | None = None
    currency: str | None = None
    payment_terms: str | None = None
    quote_validity_days: int | None = None
    lines: list[DocumentLine] = Field(min_length=1)
    asks: list[str] = Field(default_factory=list[str])


def _quantity(value: Decimal) -> str:
    return format_quantity(value)


def build_document(
    data: RenderInput, *, supplier_name: str, currency: str | None, issued_on: date
) -> RfqDocument:
    """The document for one supplier, from the same input as that supplier's email."""
    return RfqDocument(
        language=data.language,
        rfq_ref=data.rfq_ref,
        title=data.rfq_title,
        issued_on=issued_on,
        buyer_company=data.buyer_company,
        buyer_name=data.buyer_name,
        supplier_name=supplier_name,
        reply_by=data.response_due,
        currency=currency,
        payment_terms=data.payment_terms,
        quote_validity_days=data.quote_validity_days,
        lines=[
            DocumentLine(
                position=line.position,
                description=line.description,
                part_number=line.part_number,
                mpn=line.mpn,
                manufacturer=line.manufacturer,
                quantities=[_quantity(q) for q in line.quantities],
                unit=line.unit,
                need_by=line.need_by,
                incoterm=line.incoterm,
                delivery_location=line.delivery_location,
            )
            for line in data.lines
        ],
        asks=asks(data),
    )


# --- Staleness: the RFQ must still say what was approved ------------------------------


def content_key(document: RfqDocument) -> tuple[object, ...]:
    """The RFQ facts a document carries (not its wording): title, terms and lines."""
    return (
        document.rfq_ref,
        document.title,
        document.reply_by,
        document.currency,
        document.payment_terms,
        document.quote_validity_days,
        tuple(
            (
                line.position,
                line.description,
                line.part_number,
                line.mpn,
                line.manufacturer,
                tuple(line.quantities),
                line.unit,
                line.need_by,
                line.incoterm,
                line.delivery_location,
            )
            for line in document.lines
        ),
    )


def with_current_facts(approved: RfqDocument, rfq: Rfq, lines: Sequence[RfqLine]) -> RfqDocument:
    """The approved document with the RFQ's facts as they are now (wording kept)."""
    return approved.model_copy(
        update={
            "rfq_ref": rfq.ref,
            "title": rfq.title,
            "reply_by": rfq.response_due,
            "currency": rfq.base_currency,
            "payment_terms": rfq.payment_terms,
            "quote_validity_days": rfq.quote_validity_days,
            "lines": [
                DocumentLine(
                    position=line.position,
                    description=line.description,
                    part_number=line.part_number,
                    mpn=line.mpn,
                    manufacturer=line.manufacturer,
                    quantities=[_quantity(Decimal(str(q))) for q in line.quantities],
                    unit=line.unit,
                    need_by=line.need_by,
                    incoterm=line.incoterm,
                    delivery_location=line.delivery_location,
                )
                for line in lines
            ],
        }
    )


def stale_reasons(approved: RfqDocument, current: RfqDocument) -> list[str]:
    """What changed in the RFQ since the document was approved; empty when nothing did."""
    reasons: list[str] = []
    a, c = content_key(approved), content_key(current)
    names = ("ref", "title", "reply_by", "currency", "payment_terms", "quote_validity", "lines")
    for name, before, after in zip(names, a, c, strict=True):
        if before != after:
            reasons.append(name)
    return reasons


# --- File name ----------------------------------------------------------------------------


def _slug(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:60].rstrip("-") or "supplier"


def document_filename(document: RfqDocument) -> str:
    """E.g. `RFQ-3-metalurgica-horizonte-ltda.pdf`."""
    ref = re.sub(r"[^A-Za-z0-9-]+", "-", document.rfq_ref).strip("-") or "RFQ"
    return f"{ref}-{_slug(document.supplier_name)}.pdf"


# --- HTML + CSS -----------------------------------------------------------------------------


@cache
def _logo_data_uri() -> str:
    data = resources.files("bido_back.rfqs").joinpath("assets/bido-logo.png").read_bytes()
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


_CSS: Final = """
@page { size: A4; margin: 18mm 16mm 18mm 16mm;
  @bottom-left { content: element(footer-left); }
  @bottom-right { content: counter(page) " / " counter(pages); font-size: 7.5pt; color: #9aa1ad; } }
* { box-sizing: border-box; }
body { font-family: "Inter", "DejaVu Sans", "Liberation Sans", Arial, sans-serif;
  font-size: 9.5pt; color: #1f2430; line-height: 1.35; margin: 0; }
.header { display: flex; justify-content: space-between; align-items: center; }
.header img { height: 30px; }
.header .doc-title { font-size: 17pt; font-weight: 400; }
.parties { display: flex; justify-content: space-between; gap: 18px;
  border-top: 1px solid #e3e6ec; margin-top: 14px; padding-top: 12px; }
.parties .left { flex: 1; }
.rfq-title { font-size: 11pt; margin-bottom: 8px; }
.eyebrow { font-size: 7pt; text-transform: uppercase; letter-spacing: .04em; color: #8a91a0; }
.pair { margin-bottom: 7px; }
.pair .value { font-size: 9.5pt; }
.meta { flex: 1; font-size: 8.5pt; color: #6b7280; }
.meta table { margin-left: auto; border-collapse: collapse; }
.meta td { padding: 1.5px 0 1.5px 14px; text-align: right; }
.meta td.v { color: #1f2430; }
.band { text-align: right; margin-top: 14px; }
.band-inner { display: inline-block; background: #f0ecff; border-radius: 4px;
  padding: 9px 16px 9px 14px; text-align: left; }
.band-inner > div { display: inline-block; vertical-align: top; margin-right: 26px; }
.band-inner > div:last-child { margin-right: 0; }
.band .value { font-size: 11pt; white-space: nowrap; }
table.items { width: 100%; border-collapse: collapse; margin-top: 18px; table-layout: fixed; }
table.items th { font-weight: 400; font-size: 7pt; text-transform: uppercase; letter-spacing: .04em;
  color: #6b7280; border-bottom: 1px solid #1f2430; padding: 5px 0; text-align: left; }
table.items th.num, table.items td.num { text-align: right; }
table.items td { border-bottom: 1px solid #e3e6ec; padding: 8px 0; vertical-align: top; }
table.items td.desc { padding-right: 8px; }
table.items .meta-line { color: #8a91a0; font-size: 8.5pt; }
table.items tr { page-break-inside: avoid; }
.col-qty { width: 16%; } .col-unit { width: 10%; } .col-need { width: 18%; }
.terms { margin-top: 22px; display: flex; justify-content: flex-end; }
.terms table { border-collapse: collapse; font-size: 8.5pt; color: #6b7280; }
.terms td { padding: 1.5px 0 1.5px 18px; }
.terms td.v { color: #1f2430; text-align: right; }
.asks { margin-top: 20px; }
.asks ul { margin: 6px 0 0 0; padding-left: 16px; }
.asks li { margin-bottom: 2px; }
.reference { margin-top: 14px; color: #6b7280; font-size: 8.5pt; }
.footer-left { position: running(footer-left); font-size: 7.5pt; color: #9aa1ad; }
"""


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _date(value: date | None, language: str) -> str:
    return format_date(value, language) if value else "—"


def _unique(values: Sequence[str | None]) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen


def document_html(document: RfqDocument) -> str:
    """The document as HTML (every value escaped), ready for WeasyPrint."""
    lang = document.language
    words = labels(lang)

    deliver_to = _unique([line.delivery_location for line in document.lines])
    incoterms = _unique([line.incoterm for line in document.lines])
    need_dates = sorted({line.need_by for line in document.lines if line.need_by})

    meta_rows = [
        (words["number"], document.rfq_ref),
        (words["date"], format_date(document.issued_on, lang)),
    ]
    meta = "".join(
        f'<tr><td>{_e(label)}</td><td class="v">{_e(value)}</td></tr>' for label, value in meta_rows
    )

    parties = [
        f'<div class="rfq-title">{_e(document.title)}</div>',
        f'<div class="pair"><div class="eyebrow">{_e(words["to"])}</div>'
        f'<div class="value">{_e(document.supplier_name)}</div></div>',
        f'<div class="pair"><div class="eyebrow">{_e(words["from"])}</div>'
        f'<div class="value">{_e(_from_line(document))}</div></div>',
    ]
    if deliver_to:
        parties.append(
            f'<div class="pair"><div class="eyebrow">{_e(words["deliver_to"])}</div>'
            f'<div class="value">{_e(" · ".join(deliver_to))}</div></div>'
        )

    need_text = " · ".join(format_date(d, lang) for d in need_dates) if need_dates else "—"
    band = (
        '<div class="band"><div class="band-inner">'
        f'<div><div class="eyebrow">{_e(words["reply_by"])}</div>'
        f'<div class="value">{_e(_date(document.reply_by, lang))}</div></div>'
        f'<div><div class="eyebrow">{_e(words["need_by"])}</div>'
        f'<div class="value">{_e(need_text)}</div></div>'
        "</div></div>"
    )

    rows: list[str] = []
    for line in document.lines:
        meta_bits: list[str] = []
        if line.part_number:
            meta_bits.append(f"{words['part_no']} {line.part_number}")
        if line.mpn and line.mpn != line.part_number:
            meta_bits.append(f"{words['mpn']} {line.mpn}")
        if line.manufacturer:
            meta_bits.append(line.manufacturer)
        meta_line = f'<div class="meta-line">{_e(" · ".join(meta_bits))}</div>' if meta_bits else ""
        rows.append(
            "<tr>"
            f'<td class="desc">{_e(line.position)}. {_e(line.description)}{meta_line}</td>'
            f'<td class="num">{_e(" / ".join(line.quantities))}</td>'
            f'<td class="num">{_e(line.unit)}</td>'
            f'<td class="num">{_e(_date(line.need_by, lang))}</td>'
            "</tr>"
        )
    items = (
        '<table class="items"><colgroup><col><col class="col-qty"><col class="col-unit">'
        '<col class="col-need"></colgroup><thead><tr>'
        f'<th>{_e(words["item"])}</th><th class="num">{_e(words["qty"])}</th>'
        f'<th class="num">{_e(words["unit"])}</th><th class="num">{_e(words["need_by"])}</th>'
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )

    term_rows: list[tuple[str, str]] = []
    if incoterms:
        term_rows.append((words["incoterm"], " · ".join(incoterms)))
    if document.payment_terms:
        term_rows.append((words["payment_terms"], document.payment_terms))
    if document.quote_validity_days:
        term_rows.append(
            (words["validity"], words["days"].format(days=document.quote_validity_days))
        )
    if document.currency:
        term_rows.append((words["currency"], document.currency))
    terms = (
        '<div class="terms"><table>'
        + "".join(
            f'<tr><td>{_e(label)}</td><td class="v">{_e(value)}</td></tr>'
            for label, value in term_rows
        )
        + "</table></div>"
        if term_rows
        else ""
    )

    asks_html = (
        f'<div class="asks"><div class="eyebrow">{_e(words["include"])}</div><ul>'
        + "".join(f"<li>{_e(ask)}</li>" for ask in document.asks)
        + "</ul></div>"
        if document.asks
        else ""
    )
    reference_text = words["reference"].format(ref=document.rfq_ref)
    reference = f'<div class="reference">{_e(reference_text)}</div>'
    footer = f'<div class="footer-left">{_e(document.rfq_ref)} · {_e(document.buyer_company)}</div>'

    return (
        f'<!doctype html><html lang="{_e(lang)}"><head><meta charset="utf-8">'
        f"<title>{_e(document.rfq_ref)}</title><style>{_CSS}</style></head><body>"
        + footer
        + '<div class="header">'
        f'<img src="{_logo_data_uri()}" alt="Bido">'
        f'<span class="doc-title">{_e(words["title"])}</span></div>'
        '<div class="parties"><div class="left">'
        + "".join(parties)
        + f'</div><div class="meta"><table>{meta}</table></div></div>'
        + band
        + items
        + terms
        + asks_html
        + reference
        + "</body></html>"
    )


def _from_line(document: RfqDocument) -> str:
    name, company = document.buyer_name.strip(), document.buyer_company.strip()
    if not name or name.casefold() == company.casefold():
        return company
    return f"{name} · {company}" if company else name


def _fetcher() -> URLFetcher:
    """Only `data:` URIs (the embedded logo): nothing from the network or the disk."""
    return URLFetcher(allowed_protocols={"data"}, fail_on_errors=False)


def render_html_pdf(markup: str) -> bytes:
    """Any HTML to PDF with the locked-down fetcher (also used by the tests)."""
    pdf = HTML(string=markup, url_fetcher=_fetcher()).write_pdf()  # pyright: ignore[reportUnknownMemberType]
    if not isinstance(pdf, bytes):  # pragma: no cover - write_pdf without a target returns bytes
        raise TypeError("WeasyPrint did not return PDF bytes")
    return pdf


def render_pdf(document: RfqDocument) -> bytes:
    """The supplier's PDF."""
    return render_html_pdf(document_html(document))
