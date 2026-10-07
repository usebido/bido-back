"""Render the RFQ email for one supplier from a template version and the RFQ lines.

Deterministic: code builds the subject, the item table (plain text and HTML)
and the closing; no model writes any part of it. The buyer's own text (the
greeting, column headings, footer fields) is used as written, or as
translated into the supplier's language by the email writer agent
(`bido_back.templates.localize`); everything else comes from fixed phrases
per language below (English when the supplier's language has none).

The table follows the template's columns. A column whose heading names line
data (MPN, quantity, unit, ...) is filled from the RFQ; any other column
(e.g. "Preço unitário", "Lead time") is left blank for the supplier to fill.
Required line data the template has no column for is appended, so every
requested MPN, quantity tier and unit is always in the email. Footer fields
are asked after the table, followed by the required sector questions the
footer does not cover (see `bido_back.templates.required`).

Matching headings to line data uses the template's ORIGINAL labels (the
language the synonyms are written for); the email shows the localized ones.
"""

import html
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from bido_back.onboarding.standard import TemplateContent
from bido_back.templates.required import (
    REQUIRED_FIELDS,
    covered_keys,
    label_matches,
    normalize,
    required_field,
)

# --- Fixed phrases --------------------------------------------------------------------

_PHRASES: Final[dict[str, dict[str, str]]] = {
    "en": {
        "subject": "Request for quotation",
        "reference": "Our reference: {ref}. Please keep it in the subject of your reply.",
        "items": "Requested items:",
        "prices": "Please quote the unit price for each quantity tier, with the currency.",
        "asks": "Please also let us know:",
        "alternative": (
            "If you offer an alternative part, please say so clearly and give its MPN "
            "and manufacturer."
        ),
        "due": "Please send your quote by {date}.",
        "closing": "Best regards,",
    },
    "pt": {
        "subject": "Solicitação de cotação",
        "reference": "Nossa referência: {ref}. Por favor, mantenha-a no assunto da resposta.",
        "items": "Itens solicitados:",
        "prices": "Por favor, informe o preço unitário para cada quantidade, com a moeda.",
        "asks": "Por favor, informe também:",
        "alternative": (
            "Se oferecer um item alternativo, indique claramente, com o código e o fabricante."
        ),
        "due": "Por favor, envie sua proposta até {date}.",
        "closing": "Atenciosamente,",
    },
    "de": {
        "subject": "Angebotsanfrage",
        "reference": (
            "Unsere Referenz: {ref}. Bitte behalten Sie sie im Betreff Ihrer Antwort bei."
        ),
        "items": "Angefragte Positionen:",
        "prices": "Bitte nennen Sie den Stückpreis je Mengenstaffel mit Währung.",
        "asks": "Bitte teilen Sie uns außerdem mit:",
        "alternative": (
            "Falls Sie einen alternativen Artikel anbieten, kennzeichnen Sie ihn bitte "
            "eindeutig und nennen Sie MPN und Hersteller."
        ),
        "due": "Bitte senden Sie Ihr Angebot bis {date}.",
        "closing": "Mit freundlichen Grüßen",
    },
    "fr": {
        "subject": "Demande de prix",
        "reference": (
            "Notre référence : {ref}. Merci de la conserver dans l'objet de votre réponse."
        ),
        "items": "Articles demandés :",
        "prices": "Merci d'indiquer le prix unitaire pour chaque quantité, avec la devise.",
        "asks": "Merci de nous indiquer également :",
        "alternative": (
            "Si vous proposez un article alternatif, merci de l'indiquer clairement avec "
            "sa référence et son fabricant."
        ),
        "due": "Merci de nous envoyer votre offre avant le {date}.",
        "closing": "Cordialement,",
    },
    "es": {
        "subject": "Solicitud de cotización",
        "reference": (
            "Nuestra referencia: {ref}. Por favor, manténgala en el asunto de su respuesta."
        ),
        "items": "Artículos solicitados:",
        "prices": "Por favor, indique el precio unitario para cada cantidad, con la moneda.",
        "asks": "Por favor, indíquenos también:",
        "alternative": (
            "Si ofrece una pieza alternativa, indíquelo claramente con su número de pieza "
            "y fabricante."
        ),
        "due": "Por favor, envíe su cotización antes del {date}.",
        "closing": "Saludos cordiales,",
    },
    "it": {
        "subject": "Richiesta di offerta",
        "reference": (
            "Nostro riferimento: {ref}. Vi preghiamo di mantenerlo nell'oggetto della risposta."
        ),
        "items": "Articoli richiesti:",
        "prices": "Vi preghiamo di indicare il prezzo unitario per ogni quantità, con la valuta.",
        "asks": "Vi preghiamo di indicarci inoltre:",
        "alternative": (
            "Se offrite un articolo alternativo, indicatelo chiaramente con codice e produttore."
        ),
        "due": "Vi preghiamo di inviare l'offerta entro il {date}.",
        "closing": "Cordiali saluti,",
    },
}

