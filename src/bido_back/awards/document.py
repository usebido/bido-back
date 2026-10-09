"""The purchase order as the supplier receives it: a PDF in the supplier's language.

Rendered by code from the stored PO rows (`bido_back.awards.orders.po_document`),
with the look of the RFQ document (`bido_back.rfqs.document`): the buyer company's
header (logo, legal name, tax id, address), the supplier it is addressed to, the
offer it accepts, every line (the buyer's part number, MPN, quantity, unit, unit
price, line total, delivery date and place), the terms (Incoterm, payment,
currency, total), who approved it, and the request to confirm it quoting the PO
number. Every value is escaped; WeasyPrint fetches nothing but the embedded logo.

What the PDF shows is what `render.content_hash` covers: the send approval holds
that hash, and a PO whose document changed is not sent.
"""

import html
import re
from datetime import date
from decimal import Decimal
from typing import Final

from bido_back.awards.render import PoDocument, deliver_to
from bido_back.rfqs.document import (
    DOCUMENT_CSS,
    accent_css,
    labels,
    logo_style,
    render_html_pdf,
)
from bido_back.templates.render import format_date

PDF_MEDIA_TYPE: Final = "application/pdf"

_WORDS: Final[dict[str, dict[str, str]]] = {
    "en": {
        "title": "Purchase order",
        "supplier": "Supplier",
        "attention": "Attn.",
        "buyer": "Buyer",
        "offer": "Your offer",
        "offer_value": "{rfq_ref}, version {version} of {date}",
        "offer_short": "{rfq_ref}",
        "price": "Unit price",
        "line_total": "Total",
        "delivery": "Delivery",
        "lead": "{days} days after order",
        "total": "Order total",
        "approved_by": "Approved by",
        "confirm": "Please confirm this order, its prices and delivery dates by replying to "
        "this email. Quote {ref} on your order confirmation, delivery note and invoice.",
    },
    "pt": {
        "title": "Pedido de compra",
        "supplier": "Fornecedor",
        "attention": "A/C",
        "buyer": "Comprador",
        "offer": "Sua proposta",
        "offer_value": "{rfq_ref}, versão {version} de {date}",
        "offer_short": "{rfq_ref}",
        "price": "Preço unit.",
        "line_total": "Total",
        "delivery": "Entrega",
        "lead": "{days} dias após o pedido",
        "total": "Total do pedido",
        "approved_by": "Aprovado por",
        "confirm": "Por favor, confirmem este pedido, os preços e as datas de entrega "
        "respondendo a este e-mail. Informem {ref} na confirmação, na nota fiscal e no "
        "romaneio.",
    },
    "de": {
        "title": "Bestellung",
        "supplier": "Lieferant",
        "attention": "z. Hd.",
        "buyer": "Besteller",
        "offer": "Ihr Angebot",
        "offer_value": "{rfq_ref}, Version {version} vom {date}",
        "offer_short": "{rfq_ref}",
        "price": "Einzelpreis",
        "line_total": "Gesamt",
        "delivery": "Liefertermin",
        "lead": "{days} Tage nach Bestellung",
        "total": "Bestellsumme",
        "approved_by": "Freigegeben von",
        "confirm": "Bitte bestätigen Sie diese Bestellung, die Preise und Liefertermine per "
        "Antwort auf diese E-Mail. Geben Sie {ref} auf Auftragsbestätigung, Lieferschein und "
        "Rechnung an.",
    },
    "fr": {
        "title": "Bon de commande",
        "supplier": "Fournisseur",
        "attention": "À l'attention de",
        "buyer": "Acheteur",
        "offer": "Votre offre",
        "offer_value": "{rfq_ref}, version {version} du {date}",
        "offer_short": "{rfq_ref}",
        "price": "Prix unitaire",
        "line_total": "Total",
        "delivery": "Livraison",
        "lead": "{days} jours après commande",
        "total": "Total de la commande",
        "approved_by": "Approuvé par",
        "confirm": "Merci de confirmer cette commande, ses prix et ses dates de livraison en "
        "répondant à cet e-mail. Indiquez {ref} sur l'accusé de réception, le bon de livraison "
        "et la facture.",
    },
    "es": {
        "title": "Orden de compra",
        "supplier": "Proveedor",
        "attention": "Atención",
        "buyer": "Comprador",
        "offer": "Su oferta",
        "offer_value": "{rfq_ref}, versión {version} del {date}",
        "offer_short": "{rfq_ref}",
        "price": "Precio unit.",
        "line_total": "Total",
        "delivery": "Entrega",
        "lead": "{days} días tras el pedido",
        "total": "Total del pedido",
        "approved_by": "Aprobado por",
        "confirm": "Les rogamos confirmen este pedido, sus precios y fechas de entrega "
        "respondiendo a este correo. Indiquen {ref} en la confirmación, el albarán y la "
        "factura.",
    },
    "it": {
        "title": "Ordine di acquisto",
        "supplier": "Fornitore",
        "attention": "Alla c.a.",
        "buyer": "Acquirente",
        "offer": "La vostra offerta",
        "offer_value": "{rfq_ref}, versione {version} del {date}",
        "offer_short": "{rfq_ref}",
        "price": "Prezzo unit.",
        "line_total": "Totale",
        "delivery": "Consegna",
        "lead": "{days} giorni dall'ordine",
        "total": "Totale ordine",
        "approved_by": "Approvato da",
        "confirm": "Vi preghiamo di confermare questo ordine, i prezzi e le date di consegna "
        "rispondendo a questa e-mail. Indicate {ref} su conferma d'ordine, DDT e fattura.",
    },
}

