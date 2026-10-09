"""Answers to our clarification, holds recomputed per reply, and the buyer's release.

Offer reconciliation is pure: these build validated replies and replay them.
"""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from bido_back.agents.catalog.contracts import DeclinedLine, ProposalLineDraft
from bido_back.quotes.state import AskedField, OfferEvent, reconcile
from bido_back.quotes.validation import (
    LINE_FIELDS,
    QuoteSources,
    RequestedLine,
    ValidatedQuote,
    validate_quote,
)
from tests.quotes import rfq6 as rfq6_v5
from tests.quotes.test_validation import BODY, LINE_2, NONE, ev, line, output, validate

START = datetime(2026, 10, 6, 12, tzinfo=UTC)


def event(
    quote: ValidatedQuote,
    hour: int = 0,
    *,
    answers: tuple[AskedField, ...] = (),
    released: bool = False,
) -> OfferEvent:
    message_id = uuid4()
    return OfferEvent(
        message_id=message_id,
        message_header=f"<{message_id}@supplier.test>",
        in_reply_to=None,
        occurred_at=START + timedelta(hours=hour),
        received_at=START + timedelta(hours=hour, minutes=1),
        quote=quote,
        answers=answers,
        released=released,
    )


def delta(position: int = 1, **values: object) -> ProposalLineDraft:
    return ProposalLineDraft.model_validate(
        {"rfq_line_position": position, **{name: NONE for name in LINE_FIELDS}, **values}
    )


def partial(*lines: ProposalLineDraft, body: str, unchanged: str | None = None) -> ValidatedQuote:
    return validate(
        output(
            *lines,
            currency=NONE,
            valid_until=NONE,
            incoterm=NONE,
            payment_terms=NONE,
            reply_kind="amendment",
            unchanged_terms=ev(unchanged, unchanged) if unchanged else NONE,
        ),
        body=body,
    )


def first_offer() -> ValidatedQuote:
    """One line, one price, no quantity tier and no lead time."""
    return validate(output(line(min_quantity=NONE, lead_time_days=NONE)))


LEAD = AskedField(1, "lead_time")


def test_answers_to_our_clarification_apply_without_an_unchanged_sentence() -> None:
    answer = partial(delta(lead_time_days=ev("42", "6 Wochen")), body="Lieferzeit 6 Wochen.")
    first = event(first_offer())
    state = reconcile([first, event(answer, 1, answers=(LEAD,))])

    assert state.issues == () and state.reply_kind == "amendment"
    [offer] = state.quote.lines
    assert offer.parsed("lead_time_days") == 42
    assert not offer.needs_review
    # Everything else carries over, with the answer as the reason it may.
    assert offer.parsed("unit_price") == Decimal("0.45")
    assert state.quote.parsed("payment_terms") == "30 Tage netto"
    retained = offer.fields["unit_price"].retained_by[-1]
    assert retained["basis"] == "clarification_answer"
    assert state.answered == (LEAD,)
    assert state.provenance()["answered_asks"] == [{"position": 1, "field": "lead_time"}]

    # The same reply without our question: an amendment without the statement, held.
    held = reconcile([first, event(answer, 1)])
    assert "amendment_without_explicit_preservation" in held.issues
    assert held.quote.lines[0].parsed("lead_time_days") is None


def test_restating_values_and_filling_gaps_is_not_a_change() -> None:
    # The supplier restates its price and adds the quantity it applies to (RFQ-6 reply 2).
    answer = partial(
        delta(
            unit_price=ev("0.45", "0,45 €"),
            min_quantity=ev("1000", "1.000 Stück"),
            lead_time_days=ev("42", "6 Wochen"),
        ),
        body="0,45 € für 1.000 Stück, Lieferzeit 6 Wochen.",
    )
    state = reconcile([event(first_offer()), event(answer, 1, answers=(LEAD,))])
    assert state.issues == ()
    [offer] = state.quote.lines
    assert (offer.parsed("min_quantity"), offer.parsed("lead_time_days")) == (Decimal(1000), 42)


