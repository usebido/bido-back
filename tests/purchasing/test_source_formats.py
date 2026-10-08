"""Locale-dependent spellings, categorical vocabularies and identifier keys, without a model."""

from datetime import date
from decimal import Decimal

import pytest

from bido_back.purchasing.sources.contracts import CanonicalValue, Record, RecordKind
from bido_back.purchasing.sources.evidence import (
    categorical,
    fact_key,
    join_key,
    normalize_unit,
    source_date,
    source_number,
    unit_key,
)
from bido_back.purchasing.sources.formats import (
    date_votes,
    decimal_vote,
    number_needs_separator,
    read_date,
    read_number,
)
from bido_back.purchasing.sources.vocabulary import VOCABULARY, allowed, built_in
from evals.source_fixtures import fact


@pytest.mark.parametrize(
    "text",
    [
        "2026-10-08",
        "2026-10-08 00:00:00",
        "2026-10-08T00:00:00Z",
        "2026-10-08T23:30:00-03:00",
        "2026/10/08",
        "20261008",
        "46303",
        "46303.0",
        "08-Oct-2026",
        "8 Oct 2026",
        "Oct 8, 2026",
        "8th October 2026",
        "8 de outubro de 2026",
        "08/out/2026",
        "8. Oktober 2026",
        "8 ottobre 2026",
        "8 octubre 2026",
        "8 octobre 2026",
        "Posição em 8 de outubro de 2026",
        "Gerado em 2026-10-08 14:30",
    ],
)
def test_unambiguous_date_spellings_are_read(text: str) -> None:
    assert read_date(text).value == date(2026, 10, 8)


@pytest.mark.parametrize("text", ["08/10/2026", "08.10.2026", "08-10-2026", "08/10/26"])
def test_day_month_order_is_never_guessed_from_one_value(text: str) -> None:
    reading = read_date(text)
    assert reading.value is None and reading.ambiguous
    assert (reading.day_first, reading.month_first) == (date(2026, 10, 8), date(2026, 8, 10))
    assert date_votes(text) == []


@pytest.mark.parametrize(
    ("text", "value", "order"),
    [
        ("25/09/2026", date(2026, 9, 25), "day_first"),
        ("10/15/2026", date(2026, 10, 15), "month_first"),
        ("Emissão: 25.09.2026", date(2026, 9, 25), "day_first"),
    ],
)
def test_a_value_valid_in_one_order_only_proves_that_order(
    text: str, value: date, order: str
) -> None:
    assert read_date(text).value == value
    assert read_date(text).proves == order
    assert date_votes(text) == [order]


@pytest.mark.parametrize(
    "text", ["26-10-08", "Planta A", "12345", "01/10/2026 a 31/10/2026", "2026-13-01"]
)
def test_invalid_or_multiple_dates_are_not_read(text: str) -> None:
    assert read_date(text).value is None
    assert not read_date(text).ambiguous


def test_same_day_and_month_is_one_date() -> None:
    assert read_date("05/05/2026").value == date(2026, 5, 5)


@pytest.mark.parametrize(
    ("text", "comma", "point"),
    [("1,000", Decimal("1.000"), Decimal(1000)), ("1.125", Decimal(1125), Decimal("1.125"))],
)
def test_ambiguous_numbers_need_a_decimal_separator(
    text: str, comma: Decimal, point: Decimal
) -> None:
    assert read_number(text) is None
    assert number_needs_separator(text)
    assert read_number(text, ",") == comma
    assert read_number(text, ".") == point
    assert decimal_vote(text) is None


