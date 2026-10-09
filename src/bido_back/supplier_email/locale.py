"""Locale formatting for supplier emails: numbers, money, quantities, units, dates.

Pure functions, no I/O, no Babel. Values are shown exactly as stored: the
formatter only adds separators and pads to a minimum number of decimals; it
never rounds or truncates (Babel's `format_currency` silently rounds 45.6677
to 45,67). Rounding is a decision, made when the value is decided, never here.

A locale is a catalog language plus an optional region (ISO 3166-1 alpha-2):

| language | decimal | group | money (BRL / EUR / other)          | date        |
|----------|---------|-------|------------------------------------|-------------|
| pt       | ,       | .     | R$ 19,50 / € 19,50 / USD 19,50     | 22/10/2026  |
| en       | .       | ,     | BRL 19.50 / EUR 19.50 / USD 19.50  | 22 Oct 2026 |
| de       | ,       | .     | 19,50 BRL / 19,50 € / 19,50 USD    | 22.10.2026  |
| de-CH    | .       | ’     | BRL 19.50 / EUR 19.50 / CHF 19.50  | 22.10.2026  |
| fr       | ,       | NNBSP | 19,50 BRL / 19,50 € (NBSP before)  | 22/10/2026  |
| it       | ,       | .     | 19,50 BRL / 19,50 €                | 22/10/2026  |
| es       | ,       | .     | 19,50 BRL / 19,50 €                | 22/10/2026  |
| es-MX    | .       | ,     | BRL 19.50 / EUR 19.50 / USD 19.50  | 22/10/2026  |

A symbol is used only where it is unambiguous (R$ in Portuguese, € in the euro
languages); otherwise the ISO code. Never a bare "$".
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final

from bido_back.decimals import format_decimal

CATALOG_LANGUAGES: Final = ("pt", "en", "de", "fr", "it", "es")
DEFAULT_LANGUAGE: Final = "en"

NBSP: Final = " "
NNBSP: Final = " "

# Spanish-speaking regions that write a decimal point (CLDR).
_POINT_REGIONS_ES: Final = frozenset({"MX", "US", "PR", "DO", "GT", "HN", "NI", "PA", "SV", "PE"})
_POINT_REGIONS_DE: Final = frozenset({"CH", "LI"})
# Currencies whose minor unit is not 2 (ISO 4217).
_MINOR_UNITS: Final[dict[str, int]] = {
    **dict.fromkeys(("JPY", "KRW", "CLP", "ISK", "VND", "PYG", "UGX", "XOF", "XAF"), 0),
    **dict.fromkeys(("BHD", "KWD", "OMR", "JOD", "TND", "LYD", "IQD"), 3),
}
_EURO_LANGUAGES: Final = frozenset({"pt", "de", "fr", "it", "es"})


def catalog_language(language: str | None) -> str:
    """The catalog language for an ISO 639-1 code: itself when we have phrases, else English.

    A language without a catalog gets the whole email in English, never half and half.
    """
    code = (language or "").strip().lower()[:2]
    return code if code in CATALOG_LANGUAGES else DEFAULT_LANGUAGE


@dataclass(frozen=True, slots=True)
class Locale:
    """A catalog language and an optional region, e.g. pt-BR, de-CH, es-MX."""

    language: str
    region: str | None = None

    def __post_init__(self) -> None:
        if self.language not in CATALOG_LANGUAGES:
            raise ValueError(f"no catalog for language {self.language!r}")
        if self.region is not None and not re.fullmatch(r"[A-Z]{2}", self.region):
            raise ValueError(f"region must be ISO 3166-1 alpha-2: {self.region!r}")

    @classmethod
    def of(cls, language: str | None, region: str | None = None) -> "Locale":
        code = (region or "").strip().upper()
        return cls(catalog_language(language), code if re.fullmatch(r"[A-Z]{2}", code) else None)

    @classmethod
    def parse(cls, tag: str) -> "Locale":
        language, _, region = tag.replace("_", "-").partition("-")
        return cls.of(language, region or None)

    @property
    def tag(self) -> str:
        return f"{self.language}-{self.region}" if self.region else self.language

    @property
    def decimal_point(self) -> bool:
        if self.language == "en":
            return True
        if self.language == "es":
            return self.region in _POINT_REGIONS_ES
        if self.language == "de":
            return self.region in _POINT_REGIONS_DE
        return False

    @property
    def separators(self) -> tuple[str, str]:
        """(decimal separator, group separator)."""
        if self.language == "en" or (self.language == "es" and self.decimal_point):
            return ".", ","
        if self.language == "de" and self.decimal_point:
            return ".", "’"
        if self.language == "fr":
            return ",", NNBSP
        return ",", "."


# --- Numbers ---------------------------------------------------------------------------


def decimals_of(value: Decimal) -> int:
    """How many decimals the value needs to be shown exactly (trailing zeros not counted)."""
    _, _, fraction = format_decimal(abs(value)).partition(".")
    return len(fraction)


def _group(whole: str, separator: str) -> str:
    if len(whole) < 4:
        return whole
    head = len(whole) % 3 or 3
    parts = [whole[:head]] + [whole[i : i + 3] for i in range(head, len(whole), 3)]
    return separator.join(parts)


def number(value: Decimal, locale: Locale, *, min_decimals: int = 0) -> str:
    """The value with the locale's separators, padded to `min_decimals`, never rounded.

    The digits are `bido_back.decimals.format_decimal`'s: trailing zeros dropped, never in
    exponent form (`Decimal("9E+1")` is "90", never "9E+1").
    """
    digits = format_decimal(abs(value))
    whole, _, fraction = digits.partition(".")
    fraction = fraction.ljust(min_decimals, "0")
    decimal_sep, group_sep = locale.separators
    text = _group(whole, group_sep) + (decimal_sep + fraction if fraction else "")
    return f"-{text}" if value < 0 else text


def minor_unit(currency: str) -> int:
    return _MINOR_UNITS.get(currency.upper(), 2)


def money(amount: Decimal, currency: str, locale: Locale) -> str:
    """An amount with its currency: the currency's minor unit, more decimals only when stored."""
    code = currency.strip().upper()
    digits = number(amount, locale, min_decimals=minor_unit(code))
    language = locale.language
    if language == "pt":
        if code == "BRL":
            return f"R${NBSP}{digits}"
        if code == "EUR":
            return f"€{NBSP}{digits}"
        return f"{code}{NBSP}{digits}"
    if language == "en" or locale.decimal_point:
        return f"{code}{NBSP}{digits}"
    symbol = "€" if code == "EUR" and language in _EURO_LANGUAGES else code
    return f"{digits}{NBSP}{symbol}"