def test_an_unasked_price_change_is_taken_but_holds_the_offer() -> None:
    answer = partial(
        delta(unit_price=ev("0.40", "0,40 €"), lead_time_days=ev("42", "6 Wochen")),
        body="Neuer Preis 0,40 €, Lieferzeit 6 Wochen.",
    )
    state = reconcile([event(first_offer()), event(answer, 1, answers=(LEAD,))])
    assert state.issues == ("changed_commercial_basis_requires_replacement",)
    [offer] = state.quote.lines
    # The supplier's latest statement is shown, and the answer is applied.
    assert offer.parsed("unit_price") == Decimal("0.40") and offer.parsed("lead_time_days") == 42
    assert offer.needs_review


def test_an_answer_can_price_a_line_the_offer_lacked() -> None:
    second = delta(
        2,
        offered_mpn=ev("LM317DCYR", "LM317DCYR"),
        unit_price=ev("0.80", "0,80 €"),
        unit=ev("pcs", "Stück"),
        lead_time_days=ev("42", "6 Wochen"),
    )
    answer = partial(second, body="LM317DCYR: 0,80 € je Stück, Lieferzeit 6 Wochen.")
    state = reconcile(
        [event(first_offer()), event(answer, 1, answers=(AskedField(2, "unit_price"),))]
    )
    assert state.issues == ()
    assert [line.rfq_line_id for line in state.quote.lines] == [
        state.quote.lines[0].rfq_line_id,
        LINE_2.id,
    ]
    assert state.quote.lines[1].parsed("unit_price") == Decimal("0.80")


def test_an_amendment_with_the_statement_may_quote_a_line_the_offer_lacked() -> None:
    second = delta(
        2,
        offered_mpn=ev("LM317DCYR", "LM317DCYR"),
        unit_price=ev("0.80", "0,80 €"),
        unit=ev("pcs", "Stück"),
    )
    completion = partial(
        second,
        body="LM317DCYR: 0,80 € je Stück. Alle anderen Bedingungen bleiben unverändert.",
        unchanged="anderen Bedingungen bleiben unverändert",
    )
    state = reconcile([event(first_offer()), event(completion, 1)])
    assert state.issues == ()
    assert len(state.quote.lines) == 2


def test_a_held_partial_update_is_covered_by_a_later_unchanged_statement() -> None:
    # No sticky hold: the second update says the other terms stand, which covers the first.
    lead = partial(delta(lead_time_days=ev("15", "15 Tage")), body="Lieferzeit jetzt 15 Tage.")
    price = partial(
        delta(unit_price=ev("0.40", "0,40 €")),
        body="Preis jetzt 0,40 €. Alle anderen Bedingungen bleiben unverändert.",
        unchanged="anderen Bedingungen bleiben unverändert",
    )
    first = event(first_offer())
    held = reconcile([first, event(lead, 1)])
    assert "amendment_without_explicit_preservation" in held.issues

    state = reconcile([first, event(lead, 1), event(price, 2)])
    assert state.issues == () and state.held_message_ids == ()
    [offer] = state.quote.lines
    assert (offer.parsed("lead_time_days"), offer.parsed("unit_price")) == (15, Decimal("0.40"))
    assert not offer.needs_review


def test_a_held_partial_update_is_covered_by_answers_to_our_clarification() -> None:
    lead = partial(delta(lead_time_days=ev("15", "15 Tage")), body="Lieferzeit jetzt 15 Tage.")
    answer = partial(
        delta(min_quantity=ev("1000", "1.000 Stück")), body="Der Preis gilt für 1.000 Stück."
    )
    state = reconcile(
        [
            event(first_offer()),
            event(lead, 1),
            event(answer, 2, answers=(AskedField(1, "quantity"),)),
        ]
    )
    assert state.issues == ()
    [offer] = state.quote.lines
    assert (offer.parsed("lead_time_days"), offer.parsed("min_quantity")) == (15, Decimal(1000))


