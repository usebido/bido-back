"""Render a `SupplierEmail` into its plain-text and HTML parts: one layout for every type.

The blocks, in order (the Bido email standard, §2.2):

1. salutation (company form; the trading name without its legal suffix in English);
2. purpose line: `<type label> – RFQ-6` (revised offers add the round);
3. context line: the message answered, by its date, and what is needed;
4. positions block: only the positions concerned, each with the identity line it has in
   every email (`Pos. n · description · code · quantity unit`) and indented fact lines;
5. numbered asks, at most three;
6. reply-by (weekday and date; the exact cut-off with time and zone for a best and final
   offer) and 7. reply instructions (fill-in lines with two or more positions);
8. the model's optional note (empty by default);
9. the legal line (every type but the outcome notice);
10. closing and a single signature.

Both parts are built from the same strings, so they carry the same numbers and dates.
The HTML uses inline styles and one table with `<th scope>` headers; no images, links or
tracking. The plain text has one block per position and no padded columns.
"""

import html
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from bido_back.supplier_email.catalog import Key, Phrases, count_word, phrases, type_label
from bido_back.supplier_email.locale import (
    NBSP,
    Locale,
    clock,
    day,
    money,
    number,
    quantity,
    unit_kind,
    unit_label,
    weekday,
)
from bido_back.supplier_email.model import (
    ANSWER_REQUESTED,
    Ask,
    Fact,
    LeadTime,
    MissingValues,
    Money,
    OfferedTerms,
    OfferField,
    Position,
    QuotedLeadTime,
    ReplyBy,
    RequiredDelivery,
    RequiredLeadTime,
    Signature,
    SupplierEmail,
    TargetPrice,
    ValuesToConfirm,
)
from bido_back.templates.render import signature

# The zone a cut-off is shown in: an unambiguous instant, the same for every supplier.
CUTOFF_ZONE: Final = "UTC"
_INDENT: Final = "   "
_NAME_LIMIT: Final = 100

_FIELD_KEYS: Final[dict[OfferField, Key]] = {
    "currency": "field.currency",
    "valid_until": "field.valid_until",
    "unit_price": "field.unit_price",
    "quantity": "field.quantity",
    "lead_time": "field.lead_time",
}
_FIELD_THE_KEYS: Final[dict[OfferField, Key]] = {
    "currency": "field_the.currency",
    "valid_until": "field_the.valid_until",
}


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    subject: str
    text: str
    html: str
    language: str


# --- Names ------------------------------------------------------------------------------

# Legal suffixes dropped from a company name in the salutation ("MetalNova Teste Ltda").
_LEGAL_SUFFIXES: Final = frozenset(
    {
        "ltda", "ltda.", "ltda-me", "me", "epp", "eireli", "s.a.", "s.a", "s/a", "sa", "gmbh",
        "ag", "kg", "ohg", "ug", "e.k.", "se", "s.r.l.", "srl", "s.p.a.", "spa", "s.a.s.",
        "sas", "sarl", "s.a.r.l.", "s.l.", "sl", "s.l.u.", "slu", "ltd", "ltd.", "limited",
        "plc", "inc", "inc.", "llc", "l.l.c.", "corp", "corp.", "co.", "bv", "b.v.", "nv",
        "n.v.", "oy", "ab", "a/s", "aps", "s.r.o.", "kft", "zrt", "&", "co",
    }
)  # fmt: skip


def clean_text(value: str, limit: int | None = None) -> str:
    """One line of data: control and formatting characters (CR/LF, bidi marks) removed,
    whitespace collapsed, and cut at `limit` characters."""
    kept = "".join(
        " " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c
        for c in value
        if unicodedata.category(c) not in ("Cf", "Cs", "Co", "Cn")
    )
    text = " ".join(kept.split())
    return text[:limit].rstrip() if limit is not None else text


def trading_name(name: str) -> str:
    """The company name without its legal suffix: "MetalNova Teste Ltda" -> "MetalNova Teste"."""
    words = clean_text(name, _NAME_LIMIT).split(" ")
    while len(words) > 1 and words[-1].strip(",").casefold() in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words).strip(" ,-") if words != [""] else ""