_PO_CSS: Final = """
.col-qty { width: 11%; } .col-unit { width: 8%; } .col-price { width: 13%; }
.col-total { width: 14%; } .col-need { width: 15%; }
.totals { margin-top: 10px; display: flex; justify-content: flex-end; }
.totals table { border-collapse: collapse; }
.totals td { padding: 2px 0 2px 18px; text-align: right; }
.totals td.v { font-size: 11pt; font-weight: 700; }
.approval { margin-top: 16px; font-size: 8.5pt; color: #6b7280; }
"""


def language_of(doc: PoDocument) -> str:
    code = doc.language.lower()[:2]
    return code if code in _WORDS else "en"


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _num(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if text != "-0" else "0"


def _decimal(value: Decimal, language: str) -> str:
    """A quantity or unit price as the supplier writes it (decimal comma where usual)."""
    text = _num(value)
    return text.replace(".", ",") if language in _COMMA else text


_COMMA: Final = frozenset({"pt", "de", "es", "it", "fr"})


def _money(value: Decimal, language: str) -> str:
    text = f"{value:,.2f}"
    if language in _COMMA:
        text = text.replace(",", "\0").replace(".", ",").replace("\0", ".")
    return text


def _date(value: date | None, language: str) -> str:
    return format_date(value, language) if value is not None else "—"


def _header(doc: PoDocument, title: str, tax_label: str) -> str:
    brand = doc.branding
    if brand is None:
        return (
            f'<div class="header"><div><span class="company">{_e(doc.buyer_company)}</span>'
            f'</div><span class="doc-title">{_e(title)}</span></div>'
        )
    logo = brand.logo_base64
    left = (
        f'<div class="logo-box"><img src="data:{_e(brand.logo_media_type)};base64,{logo}" '
        f'alt="{_e(brand.company_name)}" style="{logo_style(brand)}"></div>'
        if logo is not None
        else f'<span class="company">{_e(brand.company_name)}</span>'
    )
    details: list[str] = []
    if brand.legal_name and brand.legal_name != brand.company_name:
        details.append(brand.legal_name)
    if brand.tax_id:
        details.append(f"{tax_label} {brand.tax_id}")
    if brand.address:
        details.append(brand.address)
    if logo is not None and not brand.legal_name:
        details.insert(0, brand.company_name)
    detail_html = f'<div class="company-details">{_e(" · ".join(details))}</div>' if details else ""
    return (
        f'<div class="header"><div>{left}{detail_html}</div>'
        f'<span class="doc-title">{_e(title)}</span></div>'
    )


def po_document_html(doc: PoDocument) -> str:
    """The PO as HTML (every value escaped), ready for WeasyPrint."""
    lang = language_of(doc)
    words = _WORDS[lang]
    shared = labels(lang)
    places = deliver_to(doc)
    dated = any(line.need_by or line.lead_time_days is not None for line in doc.lines)

    offer = (
        words["offer_value"].format(
            rfq_ref=doc.rfq_ref,
            version=doc.quote_version,
            date=_date(doc.quote_date, lang),
        )
        if doc.quote_version is not None and doc.quote_date is not None
        else words["offer_short"].format(rfq_ref=doc.rfq_ref)
    )
    supplier = _e(doc.supplier_name) + (
        f'<div class="meta-line">{_e(words["attention"])} {_e(doc.supplier_contact)}</div>'
        if doc.supplier_contact
        else ""
    )
    parties = [
        f'<div class="pair"><div class="eyebrow">{_e(words["supplier"])}</div>'
        f'<div class="value">{supplier}</div></div>',
        f'<div class="pair"><div class="eyebrow">{_e(words["offer"])}</div>'
        f'<div class="value">{_e(offer)}</div></div>',
    ]
    if places:
        parties.append(
            f'<div class="pair"><div class="eyebrow">{_e(shared["deliver_to"])}</div>'
            f'<div class="value">{_e(places)}</div></div>'
        )
    meta = "".join(
        f'<tr><td>{_e(label)}</td><td class="v">{_e(value)}</td></tr>'
        for label, value in (
            (shared["number"], doc.ref),
            (shared["date"], _date(doc.issued_on, lang)),
        )
    )

    rows: list[str] = []
    for line in doc.lines:
        bits: list[str] = []
        if line.part_number:
            bits.append(f"{shared['part_no']} {line.part_number}")
        if line.mpn and line.mpn != line.part_number:
            bits.append(f"{shared['mpn']} {line.mpn}")
        if line.delivery_location and len({ln.delivery_location for ln in doc.lines}) > 1:
            bits.append(f"{shared['deliver_to']} {line.delivery_location}")
        meta_line = f'<div class="meta-line">{_e(" · ".join(bits))}</div>' if bits else ""
        when = (
            _date(line.need_by, lang)
            if line.need_by
            else words["lead"].format(days=line.lead_time_days)
            if line.lead_time_days is not None
            else "—"
        )
        rows.append(
            "<tr>"
            f'<td class="desc">{_e(line.position)}. {_e(line.description)}{meta_line}</td>'
            f'<td class="num">{_e(_decimal(line.quantity, lang))}</td>'
            f'<td class="num">{_e(line.unit)}</td>'
            f'<td class="num">{_e(_decimal(line.unit_price, lang))}</td>'
            f'<td class="num">{_e(_money(line.line_total, lang))}</td>'
            + (f'<td class="num">{_e(when)}</td>' if dated else "")
            + "</tr>"
        )
    items = (
        '<table class="items"><colgroup><col><col class="col-qty"><col class="col-unit">'
        '<col class="col-price"><col class="col-total">'
        + ('<col class="col-need">' if dated else "")
        + "</colgroup><thead><tr>"
        f'<th>{_e(shared["item"])}</th><th class="num">{_e(shared["qty"])}</th>'
        f'<th class="num">{_e(shared["unit"])}</th>'
        f'<th class="num">{_e(words["price"])} ({_e(doc.currency)})</th>'
        f'<th class="num">{_e(words["line_total"])} ({_e(doc.currency)})</th>'
        + (f'<th class="num">{_e(words["delivery"])}</th>' if dated else "")
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )
    totals = (
        '<div class="totals"><table><tr>'
        f"<td>{_e(words['total'])}</td>"
        f'<td class="v">{_e(_money(doc.total, lang))} {_e(doc.currency)}</td>'
        "</tr></table></div>"
    )
    term_rows = [
        (label, value)
        for label, value in (
            (shared["incoterm"], doc.incoterm),
            (shared["payment_terms"], doc.payment_terms),
            (shared["currency"], doc.currency),
        )
        if value
    ]
    terms = (
        '<div class="terms"><table>'
        + "".join(
            f'<tr><td>{_e(label)}</td><td class="v">{_e(value)}</td></tr>'
            for label, value in term_rows
        )
        + "</table></div>"
    )
    approval = (
        f'<div class="approval">{_e(words["approved_by"])}: {_e(doc.approved_by)}</div>'
        if doc.approved_by
        else ""
    )
    confirm = f'<div class="reference">{_e(words["confirm"].format(ref=doc.ref))}</div>'
    footer_text = doc.branding.footer_text if doc.branding else None
    footer = (
        '<div class="page-footer">'
        + (f'<div class="text">{_e(footer_text)}</div>' if footer_text else "")
        + f"{_e(doc.ref)} · {_e(doc.buyer_company)} · {_e(shared['sent_via'])}</div>"
    )
    accent = doc.branding.accent_color if doc.branding else None
    return (
        f'<!doctype html><html lang="{_e(lang)}"><head><meta charset="utf-8">'
        f"<title>{_e(doc.ref)}</title>"
        f"<style>{DOCUMENT_CSS}{_PO_CSS}{accent_css(accent)}</style></head><body>"
        + footer
        + _header(doc, words["title"], shared["tax_id"])
        + '<div class="parties"><div class="left">'
        + "".join(parties)
        + f'</div><div class="meta"><table>{meta}</table></div></div>'
        + items
        + totals
        + terms
        + approval
        + confirm
        + "</body></html>"
    )


_BLOCK_END: Final = re.compile(r"</(?:div|p|li|tr|td|th|span)>|<br\s*/?>")
_TAG: Final = re.compile(r"<[^>]+>")


def po_document_text(doc: PoDocument) -> str:
    """What the PDF says, as plain text: for the checks and the tests."""
    markup = po_document_html(doc)
    body = markup[markup.index("<body>") + len("<body>") :]
    text = html.unescape(_TAG.sub("", _BLOCK_END.sub("\n", body)))
    return "\n".join(part.strip() for part in text.splitlines() if part.strip())


def po_pdf(doc: PoDocument) -> bytes:
    return render_html_pdf(po_document_html(doc))


def po_pdf_filename(doc: PoDocument) -> str:
    return f"{re.sub(r'[^A-Za-z0-9-]+', '-', doc.ref).strip('-') or 'PO'}.pdf"