def test_other_holds_stay_until_a_whole_offer_or_the_buyer() -> None:
    first = event(first_offer())
    currency = partial(delta(), body="")
    currency = replace(
        currency,
        header={
            **currency.header,
            "currency": replace(currency.header["currency"], extracted=ev("USD"), parsed="USD"),
        },
    )
    changed = event(currency, 1)
    clean = partial(
        delta(lead_time_days=ev("15", "15 Tage")),
        body="Lieferzeit 15 Tage. Alle anderen Bedingungen bleiben unverändert.",
        unchanged="anderen Bedingungen bleiben unverändert",
    )
    held = reconcile([first, changed, event(clean, 2)])
    assert "changed_commercial_basis_requires_replacement" in held.issues
    assert held.held_message_ids == (changed.message_id,)

    # A whole new offer replaces everything before it.
    assert reconcile([first, changed, event(validate(output()), 3)]).issues == ()

    # The buyer took the offer as shown after the held reply: its hold is gone, and the
    # next clean update applies.
    released = reconcile([first, replace(changed, released=True), event(clean, 2)])
    assert released.issues == ()
    assert released.quote.lines[0].parsed("lead_time_days") == 15
    assert released.quote.parsed("currency") == "EUR"


def test_an_answer_clears_the_issues_its_gap_caused() -> None:
    # No currency: every priced line is unverified, the offer is missing its currency.
    no_currency = validate(output(line(min_quantity=NONE), currency=NONE), body=BODY)
    assert "missing:currency" in no_currency.issues
    assert "currency_unverified" in no_currency.lines[0].issues
    answer = validate(
        output(
            lines=[],
            currency=ev("EUR", "EUR"),
            valid_until=NONE,
            incoterm=NONE,
            payment_terms=NONE,
            reply_kind="clarification",
        ),
        body="Alle Preise in EUR.",
    )
    state = reconcile(
        [event(no_currency), event(answer, 1, answers=(AskedField(None, "currency"),))]
    )
    assert state.quote.parsed("currency") == "EUR"
    assert state.issues == ()
    assert not state.quote.lines[0].needs_review


def test_a_question_holds_nothing_and_names_the_question() -> None:
    question = validate(
        output(
            lines=[],
            is_quote=False,
            currency=NONE,
            valid_until=NONE,
            incoterm=NONE,
            payment_terms=NONE,
            reply_kind="clarification",
            technical_questions=["Which qualification grade do you require?"],
        )
    )
    state = reconcile([event(first_offer()), event(question, 1)])
    assert state.issues == () and state.reply_kind == "replacement"
    assert state.quote.lines[0].parsed("unit_price") == Decimal("0.45")
    assert not state.quote.lines[0].needs_review
    provenance = state.provenance()
    assert provenance["technical_questions"] == ["Which qualification grade do you require?"]
    # The negotiation waits for the buyer's answer (comparison.loader.negotiation_holds).
    assert provenance["requires_clarification"] is True


def test_withdrawn_lines_count_as_declined_until_offered_again() -> None:
    two = validate(output(line(), line(rfq_line_position=2, offered_mpn=NONE)))
    withdrawal = validate(
        output(
            lines=[],
            reply_kind="withdrawal",
            withdrawn_line_positions=[2],
            update_evidence=ev("We withdraw line 2"),
            currency=NONE,
            valid_until=NONE,
            incoterm=NONE,
            payment_terms=NONE,
        ),
        body="We withdraw line 2",
    )
    first = event(two)
    withdrawn = event(withdrawal, 1)
    assert reconcile([first, withdrawn]).declined_line_positions == (2,)
    # A later whole offer quoting it again: no longer declined.
    again = reconcile([first, withdrawn, event(two, 2)])
    assert again.declined_line_positions == ()


DECLINE = "LM317DCYR können wir leider nicht anbieten."


