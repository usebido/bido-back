"""Render the RFQ email for one supplier from a template version and the RFQ lines.

Two bodies. By default the RFQ goes as the supplier's PDF
(`bido_back.rfqs.document`, which carries the items, terms and questions built
here) and the email is a short cover note (`render_cover`): what is attached,
how many items, the reply-by date and how to reply. When no PDF can be
attached, the full email (`render_rfq`) lists everything in the body.

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

The RFQ's own commercial terms (payment terms, minimum quote validity) are
stated after the questions only when the buyer set them; nothing fills them in
otherwise.

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
    LINE_HEADINGS,
    REQUIRED_FIELDS,
    accepted_terms,
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
        "price": "Please quote the unit price, with the currency.",
        "asks": "Please also let us know:",
        "alternative": (
            "If you offer an alternative part, please say so clearly and give its MPN "
            "and manufacturer."
        ),
        "payment": "Our payment terms: {terms}",
        "validity": "Please keep your quote valid for at least {days} days.",
        "due": "Please send your quote by {date}.",
        "closing": "Best regards,",
    },
    "pt": {
        "subject": "Solicitação de cotação",
        "reference": "Nossa referência: {ref}. Por favor, mantenha-a no assunto da resposta.",
        "items": "Itens solicitados:",
        "prices": "Por favor, informe o preço unitário para cada quantidade, com a moeda.",
        "price": "Por favor, informe o preço unitário, com a moeda.",
        "asks": "Por favor, informe também:",
        "alternative": (
            "Se oferecer um item alternativo, indique claramente, com o código e o fabricante."
        ),
        "payment": "Nossa condição de pagamento: {terms}",
        "validity": "Por favor, mantenha a proposta válida por no mínimo {days} dias.",
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
        "price": "Bitte nennen Sie den Stückpreis mit Währung.",
        "asks": "Bitte teilen Sie uns außerdem mit:",
        "alternative": (
            "Falls Sie einen alternativen Artikel anbieten, kennzeichnen Sie ihn bitte "
            "eindeutig und nennen Sie MPN und Hersteller."
        ),
        "payment": "Unsere Zahlungsbedingungen: {terms}",
        "validity": "Bitte halten Sie Ihr Angebot mindestens {days} Tage gültig.",
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
        "price": "Merci d'indiquer le prix unitaire, avec la devise.",
        "asks": "Merci de nous indiquer également :",
        "alternative": (
            "Si vous proposez un article alternatif, merci de l'indiquer clairement avec "
            "sa référence et son fabricant."
        ),
        "payment": "Nos conditions de paiement : {terms}",
        "validity": "Merci de maintenir votre offre valable au moins {days} jours.",
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
        "price": "Por favor, indique el precio unitario, con la moneda.",
        "asks": "Por favor, indíquenos también:",
        "alternative": (
            "Si ofrece una pieza alternativa, indíquelo claramente con su número de pieza "
            "y fabricante."
        ),
        "payment": "Nuestras condiciones de pago: {terms}",
        "validity": "Por favor, mantenga su cotización válida al menos {days} días.",
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
        "price": "Vi preghiamo di indicare il prezzo unitario, con la valuta.",
        "asks": "Vi preghiamo di indicarci inoltre:",
        "alternative": (
            "Se offrite un articolo alternativo, indicatelo chiaramente con codice e produttore."
        ),
        "payment": "Le nostre condizioni di pagamento: {terms}",
        "validity": "Vi preghiamo di mantenere l'offerta valida per almeno {days} giorni.",
        "due": "Vi preghiamo di inviare l'offerta entro il {date}.",
        "closing": "Cordiali saluti,",
    },
}

# Which template headings carry which line data. Checked in this order, so
# "Item e descrição" is the description, not the position.
_COLUMN_KEYS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("mpn", accepted_terms("mpn")),
    ("manufacturer", accepted_terms("manufacturer")),
    ("quantity", accepted_terms("quantity_tiers")),
    ("unit", accepted_terms("unit")),
    ("need_by", (*accepted_terms("need_by"), "prazo", "termin")),
    ("qualification", accepted_terms("qualification")),
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


# The cover note that carries the RFQ document (the PDF): no item table, no questions.
_COVER: Final[dict[str, dict[str, str]]] = {
    "en": {
        "salutation": "Dear Sir or Madam,",
        "attached": "Please find attached our request for quotation {ref} ({title}).",
        "count_one": (
            "It covers 1 item; the quantities, terms and what to include in your quote "
            "are in the attached PDF."
        ),
        "count_many": (
            "It covers {count} items; the quantities, terms and what to include in your "
            "quote are in the attached PDF."
        ),
        "reply": (
            "To reply, answer this email keeping {marker} in the subject and attach your quote."
        ),
    },
    "pt": {
        "salutation": "Prezados,",
        "attached": "Segue em anexo nossa solicitação de cotação {ref} ({title}).",
        "count_one": (
            "A solicitação tem 1 item; as quantidades, as condições e o que informar na "
            "proposta estão no PDF anexo."
        ),
        "count_many": (
            "A solicitação tem {count} itens; as quantidades, as condições e o que informar "
            "na proposta estão no PDF anexo."
        ),
        "reply": (
            "Para responder, responda a este e-mail mantendo {marker} no assunto e anexe "
            "sua proposta."
        ),
    },
    "de": {
        "salutation": "Sehr geehrte Damen und Herren,",
        "attached": "anbei erhalten Sie unsere Angebotsanfrage {ref} ({title}).",
        "count_one": (
            "Sie umfasst 1 Position; Mengen, Konditionen und die gewünschten Angaben "
            "finden Sie im beigefügten PDF."
        ),
        "count_many": (
            "Sie umfasst {count} Positionen; Mengen, Konditionen und die gewünschten "
            "Angaben finden Sie im beigefügten PDF."
        ),
        "reply": (
            "Bitte antworten Sie auf diese E-Mail, behalten Sie {marker} im Betreff bei "
            "und hängen Sie Ihr Angebot an."
        ),
    },
    "fr": {
        "salutation": "Madame, Monsieur,",
        "attached": "Veuillez trouver ci-joint notre demande de prix {ref} ({title}).",
        "count_one": (
            "Elle porte sur 1 article ; les quantités, les conditions et les éléments à "
            "indiquer dans votre offre figurent dans le PDF joint."
        ),
        "count_many": (
            "Elle porte sur {count} articles ; les quantités, les conditions et les éléments "
            "à indiquer dans votre offre figurent dans le PDF joint."
        ),
        "reply": (
            "Pour répondre, répondez à cet e-mail en gardant {marker} dans l'objet et "
            "joignez votre offre."
        ),
    },
    "es": {
        "salutation": "Estimados señores:",
        "attached": "Adjuntamos nuestra solicitud de cotización {ref} ({title}).",
        "count_one": (
            "Incluye 1 artículo; las cantidades, las condiciones y lo que debe indicar en "
            "su oferta están en el PDF adjunto."
        ),
        "count_many": (
            "Incluye {count} artículos; las cantidades, las condiciones y lo que debe "
            "indicar en su oferta están en el PDF adjunto."
        ),
        "reply": (
            "Para responder, conteste este correo manteniendo {marker} en el asunto y "
            "adjunte su cotización."
        ),
    },
    "it": {
        "salutation": "Gentili Signori,",
        "attached": "In allegato trovate la nostra richiesta di offerta {ref} ({title}).",
        "count_one": (
            "Comprende 1 articolo; le quantità, le condizioni e le informazioni da indicare "
            "nell'offerta sono nel PDF allegato."
        ),
        "count_many": (
            "Comprende {count} articoli; le quantità, le condizioni e le informazioni da "
            "indicare nell'offerta sono nel PDF allegato."
        ),
        "reply": (
            "Per rispondere, rispondete a questa e-mail mantenendo {marker} nell'oggetto e "
            "allegate la vostra offerta."
        ),
    },
}


def phrases(language: str) -> dict[str, str]:
    return _PHRASES.get(language, _PHRASES["en"])


def cover_phrases(language: str) -> dict[str, str]:
    return _COVER.get(language, _COVER["en"])


def _headings(language: str) -> dict[str, str]:
    return LINE_HEADINGS.get(language, LINE_HEADINGS["en"])


# --- Inputs and output ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RenderLine:
    position: int
    description: str
    part_number: str | None
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
    # The buyer's commercial terms, stated after the questions when set; never guessed.
    payment_terms: str | None = None
    quote_validity_days: int | None = None


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


def _part(line: RenderLine) -> str:
    """The part-number cell: the buyer's own code first; a different MPN after it, in brackets."""
    if line.part_number and line.mpn and line.mpn != line.part_number:
        return f"{line.part_number} ({line.mpn})"
    return line.part_number or line.mpn or ""