# Short headings for the line-data columns the render adds.
_HEADINGS: Final[dict[str, dict[str, str]]] = {
    "en": {
        "position": "Pos.",
        "description": "Description",
        "mpn": "MPN",
        "manufacturer": "Manufacturer",
        "quantity": "Quantity",
        "unit": "Unit",
        "need_by": "Need by",
        "qualification": "Qualification",
        "incoterm": "Incoterm",
    },
    "pt": {
        "position": "Item",
        "description": "Descrição",
        "mpn": "Código (MPN)",
        "manufacturer": "Fabricante",
        "quantity": "Quantidade",
        "unit": "Un.",
        "need_by": "Data de entrega",
        "qualification": "Qualificação",
        "incoterm": "Incoterm",
    },
    "de": {
        "position": "Pos.",
        "description": "Bezeichnung",
        "mpn": "MPN",
        "manufacturer": "Hersteller",
        "quantity": "Menge",
        "unit": "Einheit",
        "need_by": "Liefertermin",
        "qualification": "Qualifikation",
        "incoterm": "Incoterm",
    },
    "fr": {
        "position": "Pos.",
        "description": "Désignation",
        "mpn": "Réf. fabricant",
        "manufacturer": "Fabricant",
        "quantity": "Quantité",
        "unit": "Unité",
        "need_by": "Date souhaitée",
        "qualification": "Qualification",
        "incoterm": "Incoterm",
    },
    "es": {
        "position": "Pos.",
        "description": "Descripción",
        "mpn": "MPN",
        "manufacturer": "Fabricante",
        "quantity": "Cantidad",
        "unit": "Unidad",
        "need_by": "Fecha requerida",
        "qualification": "Calificación",
        "incoterm": "Incoterm",
    },
    "it": {
        "position": "Pos.",
        "description": "Descrizione",
        "mpn": "Codice produttore",
        "manufacturer": "Produttore",
        "quantity": "Quantità",
        "unit": "Unità",
        "need_by": "Data richiesta",
        "qualification": "Qualifica",
        "incoterm": "Incoterm",
    },
}

# Which template headings carry which line data. Checked in this order, so
# "Item e descrição" is the description, not the position.
_COLUMN_KEYS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("mpn", required_field("mpn").synonyms),
    ("manufacturer", required_field("manufacturer").synonyms),
    ("quantity", required_field("quantity_tiers").synonyms),
    ("unit", required_field("unit").synonyms),
    ("need_by", (*required_field("need_by").synonyms, "prazo", "termin")),
    ("qualification", required_field("qualification").synonyms),
    ("incoterm", ("incoterm", "frete", "freight", "local de entrega", "lieferbedingung")),
    (
        "description",
        (
            "descricao",
            "description",
            "beschreibung",
            "bezeichnung",
            "designation",
            "artikel",
            "produto",
            "product",
            "material",
            "descripcion",
            "descrizione",
        ),
    ),
    ("position", ("item", "pos", "posicao", "position", "nr", "no", "linha", "line", "#")),
)
# Lead time in a heading means the supplier fills it, even if it also says "prazo".
_SUPPLIER_FILLS: Final = (
    "prazo de entrega",
    "lead time",
    "lieferzeit",
    "delai",
    "plazo de entrega",
)