@pytest.mark.parametrize(
    ("text", "value", "vote"),
    [
        ("2,5", Decimal("2.5"), ","),
        ("1.234,56", Decimal("1234.56"), ","),
        ("1.234.567", Decimal(1234567), ","),
        ("1,234,567", Decimal(1234567), "."),
        ("12.5", Decimal("12.5"), "."),
        ("1.250,00-", Decimal("-1250.00"), ","),
        ("100", Decimal(100), None),
        ("1e-05", Decimal("0.00001"), None),
    ],
)
def test_unambiguous_numbers_are_read_and_prove_their_separator(
    text: str, value: Decimal, vote: str | None
) -> None:
    assert read_number(text) == value
    assert decimal_vote(text) == vote


def test_only_pure_number_cells_vote_at_sheet_level() -> None:
    assert decimal_vote("R$ 1.234,56") == ","
    assert decimal_vote("R$ 1.234,56", pure=True) is None
    assert decimal_vote("1.234,56", pure=True) == ","


@pytest.mark.parametrize(
    ("kind", "field", "spellings", "canonical"),
    [
        (
            "purchase",
            "status",
            ["Encerrado", "Fechado", "Concluído", "Closed", "Completed"],
            "closed",
        ),
        ("purchase", "status", ["Cerrado", "Chiuso", "Evaso", "Erledigt", "Clôturé"], "closed"),
        ("purchase", "status", ["Em aberto", "Open", "Abierto", "Aperto", "Offen"], "open"),
        ("purchase", "status", ["Firme", "Confirmada", "Released", "Freigegeben"], "open"),
        ("purchase", "status", ["Cancelado", "Annullato", "Storniert", "Annulé"], "cancelled"),
        ("delivery_schedule", "status", ["Não recebido", "Em aberto", "Ausstehend"], "pending"),
        ("delivery_schedule", "status", ["Pendiente", "In attesa", "En attente"], "pending"),
        ("production_plan", "status", ["Planejada", "Firme", "Geplant", "Pianificato"], "planned"),
        ("production_plan", "status", ["Liberada", "Em aberto", "Released", "Lancé"], "released"),
        ("production_plan", "status", ["Concluída", "Abgeschlossen", "Terminé"], "closed"),
        ("receipt", "movement", ["101", "Entrada", "Wareneingang", "Goods receipt"], "receipt"),
        ("receipt", "movement", ["102", "Estorno", "Storno"], "reversal"),
        ("material_requirement", "demand_basis", ["Bruta", "Brutto", "Lordo", "Brut"], "gross"),
        ("material_requirement", "demand_basis", ["Líquida", "Netto", "Neta", "Net"], "net"),
        ("inventory", "balance_basis", ["Livre utilização", "Frei verwendbar"], "available"),
        ("supplier_item", "status", ["Homologado", "Approvato", "Freigegeben"], "approved"),
    ],
)
def test_built_in_vocabulary_reads_six_languages(
    kind: str, field: str, spellings: list[str], canonical: str
) -> None:
    assert all(built_in(kind, field, spelling) == canonical for spelling in spellings)
    assert canonical in allowed(kind, field)


@pytest.mark.parametrize(
    ("spellings", "canonical"),
    [
        (
            ["UN", "EA", "pcs", "pç", "Pç.", "Stk", "Stück", "pz", "pezzi", "pieza", "unidade"],
            "count",
        ),
        (["KG", "quilo", "Kilos", "kilogramm"], "kg"),
        (["L", "litro", "Liter"], "l"),
        (["m²", "M2"], "m2"),
        (["kits", "Kit"], "kit"),
        (["Cx", "caixa"], "box"),
    ],
)
def test_unit_spellings_share_a_canonical_unit(spellings: list[str], canonical: str) -> None:
    assert {normalize_unit(spelling) for spelling in spellings} == {canonical}


def test_spellings_with_two_meanings_stay_unresolved() -> None:
    # French "fermé" (closed) and "ferme" (a firm, open order) normalize to the same text.
    assert built_in("purchase", "status", "Fermé") is None
    assert built_in("purchase", "status", "Ferme") is None
    # French "livré" (delivered) normalizes to Portuguese "livre" (free).
    assert built_in("delivery_schedule", "status", "Livré") is None
    # Purchase "pending" may be an approval state, not an open order.
    assert built_in("purchase", "status", "Pendente") is None
    # A kit, a pair or a box is never a count of pieces.
    assert {normalize_unit(u) for u in ("kit", "par", "cx")} == {"kit", "pair", "box"}


