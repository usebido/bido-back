"""Deterministic validation of extractions: grounding, confidence, mapping, review flags."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import uuid4

import pytest

from bido_back.agents.catalog.contracts import (
    ExtractedValue,
    ProposalLineDraft,
    QuoteExtractionOutput,
    SourceRef,
)
from bido_back.documents import DocumentText, extract_text
from bido_back.quotes.validation import (
    QuoteSources,
    RequestedLine,
    ValidatedQuote,
    validate_quote,
)
from tests.documents.fixtures import PdfPage, PdfText, build_pdf, build_xlsx

TODAY = date(2026, 10, 6)
LINE_1 = RequestedLine(id=uuid4(), position=1, mpn="TPS7A4701RGWR")
LINE_2 = RequestedLine(id=uuid4(), position=2, mpn="LM317DCYR")

BODY = (
    "Sehr geehrte Damen und Herren,\n"
    "für TPS7A4701RGWR bieten wir an: 1.000 Stück zu 0,45 € je Stück.\n"
    "Lieferzeit: 6 Wochen. Angebot gültig bis 31.12.2026. Lieferbedingung FCA Shenzhen.\n"
    "Zahlung 30 Tage netto. Ursprungsland: China.\n"
)

Kind = Literal["email_body", "pdf", "sheet", "image"]


def ev(
    value: str | None,
    quote: str | None = None,
    *,
    kind: Kind = "email_body",
    attachment: str | None = None,
    page: int | None = None,
    sheet: str | None = None,
    cell: str | None = None,
    confidence: float = 0.95,
) -> ExtractedValue:
    if value is None:
        return ExtractedValue(value=None, source=None, confidence=0)
    source = SourceRef(
        kind=kind,
        attachment=attachment,
        page=page,
        sheet=sheet,
        cell=cell,
        quote=quote if quote is not None else value,
    )
    return ExtractedValue(value=value, source=source, confidence=confidence)


NONE = ev(None)


def line(**overrides: object) -> ProposalLineDraft:
    fields: dict[str, object] = {
        "rfq_line_position": 1,
        "offered_mpn": ev("TPS7A4701RGWR", "für TPS7A4701RGWR bieten wir an"),
        "min_quantity": ev("1000", "1.000 Stück"),
        "unit_price": ev("0.45", "0,45 € je Stück"),
        "unit": ev("pcs", "Stück"),
        "tooling_cost": NONE,
        "moq": NONE,
        "lead_time_days": ev("42", "Lieferzeit: 6 Wochen"),
        "origin_country": ev("CN", "Ursprungsland: China"),
        "qualification": NONE,
    }
    fields.update(overrides)
    return ProposalLineDraft.model_validate(fields)


def output(*lines: ProposalLineDraft, **overrides: object) -> QuoteExtractionOutput:
    fields: dict[str, object] = {
        "is_quote": True,
        "currency": ev("EUR", "0,45 €"),
        "valid_until": ev("2026-12-31", "Angebot gültig bis 31.12.2026"),
        "incoterm": ev("FCA", "Lieferbedingung FCA Shenzhen"),
        "payment_terms": ev("30 Tage netto", "Zahlung 30 Tage netto"),
        "lines": list(lines) if lines else [line()],
    }
    fields.update(overrides)
    return QuoteExtractionOutput.model_validate(fields)


def validate(
    out: QuoteExtractionOutput,
    *,
    body: str = BODY,
    documents: list[DocumentText] | None = None,
    decimal: Literal[",", "."] | None = ",",
) -> ValidatedQuote:
    return validate_quote(
        out,
        requested=[LINE_1, LINE_2],
        sources=QuoteSources(
            email_subject="AW: [RFQ-12] Spannungsregler",
            email_body=body,
            documents={d.filename: d for d in documents or []},
        ),
        today=TODAY,
        decimal=decimal,
    )


def issues_of(result: ValidatedQuote, name: str, line_index: int = 0) -> tuple[str, ...]:
    return result.lines[line_index].fields[name].issues


# --- A clean extraction ----------------------------------------------------------------


def test_grounded_quote_needs_no_review() -> None:
    result = validate(output())

    assert not result.needs_review, (result.issues, result.lines[0].issues)
    assert result.parsed("currency") == "EUR"
    assert result.parsed("valid_until") == date(2026, 12, 31)
    assert result.parsed("incoterm") == "FCA"
    [checked] = result.lines
    assert checked.rfq_line_id == LINE_1.id
    assert checked.parsed("unit_price") == Decimal("0.45")
    assert checked.parsed("min_quantity") == Decimal("1000")
    assert checked.parsed("lead_time_days") == 42
    assert checked.parsed("origin_country") == "CN"
    assert not checked.is_alternative


def test_provenance_keeps_value_source_confidence_and_reasons() -> None:
    result = validate(output(line(unit_price=ev("0.54", "0,45 € je Stück"))))

    provenance = result.lines[0].provenance()
    unit_price = provenance["unit_price"]
    assert unit_price == {
        "value": "0.54",
        "parsed": "0.54",
        "source": {
            "kind": "email_body",
            "attachment": None,
            "page": None,
            "sheet": None,
            "cell": None,
            "quote": "0,45 € je Stück",
        },
        "confidence": 0.95,
        "needs_review": True,
        "issues": ["not_grounded"],
    }
    review = provenance["_review"]
    assert isinstance(review, dict)
    assert review["needs_review"] is True
    header = result.provenance()
    assert set(header) == {
        "currency",
        "valid_until",
        "incoterm",
        "delivery_location",
        "payment_terms",
        "validity",
        "lead_time",
        "_review",
    }


# --- Grounding -----------------------------------------------------------------------


def test_number_not_in_its_quote_needs_review() -> None:
    result = validate(output(line(unit_price=ev("0.54", "0,45 € je Stück"))))

    assert issues_of(result, "unit_price") == ("not_grounded",)
    assert result.lines[0].needs_review
    assert result.needs_review


def test_unit_price_basis_of_one_never_needs_review() -> None:
    # The model reads "je Stück" as a basis of 1; the "1" is not written anywhere.
    result = validate(output(line(price_basis_quantity=ev("1", "0,45 € je Stück"))))

    assert issues_of(result, "price_basis_quantity") == ()
    assert result.lines[0].parsed("price_basis_quantity") == Decimal(1)
    assert not result.lines[0].needs_review
    assert not result.needs_review


def test_price_basis_above_one_must_be_grounded() -> None:
    result = validate(output(line(price_basis_quantity=ev("100", "0,45 € je Stück"))))

    assert issues_of(result, "price_basis_quantity") == ("not_grounded",)
    assert result.lines[0].needs_review


def test_quote_not_in_the_source_needs_review() -> None:
    invented = ev("0.39", "Sonderpreis 0,39 €")

    result = validate(output(line(unit_price=invented)))

    assert issues_of(result, "unit_price") == ("quote_not_found",)


def test_quote_matching_ignores_whitespace_and_case() -> None:
    result = validate(output(line(unit_price=ev("0.45", "0,45  €   JE stück"))))

    assert issues_of(result, "unit_price") == ()


def test_value_without_source_needs_review() -> None:
    unsourced = ExtractedValue(value="0.45", source=None, confidence=0.99)

    result = validate(output(line(unit_price=unsourced)))

    assert issues_of(result, "unit_price") == ("no_source",)


def test_ambiguous_separator_without_locale_needs_review() -> None:
    body = "Unit price 0.45 EUR, MOQ 1,000 pcs, lead time 42 days."
    draft = line(
        offered_mpn=NONE,
        min_quantity=NONE,
        unit_price=ev("0.45", "0.45 EUR"),
        unit=NONE,
        moq=ev("1000", "MOQ 1,000 pcs"),
        lead_time_days=ev("42", "lead time 42 days"),
        origin_country=NONE,
    )
    header: dict[str, object] = {
        "currency": ev("EUR", "0.45 EUR"),
        "valid_until": NONE,
        "incoterm": NONE,
        "payment_terms": NONE,
    }

    unknown = validate(output(draft, **header), body=body, decimal=None)
    english = validate(output(draft, **header), body=body, decimal=".")
    german = validate(output(draft, **header), body=body, decimal=",")

    assert issues_of(unknown, "moq") == ("ambiguous_number",)
    assert issues_of(english, "moq") == ()
    # A German "1,000" is one; reading it as a thousand needs a person.
    assert issues_of(german, "moq") == ("ambiguous_number",)


def test_lead_time_in_weeks_and_working_days() -> None:
    weeks = validate(output(line(lead_time_days=ev("42", "Lieferzeit: 6 Wochen"))))
    wrong = validate(output(line(lead_time_days=ev("40", "Lieferzeit: 6 Wochen"))))
    body = BODY + "Lieferzeit 30 AT.\n"
    working = validate(output(line(lead_time_days=ev("30", "Lieferzeit 30 AT"))), body=body)

    assert issues_of(weeks, "lead_time_days") == ()
    assert weeks.lines[0].parsed("lead_time_days") == 42
    assert issues_of(wrong, "lead_time_days") == ("not_grounded",)
    # 30 business days from Tuesday 6 October are 6 business weeks: 42 calendar days (a
    # EUR quote from a supplier without a country has no holiday calendar: weekends only).
    assert issues_of(working, "lead_time_days") == ()
    assert working.lines[0].parsed("lead_time_days") == 42
    conversion = working.lines[0].fields["lead_time_days"].conversion
    assert conversion is not None
    assert {k: conversion[k] for k in ("quoted", "unit", "calendar_days", "holiday_calendar")} == {
        "quoted": 30,
        "unit": "business_days",
        "calendar_days": 42,
        "holiday_calendar": None,
    }
    assert conversion["reference_date"] == "2026-10-06"
    # The number as written stays the field's value; the statement is the line's terms.
    assert working.lines[0].fields["lead_time_days"].extracted.value == "30"
    assert working.lines[0].fields["lead_time_terms"].extracted.value == "30 business_days"


@pytest.mark.parametrize(
    ("written", "value", "days", "unit"),
    [
        ("Prazo de entrega: 25 dias úteis após pedido", "25", 35, "business_days"),
        ("Prazo de entrega: 25 dias uteis", "25", 35, "business_days"),
        ("Lead time: 25 business days", "25", 35, "business_days"),
        ("Lead time: 25 working days", "25", 35, "business_days"),
        ("Lead time 25 BD", "25", 35, "business_days"),
        ("Lieferzeit: 20 Arbeitstage", "20", 28, "business_days"),
        ("Plazo de entrega: 10 días hábiles", "10", 14, "business_days"),
        ("Prazo de entrega: 25 dias", "25", 25, None),
        ("Lead time: 42 days", "42", 42, None),
        ("Prazo: 4 semanas", "28", 28, "weeks"),
        ("Prazo: 4 semanas", "4", 28, "weeks"),
        ("Lieferzeit: 6 Wochen", "6", 42, "weeks"),
        # The model converted business days itself: the written number still grounds it.
        ("Prazo de entrega: 25 dias úteis", "35", 35, "business_days"),
        ("Lead time: 4 weeks (28 days)", "28", 28, None),
    ],
)
def test_lead_times_are_stored_as_calendar_days(
    written: str, value: str, days: int, unit: str | None
) -> None:
    result = validate(output(line(lead_time_days=ev(value, written))), body=BODY + written + "\n")

    [checked] = result.lines
    assert issues_of(result, "lead_time_days") == ()
    assert checked.parsed("lead_time_days") == days
    conversion = checked.fields["lead_time_days"].conversion
    if unit is None:
        assert conversion is None
    else:
        assert conversion is not None
        assert conversion["unit"] == unit and conversion["calendar_days"] == days
        provenance = checked.fields["lead_time_days"].provenance()
        assert provenance["conversion"] == conversion
    assert not checked.needs_review


def test_a_lead_time_number_not_in_its_quote_is_not_grounded() -> None:
    body = BODY + "Prazo de entrega: 25 dias úteis.\n"
    result = validate(
        output(line(lead_time_days=ev("30", "Prazo de entrega: 25 dias úteis"))), body=body
    )

    assert issues_of(result, "lead_time_days") == ("not_grounded",)
    assert result.lines[0].parsed("lead_time_days") == 30
    assert result.lines[0].fields["lead_time_days"].conversion is None


def test_a_part_code_is_not_evidence_for_a_lead_time() -> None:
    # Live journey run: "Ímã cód. 000105 ... prazo de entrega de 4 dias" came back as
    # 105 days, grounded on the part code. A number inside an identifier is no value.
    quote = "Ímã cód. 000105: 220 pcs a R$ 21,90 por pcs, prazo de entrega de 4 dias"
    body = BODY + quote + ".\n"

    wrong = validate(output(line(lead_time_days=ev("105", quote))), body=body)
    right = validate(output(line(lead_time_days=ev("4", quote))), body=body)

    assert "not_grounded" in issues_of(wrong, "lead_time_days")
    assert issues_of(right, "lead_time_days") == ()
    assert right.lines[0].parsed("lead_time_days") == 4


def test_header_values_are_checked_against_their_quotes() -> None:
    result = validate(
        output(
            currency=ev("USD", "0,45 €"),
            valid_until=ev("2026-11-30", "Angebot gültig bis 31.12.2026"),
            incoterm=ev("DAP", "Lieferbedingung FCA Shenzhen"),
        )
    )

    assert result.header["currency"].issues == ("not_grounded",)
    assert result.header["valid_until"].issues == ("not_grounded",)
    assert result.header["incoterm"].issues == ("not_grounded",)
    # Prices cannot be trusted without a trusted currency.
    assert "currency_unverified" in result.lines[0].issues


def test_malformed_values_are_rejected() -> None:
    result = validate(
        output(
            line(
                unit_price=ev("-0.45", "0,45 € je Stück"),
                min_quantity=ev("lots", "1.000 Stück"),
                origin_country=ev("China", "Ursprungsland: China"),
            ),
            valid_until=ev("31.12.2026", "Angebot gültig bis 31.12.2026"),
            incoterm=ev("Free carrier", "Lieferbedingung FCA Shenzhen"),
        )
    )

    assert issues_of(result, "unit_price") == ("negative",)
    assert issues_of(result, "min_quantity") == ("invalid_value",)
    assert issues_of(result, "origin_country") == ("invalid_value",)
    assert result.header["valid_until"].issues == ("invalid_value",)
    assert result.header["incoterm"].issues == ("invalid_value",)
    assert result.lines[0].parsed("unit_price") is None


def test_low_confidence_needs_review() -> None:
    result = validate(output(line(unit_price=ev("0.45", "0,45 € je Stück", confidence=0.79))))

    assert issues_of(result, "unit_price") == ("low_confidence",)


# --- Attachments ---------------------------------------------------------------------------


def test_pdf_quotes_are_checked_on_their_page() -> None:
    pdf = extract_text(
        "Angebot.pdf",
        "application/pdf",
        build_pdf(
            [
                PdfPage(texts=[PdfText(50, 800, "Seite eins")]),
                PdfPage(texts=[PdfText(50, 800, "TPS7A4701RGWR 1.000 Stk 0,45 EUR")]),
            ]
        ),
    )
    right = ev("0.45", "0,45 EUR", kind="pdf", attachment="Angebot.pdf", page=2)
    wrong_page = ev("0.45", "0,45 EUR", kind="pdf", attachment="Angebot.pdf", page=1)
    unknown_file = ev("0.45", "0,45 EUR", kind="pdf", attachment="Other.pdf", page=2)

    results = [
        validate(output(line(unit_price=value)), documents=[pdf])
        for value in (right, wrong_page, unknown_file)
    ]

    assert [issues_of(r, "unit_price") for r in results] == [
        (),
        ("wrong_locator",),
        ("unknown_attachment",),
    ]


def test_sheet_quotes_are_checked_in_their_cell() -> None:
    sheet = extract_text(
        "offer.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        build_xlsx({"Prices": [["Part", "Qty", "Price"], ["TPS7A4701RGWR", 1000, 0.45]]}),
    )
    right = ev("0.45", "0.45", kind="sheet", attachment="offer.xlsx", sheet="Prices", cell="C2")
    wrong_cell = ev(
        "0.45", "0.45", kind="sheet", attachment="offer.xlsx", sheet="Prices", cell="B2"
    )

    results = [
        validate(output(line(unit_price=value)), documents=[sheet], decimal=".")
        for value in (right, wrong_cell)
    ]

    assert [issues_of(r, "unit_price") for r in results] == [(), ("wrong_locator",)]


def test_values_from_unread_documents_need_review() -> None:
    image = extract_text("scan.png", "image/png", b"\x89PNG")
    from_image = ev("0.45", "0,45", kind="image", attachment="scan.png")
    from_scan = ev("0.45", "0,45", kind="pdf", attachment="scan.png", page=1)

    results = [
        validate(output(line(unit_price=value)), documents=[image])
        for value in (from_image, from_scan)
    ]

    assert [issues_of(r, "unit_price") for r in results] == [
        ("source_not_read",),
        ("source_not_read",),
    ]


# --- Lines and the proposal ------------------------------------------------------------------


def test_lines_map_to_requested_positions() -> None:
    result = validate(output(line(), line(rfq_line_position=None), line(rfq_line_position=9)))

    assert [checked.rfq_line_id for checked in result.lines] == [LINE_1.id, None, None]
    assert result.lines[1].issues == ("unmatched_rfq_line",)
    assert result.lines[2].issues == ("unknown_rfq_line",)


def test_a_different_mpn_is_an_alternative_but_not_a_review() -> None:
    body = BODY + "Alternativ: TPS7A4700RGWR zum selben Preis.\n"
    offered = ev("TPS7A4700RGWR", "Alternativ: TPS7A4700RGWR")

    result = validate(output(line(offered_mpn=offered)), body=body)

    [checked] = result.lines
    assert checked.is_alternative
    assert checked.notes == ("alternative_offered",)
    assert not checked.needs_review


def test_mpn_must_be_in_its_quote() -> None:
    result = validate(output(line(offered_mpn=ev("TPS7A4701RGWR", "für TPS7A4701"))))

    assert issues_of(result, "offered_mpn") == ("not_grounded",)


def test_missing_required_values() -> None:
    result = validate(output(line(unit_price=NONE), line(), currency=NONE))

    assert "missing:unit_price" in result.lines[0].issues
    assert "missing:currency" in result.issues
    assert "currency_unverified" in result.lines[1].issues
    assert result.needs_review


def test_price_in_another_currency_than_the_header() -> None:
    body = BODY + "Werkzeugkosten: USD 1.500\n"
    result = validate(output(line(tooling_cost=ev("1500", "Werkzeugkosten: USD 1.500"))), body=body)

    assert "currency_mismatch:tooling_cost" in result.lines[0].issues


def test_reais_are_the_brl_header_currency_not_a_mismatch() -> None:
    body = "Item 1: R$ 46,90 por pcs. Moeda: BRL.\n"
    result = validate(
        output(
            line(
                offered_mpn=NONE,
                min_quantity=NONE,
                unit=NONE,
                lead_time_days=NONE,
                origin_country=NONE,
                unit_price=ev("46.90", "R$ 46,90 por pcs"),
            ),
            currency=ev("BRL", "Moeda: BRL"),
            valid_until=NONE,
            incoterm=NONE,
            payment_terms=NONE,
        ),
        body=body,
    )

    assert not result.lines[0].issues
    assert not result.needs_review


def test_a_basis_of_one_unit_stated_in_words_is_grounded() -> None:
    body = BODY + "Preis: 0,45 € je Stück; ab 100 Stück: 40,00 € per 100 pcs.\n"
    per_unit = validate(output(line(price_basis_quantity=ev("1", "0,45 € je Stück"))), body=body)
    assert issues_of(per_unit, "price_basis_quantity") == ()
    assert per_unit.lines[0].fields["price_basis_quantity"].parsed == Decimal(1)

    # A per-unit sentence never grounds any other number.
    wrong = validate(output(line(price_basis_quantity=ev("10", "0,45 € je Stück"))), body=body)
    assert "not_grounded" in issues_of(wrong, "price_basis_quantity")


def test_expired_validity_needs_review() -> None:
    body = BODY + "Gültig bis 01.10.2026.\n"
    result = validate(output(valid_until=ev("2026-10-01", "Gültig bis 01.10.2026")), body=body)

    assert result.header["valid_until"].issues == ()
    assert result.issues == ("validity_expired",)
    assert result.needs_review


def test_not_a_quote_has_no_lines_and_no_review() -> None:
    out_of_office = output(
        is_quote=False,
        currency=NONE,
        valid_until=NONE,
        incoterm=NONE,
        payment_terms=NONE,
        lines=[],
    )

    result = validate(out_of_office, body="Ich bin bis 12.10. nicht im Büro.")

    assert not result.is_quote
    assert result.lines == ()
    assert not result.needs_review


def test_a_quote_without_lines_needs_review() -> None:
    result = validate(output(lines=[]))

    assert result.issues == ("no_lines",)


def test_lead_time_in_weeks_from_the_column_header() -> None:
    sheet = extract_text(
        "offer.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        build_xlsx({"Angebot": [["Teil", "Lieferzeit (Wochen)"], ["TPS7A4701RGWR", 6]]}),
    )

    def cited(days: str) -> ExtractedValue:
        return ev(days, "6", kind="sheet", attachment="offer.xlsx", sheet="Angebot", cell="B2")

    results = [
        validate(output(line(lead_time_days=cited(days))), documents=[sheet])
        for days in ("42", "40")
    ]

    assert [issues_of(r, "lead_time_days") for r in results] == [(), ("not_grounded",)]


def test_a_cost_written_as_none_grounds_zero() -> None:
    body = BODY + "Werkzeugkosten: keine.\nRüstkosten: siehe Anlage.\n"
    none = validate(output(line(tooling_cost=ev("0", "Werkzeugkosten: keine."))), body=body)
    other = validate(output(line(tooling_cost=ev("0", "Rüstkosten: siehe Anlage."))), body=body)

    assert issues_of(none, "tooling_cost") == ()
    assert issues_of(other, "tooling_cost") == ("not_grounded",)


# --- The offer against what the RFQ asked ------------------------------------------------------

RFQ3_LINE = RequestedLine(
    id=uuid4(),
    position=1,
    mpn=None,
    quantities=(Decimal(1500),),
    incoterm="FOB",
    delivery_location="Joinville-SC",
)
RFQ3_BODY = (
    "Item 1 - Motor MTR-100\n"
    "Quantidade: 50 pcs\n"
    "Preco unitario: R$ 1.284,50\n"
    "Prazo de entrega: 25 dias uteis apos pedido - entrega CIF Joinville/SC\n"
    "Condicao de pagamento: 28 DDL\n"
    "Validade da proposta: 30 dias\n"
    "Moeda: BRL\n"
)


def rfq3_line(**overrides: object) -> ProposalLineDraft:
    fields: dict[str, object] = {
        "rfq_line_position": 1,
        "offered_mpn": NONE,
        "min_quantity": ev("50", "Quantidade: 50 pcs"),
        "unit_price": ev("1284.50", "Preco unitario: R$ 1.284,50"),
        "unit": ev("pcs", "Quantidade: 50 pcs"),
        "tooling_cost": NONE,
        "moq": NONE,
        "lead_time_days": ev("25", "Prazo de entrega: 25 dias uteis apos pedido"),
        "origin_country": NONE,
        "qualification": NONE,
    }
    fields.update(overrides)
    return ProposalLineDraft.model_validate(fields)


def rfq3_output(*lines: ProposalLineDraft, **overrides: object) -> QuoteExtractionOutput:
    fields: dict[str, object] = {
        "is_quote": True,
        "currency": ev("BRL", "Moeda: BRL"),
        "valid_until": NONE,
        "incoterm": ev("CIF", "entrega CIF Joinville/SC"),
        "delivery_location": ev("Joinville/SC", "entrega CIF Joinville/SC"),
        "payment_terms": ev("28 DDL", "Condicao de pagamento: 28 DDL"),
        "lines": list(lines) if lines else [rfq3_line()],
    }
    fields.update(overrides)
    return QuoteExtractionOutput.model_validate(fields)


def validate_rfq3(
    out: QuoteExtractionOutput,
    *,
    requested: list[RequestedLine] | None = None,
    rfq_currency: str | None = "BRL",
) -> ValidatedQuote:
    return validate_quote(
        out,
        requested=requested if requested is not None else [RFQ3_LINE],
        sources=QuoteSources(email_subject="", email_body=RFQ3_BODY),
        today=TODAY,
        decimal=",",
        rfq_currency=rfq_currency,
    )


def test_the_rfq3_quote_differs_from_the_request_in_quantity_and_incoterm() -> None:
    # 1,500 pcs FOB Joinville-SC requested; 50 pcs CIF Joinville/SC quoted, in 25 business days.
    result = validate_rfq3(rfq3_output())

    [checked] = result.lines
    assert checked.issues == ("quantity_mismatch",)
    assert result.issues == ("incoterm_mismatch",)
    assert result.needs_review
    # 25 business days from Tuesday 6 October 2026 in Brazil (a BRL quote), which skip
    # 12 October and 2 November: 12 November, 37 calendar days.
    assert checked.parsed("lead_time_days") == 37
    conversion = checked.fields["lead_time_days"].conversion
    assert conversion is not None
    assert (conversion["quoted"], conversion["unit"], conversion["holiday_calendar"]) == (
        25,
        "business_days",
        "BR",
    )
    assert issues_of(result, "lead_time_days") == ()
    assert result.parsed("delivery_location") == "Joinville/SC"


def test_a_quote_that_answers_the_request_has_no_mismatch() -> None:
    answered = RequestedLine(
        id=RFQ3_LINE.id,
        position=1,
        quantities=(Decimal(50), Decimal(1500)),
        incoterm="CIF",
        delivery_location="Joinville-SC",
    )

    result = validate_rfq3(rfq3_output(), requested=[answered])

    assert result.issues == () and result.lines[0].issues == ()
    assert not result.needs_review


def test_a_quoted_quantity_that_is_none_of_the_requested_tiers_is_a_mismatch() -> None:
    tiers = RequestedLine(id=RFQ3_LINE.id, position=1, quantities=(Decimal(1000), Decimal(5000)))
    quoted = [
        rfq3_line(min_quantity=ev(value, f"Quantidade: {value} pcs"))
        for value in ("1000", "2000", "5000")
    ]
    body = RFQ3_BODY + "Quantidade: 1000 pcs\nQuantidade: 2000 pcs\nQuantidade: 5000 pcs\n"

    result = validate_quote(
        rfq3_output(*quoted, incoterm=NONE, delivery_location=NONE),
        requested=[tiers],
        sources=QuoteSources(email_subject="", email_body=body),
        today=TODAY,
        decimal=",",
        rfq_currency="BRL",
    )

    assert [line.issues for line in result.lines] == [(), ("quantity_mismatch",), ()]
    # A line without a quantity, or one the RFQ does not have, is not compared.
    unknown = validate_rfq3(
        rfq3_output(rfq3_line(min_quantity=NONE), rfq3_line(rfq_line_position=9))
    )
    assert [line.issues for line in unknown.lines] == [(), ("unknown_rfq_line",)]


def test_an_incoterm_other_than_the_rfqs_is_a_mismatch_only_when_both_are_stated() -> None:
    same = replace_line(RFQ3_LINE, incoterm="CIF")
    unstated = replace_line(RFQ3_LINE, incoterm=None)

    assert "incoterm_mismatch" in validate_rfq3(rfq3_output()).issues
    assert "incoterm_mismatch" not in validate_rfq3(rfq3_output(), requested=[same]).issues
    assert "incoterm_mismatch" not in validate_rfq3(rfq3_output(), requested=[unstated]).issues
    assert "incoterm_mismatch" not in validate_rfq3(rfq3_output(incoterm=NONE)).issues
    # An Incoterm the quote does not back is not compared either.
    ungrounded = validate_rfq3(rfq3_output(incoterm=ev("DAP", "entrega CIF Joinville/SC")))
    assert ungrounded.header["incoterm"].issues == ("not_grounded",)
    assert "incoterm_mismatch" not in ungrounded.issues


def test_a_delivery_location_other_than_the_rfqs_is_a_mismatch() -> None:
    curitiba = replace_line(RFQ3_LINE, delivery_location="Planta Curitiba - PR")
    sao_paulo = replace_line(RFQ3_LINE, delivery_location="São Paulo")
    unstated = replace_line(RFQ3_LINE, delivery_location=None)

    # Joinville/SC is Joinville-SC, Joinville, and a full address in Joinville.
    assert "delivery_location_mismatch" not in validate_rfq3(rfq3_output()).issues
    for same in ("Joinville", "Rua das Flores, 123 - Joinville, SC, 89200-000", "JOINVILLE SC"):
        requested = [replace_line(RFQ3_LINE, delivery_location=same)]
        assert (
            "delivery_location_mismatch"
            not in validate_rfq3(rfq3_output(), requested=requested).issues
        ), same
    assert "delivery_location_mismatch" in validate_rfq3(rfq3_output(), requested=[curitiba]).issues
    assert (
        "delivery_location_mismatch" in validate_rfq3(rfq3_output(), requested=[sao_paulo]).issues
    )
    assert (
        "delivery_location_mismatch"
        not in validate_rfq3(rfq3_output(), requested=[unstated]).issues
    )
    assert (
        "delivery_location_mismatch"
        not in validate_rfq3(rfq3_output(delivery_location=NONE), requested=[curitiba]).issues
    )


def test_a_currency_other_than_the_rfqs_is_a_mismatch() -> None:
    assert "currency_mismatch" not in validate_rfq3(rfq3_output()).issues
    assert "currency_mismatch" in validate_rfq3(rfq3_output(), rfq_currency="EUR").issues
    assert "currency_mismatch" not in validate_rfq3(rfq3_output(), rfq_currency=None).issues
    # The line's own prices in reais agree with the BRL header: no line-level mismatch.
    result = validate_rfq3(rfq3_output(), rfq_currency="EUR")
    assert result.issues == ("incoterm_mismatch", "currency_mismatch")
    assert result.lines[0].issues == ("quantity_mismatch",)
    assert result.needs_review


def test_mismatches_are_compared_with_the_lines_the_quote_answers() -> None:
    other = RequestedLine(
        id=uuid4(),
        position=2,
        quantities=(Decimal(10),),
        incoterm="CIF",
        delivery_location="Joinville",
    )
    # Line 1 asks FOB; the quote answers line 2 only, which asks CIF: no mismatch.
    answers_two = validate_rfq3(
        rfq3_output(rfq3_line(rfq_line_position=2, min_quantity=NONE)), requested=[RFQ3_LINE, other]
    )
    assert answers_two.issues == ()
    # No line matched at all: every requested line is compared.
    unmatched = validate_rfq3(
        rfq3_output(rfq3_line(rfq_line_position=None)), requested=[RFQ3_LINE, other]
    )
    assert unmatched.issues == ("incoterm_mismatch",)


def replace_line(line: RequestedLine, **changes: object) -> RequestedLine:
    return replace(line, **changes)