def signature_lines(sig: Signature) -> list[str]:
    """The signature, printed once.

    STAND-IN (N1): workstream N1 adds the single signature function; switch to it at
    merge. Until then: the RFQ email's own rule (`templates.render.signature`, name then
    company, the company alone when they are the same), then role and phone when set.
    """
    lines = signature(clean_text(sig.name, _NAME_LIMIT), clean_text(sig.company, _NAME_LIMIT))
    for extra in (sig.role, sig.phone):
        cleaned = clean_text(extra or "", _NAME_LIMIT)
        if cleaned and cleaned not in lines:
            lines.append(cleaned)
    return lines


# --- Values ----------------------------------------------------------------------------


def _fill(template: str, **values: str) -> str:
    return template.format(**values)


def join_numbers(numbers: Sequence[int], words: Phrases) -> str:
    """`1`, `1 and 2`, `1, 2 and 3` (in the email's language)."""
    texts = [str(n) for n in numbers]
    if len(texts) < 2:
        return "".join(texts)
    return f"{', '.join(texts[:-1])} {words['word.and']} {texts[-1]}"


def join_words(items: Sequence[str], words: Phrases) -> str:
    if len(items) < 2:
        return "".join(items)
    return f"{', '.join(items[:-1])} {words['word.and']} {items[-1]}"


def price_text(price: Money, unit: str, locale: Locale, words: Phrases) -> str:
    amount = money(price.amount, price.currency, locale)
    if price.per is None or price.per == 1:
        return _fill(
            words["value.price_per_unit"],
            price=amount,
            unit=unit_label(unit, locale, singular=True),
        )
    return _fill(
        words["value.price_per_basis"], price=amount, quantity=quantity(price.per, unit, locale)
    )


_DAY_KEYS: Final[dict[tuple[bool, bool], Key]] = {
    (False, True): "lead.calendar.one",
    (False, False): "lead.calendar.many",
    (True, True): "lead.business.one",
    (True, False): "lead.business.many",
}


def days_text(days: int, words: Phrases, locale: Locale, *, business: bool = False) -> str:
    """`12 days`, `1 business day`: a count of days without "after order"."""
    return _fill(words[_DAY_KEYS[(business, days == 1)]], n=number(Decimal(days), locale))


def lead_text(lead: LeadTime, words: Phrases, locale: Locale) -> str:
    """`approx. 10 business days + a few additional days after order`."""
    text = days_text(lead.days, words, locale, business=lead.basis == "business")
    if lead.plus_a_few_days:
        text = _fill(words["lead.plus_few"], lead=text)
    text = _fill(words["lead.after_order"], lead=text)
    if lead.approximate:
        text = _fill(words["lead.approx"], lead=text)
    return text


def quantities_text(values: Sequence[Decimal], unit: str, locale: Locale) -> str:
    """The requested quantity as in the RFQ: `70 pç`, `12,5 kg`, `1.000 / 5.000 pç`."""
    if len(values) == 1:
        return quantity(values[0], unit, locale)
    joined = " / ".join(number(v, locale) for v in values)
    return f"{joined}{NBSP}{unit_label(unit, locale)}"


def fact_line(fact: Fact, position: Position, locale: Locale, words: Phrases) -> tuple[str, str]:
    """(label, value) of one fact under a position."""
    match fact:
        case OfferedTerms(price=price, lead_time=lead):
            parts: list[str] = []
            if price is not None:
                parts.append(price_text(price, position.unit, locale, words))
            if lead is not None:
                parts.append(lead_text(lead, words, locale))
            return words["fact.offer"], " · ".join(parts)
        case QuotedLeadTime(lead_time=lead):
            return words["fact.quoted_lead"], lead_text(lead, words, locale)
        case TargetPrice(price=price):
            return words["fact.target"], price_text(price, position.unit, locale, words)
        case RequiredDelivery(by=by):
            return words["fact.required_delivery"], _fill(
                words["value.by_date"], date=day(by, locale)
            )
        case RequiredLeadTime(days=days):
            lead = _fill(words["lead.after_order"], lead=days_text(days, words, locale))
            return words["fact.required_lead"], _fill(words["value.at_most"], lead=lead)
        case MissingValues(fields=fields):
            return words["fact.missing"], ", ".join(words[_FIELD_KEYS[f]] for f in fields)
        case ValuesToConfirm(fields=fields):
            return words["fact.confirm"], ", ".join(words[_FIELD_KEYS[f]] for f in fields)


