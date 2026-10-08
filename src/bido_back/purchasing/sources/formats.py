"""Locale-dependent spellings of numbers and dates in source cells.

Nothing here guesses. A value with one reading is read; `1,000` (one thousand or one) and
`08/10/2026` (8 October or 10 August) have two, and are only resolved by a separator or
an order that other values of the same column, sheet or source prove, or that the mapper
declared from the source's language and no value contradicts (`mapping.materialize`).
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final

from bido_back.purchasing.importers import norm
from bido_back.purchasing.sources.contracts import DateOrder, EvidenceTable, SourceRef
from bido_back.quotes.parsing import DecimalSeparator, number_tokens, parse_decimal

_EXCEL_EPOCH: Final = date(1899, 12, 30)
# Excel serial days written as text: 1970-01-01 .. 2099-12-31.
_SERIAL_RANGE: Final = (25_569, 73_050)
_SCIENTIFIC = re.compile(r"[-+]?\d+(?:\.\d+)?[eE][-+]?\d+")
_TRAILING_MINUS = re.compile(r"(.*\d)\s*-")
_PURE_NUMBER = re.compile("[-+−]?[\\d.,'’    ]*\\d[\\d.,'’    ]*-?")

_TIME = re.compile(
    r"(?:[ T]|\s+)\d{1,2}:\d{2}(?::\d{2}(?:[.,]\d{1,9})?)?\s*(?:[ap]\.?m\.?)?"
    r"\s*(?:z|utc|[+-]\d{2}(?::?\d{2})?)?$",
    re.IGNORECASE,
)
_YMD = re.compile(r"(\d{4})([-/.])(\d{1,2})\2(\d{1,2})")
# Two-digit years only with "/" or ".": "26-10-08" may be year-first (yy-mm-dd).
_NUMERIC = re.compile(r"(\d{1,2})([-/.])(\d{1,2})\2(\d{4})|(\d{1,2})([/.])(\d{1,2})\6(\d{2})")
_COMPACT = re.compile(r"(\d{4})(\d{2})(\d{2})")
_SERIAL = re.compile(r"(\d{5})(?:[.,]\d+)?")
_EMBEDDED_YMD = re.compile(r"(?<![\d./-])\d{4}([-/.])\d{1,2}\1\d{1,2}(?![\d./-])")
_EMBEDDED_NUMERIC = re.compile(
    r"(?<![\d./-])(?:\d{1,2}([-/.])\d{1,2}\1\d{4}|\d{1,2}([/.])\d{1,2}\2\d{2})(?![\d./-])"
)

# Month names and abbreviations in pt, en, es, it, de and fr, compared after `norm`.
_MONTHS: Final[dict[str, int]] = {
    norm(name): number
    for number, names in enumerate(
        (
            (
                "january",
                "jan",
                "janeiro",
                "enero",
                "ene",
                "gennaio",
                "gen",
                "januar",
                "jänner",
                "janvier",
                "janv",
            ),
            (
                "february",
                "feb",
                "fevereiro",
                "fev",
                "febrero",
                "febbraio",
                "februar",
                "février",
                "févr",
                "fevr",
            ),
            ("march", "mar", "março", "marzo", "märz", "mär", "mrz", "mars"),
            ("april", "apr", "abril", "abr", "aprile", "avril", "avr"),
            ("may", "maio", "mai", "mayo", "maggio", "mag"),
            ("june", "jun", "junho", "junio", "giugno", "giu", "juni", "juin"),
            ("july", "jul", "julho", "julio", "luglio", "lug", "juli", "juillet", "juil"),
            ("august", "aug", "agosto", "ago", "août", "aout"),
            (
                "september",
                "sep",
                "sept",
                "setembro",
                "set",
                "septiembre",
                "setiembre",
                "settembre",
                "septembre",
            ),
            (
                "october",
                "oct",
                "outubro",
                "out",
                "octubre",
                "ottobre",
                "ott",
                "oktober",
                "okt",
                "octobre",
            ),
            ("november", "nov", "novembro", "noviembre", "novembre"),
            (
                "december",
                "dec",
                "dezembro",
                "dez",
                "diciembre",
                "dic",
                "dicembre",
                "dezember",
                "décembre",
                "déc",
            ),
        ),  # fmt: skip
        start=1,
    )
    for name in names
}
_CONNECTORS: Final = frozenset({"de", "del", "of", "the", "st", "nd", "rd", "th", "er"})


# --- Numbers -----------------------------------------------------------------------------


def read_number(text: str, separator: DecimalSeparator | None = None) -> Decimal | None:
    """One number, or None when it is missing, invalid or needs a separator to be read."""
    raw = text.strip()
    if _SCIENTIFIC.fullmatch(raw):
        # Spreadsheet readers render very small or large native numbers this way.
        try:
            return Decimal(raw)
        except InvalidOperation:
            return None
    trailing = _TRAILING_MINUS.fullmatch(raw)
    if trailing is not None:
        # ERP lists write negatives with the sign after the number: "1.250,00-".
        value = parse_decimal(trailing.group(1), decimal=separator)
        return None if value is None else -abs(value)
    return parse_decimal(raw, decimal=separator)


def number_needs_separator(text: str) -> bool:
    """`1,000` and `1.125` read differently with a decimal comma or a decimal point."""
    return (
        read_number(text) is None
        and read_number(text, ",") is not None
        and read_number(text, ".") is not None
        and read_number(text, ",") != read_number(text, ".")
    )


def decimal_vote(text: str, *, pure: bool = False) -> DecimalSeparator | None:
    """The separator a value proves: `12,5` or `1.234,56` a comma, `12.5` a point.

    `1,000` and `100` prove nothing. With `pure`, only a cell holding nothing but the
    number votes, so that a version or reference number elsewhere in a sheet cannot.
    """
    if pure and _PURE_NUMBER.fullmatch(text.strip()) is None:
        return None
    tokens = number_tokens(text, with_pieces=False)
    if len(tokens) != 1 or len(tokens[0]) != 1:
        return None
    if tokens[0][0].decimal is not None:
        return tokens[0][0].decimal
    # An integer read only with grouping proves the other separator: "1.234.567" is
    # impossible with a decimal point, "1,234,567" with a decimal comma.
    if re.search(r"\d\.\d{3}(?!\d)", text):
        return ","
    if re.search(r"\d,\d{3}(?!\d)", text):
        return "."
    return None


# --- Dates -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DateReading:
    """A date with one reading, or both numeric readings when the order is ambiguous."""

    value: date | None = None
    day_first: date | None = None
    month_first: date | None = None
    # The order the value itself proves (`25/09/2026` can only be day-first).
    proves: DateOrder | None = None

    @property
    def ambiguous(self) -> bool:
        return self.value is None and self.day_first is not None and self.month_first is not None


def _date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _year(text: str) -> int:
    # Two-digit years follow strptime's %y: 00-68 -> 2000-2068, 69-99 -> 1969-1999.
    value = int(text)
    if len(text) == 2:
        return value + (2000 if value < 69 else 1900)
    return value


def _numeric(first: str, second: str, year: str) -> DateReading:
    full = _year(year)
    day_first = _date(full, int(second), int(first))
    month_first = _date(full, int(first), int(second))
    if day_first is not None and day_first == month_first:
        return DateReading(value=day_first)
    if day_first is not None and month_first is not None:
        return DateReading(day_first=day_first, month_first=month_first)
    if day_first is not None:
        return DateReading(value=day_first, proves="day_first")
    if month_first is not None:
        return DateReading(value=month_first, proves="month_first")
    return DateReading()


def _named(text: str) -> date | None:
    tokens = [t for t in re.findall(r"\d+|[a-z]+", norm(text)) if t not in _CONNECTORS]
    if len(tokens) != 3:
        return None
    first, second, third = tokens
    if first.isdigit() and second in _MONTHS and third.isdigit() and len(third) in (2, 4):
        return _date(_year(third), _MONTHS[second], int(first)) if len(first) <= 2 else None
    if first in _MONTHS and second.isdigit() and len(second) <= 2 and len(third) in (2, 4):
        return _date(_year(third), _MONTHS[first], int(second)) if third.isdigit() else None
    if len(first) == 4 and first.isdigit() and second in _MONTHS and third.isdigit():
        return _date(int(first), _MONTHS[second], int(third)) if len(third) <= 2 else None
    return None


def _whole(text: str) -> DateReading | None:
    if (match := _YMD.fullmatch(text)) is not None:
        return DateReading(value=_date(int(match[1]), int(match[3]), int(match[4])))
    if (match := _NUMERIC.fullmatch(text)) is not None:
        if match[1] is not None:
            return _numeric(match[1], match[3], match[4])
        return _numeric(match[5], match[7], match[8])
    if (match := _COMPACT.fullmatch(text)) is not None and 1900 <= int(match[1]) <= 2099:
        return DateReading(value=_date(int(match[1]), int(match[2]), int(match[3])))
    if (match := _SERIAL.fullmatch(text)) is not None:
        serial = int(match[1])
        if _SERIAL_RANGE[0] <= serial <= _SERIAL_RANGE[1]:
            return DateReading(value=_EXCEL_EPOCH + timedelta(days=serial))
        return None
    named = _named(text)
    return DateReading(value=named) if named is not None else None


def _embedded(text: str) -> list[str]:
    found = [m.group() for m in _EMBEDDED_YMD.finditer(text)]
    found += [m.group() for m in _EMBEDDED_NUMERIC.finditer(text)]
    tokens = [t for t in re.findall(r"\d+|[a-z]+", norm(text)) if t not in _CONNECTORS]
    for i in range(len(tokens) - 2):
        window = " ".join(tokens[i : i + 3])
        if any(t in _MONTHS for t in tokens[i : i + 3]) and _named(window) is not None:
            found.append(window)
    return found


def read_date(text: str) -> DateReading:
    """The date in a cell: the whole text, or the only date written inside it."""
    stripped = _TIME.sub("", text.strip()).strip()
    whole = _whole(stripped)
    if whole is not None:
        return whole
    candidates = _embedded(stripped)
    if len(candidates) == 1:
        return _whole(_TIME.sub("", candidates[0]).strip()) or DateReading()
    return DateReading()


def resolve_date(text: str, order: DateOrder | None) -> date | None:
    reading = read_date(text)
    if reading.value is not None or order is None:
        return reading.value
    return reading.day_first if order == "day_first" else reading.month_first


def date_votes(text: str) -> list[DateOrder]:
    """The day/month orders proved by every numeric date written in a cell."""
    votes: list[DateOrder] = []
    for match in _EMBEDDED_NUMERIC.finditer(text):
        first, second, year = re.split(r"[-/.]", match.group())
        proves = _numeric(first, second, year).proves
        if proves is not None:
            votes.append(proves)
    return votes


# --- Evidence for a column, a sheet or a source ----------------------------------------


@dataclass
class Votes:
    """The first cell proving each choice: one choice decides, two are a conflict."""

    source_id: str
    first: dict[str, tuple[str, int, int]] = field(default_factory=dict[str, tuple[str, int, int]])

    def add(self, choice: str | None, table: str, row: int, column: int) -> None:
        if choice is not None:
            self.first.setdefault(choice, (table, row, column))

    def merge(self, other: "Votes") -> None:
        for choice, cell in other.first.items():
            self.first.setdefault(choice, cell)

    @property
    def conflict(self) -> bool:
        return len(self.first) > 1

    def proof(self) -> tuple[str, SourceRef] | None:
        if len(self.first) != 1:
            return None
        choice, (table, row, column) = next(iter(self.first.items()))
        return choice, SourceRef(source_id=self.source_id, table=table, row=row, column=column)


@dataclass(frozen=True)
class SourceFormats:
    """Decimal-separator and date-order votes of every sheet and of each whole source."""

    decimals: dict[tuple[str, str], Votes]
    dates: dict[tuple[str, str], Votes]
    source_decimals: dict[str, Votes]
    source_dates: dict[str, Votes]

    def levels(self, table: EvidenceTable, *, dates: bool) -> list[Votes]:
        sheets, sources = (
            (self.dates, self.source_dates)
            if dates
            else (
                self.decimals,
                self.source_decimals,
            )
        )
        empty = Votes(table.source_id)
        return [
            sheets.get((table.source_id, table.name), empty),
            sources.get(table.source_id, empty),
        ]


def column_votes(table: EvidenceTable, cells: Iterable[tuple[int, int]], *, dates: bool) -> Votes:
    """Votes of the given cells: pure text numbers for separators, any text for dates."""
    votes = Votes(table.source_id)
    for row, column in cells:
        values = table.rows[row - 1] if 0 < row <= len(table.rows) else []
        value = values[column - 1] if 0 < column <= len(values) else None
        if not value:
            continue
        if dates:
            for vote in date_votes(value):
                votes.add(vote, table.name, row, column)
        elif (row, column) not in table.numbers:
            votes.add(decimal_vote(value), table.name, row, column)
    return votes


def scan_formats(tables: Sequence[EvidenceTable]) -> SourceFormats:
    decimals: dict[tuple[str, str], Votes] = {}
    dates: dict[tuple[str, str], Votes] = {}
    source_decimals: dict[str, Votes] = {}
    source_dates: dict[str, Votes] = {}
    for table in tables:
        separators, orders = Votes(table.source_id), Votes(table.source_id)
        for row_number, row in enumerate(table.rows, 1):
            for column, value in enumerate(row, 1):
                if not value:
                    continue
                if (row_number, column) not in table.numbers:
                    separators.add(decimal_vote(value, pure=True), table.name, row_number, column)
                for vote in date_votes(value):
                    orders.add(vote, table.name, row_number, column)
        decimals[table.source_id, table.name] = separators
        dates[table.source_id, table.name] = orders
        source_decimals.setdefault(table.source_id, Votes(table.source_id)).merge(separators)
        source_dates.setdefault(table.source_id, Votes(table.source_id)).merge(orders)
    return SourceFormats(decimals, dates, source_decimals, source_dates)