# --- Units and quantities ---------------------------------------------------------------

_PIECE: Final = frozenset(
    {
        "pcs", "pc", "pce", "pces", "piece", "pieces", "un", "un.", "und", "und.", "unid",
        "unid.", "unidade", "unidades", "u", "ea", "each", "stk", "stk.", "stück", "stueck",
        "pç", "pç.", "pc.", "pcs.", "peça", "peças", "pz", "pz.", "pezzi", "pezzo", "pièce",
        "pièces", "pieza", "piezas", "ud", "ud.", "uds", "uds.", "unit", "units", "h87",
        "c62", "st", "st.",
    }
)  # fmt: skip
_SYMBOLS: Final[dict[str, frozenset[str]]] = {
    "kg": frozenset(
        {
            "kg",
            "kgs",
            "kg.",
            "kilo",
            "kilos",
            "kilogram",
            "kilograms",
            "kilogramm",
            "kilogramme",
            "kilogrammes",
            "quilo",
            "quilos",
            "quilograma",
            "quilogramas",
            "kgm",
            "chilogrammo",
            "chilogrammi",
            "kilogramo",
            "kilogramos",
        }
    ),  # fmt: skip
    "g": frozenset(
        {
            "g",
            "g.",
            "gr",
            "grm",
            "gram",
            "grams",
            "grama",
            "gramas",
            "gramm",
            "gramme",
            "grammes",
            "grammo",
            "grammi",
            "gramo",
            "gramos",
        }
    ),  # fmt: skip
    "m": frozenset(
        {
            "m",
            "mtr",
            "meter",
            "meters",
            "metre",
            "metres",
            "metro",
            "metros",
            "mètre",
            "mètres",
            "metri",
        }
    ),  # fmt: skip
    "l": frozenset(
        {"l", "lt", "ltr", "ltr.", "liter", "liters", "litre", "litres", "litro", "litros", "litri"}
    ),  # fmt: skip
}
# (plural, singular) for pieces, per language.
_PIECE_LABELS: Final[dict[str, tuple[str, str]]] = {
    "pt": ("pç", "pç"),
    "en": ("pcs", "pc"),
    "de": ("Stk.", "Stk."),
    "fr": ("pièces", "pièce"),
    "it": ("pz", "pz"),
    "es": ("uds.", "ud."),
}


def unit_kind(unit: str) -> str | None:
    """`piece`, a unit symbol (`kg`, `g`, `m`, `l`), or None for a unit we do not know."""
    key = " ".join(unit.split()).casefold()
    if key in _PIECE:
        return "piece"
    for symbol, names in _SYMBOLS.items():
        if key in names:
            return symbol
    return None


def unit_label(unit: str, locale: Locale, *, singular: bool = False) -> str:
    """The unit as written in the locale: `pç`, `pcs`, `Stk.`, `kg`; an unknown unit as stored."""
    kind = unit_kind(unit)
    if kind == "piece":
        plural, one = _PIECE_LABELS[locale.language]
        return one if singular else plural
    if kind is not None:
        return kind
    return " ".join(unit.split()) or "?"


def quantity_number(value: Decimal, locale: Locale) -> str:
    return number(value, locale)


def quantity(value: Decimal, unit: str, locale: Locale) -> str:
    """`12,5 kg`, `1.250 pç`, `1 pc`: the stored value, the locale's separators and unit."""
    return f"{number(value, locale)}{NBSP}{unit_label(unit, locale, singular=value == 1)}"


# --- Dates and times ------------------------------------------------------------------------

_MONTHS_EN: Final = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)  # fmt: skip
_WEEKDAYS: Final[dict[str, tuple[str, ...]]] = {
    "pt": (
        "segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira",
        "sábado", "domingo",
    ),
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "de": ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"),
    "fr": ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"),
    "it": ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"),
    "es": ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"),
}  # fmt: skip


def day(value: date, locale: Locale) -> str:
    """22/10/2026 (pt, fr, it, es), 22.10.2026 (de), 22 Oct 2026 (en): never ISO, never 10/22."""
    match locale.language:
        case "en":
            return f"{value.day} {_MONTHS_EN[value.month - 1]} {value.year}"
        case "de":
            return f"{value.day:02d}.{value.month:02d}.{value.year}"
        case _:
            return f"{value.day:02d}/{value.month:02d}/{value.year}"


def weekday(value: date, locale: Locale) -> str:
    return _WEEKDAYS[locale.language][value.weekday()]


def clock(value: datetime) -> str:
    """24-hour time, e.g. 18:00."""
    return f"{value.hour:02d}:{value.minute:02d}"
