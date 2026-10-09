"""The purchase order as a document: .xlsx and printable HTML, plus its email text.

Rendered by code from the stored PO rows only; no model writes a purchase
order. The HTML is self-contained (inline CSS, every value escaped) so a
browser can print it to PDF; there is no PDF library on purpose.

`content_hash` covers exactly what the document shows. The send approval
carries it, and the worker refuses to send a PO whose rows changed since.
"""

import html
import io
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final, cast

from pydantic import JsonValue

from bido_back.agents.events import args_hash
from bido_back.exports.render import safe_text
from bido_back.templates.signature import signature

XLSX_MEDIA_TYPE: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
HTML_MEDIA_TYPE: Final = "text/html; charset=utf-8"


@dataclass(frozen=True, slots=True)
class PoLine:
    position: int
    description: str
    mpn: str | None
    quantity: Decimal
    unit: str
    unit_price: Decimal
    line_total: Decimal
    need_by: date | None
    lead_time_days: int | None


@dataclass(frozen=True, slots=True)
class PoDocument:
    ref: str
    rfq_ref: str
    issued_on: date
    buyer_company: str
    supplier_name: str
    currency: str
    incoterm: str | None
    payment_terms: str | None
    total: Decimal
    lines: tuple[PoLine, ...]


def line_total(quantity: Decimal, unit_price: Decimal) -> Decimal:
    return (quantity * unit_price).quantize(Decimal("0.01"))


def _num(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if text != "-0" else "0"


def content_hash(doc: PoDocument) -> str:
    """sha256 of the document's canonical JSON: what the supplier will receive."""
    payload: dict[str, JsonValue] = {
        "ref": doc.ref,
        "rfq_ref": doc.rfq_ref,
        "supplier_name": doc.supplier_name,
        "currency": doc.currency,
        "incoterm": doc.incoterm,
        "payment_terms": doc.payment_terms,
        "total": _num(doc.total),
        "lines": [
            {
                "position": line.position,
                "description": line.description,
                "mpn": line.mpn,
                "quantity": _num(line.quantity),
                "unit": line.unit,
                "unit_price": _num(line.unit_price),
                "line_total": _num(line.line_total),
                "need_by": line.need_by.isoformat() if line.need_by else None,
            }
            for line in doc.lines
        ],
    }
    return args_hash(payload)


_HEADER: Final = (
    "Pos",
    "Description",
    "MPN",
    "Quantity",
    "Unit",
    "Unit price",
    "Currency",
    "Line total",
    "Need by",
    "Lead time (days)",
)


def to_xlsx(doc: PoDocument) -> bytes:
    from openpyxl import Workbook  # pyright: ignore[reportMissingTypeStubs]

    workbook = cast(Any, Workbook())
    sheet = workbook.active
    sheet.title = doc.ref[:31]
    meta: list[tuple[str, str | Decimal | date | None]] = [
        ("Purchase order", doc.ref),
        ("Issued on", doc.issued_on),
        ("Buyer", safe_text(doc.buyer_company)),
        ("Supplier", safe_text(doc.supplier_name)),
        ("Quotation", doc.rfq_ref),
        ("Currency", doc.currency),
        ("Incoterm", safe_text(doc.incoterm) if doc.incoterm else None),
        ("Payment terms", safe_text(doc.payment_terms) if doc.payment_terms else None),
    ]
    for label, value in meta:
        sheet.append([label, value])
    sheet.append([])
    sheet.append(list(_HEADER))
    for line in doc.lines:
        sheet.append(
            [
                line.position,
                safe_text(line.description),
                safe_text(line.mpn) if line.mpn else None,
                line.quantity,
                safe_text(line.unit),
                line.unit_price,
                doc.currency,
                line.line_total,
                line.need_by,
                line.lead_time_days,
            ]
        )
    sheet.append([])
    sheet.append(["Total", None, None, None, None, None, doc.currency, doc.total])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def to_html(doc: PoDocument) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{line.position}</td><td>{_e(line.description)}</td><td>{_e(line.mpn)}</td>"
        f"<td class=n>{_num(line.quantity)}</td><td>{_e(line.unit)}</td>"
        f"<td class=n>{_num(line.unit_price)}</td><td class=n>{line.line_total:.2f}</td>"
        f"<td>{_e(line.need_by)}</td><td class=n>{_e(line.lead_time_days)}</td>"
        "</tr>"
        for line in doc.lines
    )
    terms = "".join(
        f"<dt>{label}</dt><dd>{_e(value)}</dd>"
        for label, value in (
            ("Quotation", doc.rfq_ref),
            ("Currency", doc.currency),
            ("Incoterm", doc.incoterm),
            ("Payment terms", doc.payment_terms),
        )
        if value
    )
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        f"<title>{_e(doc.ref)}</title><style>"
        "body{font:13px/1.4 system-ui,sans-serif;margin:32px;color:#111}"
        "h1{font-size:20px;margin:0 0 4px}table{border-collapse:collapse;width:100%;margin:16px 0}"
        "th,td{border:1px solid #ccc;padding:4px 6px;text-align:left}td.n{text-align:right}"
        "dl{display:grid;grid-template-columns:max-content 1fr;gap:2px 12px}dt{color:#555}"
        "</style></head><body>"
        f"<h1>Purchase order {_e(doc.ref)}</h1>"
        f"<p>{_e(doc.buyer_company)} &rarr; {_e(doc.supplier_name)} &middot; "
        f"issued {_e(doc.issued_on)}</p><dl>{terms}</dl>"
        "<table><thead><tr><th>Pos</th><th>Description</th><th>MPN</th><th>Qty</th>"
        f"<th>Unit</th><th>Unit price ({_e(doc.currency)})</th><th>Line total</th>"
        f"<th>Need by</th><th>Lead time (days)</th></tr></thead><tbody>{rows}</tbody>"
        f"<tfoot><tr><th colspan=6>Total {_e(doc.currency)}</th>"
        f"<th class=n>{doc.total:.2f}</th><th colspan=2></th></tr></tfoot></table>"
        "</body></html>"
    )