def test_a_line_the_reply_declines_is_declined_until_offered_again() -> None:
    # A partial quote: line 1 priced, line 2 declined in so many words (its signals).
    body = BODY + DECLINE
    partial_quote = validate(
        output(declined_lines=[DeclinedLine(rfq_line_position=2, quote=DECLINE)]), body=body
    )
    assert partial_quote.signals.declined_line_positions == (2,)
    state = reconcile([event(partial_quote)])
    assert state.declined_line_positions == (2,) and state.issues == ()
    assert state.provenance()["declined_line_positions"] == [2]
    # It stays declined over later replies that do not offer it...
    restated = reconcile([event(partial_quote), event(first_offer(), 1)])
    assert restated.declined_line_positions == (2,)
    # ...until one does.
    two = validate(output(line(), line(rfq_line_position=2, offered_mpn=NONE)))
    assert reconcile([event(partial_quote), event(two, 1)]).declined_line_positions == ()


def test_a_reply_that_only_declines_a_line_holds_nothing() -> None:
    declined = validate(
        output(
            lines=[],
            is_quote=False,
            currency=NONE,
            valid_until=NONE,
            incoterm=NONE,
            payment_terms=NONE,
            reply_kind="clarification",
            declined_lines=[DeclinedLine(rfq_line_position=2, quote=DECLINE)],
        ),
        body=DECLINE,
    )
    state = reconcile([event(first_offer()), event(declined, 1)])
    assert state.issues == () and state.reply_kind == "replacement"
    assert state.quote.lines[0].parsed("unit_price") == Decimal("0.45")
    assert state.declined_line_positions == (2,)
    assert state.provenance()["requires_clarification"] is False


# --- RFQ-6 -------------------------------------------------------------------------------------

LINES = [RequestedLine(id=uuid4(), position=p) for p in (1, 2, 3)]
REPLY_1 = (
    "We can supply the requested items. Our current prices are BRL 19.50 per piece for the "
    "bearings (000101), BRL 89.00 per piece for the shafts (000102), and BRL 95.00 per kg for "
    "the copper wire (000104).\n\nOur standard payment terms are Net 30, and the quotation is "
    "valid for 15 days. The MOQ is 10 pieces for bearings and shafts, and 5 kg for copper wire."
    "\n\nLead time is approximately 10 business days after order confirmation."
)
REPLY_2 = (
    "Our quotation is valid for 15 calendar days from the date of our original offer.\n\n"
    "For the bearings (000101), BRL 19.50 per piece applies to the requested 70 pieces, with a "
    "lead time of 7 business days.\n\nFor the shafts (000102), BRL 89.00 per piece applies to "
    "the requested 15 pieces, with a lead time of 12 business days due to our current "
    "production schedule.\n\nFor the copper wire (000104), BRL 95.00 per kg applies to the "
    "requested 12.5 kg, with a lead time of 8 business days."
)
PRICES = {1: "19.50", 2: "89.00", 3: "95.00"}
UNITS = {1: "piece", 2: "piece", 3: "kg"}


def rfq6(body: str, drafts: list[ProposalLineDraft], **header: object) -> ValidatedQuote:
    return validate_quote(
        output(*drafts, **header),
        requested=LINES,
        sources=QuoteSources(email_subject="Re: [RFQ-6] Request for quotation", email_body=body),
        today=date(2026, 10, 9),
        decimal=".",
    )


