"""Locale formatting: the stored value exactly, never rounded, never in E notation."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from bido_back.supplier_email.locale import (
    NBSP,
    NNBSP,
    Locale,
    catalog_language,
    clock,
    day,
    money,
    number,
    quantity,
    unit_label,
    weekday,
)

PT = Locale.of("pt", "BR")
EN = Locale.of("en")
DE = Locale.of("de")
FR = Locale.of("fr")
IT = Locale.of("it")
ES = Locale.of("es")
ES_MX = Locale.of("es", "MX")
DE_CH = Locale.of("de", "CH")


def test_no_exponent_ever() -> None:
    # `quantize(...).normalize()` turns 90 into 9E+1: the strategy did that (audit F1). The
    # digits are `bido_back.decimals.format_decimal`'s.
    assert number(Decimal("9E+1"), EN) == "90"
    assert number(Decimal("1E+3"), EN) == "1,000"
    assert number(Decimal("90.0000").normalize(), PT) == "90"
    assert number(Decimal("0.0001"), PT) == "0,0001"
    assert number(Decimal("-0"), EN) == "0"
    assert money(Decimal("9E+1"), "BRL", PT) == f"R${NBSP}90,00"
    assert money(Decimal("1E+2"), "EUR", EN) == f"EUR{NBSP}100.00"


def test_values_are_shown_exactly_as_stored_never_rounded() -> None:
    # Babel's format_currency would print R$ 45,67 here.
    assert money(Decimal("45.6677"), "BRL", PT) == f"R${NBSP}45,6677"
    assert money(Decimal("0.045"), "EUR", DE) == f"0,045{NBSP}€"
    # The currency's minor unit at least; trailing zeros of the storage are not decimals.
    assert money(Decimal("19.500000"), "BRL", PT) == f"R${NBSP}19,50"
    assert money(Decimal("89"), "BRL", EN) == f"BRL{NBSP}89.00"
    assert money(Decimal("1200"), "JPY", EN) == f"JPY{NBSP}1,200"
    assert money(Decimal("1.5"), "KWD", EN) == f"KWD{NBSP}1.500"


def test_money_uses_a_symbol_only_where_it_is_unambiguous() -> None:
    assert money(Decimal("19.5"), "BRL", PT) == f"R${NBSP}19,50"
    assert money(Decimal("19.5"), "EUR", PT) == f"€{NBSP}19,50"
    assert money(Decimal("19.5"), "USD", PT) == f"USD{NBSP}19,50"
    assert money(Decimal("19.5"), "BRL", EN) == f"BRL{NBSP}19.50"
    assert money(Decimal("19.5"), "EUR", EN) == f"EUR{NBSP}19.50"
    assert money(Decimal("19.5"), "EUR", DE) == f"19,50{NBSP}€"
    assert money(Decimal("19.5"), "BRL", DE) == f"19,50{NBSP}BRL"
    assert money(Decimal("19.5"), "EUR", FR) == f"19,50{NBSP}€"
    assert money(Decimal("19.5"), "EUR", IT) == f"19,50{NBSP}€"
    assert money(Decimal("19.5"), "EUR", ES) == f"19,50{NBSP}€"
    assert money(Decimal("19.5"), "USD", ES_MX) == f"USD{NBSP}19.50"
    assert money(Decimal("1250.5"), "CHF", DE_CH) == f"CHF{NBSP}1’250.50"
    for locale in (PT, EN, DE, FR, IT, ES, ES_MX, DE_CH):
        for currency in ("USD", "MXN", "CAD", "AUD", "BRL", "EUR"):
            text = money(Decimal("3"), currency, locale)
            assert "$" not in text or text.startswith("R$"), (locale, currency, text)


def test_numbers_and_quantities_per_locale() -> None:
    assert number(Decimal("1250.5"), PT) == "1.250,5"
    assert number(Decimal("1250.5"), EN) == "1,250.5"
    assert number(Decimal("1250.5"), FR) == f"1{NNBSP}250,5"
    assert number(Decimal("1250.5"), ES_MX) == "1,250.5"
    assert number(Decimal("1250"), DE_CH) == "1’250"
    assert number(Decimal("999"), PT) == "999"
    assert number(Decimal("1234567.25"), DE) == "1.234.567,25"
    assert quantity(Decimal("12.5"), "kg", PT) == f"12,5{NBSP}kg"
    assert quantity(Decimal("1250"), "pcs", PT) == f"1.250{NBSP}pç"
    assert quantity(Decimal("12.5"), "KG", EN) == f"12.5{NBSP}kg"
    assert quantity(Decimal("1"), "pcs", EN) == f"1{NBSP}pc"
    assert quantity(Decimal("1250"), "Stück", DE) == f"1.250{NBSP}Stk."
    assert quantity(Decimal("1250"), "un", FR) == f"1{NNBSP}250{NBSP}pièces"
    assert quantity(Decimal("1250"), "pcs", IT) == f"1.250{NBSP}pz"
    assert quantity(Decimal("1250"), "pcs", ES) == f"1.250{NBSP}uds."
    # A unit we do not know stays as the buyer wrote it.
    assert quantity(Decimal("3"), "rolls", EN) == f"3{NBSP}rolls"
    assert unit_label("H87", PT) == "pç" and unit_label("MTR", EN) == "m"


def test_dates_are_never_iso_or_month_first() -> None:
    value = date(2026, 10, 8)
    assert day(value, PT) == "08/10/2026"
    assert day(value, EN) == "8 Oct 2026"
    assert day(value, DE) == "08.10.2026"
    assert day(value, FR) == day(value, IT) == day(value, ES) == "08/10/2026"
    assert weekday(date(2026, 10, 13), PT) == "terça-feira"
    assert weekday(date(2026, 10, 13), EN) == "Tuesday"
    assert weekday(date(2026, 10, 13), DE) == "Dienstag"
    assert clock(datetime(2026, 10, 16, 18, 5, tzinfo=UTC)) == "18:05"


def test_a_language_without_a_catalog_is_english_never_mixed() -> None:
    assert catalog_language("zh") == "en"
    assert catalog_language("pt-BR") == "pt"
    assert catalog_language(None) == "en"
    assert Locale.parse("es-MX") == ES_MX
    assert Locale.of("pt", "brasil").region is None  # a name, not a code: no region guessed
    with pytest.raises(ValueError):
        Locale("zh")