# --- The email that carries the PO ----------------------------------------------------

_TEXT: Final[dict[str, tuple[str, str, str, str]]] = {
    # greeting, intro, ask, sign-off
    "en": (
        "Dear {supplier},",
        "Following your offer on {rfq_ref}, please find attached our purchase order {po_ref}.",
        "Please confirm the order and the delivery dates by replying to this email.",
        "Kind regards,",
    ),
    "de": (
        "Sehr geehrte Damen und Herren von {supplier},",
        "bezugnehmend auf Ihr Angebot zu {rfq_ref} erhalten Sie anbei unsere Bestellung {po_ref}.",
        "Bitte bestätigen Sie die Bestellung und die Liefertermine per Antwort auf diese E-Mail.",
        "Mit freundlichen Grüßen",
    ),
    "fr": (
        "Madame, Monsieur,",
        "Suite à votre offre pour {rfq_ref}, veuillez trouver ci-joint notre bon de commande "
        "{po_ref}.",
        "Merci de confirmer la commande et les dates de livraison en répondant à cet e-mail.",
        "Cordialement,",
    ),
    "it": (
        "Gentili {supplier},",
        "in riferimento alla vostra offerta per {rfq_ref}, in allegato trovate il nostro ordine "
        "di acquisto {po_ref}.",
        "Vi preghiamo di confermare l'ordine e le date di consegna rispondendo a questa e-mail.",
        "Cordiali saluti,",
    ),
    "es": (
        "Estimados {supplier}:",
        "En relación con su oferta para {rfq_ref}, adjuntamos nuestra orden de compra {po_ref}.",
        "Les rogamos confirmen el pedido y las fechas de entrega respondiendo a este correo.",
        "Saludos cordiales,",
    ),
    "pt": (
        "Prezados {supplier},",
        "Com base na proposta de vocês para {rfq_ref}, segue em anexo nosso pedido de compra "
        "{po_ref}.",
        "Por favor, confirmem o pedido e as datas de entrega respondendo a este e-mail.",
        "Atenciosamente,",
    ),
}


def po_email_body(doc: PoDocument, language: str, buyer_name: str) -> str:
    """The plain-text email that carries the PO, in the supplier's language (else English)."""
    greeting, intro, ask, sign_off = _TEXT.get(language.lower()[:2], _TEXT["en"])
    values = {"supplier": doc.supplier_name, "rfq_ref": doc.rfq_ref, "po_ref": doc.ref}
    lines = "\n".join(
        f"{line.position}. {line.description}"
        + (f" ({line.mpn})" if line.mpn else "")
        + f": {_num(line.quantity)} {line.unit} x {_num(line.unit_price)} {doc.currency}"
        + f" = {line.line_total:.2f} {doc.currency}"
        + (f", {line.need_by.isoformat()}" if line.need_by else "")
        for line in doc.lines
    )
    terms = " · ".join(
        part
        for part in (
            f"Total: {doc.total:.2f} {doc.currency}",
            f"Incoterm: {doc.incoterm}" if doc.incoterm else "",
            f"Payment: {doc.payment_terms}" if doc.payment_terms else "",
        )
        if part
    )
    closing = "\n".join([sign_off, *signature(buyer_name, doc.buyer_company)])
    return (
        f"{greeting.format(**values)}\n\n{intro.format(**values)}\n\n{lines}\n{terms}\n\n"
        f"{ask}\n\n{closing}"
    )
