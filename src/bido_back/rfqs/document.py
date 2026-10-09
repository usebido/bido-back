"""The RFQ document: one PDF per supplier, attached to that supplier's RFQ email.

It follows the RFQ sheet the buyer sees in the app (bido-front `PdfPreview`),
but it goes out in the BUYER COMPANY's name, as large companies' RFQs do: the
company's logo (or its name as a text header), legal name, tax id and address
on top, "Request for quotation", the RFQ title, number and date, the addressee
and the delivery place, a band with the reply-by and need-by dates, the buyer's
own opening (their template's greeting), the item table (description with
part no. / MPN, manufacturer and qualification under it, quantity, unit,
need-by), the terms, how to price, what to include in the quote, the
alternative-part policy, and the company's footer text. Bido shows only as a
small "sent via Bido" line in the footer.

The PDF IS the RFQ: the email that carries it is a short cover note
(`bido_back.templates.render.render_cover`), so the PDF carries everything the
full email would (`document_problems` checks the required fields on it).

One rule the app's sheet does not have: a supplier's PDF is addressed to that
supplier ONLY. The app lists every invited supplier under "To" for the buyer;
the PDF never names another invitee.

`RfqDocument` is everything the PDF shows, as data, the company branding
(`DocumentBranding`, logo bytes included) too. `prepare_send` puts it in the
approved send arguments, so the approval and its payload hash cover the PDF
and its branding; at send time the
PDF is rendered again from those approved arguments (never from the live RFQ),
and the send refuses when the RFQ no longer matches them (`stale_reasons`).

Rendering is HTML + CSS through WeasyPrint. Every value is HTML-escaped, and
the fetcher allows `data:` URIs only (the embedded company logo): the PDF never
reaches the network or the file system.
"""

import base64
import html
import re
import unicodedata
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from weasyprint import HTML  # pyright: ignore[reportMissingTypeStubs]
from weasyprint.urls import URLFetcher  # pyright: ignore[reportMissingTypeStubs]

from bido_back.db.models import Rfq, RfqLine
from bido_back.supplier_email.locale import Locale, unit_label
from bido_back.templates.render import (
    RenderInput,
    display_quantity,
    document_asks,
    format_date,
    format_quantity,
    phrases,
)
from bido_back.templates.required import REQUIRED_FIELDS, covered_keys

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
        "qualification": "Qualification",
        "incoterm": "Incoterm",
        "payment_terms": "Payment terms",
        "validity": "Quote validity",
        "days": "{days} days",
        "currency": "Currency",
        "include": "Please include in your quote",
        "reference": "Please keep {ref} in the subject of your reply.",
        "from": "From",
        "tax_id": "Tax ID",
        "sent_via": "Sent via Bido",
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
        "qualification": "Qualificação",
        "incoterm": "Incoterm",
        "payment_terms": "Condição de pagamento",
        "validity": "Validade da proposta",
        "days": "{days} dias",
        "currency": "Moeda",
        "include": "Por favor, informe na sua proposta",
        "reference": "Mantenha {ref} no assunto da sua resposta.",
        "from": "De",
        "tax_id": "CNPJ",
        "sent_via": "Enviado pelo Bido",
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
        "qualification": "Qualifikation",
        "incoterm": "Incoterm",
        "payment_terms": "Zahlungsbedingungen",
        "validity": "Angebotsgültigkeit",
        "days": "{days} Tage",
        "currency": "Währung",
        "include": "Bitte geben Sie in Ihrem Angebot an",
        "reference": "Bitte behalten Sie {ref} im Betreff Ihrer Antwort.",
        "from": "Von",
        "tax_id": "USt-IdNr.",
        "sent_via": "Versendet über Bido",
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
        "qualification": "Qualification",
        "incoterm": "Incoterm",
        "payment_terms": "Conditions de paiement",
        "validity": "Validité de l'offre",
        "days": "{days} jours",
        "currency": "Devise",
        "include": "Merci d'indiquer dans votre offre",
        "reference": "Merci de garder {ref} dans l'objet de votre réponse.",
        "from": "De",
        "tax_id": "N° TVA",
        "sent_via": "Envoyé via Bido",
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
        "qualification": "Calificación",
        "incoterm": "Incoterm",
        "payment_terms": "Condiciones de pago",
        "validity": "Validez de la oferta",
        "days": "{days} días",
        "currency": "Moneda",
        "include": "Por favor, indique en su oferta",
        "reference": "Mantenga {ref} en el asunto de su respuesta.",
        "from": "De",
        "tax_id": "NIF",
        "sent_via": "Enviado mediante Bido",
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
        "qualification": "Qualifica",
        "incoterm": "Incoterm",
        "payment_terms": "Condizioni di pagamento",
        "validity": "Validità dell'offerta",
        "days": "{days} giorni",
        "currency": "Valuta",
        "include": "Si prega di indicare nella vostra offerta",
        "reference": "Mantenete {ref} nell'oggetto della risposta.",
        "from": "Da",
        "tax_id": "P. IVA",
        "sent_via": "Inviato tramite Bido",
    },
}


