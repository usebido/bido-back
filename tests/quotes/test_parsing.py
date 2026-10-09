"""Locale-aware numbers, currencies, dates, Incoterms and lead-time words."""

from datetime import date
from decimal import Decimal

import pytest

from bido_back.quotes.parsing import (
    DecimalSeparator,
    business_days_to_calendar,
    dates_in,
    day_months_in,
    decimal_separator_for,
    detect_currencies,
    find_number,
    incoterms_in,
    is_delivery_timing,
    mentions_approximation,
    mentions_calendar_days,
    mentions_days,
    mentions_months,
    mentions_original_offer,
    mentions_per_unit,
    mentions_stock,
    mentions_weeks,
    mentions_working_days,
    parse_amount,
    parse_canonical,
    parse_decimal,
    ranges_in,
)


@pytest.mark.parametrize(
    ("text", "value", "currency"),
    [
        ("1.234,56 €", "1234.56", "EUR"),
        ("EUR 1,234.56", "1234.56", "EUR"),
        ("1 234,56 EUR", "1234.56", "EUR"),
        ("1 234,56 €", "1234.56", "EUR"),
        ("USD 0.045", "0.045", "USD"),
        ("0,450 €", "0.450", "EUR"),
        ("CHF 1'250.50", "1250.50", "CHF"),
        ("£12", "12", "GBP"),
        ("RMB 3.20", "3.20", "CNY"),
        ("1.234.567,8", "1234567.8", None),
        ("2,500,000", "2500000", None),
    ],
)
def test_unambiguous_amounts(text: str, value: str, currency: str | None) -> None:
    amount = parse_amount(text)

    assert amount.value == Decimal(value)
    assert amount.currency == currency
    assert not amount.ambiguous


@pytest.mark.parametrize(
    ("text", "decimal", "value"),
    [
        ("1,000", ".", "1000"),
        ("1,000", ",", "1"),
        ("1.000", ",", "1000"),
        ("1.000", ".", "1"),
        ("12,500 pcs", ".", "12500"),
        ("12.500 Stück", ",", "12500"),
    ],
)
def test_ambiguous_numbers_resolve_only_with_a_locale(
    text: str, decimal: DecimalSeparator, value: str
) -> None:
    assert parse_decimal(text) is None
    assert parse_amount(text).ambiguous
    assert parse_decimal(text, decimal=decimal) == Decimal(value)


def test_parse_decimal_needs_exactly_one_number() -> None:
    assert parse_decimal("1000 to 2000 pcs") is None
    assert parse_decimal("no price") is None
    assert parse_decimal("-3,5", decimal=",") == Decimal("-3.5")


def test_canonical_values_are_machine_form_only() -> None:
    assert parse_canonical(" 1234.5 ") == Decimal("1234.5")
    assert parse_canonical("-3") == Decimal("-3")
    for text in ("1,5", "1e3", "NaN", "Infinity", "1 000", "€1"):
        assert parse_canonical(text) is None


def test_numbers_inside_identifiers_are_not_values() -> None:
    quote = "Ímã cód. 000105: 220 pcs a R$ 21,90 por pcs, prazo de entrega de 4 dias"
    assert not find_number(Decimal("105"), quote).found
    assert find_number(Decimal("4"), quote).found
    assert find_number(Decimal("220"), quote).found
    assert find_number(Decimal("21.90"), quote, decimal=",").found
    assert not find_number(Decimal("100"), "Motor MTR-100").found
    assert find_number(Decimal("100"), "Motor MTR-100, 100 pcs").found
    assert not find_number(Decimal("6"), "Ref. RFQ-6").found
    assert not find_number(Decimal("4711"), "Art.-Nr. 4711, 30 Stk").found
    assert find_number(Decimal("30"), "Art.-Nr. 4711, 30 Stk").found
    # Ranges and currencies written against the amount are values, not codes.
    assert find_number(Decimal("15"), "entrega em 10-15 dias").found
    assert find_number(Decimal("10"), "EUR10 each").found


def test_ranges_never_span_positions_or_part_codes() -> None:
    # Live journey: "Item 5 - 000106 ... prazo de 5 dias" became a 5-106 day range.
    assert ranges_in("Item 5 - 000106: 60 pcs a R$ 2,62, prazo de 5 dias") == []
    assert ranges_in("entrega em 10-15 dias") == [(Decimal(10), Decimal(15))]
    assert ranges_in("prazo de 4 a 6 semanas") == [(Decimal(4), Decimal(6))]
    assert not find_number(Decimal(2), "Pos. 2: 12 dias").found
    assert find_number(Decimal(12), "Pos. 2: 12 dias").found