def when_text(reply_by: ReplyBy, locale: Locale, words: Phrases) -> str:
    """`Tuesday, 13 Oct 2026`; a cut-off adds the time and zone: `…, 18:00 UTC`."""
    if reply_by.cutoff is not None:
        return _fill(
            words["when.cutoff"],
            weekday=weekday(reply_by.day, locale),
            date=day(reply_by.day, locale),
            time=clock(reply_by.cutoff),
            zone=CUTOFF_ZONE,
        )
    return _fill(
        words["when.day"], weekday=weekday(reply_by.day, locale), date=day(reply_by.day, locale)
    )


# --- Blocks ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Row:
    number: str
    identity: str
    description: str
    code: str
    quantity: str
    facts: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Blocks:
    """Every string the email shows, once: both parts are rendered from these."""

    salutation: str
    purpose: str
    context: tuple[str, ...]
    rows: tuple[Row, ...]
    asks: tuple[str, ...]
    reply: str | None
    fills: tuple[str, ...]
    no_resend: str | None
    note: str
    legal: str | None
    closing: str
    signature: tuple[str, ...]


def _position(email: SupplierEmail, number_: int) -> Position:
    for position in email.positions:
        if position.number == number_:
            return position
    raise ValueError(f"ask names position {number_}, which is not in the positions block")


def _facts[T](email: SupplierEmail, numbers: Sequence[int], kind: type[T]) -> list[T]:
    found: list[T] = []
    for n in numbers:
        found.extend(f for f in _position(email, n).facts if isinstance(f, kind))
    return found


def _positions_phrase(numbers: Sequence[int], words: Phrases) -> str:
    if len(numbers) == 1:
        return _fill(words["positions.one"], position=str(numbers[0]))
    return _fill(words["positions.many"], positions=join_numbers(numbers, words))


def _quantity_phrase(position: Position, locale: Locale, words: Phrases) -> str:
    """`all 15 pcs`, `as 15 peças`, `the full quantity (12.5 m)`."""
    values = position.quantities
    kind = unit_kind(position.unit)
    if len(values) == 1 and values[0] != 1 and kind == "piece":
        return _fill(words["qty.piece"], n=number(values[0], locale))
    if len(values) == 1 and kind == "kg":
        return _fill(words["qty.kg"], n=number(values[0], locale))
    return _fill(words["qty.other"], quantity=quantities_text(values, position.unit, locale))