def _real_mpn(line: RenderLine) -> str | None:
    """The manufacturer part number, when there is one besides the buyer's own code."""
    if line.mpn and line.mpn != line.part_number:
        return line.mpn
    return None


def _incoterm(line: RenderLine) -> str:
    return " ".join(p for p in (line.incoterm, line.delivery_location) if p)


def _cell(key: str | None, line: RenderLine, language: str) -> str:
    match key:
        case "position":
            return str(line.position)
        case "description":
            return line.description
        case "mpn":
            # A template column naming a code ("Código", "Part number"): the buyer's own
            # code, with a different manufacturer part number in brackets.
            return _part(line)
        case "part_number":
            return line.part_number or ""
        case "mpn_only":
            return _real_mpn(line) or ""
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


# Columns kept even when every cell is empty.
_ALWAYS_SHOWN: Final = frozenset({"position", "description", "quantity", "unit"})


def table(data: RenderInput) -> tuple[list[str], list[list[str]], str | None]:
    """Headings, rows, and the Incoterm line under the table when all lines share one."""
    keys: list[str | None] = []
    headings: list[str] = []
    seen: set[str] = set()
    shown_headings = _headings(data.language)
    translated = data.template.language != data.language
    for original, shown in zip(data.template.columns, data.display.columns, strict=True):
        key = column_key(original)
        if key is not None and key in seen:
            key = None
        if key is not None:
            seen.add(key)
        keys.append(key)
        # A column Bido fills keeps the buyer's own words in the buyer's language; in
        # another language it gets Bido's fixed heading, so a translation cannot turn
        # "Prazo" (the date we need it by) into "Lead time" (what we ask the supplier).
        headings.append(shown_headings[key] if translated and key is not None else shown)

    lines = data.lines
    incoterms = {_incoterm(line) for line in lines if _incoterm(line)}
    uniform_incoterm = next(iter(incoterms)) if len(incoterms) == 1 else None
    if uniform_incoterm is not None and any(not _incoterm(line) for line in lines):
        uniform_incoterm = None
    wanted = [
        ("description", True),
        ("part_number", "mpn" not in seen and any(line.part_number for line in lines)),
        ("mpn_only", "mpn" not in seen and any(_real_mpn(line) for line in lines)),
        ("manufacturer", any(line.manufacturer for line in lines)),
        ("quantity", True),
        ("unit", True),
        ("need_by", any(line.need_by for line in lines)),
        ("qualification", any(line.qualification for line in lines)),
        ("incoterm", bool(incoterms) and uniform_incoterm is None),
    ]
    if "position" not in seen:
        keys.insert(0, "position")
        headings.insert(0, shown_headings["position"])
    for key, needed in wanted:
        if needed and key not in seen:
            # The description goes right after the position; the rest at the end.
            at = keys.index("position") + 1 if key == "description" else len(keys)
            keys.insert(at, key)
            headings.insert(at, shown_headings["mpn" if key == "mpn_only" else key])
    rows = [[_cell(key, line, data.language) for key in keys] for line in lines]
    # A column Bido fills that is empty on every line (a template's "Prazo" when no line
    # has a need-by date) says nothing: drop it. Columns the supplier fills (key None)
    # stay empty on purpose, and the core columns always stay.
    keep = [
        i
        for i, key in enumerate(keys)
        if key is None or key in _ALWAYS_SHOWN or any(row[i] for row in rows)
    ]
    if len(keep) != len(keys):
        headings = [headings[i] for i in keep]
        rows = [[row[i] for i in keep] for row in rows]
    return headings, rows, (None if "incoterm" in seen else uniform_incoterm)