def test_find_number_in_supplier_text() -> None:
    assert find_number(Decimal("0.45"), "Stückpreis 0,45 € netto").found
    assert find_number(Decimal("100"), "Pos 1 100 pcs").found
    assert find_number(Decimal("2026"), "valid until 31.12.2026").found
    assert not find_number(Decimal("0.54"), "Stückpreis 0,45 €").found
    # `1,000` with an English supplier is a thousand; read as one it is ambiguous.
    assert find_number(Decimal("1000"), "MOQ 1,000", decimal=".").ambiguous is False
    assert find_number(Decimal("1"), "MOQ 1,000", decimal=".").ambiguous
    assert find_number(Decimal("1000"), "MOQ 1,000").ambiguous


def test_decimal_separator_from_language() -> None:
    assert decimal_separator_for("de") == ","
    assert decimal_separator_for("pt-BR") == ","
    assert decimal_separator_for("en") == "."
    assert decimal_separator_for("zh") == "."
    assert decimal_separator_for(None) is None


def test_currency_detection() -> None:
    assert detect_currencies("Preise in Euro, zzgl. MwSt.") == {"EUR"}
    assert detect_currencies("USD 3 / EUR 2") == {"USD", "EUR"}
    assert detect_currencies("price ¥ 20") == set()  # JPY or CNY: never guessed
    assert detect_currencies("the fee") == set()


def test_reais_are_never_read_as_dollars() -> None:
    assert detect_currencies("R$ 46,90 por pcs") == {"BRL"}
    assert detect_currencies("r$46,90") == {"BRL"}
    assert detect_currencies("US$ 5.00") == {"USD"}
    assert detect_currencies("$5.00 each") == {"USD"}
    assert detect_currencies("5 $") == {"USD"}
    # Another dollar (Canadian, Australian...): no currency, never USD.
    assert detect_currencies("C$ 5.00") == set()
    assert detect_currencies("R$ 1.234,00 ou US$ 300") == {"BRL", "USD"}


def test_a_price_for_one_unit_in_words() -> None:
    for text in ("R$ 46,90 por pcs", "0,45 €/Stk", "$2 each", "R$ 112,00 por kg",
                 "preço unitário R$ 2,45", "EUR 5 per unit"):  # fmt: skip
        assert mentions_per_unit(text), text
    for text in ("R$ 46,90 por 100 pcs", "46,90 / 1000 pcs", "R$ 46,90", "5 pcs at 2.00"):
        assert not mentions_per_unit(text), text


def test_dates_in_common_written_forms() -> None:
    expected = {date(2026, 12, 31)}
    for text in (
        "gültig bis 31.12.2026",
        "valid until 2026-12-31",
        "valid until December 31, 2026",
        "valable jusqu'au 31 décembre 2026",
        "gültig bis 31. Dezember 2026",
        "valid till 31/12/26",
    ):
        assert dates_in(text) == expected, text
    # Day-first and month-first are both kept when both are valid.
    assert dates_in("04/03/2026") == {date(2026, 3, 4), date(2026, 4, 3)}
    assert dates_in("valid 30 days") == set()


def test_incoterms_and_lead_time_words() -> None:
    assert incoterms_in("FCA Shenzhen, Incoterms 2020") == {"FCA"}
    assert incoterms_in("ab Werk München") == {"EXW"}
    assert incoterms_in("a cif") == set()
    assert mentions_weeks("6 Wochen") and mentions_weeks("6w ARO") and mentions_weeks("8-10 weeks")
    assert not mentions_weeks("42 days")
    assert mentions_working_days("30 AT") and mentions_working_days("20 working days")
    assert not mentions_working_days("at 0.45 each")


def test_dates_with_portuguese_and_spanish_month_names() -> None:
    assert dates_in("válida até 30 de outubro de 2026") == {date(2026, 10, 30)}
    assert dates_in("válida hasta el 30 de octubre del 2026") == {date(2026, 10, 30)}


def test_day_and_month_without_a_year() -> None:
    assert day_months_in("consegna entro il 30/10") == {(10, 30)}
    assert day_months_in("Liefertermin 30.10.") == {(10, 30)}
    assert day_months_in("entrega até 30 de outubro") == {(10, 30)}
    assert day_months_in("delivery by October 30th") == {(10, 30)}
    # Both readings of an ambiguous day/month; dates with a year are not yearless.
    assert day_months_in("by 05/10") == {(10, 5), (5, 10)}
    assert day_months_in("bis 30.10.2026") == set()
    assert day_months_in("BRL 19.50 per piece") == set()