def test_vocabularies_are_closed_lists_of_terms() -> None:
    assert VOCABULARY["purchase.status"] == ["open", "closed", "cancelled"]
    assert VOCABULARY["delivery_schedule.status"] == ["pending", "received", "cancelled"]
    assert "count" in VOCABULARY["unit"]
    assert allowed("bom", "product_unit") == VOCABULARY["unit"]
    assert allowed("purchase", "currency") == []


def test_a_checked_value_map_wins_and_a_conflict_stays_unknown() -> None:
    record = fact("purchase", status="Zrealizowane", unit="szt")
    assert categorical(record, "status") is None
    record.canonical["status"] = CanonicalValue(
        value="closed", source="Zrealizowane", basis="value_map"
    )
    record.canonical["unit"] = CanonicalValue(value="count", source="szt", basis="value_map")
    assert categorical(record, "status") == "closed"
    assert unit_key(record) == "count"
    record.canonical["status"] = CanonicalValue(
        value=None, source="Zrealizowane", basis="value_map"
    )
    assert categorical(record, "status") is None
    # A canonical reading never outlives a changed source value.
    record.values["status"] = "Encerrado"
    assert categorical(record, "status") == "closed"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("000101", "101"),
        ("101.0", "101"),
        ("00010", "10"),
        ("Planta A", " planta  a "),
        ("0", "000"),
    ],
)
def test_join_keys_ignore_formatting_but_not_content(left: str, right: str) -> None:
    assert join_key(left) == join_key(right)


@pytest.mark.parametrize(("left", "right"), [("A-101", "A-0101"), ("101.5", "101"), ("P1", "P 1")])
def test_join_keys_keep_distinct_codes_apart(left: str, right: str) -> None:
    assert join_key(left) != join_key(right)


@pytest.mark.parametrize(
    ("kind", "field", "left", "right"),
    [
        ("inventory", "total", "25", "25.0"),
        ("inventory", "as_of", "2026-10-08", "2026-10-08 00:00:00"),
        ("inventory", "part_number", "000101", "101"),
        ("purchase", "status", "Encerrado", "Closed"),
        ("inventory", "unit", "pcs", "UN"),
        ("item", "description", "Rolamento ", "rolamento"),
    ],
)
def test_repeated_facts_compare_by_meaning(
    kind: RecordKind, field: str, left: str, right: str
) -> None:
    first = Record(kind=kind, values={field: left}, evidence={})
    second = Record(kind=kind, values={field: right}, evidence={})
    assert fact_key(first, field) == fact_key(second, field)


def test_derived_readings_are_used_only_for_their_own_spelling() -> None:
    record = fact("inventory", total="1,000", as_of="08/10/2026")
    issues: list[object] = []
    assert source_number(record, "total", []) is None
    assert source_date(record, "as_of", []) is None
    record.canonical["total"] = CanonicalValue(
        value="1000", source="1,000", basis="decimal_separator"
    )
    record.canonical["as_of"] = CanonicalValue(
        value="2026-10-08", source="08/10/2026", basis="date_order"
    )
    assert source_number(record, "total", []) == Decimal(1000)
    assert source_date(record, "as_of", []) == date(2026, 10, 8)
    record.values["total"] = "2,000"
    assert source_number(record, "total", []) is None
    assert issues == []


def test_an_ambiguous_date_is_reported_as_such() -> None:
    from bido_back.purchasing.sources.contracts import Issue

    issues: list[Issue] = []
    assert source_date(fact("inventory", as_of="08/10/2026"), "as_of", issues) is None
    assert [issue.code for issue in issues] == ["ambiguous_date"]