def phrases(language: str) -> dict[str, str]:
    return _PHRASES.get(language, _PHRASES["en"])


def _headings(language: str) -> dict[str, str]:
    return _HEADINGS.get(language, _HEADINGS["en"])


# --- Inputs and output ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RenderLine:
    position: int
    description: str
    mpn: str | None
    manufacturer: str | None
    quantities: Sequence[Decimal]
    unit: str
    need_by: date | None
    incoterm: str | None
    delivery_location: str | None
    qualification: str | None


@dataclass(frozen=True, slots=True)
class RenderInput:
    rfq_ref: str
    rfq_title: str
    response_due: date | None
    buyer_name: str
    buyer_company: str
    # ISO 639-1 the email is written in (the supplier's language).
    language: str
    # The template as stored (its own language) and as shown (localized to
    # `language`; same number of columns and footer fields, same order).
    template: TemplateContent
    display: TemplateContent
    lines: Sequence[RenderLine]


@dataclass(frozen=True, slots=True)
class RenderedRfq:
    subject: str
    text: str
    html: str
    language: str


# --- Rendering --------------------------------------------------------------------------


def column_key(heading: str) -> str | None:
    """The line data a template heading names, or None (the supplier fills it)."""
    text = normalize(heading)
    if any(s in text for s in _SUPPLIER_FILLS):
        return None
    for key, synonyms in _COLUMN_KEYS:
        if any(label_matches(heading, s) for s in synonyms):
            return key
    return None


def format_quantity(quantity: Decimal) -> str:
    value = quantity.normalize()
    if value == value.to_integral_value():
        return str(int(value))
    return format(value, "f")


def format_date(value: date, language: str) -> str:
    match language:
        case "de":
            return value.strftime("%d.%m.%Y")
        case "pt" | "fr" | "es" | "it":
            return value.strftime("%d/%m/%Y")
        case _:
            return value.isoformat()


def _incoterm(line: RenderLine) -> str:
    return " ".join(p for p in (line.incoterm, line.delivery_location) if p)


def _cell(key: str | None, line: RenderLine, language: str) -> str:
    match key:
        case "position":
            return str(line.position)
        case "description":
            return line.description
        case "mpn":
            return line.mpn or ""
        case "manufacturer":
            return line.manufacturer or ""
        case "quantity":
            return " / ".join(format_quantity(q) for q in line.quantities)
        case "unit":
            return line.unit
        case "need_by":
            return format_date(line.need_by, language) if line.need_by else ""
        case "qualification":
            return line.qualification or ""
        case "incoterm":
            return _incoterm(line)
        case _:
            return ""


def table(data: RenderInput) -> tuple[list[str], list[list[str]], str | None]:
    """Headings, rows, and the Incoterm line under the table when all lines share one."""
    keys: list[str | None] = []
    headings: list[str] = []
    seen: set[str] = set()
    for original, shown in zip(data.template.columns, data.display.columns, strict=True):
        key = column_key(original)
        if key is not None and key in seen:
            key = None
        if key is not None:
            seen.add(key)
        keys.append(key)
        headings.append(shown)

    lines = data.lines
    incoterms = {_incoterm(line) for line in lines if _incoterm(line)}
    uniform_incoterm = next(iter(incoterms)) if len(incoterms) == 1 else None
    if uniform_incoterm is not None and any(not _incoterm(line) for line in lines):
        uniform_incoterm = None
    wanted = [
        ("description", True),
        ("mpn", any(line.mpn for line in lines)),
        ("manufacturer", any(line.manufacturer for line in lines)),
        ("quantity", True),
        ("unit", True),
        ("need_by", any(line.need_by for line in lines)),
        ("qualification", any(line.qualification for line in lines)),
        ("incoterm", bool(incoterms) and uniform_incoterm is None),
    ]
    shown_headings = _headings(data.language)
    if "position" not in seen:
        keys.insert(0, "position")
        headings.insert(0, shown_headings["position"])
    for key, needed in wanted:
        if needed and key not in seen:
            # The description goes right after the position; the rest at the end.
            at = keys.index("position") + 1 if key == "description" else len(keys)
            keys.insert(at, key)
            headings.insert(at, shown_headings[key])
    rows = [[_cell(key, line, data.language) for key in keys] for line in lines]
    return headings, rows, (None if "incoterm" in seen else uniform_incoterm)


