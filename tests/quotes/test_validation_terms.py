"""Prompt v5 terms: lead times and validity as data, quantity basis, general terms, signals."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

from bido_back.agents.catalog.contracts import (
    DeclinedLine,
    ExtractedValue,
    LeadTimeValue,
    ProposalLineDraft,
    QuoteExtractionOutput,
    SupplierQuestion,
    ValidityValue,
)
from bido_back.quotes.lead_time import StoredLeadTime, decode_lead_time, decode_validity
from bido_back.quotes.validation import (
    LineCheck,
    QuoteSources,
    RequestedLine,
    ValidatedQuote,
    quantity_bases,
    validate_quote,
)
from tests.quotes import rfq6
from tests.quotes.rfq6 import NO_LEAD, NONE, ev, lead

OFFER_DATE = date(2026, 10, 9)
PART_1 = RequestedLine(id=uuid4(), position=1, quantities=(Decimal(100),))
PART_2 = RequestedLine(id=uuid4(), position=2, quantities=(Decimal(50), Decimal(200)))
PRICE_1 = "Pos. 1: 2,35 EUR pro Stück"
PRICE_2 = "Pos. 2: 4,10 EUR pro Stück"


def draft(
    position: int = 1,
    *,
    lead_time: LeadTimeValue = NO_LEAD,
    min_quantity: ExtractedValue = NONE,
    conditional_terms: ExtractedValue = NONE,
    lead_time_days: ExtractedValue = NONE,
    offered_mpn: ExtractedValue = NONE,
    is_alternative: bool = False,
    price: ExtractedValue | None = None,
) -> ProposalLineDraft:
    quote = PRICE_1 if position == 1 else PRICE_2
    amount = "2.35" if position == 1 else "4.10"
    return ProposalLineDraft(
        rfq_line_position=position,
        offered_mpn=offered_mpn,
        is_alternative=is_alternative,
        min_quantity=min_quantity,
        unit_price=price if price is not None else ev(amount, quote),
        unit=NONE,
        tooling_cost=NONE,
        moq=NONE,
        origin_country=NONE,
        qualification=NONE,
        lead_time=lead_time,
        lead_time_days=lead_time_days,
        conditional_terms=conditional_terms,
    )


def offer(*lines: ProposalLineDraft, **overrides: object) -> QuoteExtractionOutput:
    fields: dict[str, object] = {
        "is_quote": True,
        "currency": ev("EUR", "EUR"),
        "valid_until": NONE,
        "incoterm": NONE,
        "payment_terms": NONE,
        "lines": list(lines) if lines else [draft()],
    }
    fields.update(overrides)
    return QuoteExtractionOutput.model_validate(fields)


def check(
    out: QuoteExtractionOutput,
    body: str,
    *,
    offer_date: date = OFFER_DATE,
    original_offer_date: date | None = None,
    country: str | None = None,
) -> ValidatedQuote:
    return validate_quote(
        out,
        requested=[PART_1, PART_2],
        sources=QuoteSources(email_subject="AW: [RFQ-7] Kugellager", email_body=body),
        today=OFFER_DATE,
        decimal=",",
        offer_date=offer_date,
        original_offer_date=original_offer_date,
        country=country,
    )


def body(*sentences: str) -> str:
    return "\n".join([PRICE_1 + ".", PRICE_2 + ".", *sentences])


def stored_lead(line: LineCheck) -> StoredLeadTime | None:
    return decode_lead_time(line.fields["lead_time_terms"].parsed)


# --- RFQ-6, the real thread -------------------------------------------------------------------


def test_rfq6_first_reply_is_usable_as_it_was_written() -> None:
    result = rfq6.validate(rfq6.first_reply(), rfq6.REPLY_1)

    assert not result.needs_review, [(line.issues, line.fields) for line in result.lines]
    # "valid for 15 days", counted from the message date.
    assert result.parsed("valid_until") == date(2026, 10, 24)
    validity = decode_validity(result.header["validity"].parsed)
    assert validity is not None
    assert (validity.amount, validity.unit, validity.anchor, validity.anchor_kind) == (
        Decimal(15),
        "calendar_days",
        date(2026, 10, 9),
        "message",
    )
    # "approximately 10 business days after order confirmation" for all items: 17 calendar
    # days with the Brazilian calendar (BRL; the supplier has no country), approximate.
    assert [line.parsed("lead_time_days") for line in result.lines] == [17, 17, 17]
    for line in result.lines:
        stored = stored_lead(line)
        assert stored is not None
        assert (stored.unit, stored.amount, stored.basis, stored.scope) == (
            "business_days",
            Decimal(10),
            "order",
            "offer",
        )
        assert stored.approximate and stored.holiday_calendar == "BR"
        assert stored.expected.delivery_date == date(2026, 10, 26)
        assert stored.expected.reasons == ("approximate",)
        assert line.fields["lead_time_days"].issues == ()
    stored_leads = [stored_lead(line) for line in result.lines]
    risks = [stored.risk_note if stored is not None else None for stored in stored_leads]
    assert risks == [None, rfq6.SHAFT_RISK, None]
    # Single prices without tiers: no quantity was written, none is required.
    assert [line.parsed("min_quantity") for line in result.lines] == [None, None, None]
    bases = quantity_bases(result.lines, {line.id: line.quantities for line in rfq6.LINES})
    assert [(b.quantity_basis, b.price_applies_to_requested_quantity) for b in bases] == [
        ("requested_quantity", True)
    ] * 3
    assert result.output.offer_notes == ["All items are supplied in standard industrial packaging."]
    assert not result.signals.final_offer and not result.signals.questions


def test_rfq6_second_reply_lead_times_per_line_in_business_days() -> None:
    result = rfq6.validate(rfq6.second_reply(), rfq6.REPLY_2, original_offer_date=rfq6.OFFER_DATE)

    assert not result.needs_review, [line.issues for line in result.lines]
    # 7/12/8 business days from 9 October (12 October is a holiday in Brazil).
    assert [line.parsed("lead_time_days") for line in result.lines] == [12, 19, 13]
    for line in result.lines:
        stored = stored_lead(line)
        assert stored is not None
        assert (stored.unit, stored.basis, stored.scope, stored.approximate) == (
            "business_days",
            "order",  # "All lead times are counted from purchase order confirmation."
            "line",
            False,
        )
        assert stored.expected.exact
    assert [line.parsed("min_quantity") for line in result.lines] == [
        Decimal(70),
        Decimal(15),
        Decimal("12.5"),
    ]
    bases = quantity_bases(result.lines, {line.id: line.quantities for line in rfq6.LINES})
    assert [b.price_applies_to_requested_quantity for b in bases] == [True, True, True]
    assert {b.quantity_basis for b in bases} == {"stated_tier"}
    assert result.parsed("valid_until") == date(2026, 10, 24)


def test_validity_from_the_original_offer_counts_from_that_offer() -> None:
    later = date(2026, 10, 12)

    anchored = rfq6.validate(
        rfq6.second_reply(), rfq6.REPLY_2, offer_date=later, original_offer_date=rfq6.OFFER_DATE
    )
    alone = rfq6.validate(rfq6.second_reply(), rfq6.REPLY_2, offer_date=later)

    assert anchored.parsed("valid_until") == date(2026, 10, 24)
    validity = decode_validity(anchored.header["validity"].parsed)
    assert validity is not None and validity.anchor_kind == "original_offer"
    # Without an earlier offer the period runs from this message.
    assert alone.parsed("valid_until") == date(2026, 10, 27)


def test_v4_catch_all_conditions_become_offer_notes_and_the_shaft_risk() -> None:
    # The v4 model copied the payment/validity/MOQ/packaging/lead-time paragraph onto every
    # line (and the shafts' extra days onto line 2): every line was blocked.
    output = rfq6.first_reply(conditions=rfq6.V4_CATCH_ALL)
    no_risk = output.lines[1].model_copy(update={"lead_time": NO_LEAD})
    output = output.model_copy(update={"lines": [output.lines[0], no_risk, output.lines[2]]})

    result = rfq6.validate(output, rfq6.REPLY_1)

    assert not result.needs_review, [line.issues for line in result.lines]
    assert all(line.parsed("conditional_terms") is None for line in result.lines)
    assert all(not line.draft.conditional_terms.value for line in result.lines)
    assert result.output.offer_notes == [
        "All items are supplied in standard industrial packaging.",
        "Our standard payment terms are Net 30, and the quotation is valid for 15 days.",
        "The MOQ is 10 pieces for bearings and shafts, and 5 kg for copper wire.",
        "Lead time is approximately 10 business days after order confirmation.",
    ]
    shaft = stored_lead(result.lines[1])
    assert shaft is not None and shaft.risk_note == rfq6.SHAFT_RISK


def test_a_timing_condition_without_any_lead_time_still_needs_review() -> None:
    output = rfq6.first_reply(conditions=rfq6.V4_CATCH_ALL)
    lines = [line.model_copy(update={"lead_time": NO_LEAD}) for line in output.lines]
    output = output.model_copy(update={"lines": lines, "lead_time": NO_LEAD})

    result = rfq6.validate(output, rfq6.REPLY_1)

    assert [line.parsed("conditional_terms") for line in result.lines] == [
        None,
        rfq6.SHAFT_RISK,
        None,
    ]
    assert [line.needs_review for line in result.lines] == [False, True, False]
    assert result.lines[1].issues == ("conditional_terms_require_review",)


# --- Conditions -------------------------------------------------------------------------------


def test_a_condition_on_one_line_still_needs_review() -> None:
    only = "Preis gilt nur bei Abnahme von 500 Stück."
    result = check(offer(draft(1, conditional_terms=ev(only, only)), draft(2)), body(only))

    assert result.lines[0].parsed("conditional_terms") == only
    assert "conditional_terms_require_review" in result.lines[0].issues
    assert not result.lines[1].needs_review
    assert result.output.offer_notes == []


def test_the_same_condition_on_every_tier_of_one_position_is_kept() -> None:
    # One requested position in two tiers: not a term of the whole offer.
    condition = "Preise nur gültig bei Jahresabruf."
    tiers = [
        draft(1, min_quantity=ev("100", "100 Stück"), conditional_terms=ev(condition, condition)),
        draft(1, min_quantity=ev("500", "500 Stück"), conditional_terms=ev(condition, condition)),
    ]

    result = check(offer(*tiers), body("100 Stück, 500 Stück.", condition))

    assert all(line.parsed("conditional_terms") == condition for line in result.lines)
    assert all(line.needs_review for line in result.lines)


def test_a_condition_the_supplier_gave_as_a_general_note_is_not_repeated_per_line() -> None:
    note = "Verpackung: Standardkarton."
    result = check(
        offer(draft(1, conditional_terms=ev(note, note)), offer_notes=[note]), body(note)
    )

    assert result.lines[0].parsed("conditional_terms") is None
    assert not result.lines[0].needs_review
    assert result.output.offer_notes == [note]


def test_unverified_offer_notes_are_dropped() -> None:
    result = check(offer(offer_notes=["Garantie 5 Jahre."]), body())

    assert result.output.offer_notes == []


# --- Quantities -------------------------------------------------------------------------------


def test_a_quantity_copied_from_the_request_is_dropped_not_flagged() -> None:
    copied = ev("100", PRICE_1)  # 100 is the requested quantity; the quote never says it

    result = check(offer(draft(1, min_quantity=copied)), body())

    [line] = result.lines
    assert line.parsed("min_quantity") is None
    assert line.fields["min_quantity"].issues == ()
    assert line.draft.min_quantity.value is None
    assert not line.needs_review


def test_a_written_or_unrequested_quantity_is_kept() -> None:
    written = check(offer(draft(1, min_quantity=ev("100", "100 Stück"))), body("100 Stück"))
    invented = check(offer(draft(1, min_quantity=ev("80", PRICE_1))), body())

    assert written.lines[0].parsed("min_quantity") == Decimal(100)
    assert not written.lines[0].needs_review
    # Not a requested quantity: an unverified number stays on review.
    assert invented.lines[0].fields["min_quantity"].issues == ("not_grounded",)


def test_quantity_bases() -> None:
    def bases(*lines: ProposalLineDraft, text: str = "") -> list[tuple[str, bool]]:
        result = check(offer(*lines), body(text))
        terms = quantity_bases(
            result.lines, {PART_1.id: (Decimal(100),), PART_2.id: (Decimal(50),)}
        )
        return [(t.quantity_basis, t.price_applies_to_requested_quantity) for t in terms]

    # One price per part, no quantity: it applies to the requested quantity.
    assert bases(draft(1), draft(2)) == [("requested_quantity", True)] * 2
    # Two prices for one part and no quantities: which one applies is unclear.
    assert bases(draft(1), draft(1)) == [("ambiguous", False)] * 2
    # A written tier applies when it is at or below a requested quantity.
    assert bases(
        draft(1, min_quantity=ev("100", "ab 100 Stück")),
        draft(1, min_quantity=ev("500", "ab 500 Stück")),
        text="ab 100 Stück, ab 500 Stück",
    ) == [("stated_tier", True), ("stated_tier", False)]
    # An alternative part is priced on its own.
    alternative = draft(1, offered_mpn=ev("6204-ZZ", "6204-ZZ"), is_alternative=True)
    assert (
        bases(draft(1), alternative, text="Alternative 6204-ZZ")
        == [("requested_quantity", True)] * 2
    )
    unmatched = draft(1).model_copy(update={"rfq_line_position": None})
    assert bases(unmatched) == [("unmatched", False)]


# --- Lead times --------------------------------------------------------------------------------


def test_a_line_lead_time_overrides_the_offer_lead_time() -> None:
    general = "Lieferzeit ca. 3 Wochen nach Auftragseingang"
    own = "Pos. 2: Lieferzeit 6 Wochen"
    result = check(
        offer(
            draft(1),
            draft(2, lead_time=lead("6", own, unit="weeks")),
            lead_time=lead("3", general, unit="weeks", basis="order", approximate=True),
        ),
        body(general + ".", own + "."),
    )

    assert [line.parsed("lead_time_days") for line in result.lines] == [21, 42]
    first, second = (stored_lead(line) for line in result.lines)
    assert first is not None and second is not None
    assert (first.scope, first.approximate, first.basis) == ("offer", True, "order")
    # The line's own lead time takes the offer's basis when it states none.
    assert (second.scope, second.approximate, second.basis) == ("line", False, "order")
    assert not result.needs_review
    # The offer-level statement is kept as evidence, without review issues.
    assert result.header["lead_time"].issues == ()
    assert decode_lead_time(result.header["lead_time"].parsed) is not None


def test_approximation_and_ranges_are_read_from_the_quote() -> None:
    hedged = "Lieferzeit etwa 4 Wochen"
    ranged = "Lieferzeit 2-3 Wochen"
    by_hedge = check(offer(draft(1, lead_time=lead("4", hedged, unit="weeks"))), body(hedged))
    # The model wrote the lower bound: the buyer plans with the end of the range.
    by_range = check(offer(draft(1, lead_time=lead("2", ranged, unit="weeks"))), body(ranged))

    hedge = stored_lead(by_hedge.lines[0])
    assert hedge is not None and hedge.approximate
    assert by_range.lines[0].parsed("lead_time_days") == 21
    in_range = stored_lead(by_range.lines[0])
    assert in_range is not None and in_range.approximate and in_range.amount == Decimal(3)


def test_the_unit_word_must_be_in_the_quote() -> None:
    quote = "Lieferzeit 10 Arbeitstage"
    as_calendar = check(
        offer(draft(1, lead_time=lead("10", quote, unit="calendar_days"))), body(quote)
    )
    as_weeks = check(offer(draft(1, lead_time=lead("10", quote, unit="weeks"))), body(quote))
    right = check(offer(draft(1, lead_time=lead("10", quote))), body(quote), country="DE")

    assert as_calendar.lines[0].fields["lead_time_days"].issues == ("unit_not_grounded",)
    assert as_weeks.lines[0].fields["lead_time_days"].issues == ("unit_not_grounded",)
    assert right.lines[0].fields["lead_time_days"].issues == ()
    # 10 German business days from Friday 9 October 2026 (3 October is long past).
    assert right.lines[0].parsed("lead_time_days") == 14


def test_plain_days_are_calendar_days() -> None:
    quote = "Lieferzeit: 15"  # a bare number under "Lieferzeit" is days, as before v5
    result = check(offer(draft(1, lead_time=lead("15", quote, unit="calendar_days"))), body(quote))

    assert result.lines[0].fields["lead_time_days"].issues == ()
    assert result.lines[0].parsed("lead_time_days") == 15


def test_lead_time_problems_put_lead_time_days_on_review() -> None:
    quote = "Lieferzeit 10 Arbeitstage"
    wrong_number = check(offer(draft(1, lead_time=lead("12", quote))), body(quote))
    no_unit = check(offer(draft(1, lead_time=lead("10", quote, unit=None))), body(quote))
    unsure = check(offer(draft(1, lead_time=lead("10", quote, confidence=0.6))), body(quote))

    assert wrong_number.lines[0].fields["lead_time_days"].issues == ("not_grounded",)
    assert no_unit.lines[0].fields["lead_time_days"].issues == ("unit_missing",)
    assert no_unit.lines[0].parsed("lead_time_days") is None
    assert unsure.lines[0].fields["lead_time_days"].issues == ("low_confidence",)
    for result in (wrong_number, no_unit, unsure):
        assert result.lines[0].needs_review
        # The statement itself never needs review: only the correctable field does.
        assert result.lines[0].fields["lead_time_terms"].issues == ()


def test_a_delivery_date_without_a_year_is_the_next_such_day() -> None:
    quote = "consegna entro il 30/10"
    right = check(offer(draft(1, lead_time=lead("2026-10-30", quote, unit="date"))), body(quote))
    wrong_year = check(
        offer(draft(1, lead_time=lead("2027-10-30", quote, unit="date"))), body(quote)
    )

    assert right.lines[0].parsed("lead_time_days") == 21
    assert right.lines[0].fields["lead_time_days"].issues == ()
    stored = stored_lead(right.lines[0])
    assert stored is not None and stored.delivery_date == date(2026, 10, 30)
    assert wrong_year.lines[0].fields["lead_time_days"].issues == ("not_grounded",)


def test_ready_from_stock_is_zero_days() -> None:
    quote = "Prazo de entrega: pronta entrega"
    result = check(offer(draft(1, lead_time=lead("0", quote, unit="calendar_days"))), body(quote))

    assert result.lines[0].parsed("lead_time_days") == 0
    assert result.lines[0].fields["lead_time_days"].issues == ()


def test_lead_time_days_written_by_older_prompts_still_work() -> None:
    weeks = "Lieferzeit: 6 Wochen"
    business = "Prazo: 15 dias úteis"
    in_weeks = check(offer(draft(1, lead_time_days=ev("42", weeks))), body(weeks))
    in_business_days = check(offer(draft(1, lead_time_days=ev("15", business))), body(business))

    assert in_weeks.lines[0].parsed("lead_time_days") == 42
    assert in_weeks.lines[0].fields["lead_time_terms"].parsed is None
    # A raw business-day number is not calendar days: flagged, as before (now in Portuguese).
    assert in_business_days.lines[0].fields["lead_time_days"].issues == ("working_days",)


# --- Validity --------------------------------------------------------------------------------


def test_a_written_date_wins_over_a_duration() -> None:
    text = "Angebot gültig 30 Tage, d.h. bis 30.11.2026"
    result = check(
        offer(
            valid_until=ev("2026-11-30", text),
            validity=ValidityValue(
                value="30", unit="calendar_days", source=ev("30", text).source, confidence=0.95
            ),
        ),
        body(text),
    )

    assert result.parsed("valid_until") == date(2026, 11, 30)


def test_a_computed_date_is_replaced_by_the_stated_duration() -> None:
    text = "Angebot gültig 4 Wochen"
    computed = ev("2026-11-06", text)  # what a model computed: no such date in the text
    result = check(
        offer(
            valid_until=computed,
            validity=ValidityValue(
                value="4", unit="weeks", source=ev("4", text).source, confidence=0.95
            ),
        ),
        body(text),
    )

    assert result.parsed("valid_until") == date(2026, 11, 6)
    assert result.header["valid_until"].issues == ()


def test_an_unverified_duration_is_asked_to_be_confirmed() -> None:
    text = "Angebot gültig 30 Tage"
    result = check(
        offer(
            validity=ValidityValue(
                value="45", unit="calendar_days", source=ev("45", text).source, confidence=0.95
            )
        ),
        body(text),
    )

    # The date is computed but the number is not in the quote: valid_until needs review.
    assert result.parsed("valid_until") == date(2026, 11, 23)
    assert result.header["valid_until"].issues == ("not_grounded",)
    assert result.header["validity"].issues == ()


def test_a_business_day_validity_uses_the_holiday_calendar() -> None:
    text = "validade de 10 dias úteis"
    result = check(
        offer(
            validity=ValidityValue(
                value="10", unit="business_days", source=ev("10", text).source, confidence=0.95
            )
        ),
        body(text),
        country="BR",
    )

    assert result.parsed("valid_until") == date(2026, 10, 26)


# --- Signals ---------------------------------------------------------------------------------


def test_final_offer_statement() -> None:
    final = "Dies ist unser letztes Angebot."
    stated = check(offer(final_offer=ev(final, final)), body(final))
    unsure = check(offer(final_offer=ev(final, final, confidence=0.5)), body(final))
    invented = check(offer(final_offer=ev(final, final)), body())

    assert stated.signals.final_offer
    assert stated.signals.final_offer_quote == final
    assert not unsure.signals.final_offer
    assert not invented.signals.final_offer
    # A signal is not a reason for review.
    assert not stated.needs_review


def test_declined_lines() -> None:
    refusal = "Pos. 2 können wir leider nicht anbieten."
    result = check(
        offer(
            draft(1),
            declined_lines=[
                DeclinedLine(rfq_line_position=2, quote=refusal),
                DeclinedLine(rfq_line_position=1, quote=PRICE_1),  # priced: not declined
                DeclinedLine(rfq_line_position=9, quote=refusal),  # not requested
                DeclinedLine(rfq_line_position=2, quote=refusal),  # once
            ],
        ),
        body(refusal),
    )
    invented = check(
        offer(draft(1), declined_lines=[DeclinedLine(rfq_line_position=2, quote=refusal)]),
        body(),
    )

    assert result.signals.declined_line_positions == (2,)
    assert result.signals.declined_lines[0].quote == refusal
    assert invented.signals.declined_lines == ()


def test_supplier_questions_are_grounded_and_hold_the_offer() -> None:
    asked = "Können Sie uns die Zeichnung für Pos. 2 senden?"
    result = check(
        offer(
            supplier_questions=[
                SupplierQuestion(quote=asked, topic="technical", rfq_line_position=2),
                SupplierQuestion(quote="Accept Net 60?", topic="commercial", rfq_line_position=7),
            ]
        ),
        body(asked),
    )

    first, second = result.signals.questions
    assert (first.quote, first.topic, first.rfq_line_position, first.grounded) == (
        asked,
        "technical",
        2,
        True,
    )
    assert (second.rfq_line_position, second.grounded) == (None, False)
    # Every question also holds the offer for the buyer (technical_questions).
    assert result.output.technical_questions == [asked, "Accept Net 60?"]


def test_questions_without_an_offer_are_still_read() -> None:
    asked = "Welches Material ist für Pos. 1 gefordert?"
    result = check(
        offer(
            is_quote=False,
            lines=[],
            currency=NONE,
            reply_kind="clarification",
            supplier_questions=[
                SupplierQuestion(quote=asked, topic="technical", rfq_line_position=1)
            ],
        ),
        asked,
    )

    assert [q.quote for q in result.signals.questions] == [asked]
    assert result.output.technical_questions == [asked]