def labels(language: str) -> dict[str, str]:
    return _LABELS.get(language, _LABELS["en"])


# --- The buyer company's branding ---------------------------------------------------------

LogoMediaType = Literal["image/png", "image/jpeg", "image/svg+xml"]
# About 500 KB of logo once decoded.
_MAX_LOGO_BASE64: Final = 700_000


class DocumentBranding(BaseModel):
    """Whose RFQ this is: the buyer company. Part of the approved arguments (logo too)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    company_name: str = Field(min_length=1, max_length=300)
    legal_name: str | None = Field(default=None, max_length=300)
    tax_id: str | None = Field(default=None, max_length=60)
    address: str | None = Field(default=None, max_length=600)
    footer_text: str | None = Field(default=None, max_length=2000)
    accent_color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    logo_media_type: LogoMediaType | None = None
    logo_base64: str | None = Field(default=None, max_length=_MAX_LOGO_BASE64)
    # Where the logo sits in its reserved box (the buyer's choice; part of the approval).
    logo_scale: float = Field(default=1.0, ge=0.5, le=2.0)
    logo_align: Literal["left", "center", "right"] = "left"
    logo_offset_y: int = Field(default=0, ge=-20, le=20)

    @model_validator(mode="after")
    def _logo_is_complete(self) -> Self:
        if (self.logo_media_type is None) != (self.logo_base64 is None):
            raise ValueError("a logo needs both its media type and its bytes")
        if self.logo_base64 is not None:
            base64.b64decode(self.logo_base64, validate=True)
        return self

    @classmethod
    def create(
        cls,
        *,
        company_name: str,
        logo: bytes | None = None,
        logo_media_type: LogoMediaType | None = None,
        legal_name: str | None = None,
        tax_id: str | None = None,
        address: str | None = None,
        footer_text: str | None = None,
        accent_color: str | None = None,
        logo_scale: float = 1.0,
        logo_align: Literal["left", "center", "right"] = "left",
        logo_offset_y: int = 0,
    ) -> Self:
        """Branding from plain values; `logo` is the image's bytes."""
        return cls(
            company_name=company_name,
            legal_name=legal_name,
            tax_id=tax_id,
            address=address,
            footer_text=footer_text,
            accent_color=accent_color,
            logo_scale=logo_scale,
            logo_align=logo_align,
            logo_offset_y=logo_offset_y,
            logo_media_type=logo_media_type if logo is not None else None,
            logo_base64=base64.b64encode(logo).decode("ascii") if logo is not None else None,
        )

    @property
    def logo(self) -> bytes | None:
        return base64.b64decode(self.logo_base64) if self.logo_base64 is not None else None


def default_branding(company_name: str) -> DocumentBranding:
    """What exists today: the company's name, no logo (a text header)."""
    return DocumentBranding(company_name=company_name)


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
    # E.g. AEC-Q200. Optional: older approved documents have none.
    qualification: str | None = None