def electronic(lines: Sequence[RenderLine]) -> bool:
    """Whether RoHS/REACH declarations apply: a line names a manufacturer part or an
    automotive qualification (electronic components). Steel, fasteners, paint or rubber
    bought by the buyer's own code do not get the question."""
    return any(_real_mpn(line) or line.manufacturer or line.qualification for line in lines)


def asks(data: RenderInput) -> list[str]:
    """The template's footer fields, then the required questions nothing else asks.

    A column the supplier fills ("Prazo de entrega") already asks its question.
    """
    shown = list(data.display.footer_fields)
    covered = covered_keys(template_ask_labels(data))
    for field in REQUIRED_FIELDS:
        if field.kind != "ask" or field.key in covered:
            continue
        if field.key == "rohs_reach" and not electronic(data.lines):
            continue
        shown.append(field.label(data.language))
    qualification = required_field("qualification")
    if any(line.qualification for line in data.lines) and qualification.key not in covered:
        shown.append(qualification.label(data.language))
    return shown


def _supplier_columns(data: RenderInput) -> list[tuple[str, str]]:
    """The template's columns the supplier fills: (original, shown) labels."""
    return [
        (original, shown)
        for original, shown in zip(data.template.columns, data.display.columns, strict=True)
        if column_key(original) is None
    ]


