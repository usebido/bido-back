"""The send gate: structural checks on every supplier email before it is proposed.

Code renders the email from the decision, so most checks guard the renderer itself and
whatever a future change of the catalog, a builder or the model's note could break. They
return reason codes and log nothing; a refused email is not proposed.

- `language_not_thread`: the language is not the thread's (the RFQ email's).
- `subject_*`: the subject is not `Re: ` plus the first subject (`subject`).
- `number_not_decided`, `exponent`: a number that is not the decision's, or E notation.
- `decided_value_missing`, `parity`: a decided value is missing, or the parts differ.
- `iso_date`, `bare_dollar`, `brl_code`: ISO dates, a bare "$", "BRL" instead of R$ (pt).
- `signature_*`, `repeated_line`: the closing twice, a second signature, a line repeated.
- `reply_by_*`: an email that asks without a reply-by date, or a notice with one.
- `asks_*`: no ask where one is needed, more than three, or any in a notice.
- `legal_line_*`: the legal line is missing, or where it does not belong.
- `html_*`, `too_long`: tags or links outside the layout, size limits.
- `rfq_ref_missing_in_body`, `price_mentioned`: moved from the v1 draft checks; no price in
  a reminder, a clarification or the outcome notice.
- `note_*`: the model's note breaks a rule (`note_problems`).

Only the note is free text, so the commitment, name and greeting checks run on the note
alone: the catalog's fixed sentences are checked once, by tests, and the buyer's own text
(descriptions, titles) is the buyer's. The mandate's own commitment check
(`mandates.policy.commitment_language`) still reads the whole email when it decides an
automatic approval; the legal line is worded so it never trips it.
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from bido_back.mandates.policy import commitment_language
from bido_back.supplier_email.catalog import Phrases, phrases
from bido_back.supplier_email.locale import catalog_language, clock, day, minor_unit, number
from bido_back.supplier_email.model import (
    ANSWER_REQUESTED,
    MAX_ASKS,
    EmailType,
    MissingValues,
    Money,
    OfferedTerms,
    QuotedLeadTime,
    RequiredDelivery,
    RequiredLeadTime,
    SupplierEmail,
    TargetPrice,
    ValuesToConfirm,
)
from bido_back.supplier_email.render import (
    RenderedEmail,
    blocks,
    clean_text,
    fact_line,
    html_text,
    quantities_text,
    signature_lines,
    trading_name,
    when_text,
)
from bido_back.supplier_email.subject import subject_problems

TEXT_LIMIT: Final = 20_000
HTML_LIMIT: Final = 100_000
NOTE_MAX_WORDS: Final = 40
NOTE_MAX_SENTENCES: Final = 2
# Words per type, not counting the positions, the fill-in lines, the legal line and the
# signature (§2.4). The catalog stays within them (tests); the note must fit what is left.
WORD_BUDGET: Final[dict[EmailType, int]] = {
    "reminder": 60,
    "clarification": 90,
    "revised_offer": 120,
    "delivery_date": 90,
    "bafo": 120,
    "not_selected": 60,
}
# Reminders, clarifications and the outcome notice name no price (the old draft checks'
# `price_mentioned`): only revised offers, delivery-date requests and best-and-final requests
# show the supplier's own offer and our target.
_NO_PRICES: Final[frozenset[EmailType]] = frozenset({"reminder", "clarification", "not_selected"})
_ALLOWED_TAGS: Final = frozenset(
    {"html", "body", "p", "strong", "br", "table", "thead", "tbody", "tr", "th", "td", "ol", "li"}
)


@dataclass(frozen=True, slots=True)
class GateContext:
    """What the gate compares the email with."""

    # The language the thread's RFQ email went out in (`rfqs.language.thread_language`).
    thread_language: str
    # The RFQ email's subject as stored; None when it was never sent through Bido.
    first_subject: str | None
    rfq_title: str
    # The other suppliers invited to the RFQ: the note may name none of them.
    other_suppliers: tuple[str, ...] = ()


# --- Numbers ----------------------------------------------------------------------------

# A written number: digits joined by the separators the formatters use (1.250 / 12,5 /
# 1’250 / 1 250 / 22/10/2026 / 18:00).
_TOKEN: Final = re.compile(r"\d+(?:[.,’' /:]\d+)*")
_EXPONENT: Final = re.compile(r"\d[eE][+-]?\d")
_ISO_DATE: Final = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
_BARE_DOLLAR: Final = re.compile(r"(?<![A-Za-z])\$")


def number_tokens(text: str) -> list[str]:
    return _TOKEN.findall(text)


def _money_values(price: Money, email: SupplierEmail) -> list[str]:
    values = [number(price.amount, email.locale, min_decimals=minor_unit(price.currency))]
    if price.per is not None:
        values.append(number(price.per, email.locale))
    return values


def decided_values(email: SupplierEmail) -> list[str]:
    """Every value the decision puts in the email, formatted as the email writes it."""
    locale = email.locale
    values: list[str] = []
    for position in email.positions:
        values.append(str(position.number))
        values.append(quantities_text(position.quantities, position.unit, locale))
        for fact in position.facts:
            match fact:
                case OfferedTerms(price=price, lead_time=lead):
                    if price is not None:
                        values.extend(_money_values(price, email))
                    if lead is not None:
                        values.append(number(Decimal(lead.days), locale))
                case QuotedLeadTime(lead_time=lead):
                    values.append(number(Decimal(lead.days), locale))
                case TargetPrice(price=price):
                    values.extend(_money_values(price, email))
                case RequiredDelivery(by=by):
                    values.append(day(by, locale))
                case RequiredLeadTime(days=days):
                    values.append(number(Decimal(days), locale))
                case MissingValues() | ValuesToConfirm():
                    pass
    if email.answered is not None:
        values.append(day(email.answered.on, locale))
    if email.reply_by is not None:
        values.append(day(email.reply_by.day, locale))
        if email.reply_by.cutoff is not None:
            values.append(clock(email.reply_by.cutoff))
    if email.round is not None:
        values.append(str(email.round))
    asked = [n for a in email.asks if a.kind == "deliver_by" for n in a.positions]
    if len(asked) > 10:
        values.append(str(len(asked)))
    values.extend(str(i) for i in range(1, len(email.asks) + 1))
    return values


def _free_texts(email: SupplierEmail) -> list[str]:
    """Data that is not a decision and may hold digits ("MTR-100", "000101", "RFQ-6")."""
    texts = [
        email.rfq_ref,
        clean_text(email.supplier_name),
        trading_name(email.supplier_name),
        clean_text(email.buyer_company),
        *signature_lines(email.signature),
    ]
    for position in email.positions:
        texts.append(position.description)
        if position.code:
            texts.append(position.code)
    return [t for t in texts if t]


def _masked(text: str, email: SupplierEmail) -> str:
    """The text without the free data, for the format checks (an ISO date or a "$" in a
    description is the buyer's). Short all-digit data stays: masking "10" would hide the
    lead time "10 days" too."""
    for free in sorted(set(_free_texts(email)), key=len, reverse=True):
        if len(free) >= 3 or not free.isdigit():
            text = text.replace(free, " ")
    return text


def _number_problems(email: SupplierEmail, text: str, markup_text: str) -> list[str]:
    problems: list[str] = []
    allowed: set[str] = set()
    for value in [*decided_values(email), *_free_texts(email)]:
        allowed.update(number_tokens(value))
    if any(token not in allowed for token in number_tokens(text)):
        problems.append("number_not_decided")
    if any(token not in allowed for token in number_tokens(markup_text)):
        problems.append("html_number_not_decided")
    # Parity: the same numbers in both parts (the text also numbers its asks: 1. 2. 3.).
    text_tokens = Counter(number_tokens(text))
    text_tokens.subtract(str(i) for i in range(1, len(email.asks) + 1))
    if +text_tokens != Counter(number_tokens(markup_text)):
        problems.append("parity")
    for value in _shown_values(email):
        in_text, in_html = text.count(value), markup_text.count(value)
        if in_text == 0 or in_text != in_html:
            problems.append("decided_value_missing")
            break
    masked = _masked(text, email)
    if _EXPONENT.search(masked) or _EXPONENT.search(_masked(markup_text, email)):
        problems.append("exponent")
    if _ISO_DATE.search(masked):
        problems.append("iso_date")
    if _BARE_DOLLAR.search(masked.replace("R$", "").replace("US$", "")):
        problems.append("bare_dollar")
    if email.language == "pt" and re.search(r"\bBRL\b", masked):
        problems.append("brl_code")
    return problems


def _shown_values(email: SupplierEmail) -> list[str]:
    """The fact values (whole strings) each part must show: prices, lead times, dates."""
    words = phrases(email.language)
    shown: list[str] = []
    for position in email.positions:
        for fact in position.facts:
            if isinstance(fact, MissingValues | ValuesToConfirm):
                continue
            _, value = fact_line(fact, position, email.locale, words)
            shown.append(value)
    if email.reply_by is not None:
        shown.append(when_text(email.reply_by, email.locale, words))
    return shown


# --- Structure ---------------------------------------------------------------------------


def _signature_problems(email: SupplierEmail, text: str, words: Phrases) -> list[str]:
    problems: list[str] = []
    closing = words["closing"]
    if sum(1 for line in text.splitlines() if line.strip() == closing) != 1:
        problems.append("signature_closing_count")
    expected = "\n".join([closing, *signature_lines(email.signature)]) + "\n"
    if not text.endswith(expected):
        problems.append("signature_not_last")
    lines = [line.strip() for line in text.splitlines()]
    if any(a and a == b for a, b in zip(lines, lines[1:], strict=False)):
        problems.append("repeated_line")
    return problems


def _html_problems(email: SupplierEmail, markup: str) -> list[str]:
    problems: list[str] = []
    if len(markup.encode()) > HTML_LIMIT:
        problems.append("html_too_large")
    tags = {t.lower() for t in re.findall(r"</?([A-Za-z][A-Za-z0-9]*)", markup)}
    if not tags <= _ALLOWED_TAGS:
        problems.append("html_tag")
    lowered = markup.lower()
    if any(k in lowered for k in ("href=", "src=", "http://", "https://", "<script", "<img")):
        problems.append("html_link")
    if email.positions and '<th scope="col"' not in markup:
        problems.append("html_table_headers")
    return problems


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W\d_]+(?:['’-][^\W\d_]+)*", text)


def base_words(email: SupplierEmail) -> int:
    """Words the email says, outside the positions, fill-ins, legal line and signature."""
    parts = blocks(email)
    counted = [
        parts.salutation,
        parts.purpose,
        *parts.context,
        *parts.asks,
        parts.reply or "",
        parts.no_resend or "",
        parts.closing,
    ]
    return sum(len(_words(text)) for text in counted)


def email_problems(
    email: SupplierEmail, rendered: RenderedEmail, context: GateContext
) -> list[str]:
    """Why this email may not be proposed, as codes; empty when it may."""
    words = phrases(email.language)
    text, markup = rendered.text, rendered.html
    problems: list[str] = []
    if email.language != catalog_language(context.thread_language):
        problems.append("language_not_thread")
    if rendered.subject != email.subject:
        problems.append("subject_not_rendered")
    problems += subject_problems(
        email.subject, context.first_subject, email.rfq_ref, context.rfq_title
    )
    if len(text) > TEXT_LIMIT:
        problems.append("too_long")
    if email.rfq_ref not in text:
        problems.append("rfq_ref_missing_in_body")
    if email.type in _NO_PRICES and any(
        isinstance(f, OfferedTerms | TargetPrice) and f.price is not None
        for p in email.positions
        for f in p.facts
    ):
        problems.append("price_mentioned")
    problems += _number_problems(email, text, html_text(markup))
    problems += _signature_problems(email, text, words)
    problems += _html_problems(email, markup)
    asks = len(email.asks)
    if email.type in ANSWER_REQUESTED:
        if email.reply_by is None:
            problems.append("reply_by_missing")
        elif when_text(email.reply_by, email.locale, words) not in text:
            problems.append("reply_by_not_shown")
        if asks == 0:
            problems.append("asks_missing")
        legal = blocks(email).legal
        if legal is None or text.count(legal) != 1:
            problems.append("legal_line_missing")
    else:
        if email.reply_by is not None:
            problems.append("reply_by_unexpected")
        if asks:
            problems.append("asks_unexpected")
        if words["legal"].split("{", 1)[0] in text:
            problems.append("legal_line_unexpected")
    if asks > MAX_ASKS:
        problems.append("asks_too_many")
    if email.note:
        problems += note_problems(
            email.note, language=email.language, names=names_in(email, context)
        )
        if base_words(email) + len(_words(email.note)) > WORD_BUDGET[email.type]:
            problems.append("note_over_budget")
    return problems


# --- The model's note ----------------------------------------------------------------------

_LINK: Final = re.compile(r"https?:|www\.|://|@|\[[^\]]*\]\(", re.IGNORECASE)
_CURRENCY: Final = re.compile(
    r"[$€£¥₹₽¢]|\b(?:BRL|EUR|USD|GBP|CHF|CNY|JPY|RMB|MXN|ARS|CLP|COP|reais|euros?|dollars?|"
    r"d[oó]lares?)\b",
    re.IGNORECASE,
)
# Salutations and closings: the email has its own, once.
_GREETING: Final = re.compile(
    r"\b(?:dear|hello|hi|prezad[oa]s?|ol[aá]|bom dia|boa tarde|sehr geehrte|hallo|bonjour|"
    r"madame|monsieur|buongiorno|gentil[ie]|estimad[oa]s?|hola|best regards|kind regards|"
    r"regards|sincerely|atenciosamente|abra[cç]os|mit freundlichen|cordialement|"
    r"cordiali saluti|saludos|un saludo|atentamente)\b",
    re.IGNORECASE,
)
# Commitment beyond the mandate's own list, and pressure no email of ours should carry.
_COMMITTING: Final = re.compile(
    r"\b(?:go ahead|we(?:'ll| will) (?:buy|order|take)|deal|fechad[oa]|aprovad[oa]|"
    r"approved|wir bestellen|bestellen|commandons|ordiniamo|pedimos (?:as|os) pe[cç]as|"
    r"compraremos|we will purchase|accepted|aceit[oa]|accettat[oa]|aceptad[oa])\b",
    re.IGNORECASE,
)
_THREAT: Final = re.compile(
    r"\b(?:blacklist|lawsuit|legal action|penalt(?:y|ies)|boycott|multa|processo judicial|"
    r"lista negra|klage|strafe|sanction|penale)\b",
    re.IGNORECASE,
)
_STOPWORDS: Final[dict[str, frozenset[str]]] = {
    "en": frozenset(
        "the and to of we you your our for with is are this that please thank thanks in on "
        "it be will as at".split()
    ),
    "pt": frozenset(
        "o os as de do da dos das e que para com não uma um nos sua seu nossa nosso por é em "
        "obrigado agradecemos ao aos pela pelo".split()
    ),
    "de": frozenset(
        "der die das und wir sie ihr ihre ihren für mit nicht bitte zu von ist ein eine den "
        "dem uns ihnen vielen dank".split()
    ),
    "fr": frozenset(
        "le les des et nous vous votre notre pour avec merci une est pas du au sur ce cette "
        "nos vos".split()
    ),
    "it": frozenset(
        "il lo gli di che per con non grazie vostra vostro nostra nostro della delle sono una "
        "ci vi del al".split()
    ),
    "es": frozenset(
        "el los las y que para con no gracias su sus nuestra nuestro del una un es en por les "
        "nos".split()
    ),
}


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def note_language(note: str) -> str | None:
    """The catalog language whose common words the note uses most; None when none appear."""
    tokens = Counter(w.casefold() for w in _words(note))
    scores = {lang: sum(tokens[w] for w in stop) for lang, stop in _STOPWORDS.items()}
    best = max(scores.values())
    if best == 0:
        return None
    leaders = [lang for lang, score in scores.items() if score == best]
    return leaders[0] if len(leaders) == 1 else "|".join(sorted(leaders))


def names_in(email: SupplierEmail, context: GateContext) -> list[str]:
    """Names the note may not write: the supplier, the buyer, the company, other suppliers."""
    names = [
        email.supplier_name,
        trading_name(email.supplier_name),
        email.buyer_company,
        email.signature.name,
        *context.other_suppliers,
        *(trading_name(n) for n in context.other_suppliers),
    ]
    return [n for n in names if n.strip()]


def _sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?…])\s+", text.strip()) if s.strip()])


def note_problems(note: str, *, language: str, names: Iterable[str]) -> list[str]:
    """Why the model's note may not go out, as codes; empty when it may (or is empty)."""
    text = " ".join(note.split())
    if not text:
        return []
    problems: list[str] = []
    if len(_words(text)) > NOTE_MAX_WORDS:
        problems.append("note_too_long")
    if _sentences(text) > NOTE_MAX_SENTENCES:
        problems.append("note_too_many_sentences")
    if re.search(r"\d", text):
        problems.append("note_digits")
    if _CURRENCY.search(text):
        problems.append("note_currency")
    if _LINK.search(text):
        problems.append("note_link")
    if _GREETING.search(text):
        problems.append("note_greeting")
    if text.lstrip()[:1] in ("-", "*", "•", "#", ">"):
        problems.append("note_list")
    folded = _fold(text)
    if any(_named(folded, name) for name in names):
        problems.append("note_names")
    if commitment_language(text) or _COMMITTING.search(text):
        problems.append("note_commitment")
    if _THREAT.search(text):
        problems.append("note_threat")
    detected = note_language(text)
    if detected is None or catalog_language(language) not in detected.split("|"):
        problems.append("note_language")
    return problems


def _named(folded_text: str, name: str) -> bool:
    """The name, or its first word when that is distinctive enough ("MetalNova")."""
    words = _fold(clean_text(name)).split()
    forms = [" ".join(words)] + ([words[0]] if len(words) > 1 and len(words[0]) >= 5 else [])
    return any(
        len(form) >= 3 and re.search(rf"(?<!\w){re.escape(form)}(?!\w)", folded_text)
        for form in forms
    )


def problems_text(problems: Sequence[str]) -> str:
    """Codes for a log line (ids and codes only)."""
    return ",".join(sorted(set(problems)))