class RfqDocument(BaseModel):
    """Everything one supplier's PDF shows. Part of the approved send arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    language: str
    rfq_ref: str
    title: str
    issued_on: date
    buyer_company: str
    buyer_name: str
    # The addressee: this supplier only. Empty for the RFQ's own document (the one
    # the buyer downloads from the RFQ page), which names no supplier at all.
    supplier_name: str = ""
    reply_by: date | None = None
    currency: str | None = None
    payment_terms: str | None = None
    quote_validity_days: int | None = None
    lines: list[DocumentLine] = Field(min_length=1)
    asks: list[str] = Field(default_factory=list[str])
    # The buyer's own opening (their template's greeting, in the supplier's language),
    # above the items. Optional: older approved documents have none.
    intro: str | None = Field(default=None, max_length=4000)
    # The buyer company the RFQ goes out from.
    branding: DocumentBranding
    # The supplier's country, for its number format (es-MX, de-CH). Left out of the
    # approved arguments when unset, so documents approved before it hash the same.
    region: str | None = Field(default=None, max_length=2, exclude_if=lambda v: v is None)

    @property
    def locale(self) -> Locale:
        return Locale.of(self.language, self.region)


def _quantity(value: Decimal) -> str:
    """The quantity as stored in the approved arguments (plain digits): the staleness check
    compares these, so they never depend on the locale. The PDF shows them localized."""
    return format_quantity(value)


def _shown_quantity(stored: str, locale: Locale) -> str:
    try:
        return display_quantity(Decimal(stored), locale)
    except ArithmeticError:
        return stored


def build_document(
    data: RenderInput,
    *,
    supplier_name: str,
    currency: str | None,
    issued_on: date,
    branding: DocumentBranding | None = None,
) -> RfqDocument:
    """The document for one supplier, from the same input as that supplier's email.

    Without `branding`, the company's name only (no logo)."""
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
        region=data.region,
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
                qualification=line.qualification,
            )
            for line in data.lines
        ],
        asks=document_asks(data),
        intro=data.display.greeting.strip() or None,
        branding=branding or default_branding(data.buyer_company),
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
                line.qualification,
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
                    qualification=line.qualification,
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
    """E.g. `RFQ-3-metalurgica-horizonte-ltda.pdf`; `RFQ-3.pdf` when it names no supplier."""
    ref = re.sub(r"[^A-Za-z0-9-]+", "-", document.rfq_ref).strip("-") or "RFQ"
    if not document.supplier_name:
        return f"{ref}.pdf"
    return f"{ref}-{_slug(document.supplier_name)}.pdf"


# --- HTML + CSS -----------------------------------------------------------------------------


_CSS: Final = """
@page { size: A4; margin: 18mm 16mm 22mm 16mm;
  @bottom-left { content: element(page-footer); width: 80%; }
  @bottom-right { content: counter(page) " / " counter(pages); font-size: 7.5pt; color: #9aa1ad; } }
* { box-sizing: border-box; }
body { font-family: "Inter", "DejaVu Sans", "Liberation Sans", Arial, sans-serif;
  font-size: 9.5pt; color: #1f2430; line-height: 1.35; margin: 0; }
.header { display: flex; justify-content: space-between; align-items: center; gap: 18px; }
.header .logo-box { width: 240px; height: 80px; position: relative; }
.header .logo-box img { position: absolute; }
.header .company { font-size: 15pt; font-weight: 700; }
.header .doc-title { font-size: 17pt; font-weight: 400; text-align: right; }
.company-details { margin-top: 6px; font-size: 7.5pt; color: #6b7280; }
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
.band-inner { display: inline-block; background: #f3f4f6; border-radius: 4px;
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
.asks .note { margin: 0 0 8px 0; }
.asks ul + .note { margin-top: 10px; }
.intro { margin: 16px 0 0 0; white-space: pre-line; }
.reference { margin-top: 14px; color: #6b7280; font-size: 8.5pt; }
.page-footer { position: running(page-footer); font-size: 7.5pt; color: #9aa1ad; }
.page-footer .text { color: #6b7280; margin-bottom: 2px; }
"""


def _accent_css(accent: str | None) -> str:
    """The company's accent: title, header name and the date band (a light tint)."""
    if accent is None:
        return ""
    return (
        f".header .company, .header .doc-title {{ color: {accent}; }}"
        f".band-inner {{ background: {accent}14; border-left: 3px solid {accent}; }}"
    )


# The logo's reserved box at the top left: 240 x 80 px. At scale 1 the logo fits a
# 200 x 40 px frame (the size before placement existed); the buyer's scale multiplies
# that frame, always kept inside the box, and the logo is drawn as large as fits the frame
# (up or down, keeping its proportions). `align` places it across the box and `offset_y`
# nudges it from the vertical centre.
_LOGO_BOX: Final = (240, 80)
_LOGO_BASE: Final = (200, 40)


def logo_box_style(scale: float, offset_y: int) -> dict[str, float]:
    """The logo's frame in its box and its vertical nudge, in px: what the PDF applies and
    what the app's preview mirrors."""
    width = min(_LOGO_BOX[0], _LOGO_BASE[0] * scale)
    height = min(_LOGO_BOX[1], _LOGO_BASE[1] * scale)
    room = (_LOGO_BOX[1] - height) / 2
    return {"width": width, "height": height, "offset_y": max(-room, min(room, offset_y))}