def document_asks(data: RenderInput) -> list[str]:
    """What the RFQ document asks for: the template's columns the supplier fills
    ("Preço unitário", "Prazo de entrega"; the PDF has no blank columns), then `asks`."""
    return list(dict.fromkeys([*(shown for _, shown in _supplier_columns(data)), *asks(data)]))


def template_ask_labels(data: RenderInput) -> list[str]:
    """The template's own labels (its language) that ask the supplier something: the
    footer fields and the columns the supplier fills. Coverage is matched on these."""
    return [*data.template.footer_fields, *(original for original, _ in _supplier_columns(data))]


def _text_table(headings: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headings]
    for row in rows:
        widths = [max(w, len(c)) for w, c in zip(widths, row, strict=True)]

    def fmt(cells: list[str]) -> str:
        return " | ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True)).rstrip()

    rule = "-+-".join("-" * w for w in widths)
    return "\n".join([fmt(headings), rule, *(fmt(r) for r in rows)])


_CELL: Final = "border:1px solid #d0d4dc;padding:6px 10px;vertical-align:top"


def _numeric(cell: str) -> bool:
    return bool(cell) and all(part.replace(".", "").isdigit() for part in cell.split(" / "))


def _html_table(headings: list[str], rows: list[list[str]]) -> str:
    """The item table as HTML: inline styles only (mail clients drop <style>)."""
    numeric = [
        bool(rows) and all(_numeric(r[i]) or not r[i] for r in rows) for i in range(len(headings))
    ]

    def align(i: int) -> str:
        return "right" if numeric[i] else "left"

    head = "".join(
        f'<th style="{_CELL};background:#f3f4f7;text-align:{align(i)}">{html.escape(h)}</th>'
        for i, h in enumerate(headings)
    )
    body = "".join(
        "<tr>"
        + "".join(
            f'<td style="{_CELL};text-align:{align(i)}">{html.escape(c)}</td>'
            for i, c in enumerate(row)
        )
        + "</tr>"
        for row in rows
    )
    return (
        '<table style="border-collapse:collapse;font-size:14px">'
        f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    )


def tiered(lines: Sequence[RenderLine]) -> bool:
    """Whether any line asks for more than one quantity."""
    return any(len(line.quantities) > 1 for line in lines)


def signature(buyer_name: str, buyer_company: str) -> list[str]:
    """The closing's name lines: the buyer, then the company when it says something new."""
    name, company = buyer_name.strip(), buyer_company.strip()
    if not name or name.casefold() == company.casefold():
        return [company] if company else []
    return [name, company] if company else [name]


