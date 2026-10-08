"""Validated source values and evidence utilities shared by assessment paths."""

import re
from collections.abc import Sequence
from datetime import date
from decimal import Decimal, InvalidOperation

from bido_back.purchasing.importers import norm, parse_date
from bido_back.purchasing.sources.contracts import Issue, Record, SourceRef
from bido_back.quotes.parsing import parse_decimal

_COUNT_UNITS = frozenset(
    {
        "un",
        "und",
        "unid",
        "unidade",
        "unidades",
        "ea",
        "each",
        "pc",
        "pcs",
        "pce",
        "piece",
        "pieces",
        "peca",
        "pecas",
        "unit",
        "units",
    }
)


def source_text(record: Record | None, field: str) -> str | None:
    if record is None:
        return None
    return record.values.get(field, "").strip() or None


def source_refs(records: Sequence[Record], *fields: str) -> list[SourceRef]:
    result: list[SourceRef] = []
    for record in records:
        for field, ref in record.evidence.items():
            if (not fields or field in fields) and ref not in result:
                result.append(ref)
    return result


def add_issue(
    issues: list[Issue],
    code: str,
    message: str,
    records: Sequence[Record],
    *fields: str,
    warning: bool = False,
) -> None:
    issues.append(
        Issue(
            code=code,
            severity="warning" if warning else "blocker",
            message=message,
            evidence=source_refs(records, *fields),
        )
    )


def source_number(
    record: Record | None,
    field: str,
    issues: list[Issue],
    *,
    negative: bool = False,
    warning: bool = False,
) -> Decimal | None:
    raw = source_text(record, field)
    if raw is None or record is None:
        return None
    # Readers stringify real Excel numbers, including scientific notation.
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?[eE][-+]?\d+", raw):
        try:
            value = Decimal(raw)
        except InvalidOperation:
            value = None
    else:
        value = parse_decimal(raw)
    if (
        value is None
        or not value.is_finite()
        or abs(value) >= Decimal(10) ** 12
        or (value < 0 and not negative)
    ):
        add_issue(
            issues,
            "invalid_number",
            f"The source {field} is not an unambiguous valid number.",
            [record],
            field,
            warning=warning,
        )
        return None
    return value


def source_date(record: Record | None, field: str, issues: list[Issue]) -> date | None:
    raw = source_text(record, field)
    if raw is None or record is None:
        return None
    value = parse_date(raw)
    if value is None:
        add_issue(
            issues, "invalid_date", f"The source {field} is not a valid date.", [record], field
        )
    return value


def normalize_unit(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = norm(value)
    return "count" if normalized in _COUNT_UNITS else normalized