def _logo_style(brand: DocumentBranding) -> str:
    frame = logo_box_style(brand.logo_scale, brand.logo_offset_y)
    left = {
        "left": "0px",
        "center": f"{(_LOGO_BOX[0] - frame['width']) / 2:.1f}px",
        "right": f"{_LOGO_BOX[0] - frame['width']:.1f}px",
    }[brand.logo_align]
    top = (_LOGO_BOX[1] - frame["height"]) / 2 + frame["offset_y"]
    return (
        f"left: {left}; top: {top:.1f}px; width: {frame['width']:.1f}px; "
        f"height: {frame['height']:.1f}px; object-fit: contain; "
        f"object-position: {brand.logo_align} center;"
    )


# The look other Bido documents share (the purchase order: `bido_back.awards.document`).
DOCUMENT_CSS: Final = _CSS


def accent_css(accent: str | None) -> str:
    return _accent_css(accent)


def logo_style(brand: DocumentBranding) -> str:
    return _logo_style(brand)


def _header(document: RfqDocument, words: dict[str, str]) -> str:
    brand = document.branding
    logo = brand.logo_base64
    left = (
        f'<div class="logo-box"><img src="data:{_e(brand.logo_media_type)};base64,{logo}" '
        f'alt="{_e(brand.company_name)}" style="{_logo_style(brand)}"></div>'
        if logo is not None
        else f'<span class="company">{_e(brand.company_name)}</span>'
    )
    details: list[str] = []
    if brand.legal_name and brand.legal_name != brand.company_name:
        details.append(brand.legal_name)
    if brand.tax_id:
        details.append(f"{words['tax_id']} {brand.tax_id}")
    if brand.address:
        details.append(brand.address)
    if logo is not None and not brand.legal_name:
        details.insert(0, brand.company_name)
    detail_html = f'<div class="company-details">{_e(" · ".join(details))}</div>' if details else ""
    return (
        f'<div class="header"><div>{left}{detail_html}</div>'
        f'<span class="doc-title">{_e(words["title"])}</span></div>'
    )