def asks(data: RenderInput) -> list[str]:
    """The template's footer fields, then the required questions nothing else asks.

    A column the supplier fills ("Prazo de entrega") already asks its question.
    """
    shown = list(data.display.footer_fields)
    supplier_columns = [c for c in data.template.columns if column_key(c) is None]
    covered = covered_keys([*data.template.footer_fields, *supplier_columns])
    for field in REQUIRED_FIELDS:
        if field.kind != "ask" or field.key in covered:
            continue
        shown.append(field.label(data.language))
    qualification = required_field("qualification")
    if any(line.qualification for line in data.lines) and qualification.key not in covered:
        shown.append(qualification.label(data.language))
    return shown


def _text_table(headings: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headings]
    for row in rows:
        widths = [max(w, len(c)) for w, c in zip(widths, row, strict=True)]

    def fmt(cells: list[str]) -> str:
        return " | ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True)).rstrip()

    rule = "-+-".join("-" * w for w in widths)
    return "\n".join([fmt(headings), rule, *(fmt(r) for r in rows)])


def _html_table(headings: list[str], rows: list[list[str]]) -> str:
    style = 'style="border:1px solid #ccc;padding:4px 8px;text-align:left"'
    head = "".join(f"<th {style}>{html.escape(h)}</th>" for h in headings)
    body = "".join(
        "<tr>" + "".join(f"<td {style}>{html.escape(c)}</td>" for c in row) + "</tr>"
        for row in rows
    )
    return (
        '<table style="border-collapse:collapse">'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    )


def render_rfq(data: RenderInput) -> RenderedRfq:
    words = phrases(data.language)
    incoterm_label = required_field("incoterm_place").label(data.language)
    headings, rows, incoterm = table(data)
    questions = asks(data)
    subject = f"[{data.rfq_ref}] {words['subject']}: {data.rfq_title}"
    due = (
        words["due"].format(date=format_date(data.response_due, data.language))
        if data.response_due
        else None
    )
    reference = words["reference"].format(ref=data.rfq_ref)

    text_parts = [
        data.display.greeting,
        reference,
        f"{words['items']}\n{_text_table(headings, rows)}",
    ]
    if incoterm:
        text_parts.append(f"{incoterm_label}: {incoterm}")
    text_parts.append("\n".join([words["prices"], words["asks"], *(f"- {q}" for q in questions)]))
    text_parts.append(words["alternative"])
    if due:
        text_parts.append(due)
    text_parts.append(f"{words['closing']}\n{data.buyer_name}\n{data.buyer_company}")
    text = "\n\n".join(text_parts) + "\n"

    def p(value: str) -> str:
        return f"<p>{html.escape(value)}</p>"

    html_parts = [
        p(data.display.greeting),
        p(reference),
        p(words["items"]),
        _html_table(headings, rows),
    ]
    if incoterm:
        html_parts.append(p(f"{incoterm_label}: {incoterm}"))
    html_parts.append(p(words["prices"]))
    html_parts.append(p(words["asks"]))
    html_parts.append("<ul>" + "".join(f"<li>{html.escape(q)}</li>" for q in questions) + "</ul>")
    html_parts.append(p(words["alternative"]))
    if due:
        html_parts.append(p(due))
    html_parts.append(
        "<p>"
        + "<br>".join(
            html.escape(v) for v in (words["closing"], data.buyer_name, data.buyer_company)
        )
        + "</p>"
    )
    return RenderedRfq(
        subject=subject, text=text, html="\n".join(html_parts), language=data.language
    )
