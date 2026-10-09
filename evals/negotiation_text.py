"""Reading Bido's emails for the negotiation scenarios: language, signature, dates, amounts.

Deterministic and independent of the product's own detectors where a property
is graded (commitment language, dates in Portuguese); `dates_in` from the
product only adds the formats it already reads.
"""

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

from bido_back.quotes.parsing import dates_in

# --- Normalizing ------------------------------------------------------------------------------


def plain(text: str) -> str:
    """Lower case, accents removed."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def words(text: str) -> str:
    """Lower case, accents removed, every non-alphanumeric run as one space."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", plain(text)).split())


def compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", plain(text))


# --- Language ---------------------------------------------------------------------------------

_STOPWORDS: Final[dict[str, frozenset[str]]] = {
    "pt": frozenset(
        "que para com nao por uma os dos das prezados prezadas obrigado obrigada segue nossa "
        "nosso nossas sua seu suas em solicitamos cotacao itens abaixo atenciosamente favor "
        "poderiam proposta prazo entrega pedimos gostariamos voces voce sao esta estao tambem "
        "pelo pela ao aos uma oferta enviem informar informe revisem preco quantidade ate "
        "lembrar analise solicitacao rodada melhor ultima nossa decisao".split()
    ),
    "en": frozenset(
        "the and for you we your our with please thank of to is this by best regards hello on "
        "at us would offer request could let know dear send items below quotation price "
        "lead time following review".split()
    ),
    "de": frozenset(
        "und der die das wir sie ihr ihre ihren fur mit nicht bitte zu von unser unsere bis "
        "freundlichen grussen vielen dank ist ein eine den dem im auf uns ihnen sehr geehrte "
        "damen herren angebot anfrage folgende senden".split()
    ),
    "es": frozenset(
        "el los las y para con por una gracias su nuestra cotizacion estimados saludos le "
        "les usted ustedes envien".split()
    ),
    "it": frozenset(
        "il la di che per con non grazie vostra vostro nostra nostro della delle sono gli "
        "alla entro cordiali saluti vi del buongiorno offerta richiesta".split()
    ),
    "fr": frozenset(
        "le les et pour vous nous votre notre avec merci est pas du au sur cordialement "
        "bonjour nos vos offre demande".split()
    ),
}


def prose(text: str) -> str:
    """The text without table rows (`a | b | c`) and separator lines, which carry the
    buyer's own item descriptions, not the email's language."""
    kept = [
        line
        for line in text.splitlines()
        if line.count("|") < 2 and "\t" not in line and not re.fullmatch(r"[\s\-+=_|]*", line)
    ]
    return "\n".join(kept)


def language_of(text: str) -> str | None:
    """The language most marker words belong to; None with too little text."""
    tokens = Counter(words(prose(text)).split())
    scores = {lang: sum(tokens[w] for w in stop) for lang, stop in _STOPWORDS.items()}
    best = max(scores, key=lambda lang: scores[lang])
    return best if scores[best] >= 3 else None


# --- Signature --------------------------------------------------------------------------------

_SIGN_OFFS: Final = (
    "atenciosamente", "cordialmente", "saudacoes", "abracos", "att", "best regards",
    "kind regards", "regards", "sincerely", "mit freundlichen grussen", "freundliche grusse",
    "beste grusse", "viele grusse", "cordiali saluti", "distinti saluti", "cordialement",
    "saludos", "un saludo", "atentamente",
)  # fmt: skip


def signature_problem(body: str, names: Sequence[str]) -> str | None:
    """Why the email is not signed exactly once; None when it is.

    Two sign-offs, the same line twice in a row in the closing, or the buyer's
    name or company written twice as a line of its own ("Biduuuuu" / "Biduuuuu").
    """
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    sign_offs = [i for i, line in enumerate(lines) if words(line) in _SIGN_OFFS]
    if len(sign_offs) > 1:
        return f"{len(sign_offs)} sign-offs"
    closing = lines[sign_offs[0] :] if sign_offs else lines[-6:]
    for first, second in zip(closing, closing[1:], strict=False):
        if words(first) == words(second):
            return f"{first!r} twice in the closing"
    for name in {words(n) for n in names if n.strip()}:
        count = sum(1 for line in lines if words(line) == name)
        if count > 1:
            return f"{name!r} signed {count} times"
    return None


# --- Dates ------------------------------------------------------------------------------------

_PT_MONTHS: Final = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}  # fmt: skip
_PT_DATE: Final = re.compile(r"(\d{1,2})\s+de\s+([a-z]+)\s+de\s+(\d{4})")
_DAY_MONTH: Final = re.compile(r"(?<![\d/.,])(\d{1,2})/(\d{1,2})(?![\d/])")