def test_business_days_in_every_language() -> None:
    for text in (
        "10 business days",
        "10 dias úteis",
        "10 días hábiles",
        "10 Arbeitstage",
        "10 Werktage",
        "10 giorni lavorativi",
        "10 jours ouvrés",
        "10 werkdagen",
    ):
        assert mentions_working_days(text), text
        assert mentions_days(text), text
    assert not mentions_working_days("15 dias corridos")


def test_day_month_and_calendar_day_units() -> None:
    for text in ("15 days", "15 dias", "15 Tage", "15 jours", "15 giorni", "60 gg", "30d"):
        assert mentions_days(text), text
    assert not mentions_days("6 Wochen")
    for text in ("15 calendar days", "15 dias corridos", "15 Kalendertage", "15 días naturales"):
        assert mentions_calendar_days(text), text
    assert not mentions_calendar_days("15 business days")
    for text in ("2 months", "2 meses", "1 mês", "2 Monate", "1 mois", "2 mesi"):
        assert mentions_months(text), text
    assert not mentions_months("2 weeks")


def test_hedges_ranges_stock_and_original_offer() -> None:
    for text in (
        "approximately 10 business days",
        "ca. 3 Wochen",
        "cerca de 15 dias",
        "aprox. 20 dias",
        "environ 3 semaines",
        "about 2 weeks",
        "previsão de 10 dias",
    ):
        assert mentions_approximation(text), text
    assert not mentions_approximation("Lead time 10 business days")
    assert ranges_in("2-3 Wochen") == [(Decimal(2), Decimal(3))]
    assert ranges_in("4 a 6 semanas") == [(Decimal(4), Decimal(6))]
    assert ranges_in("3/4 settimane") == [(Decimal(3), Decimal(4))]
    assert ranges_in("2 bis 3 Wochen") == [(Decimal(2), Decimal(3))]
    assert ranges_in("consegna entro il 30/10") == []  # not low-high
    assert ranges_in("10 business days") == []
    for text in ("ex stock", "pronta entrega", "ab Lager", "pronta consegna", "from stock"):
        assert mentions_stock(text), text
    assert mentions_original_offer("valid for 15 calendar days from the date of our original offer")
    assert mentions_original_offer("válida por 15 dias a partir da proposta original")
    assert mentions_original_offer("gültig 30 Tage ab dem ursprünglichen Angebot")
    assert not mentions_original_offer("the quotation is valid for 15 days")


def test_delivery_timing_sentences() -> None:
    assert is_delivery_timing(
        "We may need a few additional days for the shafts, depending on production availability."
    )
    assert is_delivery_timing(
        "Lead time is approximately 10 business days after order confirmation."
    )
    assert is_delivery_timing("Für die Wellen benötigen wir ca. 1 Woche länger in der Fertigung.")
    # Price, payment or validity are never only about timing.
    assert not is_delivery_timing("Price valid only for delivery within 30 days.")
    assert not is_delivery_timing(
        "Our standard payment terms are Net 30, and the quotation is valid for 15 days."
    )
    assert not is_delivery_timing("Subject to stock availability.")  # no time unit: a condition
    assert not is_delivery_timing("BRL 5.00 extra for delivery within 3 days.")


def test_business_day_words_and_the_calendar_rule() -> None:
    for text in (
        "25 dias úteis",
        "25 dias uteis após pedido",
        "1 dia útil",
        "10 días hábiles",
        "10 dias laborables",
        "15 giorni lavorativi",
        "25 BD",
        "25BD",
        "20 WD",
        "30 AT",
        "5 jours ouvrés",
        "10 werkdagen",
    ):
        assert mentions_working_days(text), text
        assert mentions_days(text), text
    for text in ("25 dias", "42 days", "6 Wochen", "bd 25", "wd", "lead time at best 25 days"):
        assert not mentions_working_days(text), text
    for text in ("25 dias", "42 days", "30 Tage", "42d", "42 d", "10 jours", "20 giorni"):
        assert mentions_days(text), text
    for text in ("6 Wochen", "4 semanas", "6w ARO"):
        assert not mentions_days(text), text
    # The rough rule a model may apply itself (never what Bido stores): 5 business days
    # are a calendar week; partial weeks round up; no holiday calendar.
    assert [business_days_to_calendar(n) for n in (0, 1, 5, 7, 10, 25, 30)] == [
        0,
        2,
        7,
        10,
        14,
        35,
        42,
    ]