def _ask_text(ask: Ask, email: SupplierEmail, words: Phrases) -> str:
    locale = email.locale
    numbers = list(ask.positions)
    match ask.kind:
        case "quote_or_decline":
            return words["ask.quote_or_decline"]
        case "offer_terms":
            names = [words[_FIELD_THE_KEYS[f]] for f in ask.fields]
            key: Key = "ask.offer_terms.confirm" if ask.confirm else "ask.offer_terms.state"
            return _fill(words[key], fields=join_words(names, words))
        case "complete_positions":
            missing = _facts(email, numbers, MissingValues)
            confirm = _facts(email, numbers, ValuesToConfirm)
            if missing and confirm:
                return words["ask.positions.both"]
            return words["ask.positions.confirm" if confirm else "ask.positions.missing"]
        case "deliver_by":
            dates = [f.by for f in _facts(email, numbers, RequiredDelivery)]
            if len(dates) != len(numbers):
                raise ValueError("deliver_by needs one required delivery date per position")
            if len(numbers) == 1:
                return _fill(
                    words["ask.deliver_by.one"],
                    quantity=_quantity_phrase(_position(email, numbers[0]), locale, words),
                    date=day(dates[0], locale),
                )
            if len(set(dates)) == 1:
                return _fill(
                    words["ask.deliver_by.many_same"],
                    positions=join_numbers(numbers, words),
                    date=day(dates[0], locale),
                )
            return words["ask.deliver_by.many_each"]
        case "meet_lead_time":
            days = [f.days for f in _facts(email, numbers, RequiredLeadTime)]
            if len(days) != len(numbers):
                raise ValueError("meet_lead_time needs one required lead time per position")
            if len(numbers) == 1:
                return _fill(
                    words["ask.meet_lead.one"],
                    position=str(numbers[0]),
                    days=days_text(days[0], words, locale),
                )
            if len(set(days)) == 1:
                return _fill(
                    words["ask.meet_lead.many_same"],
                    positions=join_numbers(numbers, words),
                    days=days_text(days[0], words, locale),
                )
            return words["ask.meet_lead.many_each"]
        case "revise_price":
            return _fill(words["ask.revise_price"], positions=_positions_phrase(numbers, words))
        case "best_and_final":
            return _fill(words["ask.best_and_final"], positions=_positions_phrase(numbers, words))


def _fill_lines(email: SupplierEmail, words: Phrases) -> tuple[str, ...]:
    """Fill-in lines for the reply, one per position asked, with two or more positions."""
    if len(email.positions) < 2:
        return ()
    lines: list[str] = []
    for ask in email.asks:
        match ask.kind:
            case "deliver_by":
                key: Key = "fill.delivery"
                blank = words["blank.date"]
            case "meet_lead_time":
                key, blank = "fill.lead", words["blank.value"]
            case "revise_price":
                key, blank = "fill.price", words["blank.value"]
            case "best_and_final":
                key, blank = "fill.final_price", words["blank.value"]
            case _:
                continue
        lines.extend(_fill(words[key], position=str(n), blank=blank) for n in ask.positions)
    return tuple(lines)


def _thanks(email: SupplierEmail, words: Phrases) -> list[str]:
    answered = email.answered
    if answered is None or answered.kind == "request":
        return []
    key: Key = "context.thanks.offer" if answered.kind == "offer" else "context.thanks.reply"
    return [_fill(words[key], date=day(answered.on, email.locale))]


def _needs_dates(email: SupplierEmail, numbers: Sequence[int]) -> bool:
    return len(_facts(email, numbers, RequiredDelivery)) == len(numbers)


def _context(email: SupplierEmail, words: Phrases) -> tuple[str, ...]:
    locale = email.locale
    answered = email.answered
    sentences: list[str] = []
    match email.type:
        case "reminder":
            if answered is not None:
                sentences.append(_fill(words["context.reminder"], date=day(answered.on, locale)))
        case "clarification":
            sentences.extend(_thanks(email, words))
            asked = [n for a in email.asks if a.kind == "deliver_by" for n in a.positions]
            if asked and all(a.kind == "deliver_by" for a in email.asks):
                if len(asked) == 1:
                    sentences.append(
                        _fill(words["context.clarification.delivery_one"], position=str(asked[0]))
                    )
                else:
                    sentences.append(
                        _fill(
                            words["context.clarification.delivery_many"],
                            count=count_word(len(asked), email.language),
                        )
                    )
            else:
                sentences.append(words["context.clarification.generic"])
        case "revised_offer":
            sentences.extend(_thanks(email, words))
            lead_asked = any(a.kind in ("deliver_by", "meet_lead_time") for a in email.asks)
            sentences.append(
                words["context.revised.price_lead" if lead_asked else "context.revised.price"]
            )
            if email.mention_competition:
                sentences.append(words["context.competition"])
        case "delivery_date":
            sentences.extend(_thanks(email, words))
            numbers = [p.number for p in email.positions]
            only = words["word.only"] if email.subset else ""
            dated = _needs_dates(email, numbers)
            if len(numbers) == 1:
                key: Key = "context.delivery.one.date" if dated else "context.delivery.one.lead"
                sentences.append(_fill(words[key], position=str(numbers[0]), only=only))
            elif numbers:
                key = "context.delivery.many.date" if dated else "context.delivery.many.lead"
                sentences.append(
                    _fill(words[key], positions=join_numbers(numbers, words), only=only)
                )
        case "bafo":
            sentences.extend(_thanks(email, words))
            sentences.append(words["context.bafo"])
            if email.mention_competition:
                sentences.append(words["context.competition"])
        case "not_selected":
            if answered is not None and answered.kind == "request":
                sentences.append(
                    _fill(words["context.not_selected.request"], date=day(answered.on, locale))
                )
                sentences.append(words["context.not_selected.closed"])
            else:
                if answered is not None:
                    sentences.append(
                        _fill(words["context.not_selected.offer"], date=day(answered.on, locale))
                    )
                sentences.append(words["context.not_selected.outcome"])
    return tuple(sentences)