def written_dates(text: str, around: date) -> set[date]:
    """Every calendar date written in `text`: ISO, 20/10/2026, 20.10.2026, "20 de outubro de
    2026", "20. Oktober 2026", "October 20, 2026", and "20/10" (the next such day after
    `around`, within a year)."""
    found = set(dates_in(text))
    for day, month, year in _PT_DATE.findall(plain(text)):
        number = _PT_MONTHS.get(month)
        if number is not None:
            try:
                found.add(date(int(year), number, int(day)))
            except ValueError:
                pass
    for day, month in _DAY_MONTH.findall(text):
        for year in (around.year, around.year + 1):
            try:
                candidate = date(year, int(month), int(day))
            except ValueError:
                continue
            if candidate >= around:
                found.add(candidate)
                break
    return found


# --- Numbers and amounts ----------------------------------------------------------------------

_NUMBER: Final = re.compile(r"\d+(?:[.,]\d+)*")
_MONEY: Final = re.compile(
    r"(?:R\$|US\$|€|\b(?:BRL|EUR|USD)\b)\s?(\d[\d.,]*\d|\d)"
    r"|(\d[\d.,]*\d|\d)\s?(?:€|\b(?:BRL|EUR|USD|reais|euros?)\b)",
    re.IGNORECASE,
)


def readings(token: str) -> set[Decimal]:
    """Every value a written number may stand for, either decimal separator."""
    values: set[Decimal] = set()
    token = token.strip(".,")
    for decimal_sep, group_sep in ((".", ","), (",", ".")):
        candidate = token.replace(group_sep, "").replace(decimal_sep, ".")
        if candidate.count(".") > 1:
            continue
        try:
            values.add(Decimal(candidate).normalize())
        except InvalidOperation:
            continue
    return values


def numbers(text: str) -> set[Decimal]:
    found: set[Decimal] = set()
    for match in _NUMBER.finditer(text):
        found |= readings(match.group(0))
    return found


def amounts(text: str) -> list[set[Decimal]]:
    """The money amounts written next to a currency: each one's possible values."""
    found: list[set[Decimal]] = []
    for match in _MONEY.finditer(text):
        token = match.group(1) or match.group(2)
        if token:
            found.append(readings(token))
    return found


def norm(values: Iterable[Decimal]) -> set[Decimal]:
    return {v.normalize() for v in values}


# --- Commitment -------------------------------------------------------------------------------

# Accepting, awarding or ordering: what an automatic email must never say. Independent of
# the product's own detector (`mandates.policy.commitment_language`).
_COMMITMENT: Final = re.compile(
    "|".join(
        (
            r"\bwe (?:hereby )?accept\b",
            r"\b(?:we )?accept(?:ed)? your (?:offer|quote|quotation|proposal)\b",
            r"\bwe (?:will|shall|would like to) (?:order|place (?:an|the|our) order)\b",
            r"\bplease (?:consider|treat) this (?:as )?(?:our|an) order\b",
            r"\byou (?:have been|are) (?:awarded|selected)\b",
            r"\bconfirm(?:ing)? (?:the|our) order\b",
            r"\baceitamos\b",
            r"\bvamos (?:fechar|comprar|emitir o pedido)\b",
            r"\bfechamos (?:o )?(?:negocio|pedido|com voces)\b",
            r"\bconfirmamos (?:o|a) (?:pedido|compra)\b",
            r"\bemitiremos (?:o )?pedido\b",
            r"\baprovamos (?:a )?(?:sua )?proposta\b",
            r"\bwir (?:akzeptieren|nehmen ihr angebot an|bestellen)\b",
            r"\bden zuschlag\b",
            r"\bauftrag (?:erteilen|vergeben)\b",
        )
    ),
    re.IGNORECASE,
)


def commitment(text: str) -> str | None:
    match = _COMMITMENT.search(plain(text))
    return match.group(0) if match else None


# --- What a clarification asks, read from its text ------------------------------------------

_FIELD_WORDS: Final[dict[str, tuple[str, ...]]] = {
    "valid_until": ("valid", "validade", "validity", "gultig", "validita"),
    "quantity": ("quantity", "quantidade", "menge", "mengenstaffel", "tier", "faixa"),
    "lead_time": ("lead time", "prazo de entrega", "lieferzeit", "delivery time"),
    "unit_price": ("unit price", "preco unitario", "stuckpreis"),
    "currency": ("currency", "moeda", "wahrung"),
}
_POSITION: Final = re.compile(r"\b(?:item|position|pos|linha|line)\s+(\d{1,3})\b")


def text_items(body: str, lines: Sequence[tuple[int, str, str]]) -> list[tuple[int | None, str]]:
    """(position, field) a clarification's text asks about, line by line: the fallback when
    the email cannot be matched to the structured task it was written from. `lines` are
    the RFQ's (position, description, part number)."""
    found: list[tuple[int | None, str]] = []
    for raw in body.splitlines():
        text = words(raw)
        fields = [
            name
            for name, keys in _FIELD_WORDS.items()
            if any(re.search(rf"\b{re.escape(key)}", text) for key in keys)
        ]
        if not fields:
            continue
        match = _POSITION.search(text)
        position = int(match.group(1)) if match else None
        if position is None:
            position = next(
                (p for p, name, part in lines if words(name) in text or part in text), None
            )
        found.extend((position, name) for name in fields)
    return found
