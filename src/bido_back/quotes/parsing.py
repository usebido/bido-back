"""Locale-aware reading of numbers, currencies and dates in supplier text.

Suppliers write `1.234,56 €`, `EUR 1,234.56`, `1 234,56`, `0,45`, `1'000`.
Some tokens are genuinely ambiguous (`1,000` is one thousand in English and
one in German). Nothing here guesses silently: a token yields every valid
reading, and the caller decides with a locale hint, or flags it for review.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

DecimalSeparator = Literal[",", "."]

# Languages whose suppliers write a decimal comma (ISO 639-1).
DECIMAL_COMMA_LANGUAGES = frozenset(
    {
        "bg", "cs", "da", "de", "el", "es", "et", "fi", "fr", "hr", "hu", "id", "it", "lt",
        "lv", "nl", "no", "nb", "pl", "pt", "ro", "ru", "sk", "sl", "sr", "sv", "tr", "uk",
        "vi",
    }
)  # fmt: skip


def decimal_separator_for(language: str | None) -> DecimalSeparator | None:
    """The decimal separator a supplier writing in `language` most likely uses."""
    if not language:
        return None
    code = language.strip().lower().split("-", 1)[0].split("_", 1)[0]
    return "," if code in DECIMAL_COMMA_LANGUAGES else "."


def normalize_text(text: str) -> str:
    """For containment checks: NFKC, casefolded, table pipes and runs of space collapsed."""
    text = unicodedata.normalize("NFKC", text).casefold().replace("|", " ")
    return " ".join(text.split())


# --- Numbers -------------------------------------------------------------------

# Grouping characters besides `.` and `,`: space, no-break spaces, apostrophes.
_GROUP_SPACES = " \u00a0\u202f\u2009'\u2019"
_SPACES = re.compile("[ \u00a0\u202f\u2009]+")
_NUMBER_TOKEN = re.compile("\\d(?:[\\d.,\u00a0\u202f\u2009'\u2019 ]*\\d)?")


@dataclass(frozen=True, slots=True)
class Reading:
    value: Decimal
    # The decimal separator this reading assumes; None for an integer reading.
    decimal: DecimalSeparator | None


def readings(token: str) -> list[Reading]:
    """Every valid reading of one number token (digits and separators only)."""
    found: dict[Decimal, Reading] = {}
    for decimal in (None, ".", ","):
        value = _read(token, decimal)
        if value is not None and value not in found:
            found[value] = Reading(value, decimal)
    return list(found.values())


def _read(token: str, decimal: DecimalSeparator | None) -> Decimal | None:
    integer, fraction = token, ""
    if decimal is not None:
        if token.count(decimal) != 1:
            return None
        integer, fraction = token.split(decimal)
        if not fraction.isdigit():
            return None
    separators = {c for c in integer if not c.isdigit()}
    if separators:
        # One grouping character only (spaces of any kind count as one).
        kinds = {" " if c in _GROUP_SPACES else c for c in separators}
        if len(kinds) != 1 or decimal in kinds:
            return None
        groups = re.split(r"[^\d]", integer)
        if not (1 <= len(groups[0]) <= 3) or any(len(g) != 3 for g in groups[1:]):
            return None
        if groups[0].startswith("0"):
            return None  # `0.045` is never grouped
        integer = "".join(groups)
    if not integer.isdigit():
        return None
    try:
        return Decimal(f"{integer}.{fraction}" if fraction else integer)
    except InvalidOperation:
        return None


def number_tokens(text: str, *, with_pieces: bool = True) -> list[list[Reading]]:
    """The readings of every number in `text`.

    A run that has no valid reading as one number (`1000 2000`, `31.12.2026`)
    is split into its digit groups, read separately. With `with_pieces`, a run
    with spaces that does read as one number (`1 100`) also yields its pieces,
    since the space may separate two numbers (`Pos 1 100 pcs`).
    """
    tokens: list[list[Reading]] = []
    for match in _NUMBER_TOKEN.finditer(text):
        token = match.group().strip()
        found = readings(token)
        if found:
            tokens.append(found)
            if not with_pieces or _SPACES.search(token) is None:
                continue
        for part in _SPACES.split(token):
            pieces = readings(part)
            if pieces:
                tokens.append(pieces)
            else:
                tokens.extend(readings(digits) for digits in re.findall(r"\d+", part))
    return tokens


@dataclass(frozen=True, slots=True)
class NumberMatch:
    found: bool
    # The token had several readings and the matching one is not the locale's.
    ambiguous: bool = False


# A label that introduces an identifier, ending right before the digits: "cód. 000105",
# "Art.-Nr. 4711", "ref: 205", "P/N 3300".
_CODE_LABEL = re.compile(
    r"(?:\bc[óo]d(?:igo)?|\bcode|\bart(?:ikel|icolo|\.-?nr)?|\bref|\bp/?n|\bmpn|\bsku"
    r"|\bn[º°]|\bnr|\bitem|\bpos(?:i[cç][aã]o|ition|ici[oó]n|izione)?)\.?\s*[:#]?\s*$",
    re.IGNORECASE,
)


# Letters (and digits) written against the number, optionally joined by - _ /: "MTR-100",
# "RFQ-6", "X100". Digits alone before a dash are a range ("10-15 dias"), not a code.
_CODE_PREFIX = re.compile(r"([A-Za-z][A-Za-z0-9]*)[-_/]?$|#$")


def _is_code(text: str, start: int, token: str) -> bool:
    """Whether the number at `start` is part of an identifier rather than a value.

    An identifier is never evidence for a price, a quantity or a lead time: the 105 in
    "cód. 000105", the 100 in "MTR-100" or the 6 in "RFQ-6".
    """
    before = text[:start]
    attached = _CODE_PREFIX.search(before)
    if attached is not None:
        word = attached.group(1)
        # "BRL10" / "EUR10": a currency written against its amount is not a code.
        if word is None or word.upper() not in ISO_CURRENCIES:
            return True
    if token.isdigit() and len(token) >= 3 and token.startswith("0"):
        return True  # zero-padded identifiers ("000105")
    return _CODE_LABEL.search(before[-20:]) is not None


def _value_tokens(text: str) -> list[list[Reading]]:
    """`number_tokens`, without the numbers that are part of identifiers."""
    tokens: list[list[Reading]] = []
    for match in _NUMBER_TOKEN.finditer(text):
        token, start = match.group(), match.start()
        while token.strip():
            start += len(token) - len(token.lstrip())
            token = token.strip()
            if not _is_code(text, start, token):
                tokens.extend(number_tokens(token))
                break
            # Drop the identifier's digits; a number may follow in the same run
            # ("MTR-100, 100 pcs").
            code = re.match(r"\d+", token)
            head = code.end() if code is not None else len(token)
            gap = re.match(r"[^\d]*", token[head:])
            skip = head + (gap.end() if gap is not None else 0)
            start += skip
            token = token[skip:]
    return tokens


def find_number(
    value: Decimal, text: str, *, decimal: DecimalSeparator | None = None
) -> NumberMatch:
    """Whether `value` is one reading of a number in `text`.

    Ambiguous when the only tokens it matches have several readings and either
    no locale is known or the matching reading is not the locale's. Numbers inside
    identifiers ("cód. 000105", "MTR-100") never match: they are not values.
    """
    ambiguous_match = False
    for token in _value_tokens(text):
        matching = [r for r in token if r.value == value]
        if not matching:
            continue
        if len(token) == 1:
            return NumberMatch(found=True)
        if decimal is not None and _preferred(token, decimal).value == value:
            return NumberMatch(found=True)
        ambiguous_match = True
    return NumberMatch(found=ambiguous_match, ambiguous=ambiguous_match)


def _preferred(token: list[Reading], decimal: DecimalSeparator) -> Reading:
    for reading in token:
        if reading.decimal == decimal:
            return reading
    for reading in token:
        # An integer reading means the separator was grouping, which is right
        # when the token holds no `decimal` character.
        if reading.decimal is None:
            return reading
    return token[0]


# Numbers that look like decimals but may be a day and month ("04.12", "1,05") or a clock
# time ("14.30 Uhr", "alle 9.30"): they prove no separator.
_DAY_MONTH_LIKE = re.compile(
    r"(?<![\d.,])(?:[1-9]|[0-2]\d|3[01])[.,](?:0?[1-9]|1[0-2])(?!\d|[.,]\d)"
)
_CLOCK_TIME = re.compile(
    r"(?<![\d.,])\d{1,2}[.:]\d{2}(?=\s*(?:h|hrs?|uhr|ore)\b)"
    r"|\b(?:um|ore|alle|às)\s+\d{1,2}[.:]\d{2}(?!\d|[.,]\d)",
    re.IGNORECASE,
)


def decimal_separator_in(text: str) -> DecimalSeparator | None:
    """The decimal separator the numbers in `text` prove, or None.

    A number with one reading only, and that reading a decimal, proves its separator:
    `2.75` or `5,500.00` a point, `2,75` or `5.500,00` a comma. `2,000` (one thousand
    or two) proves nothing, nor do identifiers, numbers that may be a day and month or
    a clock time. None also when the numbers disagree.
    """
    masked = _CLOCK_TIME.sub(" ", _DAY_MONTH_LIKE.sub(" ", text))
    votes: set[DecimalSeparator] = {
        token[0].decimal
        for token in _value_tokens(masked)
        if len(token) == 1 and token[0].decimal is not None
    }
    return votes.pop() if len(votes) == 1 else None


def stated_values(
    value: str, words: str, *, decimal: DecimalSeparator | None = None
) -> set[Decimal]:
    """What the one number written as `value` may mean, read in `words` (where it came from).

    `value` selects the number; `words` decide how it reads: every number of `words`
    that `value` can be one reading of counts, read with `decimal` when the separator is
    proven, else in each of its readings. Empty when `value` is not one unsigned number
    or is not in `words`; several values when the number is ambiguous (`2,000` with no
    proven separator gives 2000 and 2).
    """
    stripped = value.strip()
    tokens = number_tokens(stripped, with_pieces=False)
    if len(tokens) != 1 or re.match("^[-\u2212]", stripped) is not None:
        return set()
    token = tokens[0]
    meant = {_preferred(token, decimal).value} if decimal is not None else {r.value for r in token}
    found: set[Decimal] = set()
    for written in _value_tokens(words):
        if not any(r.value in meant for r in written):
            continue
        if decimal is not None:
            found.add(_preferred(written, decimal).value)
        else:
            found.update(r.value for r in written)
    return found


_CANONICAL = re.compile(r"-?\d+(?:\.\d+)?")


def parse_canonical(value: str) -> Decimal | None:
    """A number in machine form (`1234.5`, `-3`): what the model is asked to write."""
    stripped = value.strip()
    if _CANONICAL.fullmatch(stripped) is None:
        return None
    return Decimal(stripped)


def parse_decimal(text: str, *, decimal: DecimalSeparator | None = None) -> Decimal | None:
    """One number written by a person, or None unless there is exactly one clear reading.

    The text must hold exactly one number token; currency symbols and words
    around it are ignored. A token with several readings (`1,000`) resolves
    only with `decimal`.
    """
    stripped = text.strip()
    tokens = number_tokens(stripped, with_pieces=False)
    if len(tokens) != 1:
        return None
    token = tokens[0]
    if len(token) == 1:
        value = token[0].value
    elif decimal is not None:
        value = _preferred(token, decimal).value
    else:
        return None
    negative = re.match("^\\s*[-\u2212]", stripped) is not None
    return -value if negative else value


@dataclass(frozen=True, slots=True)
class Amount:
    value: Decimal | None
    currency: str | None
    ambiguous: bool = False


def parse_amount(text: str, *, decimal: DecimalSeparator | None = None) -> Amount:
    """`1.234,56 €` -> 1234.56 EUR. `ambiguous` when the separator needed a locale."""
    currencies = detect_currencies(text)
    currency = next(iter(currencies)) if len(currencies) == 1 else None
    tokens = number_tokens(text, with_pieces=False)
    ambiguous = len(tokens) == 1 and len(tokens[0]) > 1 and decimal is None
    return Amount(parse_decimal(text, decimal=decimal), currency, ambiguous)


# --- Currencies ------------------------------------------------------------------

ISO_CURRENCIES = frozenset(
    {
        "EUR", "USD", "GBP", "CHF", "CNY", "JPY", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF",
        "RON", "BGN", "TRY", "KRW", "TWD", "HKD", "SGD", "INR", "CAD", "AUD", "MXN", "THB",
        "MYR", "VND", "ILS", "AED", "ZAR", "BRL",
    }
)  # fmt: skip

_CURRENCY_WORDS = {
    "€": "EUR",
    "£": "GBP",
    "us$": "USD",
    "r$": "BRL",
    # A bare `$` only: `R$`, `US$`, `C$`, `A$`... are other currencies (see below).
    "$": "USD",
    "zł": "PLN",
    "kč": "CZK",
    "rmb": "CNY",
    "円": "JPY",
    "euro": "EUR",
    "euros": "EUR",
    "eur.": "EUR",
}
_CODE = re.compile(r"(?<![A-Za-z])([A-Za-z]{3})(?![A-Za-z])")


def detect_currencies(text: str) -> set[str]:
    """ISO codes named in `text` by code, symbol or word. `¥` and `kr` are ambiguous: skipped."""
    found: set[str] = set()
    for match in _CODE.finditer(text):
        code = match.group(1).upper()
        if code in ISO_CURRENCIES:
            found.add(code)
        elif code == "RMB":
            found.add("CNY")
    lowered = text.casefold()
    for word, code in _CURRENCY_WORDS.items():
        if word.isalpha():
            if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", lowered):
                found.add(code)
        elif word[0].isalpha() or word == "$":
            # `R$ 46,90` is reais: a symbol never matches inside a longer prefix.
            if re.search(rf"(?<![^\W\d_]){re.escape(word)}", lowered):
                found.add(code)
        elif word in lowered:
            found.add(code)
    return found


# --- Dates -----------------------------------------------------------------------

_MONTHS = {
    # English
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9,
    "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    # German
    "januar": 1, "jänner": 1, "februar": 2, "märz": 3, "mai": 5, "juni": 6, "juli": 7,
    "oktober": 10, "dezember": 12, "okt": 10, "dez": 12, "mär": 3,
    # French
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "juin": 6,
    "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11,
    "décembre": 12, "decembre": 12,
    # Italian
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6,
    "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10, "dicembre": 12,
    # Spanish
    "enero": 1, "febrero": 2, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    # Portuguese
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "maio": 5, "junho": 6,
    "julho": 7, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}  # fmt: skip

_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_NUMERIC_DATE = re.compile(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})(?!\d)")
# "30 de outubro de 2026", "30. Oktober 2026", "30º de octubre del 2026".
_DAY_MONTH_YEAR = re.compile(
    r"(?<!\d)(\d{1,2})(?:º|°)?\.?\s+(?:de\s+)?([^\W\d_]+)\.?,?\s+(?:de\s+|del\s+)?(\d{4})(?!\d)"
)
_MONTH_DAY_YEAR = re.compile(r"(?<![^\W\d_])([^\W\d_]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})")
# Without a year: "30/10", "30.10.", "30 ottobre", "30 de outubro", "October 30".
_DAY_MONTH = re.compile(r"(?<![\d./-])(\d{1,2})[./-](\d{1,2})(?!\d|[./-]\d)")
_DAY_MONTH_NAME = re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th|º|°)?\.?\s+(?:de\s+)?([^\W\d_]+)")
_MONTH_NAME_DAY = re.compile(r"(?<![^\W\d_])([^\W\d_]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?!\d)")


def dates_in(text: str) -> set[date]:
    """Every calendar date written in `text` (day-first and month-first both kept)."""
    found: set[date] = set()
    for year, month, day in _ISO_DATE.findall(text):
        _add_date(found, int(year), int(month), int(day))
    for first, second, year in _NUMERIC_DATE.findall(text):
        full_year = int(year) + 2000 if len(year) == 2 else int(year)
        _add_date(found, full_year, int(second), int(first))
        _add_date(found, full_year, int(first), int(second))
    lowered = text.casefold()
    for day, month, year in _DAY_MONTH_YEAR.findall(lowered):
        if month in _MONTHS:
            _add_date(found, int(year), _MONTHS[month], int(day))
    for month, day, year in _MONTH_DAY_YEAR.findall(lowered):
        if month in _MONTHS:
            _add_date(found, int(year), _MONTHS[month], int(day))
    return found


def _add_date(found: set[date], year: int, month: int, day: int) -> None:
    try:
        found.add(date(year, month, day))
    except ValueError:
        pass


def day_months_in(text: str) -> set[tuple[int, int]]:
    """Every (month, day) written in `text` without a year: `30/10`, `30 ottobre`, `Oct 30`.

    Numeric forms keep both readings (`05/10` is 5 October or 10 May).
    """
    found: set[tuple[int, int]] = set()
    for first, second in _DAY_MONTH.findall(text):
        _add_day_month(found, int(second), int(first))
        _add_day_month(found, int(first), int(second))
    lowered = text.casefold()
    for day, month in _DAY_MONTH_NAME.findall(lowered):
        if month in _MONTHS:
            _add_day_month(found, _MONTHS[month], int(day))
    for month, day in _MONTH_NAME_DAY.findall(lowered):
        if month in _MONTHS:
            _add_day_month(found, _MONTHS[month], int(day))
    return found


def _add_day_month(found: set[tuple[int, int]], month: int, day: int) -> None:
    try:
        date(2024, month, day)  # a leap year: 29 February is a valid day
    except ValueError:
        return
    found.add((month, day))


# --- Incoterms and lead times ------------------------------------------------------

INCOTERMS = frozenset(
    {"EXW", "FCA", "CPT", "CIP", "DAP", "DPU", "DDP", "FAS", "FOB", "CFR", "CIF", "DAT"}
)
_INCOTERM_NAMES = {
    "ex works": "EXW",
    "ab werk": "EXW",
    "free carrier": "FCA",
    "frei frachtführer": "FCA",
    "carriage paid to": "CPT",
    "carriage and insurance paid": "CIP",
    "delivered at place": "DAP",
    "delivered at place unloaded": "DPU",
    "delivered duty paid": "DDP",
    "geliefert verzollt": "DDP",
    "free alongside ship": "FAS",
    "free on board": "FOB",
    "cost and freight": "CFR",
    "cost, insurance and freight": "CIF",
}


def incoterms_in(text: str) -> set[str]:
    found = {
        code.upper() for code in _CODE.findall(text) if code.upper() in INCOTERMS and code.isupper()
    }
    lowered = normalize_text(text)
    found.update(code for name, code in _INCOTERM_NAMES.items() if name in lowered)
    return found


_WEEK_WORDS = re.compile(
    r"(?:(?<=\d)|\b)(weeks?|wks?|w|wochen?|semaines?|settiman[ae]|semanas?|tygodni[ea]?"
    r"|týdn[ůy]|weken)\b|週",
    re.IGNORECASE,
)
_WORKING_DAY_WORDS = re.compile(
    r"\b(working|business|work)\s+days?\b|\barbeitstage?n?\b|\bwerktage?n?\b|jours ouvr"
    r"|\bdias?\s+(?:úteis|uteis|útil|util)\b|\bd[ií]as?\s+(?:h[aá]bil(?:es)?|laborables?)\b"
    r"|\bgiorn[oi]\s+lavorativ[oi]\b|\bgg\.?\s+lavorativ[oi]\b|\bwerkdag(?:en)?\b"
    r"|\bdni\s+robocz\w*",
    re.IGNORECASE,
)
# Abbreviations: German "Arbeitstage" (AT), English "business/working days" (BD, WD).
# Case-sensitive so English "at" and "wd" inside words do not match.
_WORKING_DAYS_ABBREVIATION = re.compile(r"(?:(?<=\d)|\b)(?:AT|BD|WD)\b")
# Any day unit, including business days: "15 days", "15 dias", "15 Tage", "15 gg", "15d".
_DAY_WORDS = re.compile(
    r"\bdays?\b|\bd[ií]as?\b|\btage?n?\b|kalendertage?n?\b|arbeitstage?n?\b|werktage?n?\b"
    r"|\bjours?\b|\bgiorn[oi]\b|\bgg\b|\bdn[ií]\b|\bdag(?:en|ar)?\b|(?<=\d)\s?(?:d|j|t|gg)\b",
    re.IGNORECASE,
)
# Days said to be calendar days, even next to a business-day word.
_CALENDAR_DAY_WORDS = re.compile(
    r"\bcalendar\s+days?\b|\bconsecutive\s+days?\b|\bdias?\s+corridos\b|\bkalendertage?n?\b"
    r"|\bjours?\s+calendaires?\b|\bgiorni\s+(?:di\s+calendario|solari)\b"
    r"|\bd[ií]as?\s+(?:naturales|calendario|corridos)\b",
    re.IGNORECASE,
)
_MONTH_WORDS = re.compile(
    r"\bmonths?\b|\bm[eê]s(?:es)?\b|\bmonat(?:e|en)?\b|\bmois\b|\bmes[ei]\b|\bmaand(?:en)?\b"
    r"|\bmiesi[aą]c\w*",
    re.IGNORECASE,
)
# A hedged figure: "approximately 10 days", "ca. 3 Wochen", "cerca de 15 dias", "previsão".
_APPROXIMATE_WORDS = re.compile(
    r"\b(?:approx(?:imately|imate)?\b\.?|about\b|around\b|roughly\b|circa\b|ca\.|"
    r"aprox(?:imadamente)?\b\.?|cerca\s+de\b|em\s+torno\s+de\b|por\s+volta\s+de\b|"
    r"alrededor\s+de\b|unos\b|unas\b|etwa\b|ungef[äa]hr\b|voraussichtlich\b|environ\b|"
    r"approximativement\b|approssimativamente\b|indicativ\w*|previs\w*|estimad\w*|"
    r"estimated\b|expected\b|tentative\w*)",
    re.IGNORECASE,
)
# Two numbers given as a range: "2-3 weeks", "4 a 6 semanas", "2 bis 3 Wochen", "3/4 settimane".
_RANGE = re.compile(
    r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*(?:-|–|—|/|to|a|até|ate|bis|à|al|y|e|ou|or|oder|o)\s*"
    r"(\d+(?:[.,]\d+)?)(?![\d.,]*\d)",
    re.IGNORECASE,
)
# Goods available at once: "ex stock", "ab Lager", "pronta entrega", "pronta consegna".
_STOCK_WORDS = re.compile(
    r"\b(?:ex[-\s]?stock|from\s+stock|in\s+stock|on\s+stock|immediate(?:ly)?|ab\s+lager"
    r"|lagerware|sofort|pronta\s+entrega|imediat[oa]|em\s+estoque|de\s+stock|en\s+stock"
    r"|inmediat[oa]|imm[ée]diat\w*|pronta\s+consegna|a\s+magazzino|subito)\b",
    re.IGNORECASE,
)

# A business week: 5 business days are one calendar week (7 days). Stored lead times
# are counted by `bido_back.quotes.lead_time` (business days from the offer date,
# skipping weekends and the supplier's public holidays); this rough rule only
# recognizes a number a model converted itself ("25 dias úteis" written as 35).
BUSINESS_DAYS_PER_WEEK = 5


def mentions_weeks(text: str) -> bool:
    return _WEEK_WORDS.search(text) is not None


def mentions_working_days(text: str) -> bool:
    return (
        _WORKING_DAY_WORDS.search(text) is not None
        or _WORKING_DAYS_ABBREVIATION.search(text) is not None
    )


def mentions_days(text: str) -> bool:
    """`42 days`, `25 dias`, `30 Tage`, `42d`: a duration written in days (of any kind,
    calendar or business)."""
    return _DAY_WORDS.search(text) is not None or mentions_working_days(text)


def mentions_calendar_days(text: str) -> bool:
    return _CALENDAR_DAY_WORDS.search(text) is not None


def mentions_months(text: str) -> bool:
    return _MONTH_WORDS.search(text) is not None


def mentions_approximation(text: str) -> bool:
    return _APPROXIMATE_WORDS.search(text) is not None


def mentions_stock(text: str) -> bool:
    return _STOCK_WORDS.search(text) is not None


def ranges_in(text: str) -> list[tuple[Decimal, Decimal]]:
    """Every `low`-`high` range written in `text`, low < high (`2-3`, `4 a 6`, `2 bis 3`)."""
    found: list[tuple[Decimal, Decimal]] = []
    for match in _RANGE.finditer(text):
        first, second = match.group(1), match.group(2)
        # "Item 5 - 000106" is a position and a part code, not the range 5 to 106.
        if _is_code(text, match.start(1), first) or _is_code(text, match.start(2), second):
            continue
        low = Decimal(first.replace(",", "."))
        high = Decimal(second.replace(",", "."))
        if low < high:
            found.append((low, high))
    return found


# "valid for 15 days from the date of our original offer": the period starts earlier.
_ORIGINAL_OFFER = re.compile(
    r"\b(?:original|first|initial|previous|earlier|prior)\s+(?:offer|quot(?:e|ation)|proposal)"
    r"|\b(?:proposta|cota[cç][aã]o|oferta|or[cç]amento)\s+(?:original|inicial|anterior)"
    r"|\bprimeira\s+(?:proposta|cota[cç][aã]o|oferta)"
    r"|\b(?:cotizaci[oó]n|propuesta)\s+(?:original|inicial|anterior)"
    r"|\bprimera\s+(?:oferta|cotizaci[oó]n|propuesta)"
    r"|\b(?:urspr[üu]nglich\w*|erst\w*|vorherig\w*|bisherig\w*)\s+angebot\w*|\berstangebot\w*"
    r"|\boffre\s+(?:initiale|originale|pr[ée]c[ée]dente)|\bpremi[eè]re\s+offre|\bdevis\s+initial"
    r"|\bofferta\s+(?:originale|iniziale|precedente)|\bprima\s+offerta",
    re.IGNORECASE,
)


def mentions_original_offer(text: str) -> bool:
    return _ORIGINAL_OFFER.search(text) is not None


# Delivery timing vocabulary ("additional days ... production", "Lieferzeit", "prazo").
_DELIVERY_WORDS = re.compile(
    r"\b(?:lead\s*time|deliver\w*|ship\w*|dispatch\w*|production|manufactur\w*|ready"
    r"|prazo|entrega\w*|produ[çc][ãa]o|fabrica[çc][ãa]o|embarque"
    r"|liefer\w*|produktion\w*|fertigung\w*|versand\w*"
    r"|consegn\w*|produzion\w*|spedizion\w*|plazo|producci[oó]n|env[ií]o"
    r"|d[ée]lai\w*|livraison\w*|exp[ée]dition\w*)",
    re.IGNORECASE,
)
# Words that make a sentence about price, payment or validity rather than timing.
_PRICE_TERMS_WORDS = re.compile(
    r"\b(?:price\w*|cost\w*|pay\w*|valid\w*|discount\w*|pre[çc]o\w*|custo\w*|pag\w*"
    r"|v[aá]lid\w*|descont\w*|preis\w*|kosten\w*|zahl\w*|g[üu]ltig\w*|rabatt\w*|prix"
    r"|co[uû]t\w*|paiement\w*|valable\w*|remise\w*|prezz\w*|cost[oi]|pagament\w*"
    r"|precio\w*|pago\w*)",
    re.IGNORECASE,
)


def is_delivery_timing(text: str) -> bool:
    """A sentence only about when goods arrive: a time unit and delivery or production
    words, and nothing about price, payment or validity ("We may need a few
    additional days for the shafts, depending on production availability")."""
    has_unit = mentions_days(text) or mentions_weeks(text) or mentions_months(text)
    return (
        has_unit
        and _DELIVERY_WORDS.search(text) is not None
        and _PRICE_TERMS_WORDS.search(text) is None
        and not detect_currencies(text)
    )


def business_days_to_calendar(business_days: int) -> int:
    """Whole business weeks, rounded up, no holidays (`BUSINESS_DAYS_PER_WEEK`).

    Never what Bido stores (see `bido_back.quotes.lead_time`): only how a model may
    have converted business days itself, so its number can be traced to the quote.
    """
    return -(-business_days * 7 // BUSINESS_DAYS_PER_WEEK)


# "No tooling cost", written as a word instead of 0.
_NONE_WORDS = re.compile(
    r"\b(none|nil|free|no\s+charge|n/a|keine?|entfällt|kostenlos|gratuit|aucun[es]?"
    r"|sans\s+frais|nessun[oa]?|gratis|ninguno|sin\s+costo|nenhum|sem\s+custo)\b|无",
    re.IGNORECASE,
)


def mentions_none(text: str) -> bool:
    return _NONE_WORDS.search(text) is not None


# --- Per-unit prices ---------------------------------------------------------------

# Units a price can be "per" without a number: one piece, one kilogram, one metre...
_UNIT_WORDS = (
    r"pcs?|pç[as]?|pçs|p[cç]as?|peças?|pecas?|pe[cç]a|pieces?|pz|pzs|pezz[oi]|pièces?|stück"
    r"|stueck|stk|st|units?|unidades?|unid|und|un|ud|uds|ea|each|kg|g|t|m|m2|m3|l|lt|lb|lbs"
    r"|ft|rolls?|rolos?|sets?|jogos?|kits?|pares?|pairs?|pr"
)
_PER_UNIT = re.compile(
    rf"(?:\b(?:per|por|pro|par|je|cada)\b\s*|/\s*)(?:{_UNIT_WORDS})\b\.?"
    r"|\b(?:each|cada|unit[aá]ri[oa]|unit\s+price|price\s+per\s+unit|precio\s+unitario"
    r"|prix\s+unitaire|st[uü]ckpreis|einzelpreis|prezzo\s+unitario)\b",
    re.IGNORECASE,
)
# A stated basis other than one: "per 100 pcs", "/1000", "por cento".
_PER_QUANTITY = re.compile(
    r"(?:\b(?:per|por|pro|par|je|cada)\b\s*|/\s*)\d|\b(?:cento|hundred|thousand|mil|mille|tausend)\b",
    re.IGNORECASE,
)


def mentions_per_unit(text: str) -> bool:
    """`R$ 46,90 por pcs`, `€ 0.45 each`, `2,45/kg`: a price for ONE unit, stated in words.

    False when the text also states another basis (`per 100 pcs`), so it never
    stands in for a written number.
    """
    return _PER_UNIT.search(text) is not None and _PER_QUANTITY.search(text) is None