def _footer(document: RfqDocument, words: dict[str, str]) -> str:
    brand = document.branding
    text = f'<div class="text">{_e(brand.footer_text)}</div>' if brand.footer_text else ""
    return (
        f'<div class="page-footer">{text}{_e(document.rfq_ref)} · {_e(brand.company_name)}'
        f" · {_e(words['sent_via'])}</div>"
    )


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

    parties = [f'<div class="rfq-title">{_e(document.title)}</div>']
    if document.supplier_name:
        parties.append(
            f'<div class="pair"><div class="eyebrow">{_e(words["to"])}</div>'
            f'<div class="value">{_e(document.supplier_name)}</div></div>'
        )
    parties.append(
        f'<div class="pair"><div class="eyebrow">{_e(words["from"])}</div>'
        f'<div class="value">{_e(_from_line(document))}</div></div>'
    )
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
        + (
            f'<div><div class="eyebrow">{_e(words["need_by"])}</div>'
            f'<div class="value">{_e(need_text)}</div></div>'
            if need_dates
            else ""
        )
        + "</div></div>"
    )

    rows: list[str] = []
    locale = document.locale
    for line in document.lines:
        quantities = " / ".join(_shown_quantity(q, locale) for q in line.quantities)
        meta_bits: list[str] = []
        if line.part_number:
            meta_bits.append(f"{words['part_no']} {line.part_number}")
        if line.mpn and line.mpn != line.part_number:
            meta_bits.append(f"{words['mpn']} {line.mpn}")
        if line.manufacturer:
            meta_bits.append(line.manufacturer)
        if line.qualification:
            meta_bits.append(f"{words['qualification']} {line.qualification}")
        meta_line = f'<div class="meta-line">{_e(" · ".join(meta_bits))}</div>' if meta_bits else ""
        rows.append(
            "<tr>"
            f'<td class="desc">{_e(line.position)}. {_e(line.description)}{meta_line}</td>'
            f'<td class="num">{_e(quantities)}</td>'
            f'<td class="num">{_e(unit_label(line.unit, locale))}</td>'
            + (f'<td class="num">{_e(_date(line.need_by, lang))}</td>' if need_dates else "")
            + "</tr>"
        )
    items = (
        '<table class="items"><colgroup><col><col class="col-qty"><col class="col-unit">'
        + ('<col class="col-need">' if need_dates else "")
        + "</colgroup><thead><tr>"
        f'<th>{_e(words["item"])}</th><th class="num">{_e(words["qty"])}</th>'
        f'<th class="num">{_e(words["unit"])}</th>'
        + (f'<th class="num">{_e(words["need_by"])}</th>' if need_dates else "")
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
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

    said = phrases(lang)
    tiered = any(len(line.quantities) > 1 for line in document.lines)
    price = said["prices"] if tiered else said["price"]
    asks_html = (
        f'<div class="asks"><p class="note">{_e(price)}</p>'
        + (
            f'<div class="eyebrow">{_e(words["include"])}</div><ul>'
            + "".join(f"<li>{_e(ask)}</li>" for ask in document.asks)
            + "</ul>"
            if document.asks
            else ""
        )
        + f'<p class="note">{_e(said["alternative"])}</p></div>'
    )
    intro = f'<p class="intro">{_e(document.intro)}</p>' if document.intro else ""
    reference_text = words["reference"].format(ref=document.rfq_ref)
    reference = f'<div class="reference">{_e(reference_text)}</div>'

    return (
        f'<!doctype html><html lang="{_e(lang)}"><head><meta charset="utf-8">'
        f"<title>{_e(document.rfq_ref)}</title>"
        f"<style>{_CSS}{_accent_css(document.branding.accent_color)}</style></head><body>"
        + _footer(document, words)
        + _header(document, words)
        + '<div class="parties"><div class="left">'
        + "".join(parties)
        + f'</div><div class="meta"><table>{meta}</table></div></div>'
        + band
        + intro
        + items
        + terms
        + asks_html
        + reference
        + "</body></html>"
    )


_BLOCK_END: Final = re.compile(r"</(?:div|p|li|tr|td|th|span)>|<br\s*/?>")
_TAG: Final = re.compile(r"<[^>]+>")


def document_text(document: RfqDocument) -> str:
    """What the PDF says, as plain text (one block per line): for the checks and the evals."""
    markup = document_html(document)
    body = markup[markup.index("<body>") + len("<body>") :]
    text = _TAG.sub("", _BLOCK_END.sub("\n", body))
    lines = (" ".join(html.unescape(line).split()) for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def _line_values(key: str, line: DocumentLine, document: RfqDocument) -> list[str]:
    """The line's data a required item field stands for, as the PDF prints it (quantities
    and units in the supplier's locale)."""
    match key:
        case "mpn":
            return [v for v in (line.part_number, line.mpn) if v]
        case "manufacturer":
            return [line.manufacturer] if line.manufacturer else []
        case "quantity_tiers":
            return [_shown_quantity(q, document.locale) for q in line.quantities]
        case "unit":
            return [unit_label(line.unit, document.locale)]
        case "need_by":
            return [format_date(line.need_by, document.language)] if line.need_by else []
        case "incoterm_place":
            return [v for v in (line.incoterm, line.delivery_location) if v]
        case "qualification":
            return [line.qualification] if line.qualification else []
        case _:
            raise ValueError(f"no document check for required field {key}")


def _electronic(lines: Sequence[DocumentLine]) -> bool:
    """`bido_back.templates.render.electronic` on the document's lines."""
    return any(
        (line.mpn and line.mpn != line.part_number) or line.manufacturer or line.qualification
        for line in lines
    )


def document_problems(document: RfqDocument, template_labels: Sequence[str] = ()) -> list[str]:
    """Required fields (`bido_back.templates.required`) the PDF does not carry, as codes.

    Item fields: every line's data is printed. Ask fields: the PDF asks for each
    one (its questions, or the template's own labels behind them, given in the
    template's language as `template_labels`); RoHS/REACH only for electronic
    parts, the qualification only when a line names one. Empty means the PDF
    carries everything the full email would.
    """
    text = document_text(document)
    problems: list[str] = []
    for field in REQUIRED_FIELDS:
        if field.kind != "item":
            continue
        for line in document.lines:
            for value in _line_values(field.key, line, document):
                if " ".join(value.split()) not in text:
                    problems.append(f"{field.key}_missing:{line.position}")
                    break
    covered = covered_keys([*template_labels, *document.asks])
    for field in REQUIRED_FIELDS:
        if field.kind != "ask" or field.key in covered:
            continue
        if field.key == "rohs_reach" and not _electronic(document.lines):
            continue
        problems.append(f"ask_missing:{field.key}")
    if any(line.qualification for line in document.lines) and "qualification" not in covered:
        problems.append("ask_missing:qualification")
    if any(" ".join(ask.split()) not in text for ask in document.asks):
        problems.append("ask_not_shown")
    return problems


def _from_line(document: RfqDocument) -> str:
    name, company = document.buyer_name.strip(), document.branding.company_name.strip()
    if not name or name.casefold() == company.casefold():
        return company
    return f"{name} · {company}" if company else name


def _fetcher() -> URLFetcher:
    """Only `data:` URIs (the embedded company logo): nothing from the network or the disk."""
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