def rfq_subject(data: RenderInput) -> str:
    return f"[{data.rfq_ref}] {phrases(data.language)['subject']}: {data.rfq_title}"


def _html_email(language: str, parts: Sequence[str]) -> str:
    return (
        f'<!doctype html><html lang="{html.escape(language)}"><body '
        'style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1f2330">'
        + "\n".join(parts)
        + "</body></html>"
    )


def _p(value: str) -> str:
    return f"<p>{html.escape(value)}</p>"


def _closing_html(words: dict[str, str], data: RenderInput) -> str:
    return (
        "<p>"
        + "<br>".join(
            html.escape(v)
            for v in (words["closing"], *signature(data.buyer_name, data.buyer_company))
        )
        + "</p>"
    )


def render_cover(data: RenderInput) -> RenderedRfq:
    """The short cover note that carries the RFQ document (the PDF) for one supplier.

    No item table and no questions: those are in the PDF. It says what is
    attached, how many items, the reply-by date and how to reply. Same subject
    as the full email.
    """
    words = phrases(data.language)
    cover = cover_phrases(data.language)
    count = len(data.lines)
    paragraphs = [
        cover["salutation"],
        " ".join(
            [
                cover["attached"].format(ref=data.rfq_ref, title=data.rfq_title),
                cover["count_one"] if count == 1 else cover["count_many"].format(count=count),
            ]
        ),
    ]
    if data.response_due:
        paragraphs.append(words["due"].format(date=format_date(data.response_due, data.language)))
    paragraphs.append(cover["reply"].format(marker=f"[{data.rfq_ref}]"))
    closing = [words["closing"], *signature(data.buyer_name, data.buyer_company)]
    text = "\n\n".join([*paragraphs, "\n".join(closing)]) + "\n"
    markup = _html_email(data.language, [*map(_p, paragraphs), _closing_html(words, data)])
    return RenderedRfq(subject=rfq_subject(data), text=text, html=markup, language=data.language)


def render_rfq(data: RenderInput) -> RenderedRfq:
    """The full RFQ email, item table and questions in the body: the fallback when the
    RFQ document cannot be attached."""
    words = phrases(data.language)
    incoterm_label = required_field("incoterm_place").label(data.language)
    headings, rows, incoterm = table(data)
    questions = asks(data)
    due = (
        words["due"].format(date=format_date(data.response_due, data.language))
        if data.response_due
        else None
    )
    reference = words["reference"].format(ref=data.rfq_ref)
    terms: list[str] = []
    if data.payment_terms:
        terms.append(words["payment"].format(terms=data.payment_terms))
    if data.quote_validity_days:
        terms.append(words["validity"].format(days=data.quote_validity_days))

    text_parts = [
        data.display.greeting,
        reference,
        f"{words['items']}\n{_text_table(headings, rows)}",
    ]
    if incoterm:
        text_parts.append(f"{incoterm_label}: {incoterm}")
    price = words["prices"] if tiered(data.lines) else words["price"]
    text_parts.append("\n".join([price, words["asks"], *(f"- {q}" for q in questions)]))
    text_parts.append(words["alternative"])
    if terms:
        text_parts.append("\n".join(terms))
    if due:
        text_parts.append(due)
    text_parts.append(
        "\n".join([words["closing"], *signature(data.buyer_name, data.buyer_company)])
    )
    text = "\n\n".join(text_parts) + "\n"

    html_parts = [
        _p(data.display.greeting),
        _p(reference),
        _p(words["items"]),
        _html_table(headings, rows),
    ]
    if incoterm:
        html_parts.append(_p(f"{incoterm_label}: {incoterm}"))
    html_parts.append(_p(price))
    html_parts.append(_p(words["asks"]))
    html_parts.append("<ul>" + "".join(f"<li>{html.escape(q)}</li>" for q in questions) + "</ul>")
    html_parts.append(_p(words["alternative"]))
    html_parts.extend(_p(term) for term in terms)
    if due:
        html_parts.append(_p(due))
    html_parts.append(_closing_html(words, data))
    return RenderedRfq(
        subject=rfq_subject(data),
        text=text,
        html=_html_email(data.language, html_parts),
        language=data.language,
    )