def _rows(email: SupplierEmail, words: Phrases) -> tuple[Row, ...]:
    locale = email.locale
    rows: list[Row] = []
    for position in email.positions:
        qty = quantities_text(position.quantities, position.unit, locale)
        parts = [_fill(words["identity.position"], position=str(position.number))]
        parts.append(position.description)
        if position.code:
            parts.append(_fill(words["identity.code"], code=position.code))
        parts.append(qty)
        rows.append(
            Row(
                number=str(position.number),
                identity=" · ".join(parts),
                description=position.description,
                code=position.code or "",
                quantity=qty,
                facts=tuple(fact_line(f, position, locale, words) for f in position.facts),
            )
        )
    return tuple(rows)


def blocks(email: SupplierEmail) -> Blocks:
    words = phrases(email.language)
    label = type_label(email.type, email.language)
    if email.type == "revised_offer" and email.round is not None:
        purpose = _fill(
            words["purpose.round"], label=label, ref=email.rfq_ref, round=str(email.round)
        )
    else:
        purpose = _fill(words["purpose"], label=label, ref=email.rfq_ref)
    name = trading_name(email.supplier_name)
    salutation = _fill(words["salutation"], name=name) if name else words["salutation.anonymous"]
    fills = _fill_lines(email, words)
    reply: str | None = None
    if email.reply_by is not None:
        when = when_text(email.reply_by, email.locale, words)
        reply = _fill(words["reply.by_example" if fills else "reply.by"], when=when)
    no_resend = (
        words["reply.no_resend"]
        if email.subset and email.type in ("revised_offer", "delivery_date", "bafo")
        else None
    )
    company = clean_text(email.buyer_company, _NAME_LIMIT)
    return Blocks(
        salutation=salutation,
        purpose=purpose,
        context=_context(email, words),
        rows=_rows(email, words),
        asks=tuple(_ask_text(ask, email, words) for ask in email.asks),
        reply=reply,
        fills=fills,
        no_resend=no_resend,
        note=" ".join(email.note.split()),
        legal=_fill(words["legal"], company=company) if email.type in ANSWER_REQUESTED else None,
        closing=words["closing"],
        signature=tuple(signature_lines(email.signature)),
    )


# --- Plain text ------------------------------------------------------------------------


def render_text(parts: Blocks, *, separator: str) -> str:
    paragraphs: list[str] = [parts.salutation, "\n".join([parts.purpose, " ".join(parts.context)])]
    if parts.rows:
        lines: list[str] = []
        for row in parts.rows:
            lines.append(row.identity)
            lines.extend(f"{_INDENT}{label}{separator}{value}" for label, value in row.facts)
        paragraphs.append("\n".join(lines))
    if parts.asks:
        paragraphs.append("\n".join(f"{i}. {ask}" for i, ask in enumerate(parts.asks, 1)))
    if parts.reply is not None:
        if parts.fills:
            reply = "\n".join([parts.reply, *(f"{_INDENT}{line}" for line in parts.fills)])
            if parts.no_resend:
                reply = f"{reply}\n{parts.no_resend}"
        else:
            reply = " ".join(p for p in (parts.reply, parts.no_resend) if p)
        paragraphs.append(reply)
    if parts.note:
        paragraphs.append(parts.note)
    if parts.legal:
        paragraphs.append(parts.legal)
    paragraphs.append("\n".join([parts.closing, *parts.signature]))
    return "\n\n".join(p.rstrip() for p in paragraphs if p.strip()) + "\n"