def test_rfq6_the_second_reply_is_applied_as_answers() -> None:
    lead_1 = "Lead time is approximately 10 business days after order confirmation"
    first = rfq6(
        REPLY_1,
        [
            delta(
                p,
                unit_price=ev(PRICES[p], f"BRL {PRICES[p]} per {UNITS[p]}"),
                unit=ev(UNITS[p], f"per {UNITS[p]}"),
                moq=ev("10" if p < 3 else "5", "The MOQ is 10 pieces for bearings and shafts")
                if p < 3
                else ev("5", "5 kg for copper wire"),
                lead_time_days=ev("10", lead_1, confidence=0.7) if p != 2 else NONE,
            )
            for p in (1, 2, 3)
        ],
        currency=ev("BRL", "BRL 19.50"),
        valid_until=ev("2026-10-24", "the quotation is valid for 15 days", confidence=0.6),
        incoterm=NONE,
        payment_terms=ev("Net 30", "Our standard payment terms are Net 30"),
    )
    days = {1: "7", 2: "12", 3: "8"}
    quantities = {1: "70", 2: "15", 3: "12.5"}
    second = rfq6(
        REPLY_2,
        [
            delta(
                p,
                unit_price=ev(PRICES[p], f"BRL {PRICES[p]} per {UNITS[p]}"),
                unit=ev(UNITS[p], f"per {UNITS[p]}"),
                min_quantity=ev(quantities[p], f"the requested {quantities[p]}"),
                lead_time_days=ev(days[p], f"a lead time of {days[p]} business days"),
            )
            for p in (1, 2, 3)
        ],
        currency=ev("BRL", "BRL 19.50"),
        valid_until=NONE,
        incoterm=NONE,
        payment_terms=NONE,
        reply_kind="amendment",
    )
    # What Bido actually asked on 2026-10-08 (validity, quantity and lead time per line).
    asked = (
        AskedField(None, "valid_until"),
        *(AskedField(p, f) for p in (1, 2, 3) for f in ("quantity", "lead_time")),
    )
    state = reconcile([event(first), event(second, 1, answers=asked)])

    assert state.issues == () and state.reply_kind == "amendment"
    by_position = {line.draft.rfq_line_position: line for line in state.quote.lines}
    # Business days written the older way are counted like the v6 statement: calendar
    # days from the offer date (9 Oct), in the calendar of the BRL prices (12 Oct a
    # holiday in Brazil).
    assert {p: line.parsed("lead_time_days") for p, line in by_position.items()} == {
        1: 12,
        2: 19,
        3: 13,
    }
    assert {p: line.parsed("min_quantity") for p, line in by_position.items()} == {
        1: Decimal(70),
        2: Decimal(15),
        3: Decimal("12.5"),
    }
    # Net 30 and the MOQ were not repeated: they carry over (not lost, as a replacement would).
    assert state.quote.parsed("payment_terms") == "Net 30"
    assert {p: line.parsed("moq") for p, line in by_position.items()} == {
        1: Decimal(10),
        2: Decimal(10),
        3: Decimal(5),
    }
    # Converted, with the rule on record; nothing to review, the offer is not held.
    shaft = by_position[2].fields["lead_time_days"]
    assert shaft.issues == ()
    assert shaft.conversion is not None and shaft.conversion["unit"] == "business_days"


def test_rfq6_read_by_prompt_v5_the_second_reply_is_applied_as_answers() -> None:
    # The extraction (v5) reads the second reply right; reconciliation used to hold it
    # (an amendment without "other terms unchanged"). As answers to what we asked, it is
    # the offer: the stated lead times, the requested quantities, everything else kept.
    first = rfq6_v5.validate(rfq6_v5.first_reply(), rfq6_v5.REPLY_1, country="BR")
    second = rfq6_v5.validate(
        rfq6_v5.second_reply(),
        rfq6_v5.REPLY_2,
        original_offer_date=rfq6_v5.OFFER_DATE,
        country="BR",
    )
    held = reconcile([event(first), event(second, 1)])
    assert "amendment_without_explicit_preservation" in held.issues

    asked = (AskedField(1, "delivery_date"), AskedField(2, "delivery_date"))
    state = reconcile([event(first), event(second, 1, answers=asked)])
    assert state.issues == () and state.reply_kind == "amendment"
    assert state.answered == asked
    by_position = {line.draft.rfq_line_position: line for line in state.quote.lines}
    # 7, 12 and 8 business days from 9 Oct (12 Oct a holiday in Brazil), in calendar days.
    assert {p: line.parsed("lead_time_days") for p, line in by_position.items()} == {
        1: 12,
        2: 19,
        3: 13,
    }
    assert not any(line.needs_review for line in state.quote.lines)
    assert state.quote.parsed("payment_terms") == "Net 30"
    assert state.quote.parsed("valid_until") == date(2026, 10, 24)