# --- HTML --------------------------------------------------------------------------------

_BODY: Final = (
    "font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.45;color:#1f2330"
)
_CELL: Final = "border:1px solid #d0d4dc;padding:4px 8px;vertical-align:top"
_HEAD: Final = f"{_CELL};text-align:left;background:#f3f4f7"
_LEGAL: Final = "color:#5b6170;font-size:12px"


def _e(value: str) -> str:
    return html.escape(value, quote=True)


def _table(parts: Blocks, words: Phrases) -> str:
    labels: list[str] = []
    for row in parts.rows:
        for label, _ in row.facts:
            if label not in labels:
                labels.append(label)
    with_code = any(row.code for row in parts.rows)
    headings = [words["table.position"], words["table.description"]]
    if with_code:
        headings.append(words["table.code"])
    headings.extend([words["table.quantity"], *labels])
    head = "".join(f'<th scope="col" style="{_HEAD}">{_e(h)}</th>' for h in headings)
    body: list[str] = []
    for row in parts.rows:
        cells = [f'<th scope="row" style="{_HEAD}">{_e(row.number)}</th>']
        cells.append(f'<td style="{_CELL}">{_e(row.description)}</td>')
        if with_code:
            cells.append(f'<td style="{_CELL}">{_e(row.code)}</td>')
        cells.append(f'<td style="{_CELL};white-space:nowrap">{_e(row.quantity)}</td>')
        for label in labels:
            values = [value for name, value in row.facts if name == label]
            cells.append(f'<td style="{_CELL}">{_e("; ".join(values))}</td>')
        body.append(f"<tr>{''.join(cells)}</tr>")
    return (
        '<table style="border-collapse:collapse;font-size:14px;margin:4px 0 12px">'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def render_html(parts: Blocks, language: str) -> str:
    words = phrases(language)
    out: list[str] = [f"<p>{_e(parts.salutation)}</p>"]
    context = " ".join(parts.context)
    out.append(
        f"<p><strong>{_e(parts.purpose)}</strong>"
        + (f"<br>{_e(context)}" if context else "")
        + "</p>"
    )
    if parts.rows:
        out.append(_table(parts, words))
    if parts.asks:
        out.append(
            '<ol style="margin:0 0 12px;padding-left:22px">'
            + "".join(f"<li>{_e(ask)}</li>" for ask in parts.asks)
            + "</ol>"
        )
    if parts.reply is not None:
        lines = [parts.reply, *parts.fills]
        if parts.no_resend:
            lines.append(parts.no_resend)
        out.append("<p>" + "<br>".join(_e(line) for line in lines) + "</p>")
    if parts.note:
        out.append(f"<p>{_e(parts.note)}</p>")
    if parts.legal:
        out.append(f'<p style="{_LEGAL}">{_e(parts.legal)}</p>')
    out.append("<p>" + "<br>".join(_e(line) for line in (parts.closing, *parts.signature)) + "</p>")
    return (
        f'<!doctype html><html lang="{_e(language)}"><body style="{_BODY}">'
        + "\n".join(out)
        + "</body></html>"
    )


def render(email: SupplierEmail) -> RenderedEmail:
    """The email's subject, plain text and HTML (same numbers and dates in both)."""
    parts = blocks(email)
    words = phrases(email.language)
    return RenderedEmail(
        subject=email.subject,
        text=render_text(parts, separator=words["fact.separator"]),
        html=render_html(parts, email.language),
        language=email.language,
    )


_TAG: Final = re.compile(r"<[^>]+>")


def html_text(markup: str) -> str:
    """The visible text of the HTML part (tags dropped, entities decoded)."""
    return html.unescape(_TAG.sub(" ", markup))
