"""Offer continuity is deterministic; language models supply evidence-bearing deltas."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from bido_back.agents.catalog.contracts import OfferConflict, ProposalLineDraft, SourceRef
from bido_back.quotes.state import OfferEvent, reconcile, restore_quote, store_quote
from bido_back.quotes.validation import LINE_FIELDS, ValidatedQuote
from tests.quotes.test_validation import BODY, NONE, ev, line, output, validate

START = datetime(2026, 10, 6, 12, tzinfo=UTC)


def event(quote: ValidatedQuote, hour: int = 0, *, parent: OfferEvent | None = None) -> OfferEvent:
    message_id = uuid4()
    return OfferEvent(
        message_id=message_id,
        message_header=f"<{message_id}@supplier.test>",
        in_reply_to=parent.message_header if parent is not None else None,
        occurred_at=START + timedelta(hours=hour),
        received_at=START + timedelta(hours=hour, minutes=1),
        quote=quote,
    )


def amendment(*, unchanged: bool = True, tier: str | None = None) -> ValidatedQuote:
    delta = ProposalLineDraft.model_validate(
        {
            "rfq_line_position": 1,
            **{name: NONE for name in LINE_FIELDS},
            "min_quantity": ev(tier) if tier is not None else NONE,
            "lead_time_days": ev("15", "15 days"),
        }
    )
    body = "Delivery is now 15 days. All other terms unchanged."
    if tier:
        body += f" Tier {tier}."
    return validate(
        output(
            delta,
            currency=NONE,
            valid_until=NONE,
            incoterm=NONE,
            payment_terms=NONE,
            reply_kind="amendment",
            unchanged_terms=ev("All other terms unchanged", "All other terms unchanged")
            if unchanged
            else NONE,
        ),
        body=body,
    )


def test_delivery_only_amendment_preserves_price_and_field_origins() -> None:
    first = event(validate(output()))
    second = event(amendment(), 1, parent=first)
    state = reconcile([first, second])
    [offer] = state.quote.lines
    assert not state.quote.needs_review
    assert offer.parsed("lead_time_days") == 15
    assert offer.parsed("unit_price") == Decimal("0.45")
    assert offer.fields["unit_price"].message_id == first.message_id
    assert offer.fields["lead_time_days"].message_id == second.message_id
    assert offer.fields["unit_price"].retained_by[0]["message_id"] == str(second.message_id)
    assert state.quote.parsed("currency") == "EUR"
    assert restore_quote(store_quote(state.quote)) == state.quote


def test_silent_partial_reply_is_held_without_guessing_unchanged_terms() -> None:
    first = event(validate(output()))
    second = event(amendment(unchanged=False), 1, parent=first)
    state = reconcile([first, second])
    assert "amendment_without_explicit_preservation" in state.issues
    assert state.quote.lines[0].needs_review
    assert state.quote.lines[0].parsed("lead_time_days") == 42


def test_partial_update_requires_unambiguous_quantity_tier() -> None:
    first = event(validate(output(line(), line(min_quantity=ev("5000", "1.000 Stück")))))
    state = reconcile([first, event(amendment(), 1, parent=first)])
    assert "ambiguous_amendment_line_or_tier" in state.issues
    assert all(offer.parsed("lead_time_days") == 42 for offer in state.quote.lines)


def test_explicit_tier_update_does_not_change_other_tiers() -> None:
    first_quote = validate(
        output(line(), line(min_quantity=ev("5000", "5000"))),
        body="5000\n" + BODY,
    )
    first = event(first_quote)
    state = reconcile([first, event(amendment(tier="1000"), 1, parent=first)])
    assert [offer.parsed("lead_time_days") for offer in state.quote.lines] == [15, 42]


def test_changed_currency_cannot_reuse_old_price() -> None:
    first = event(validate(output()))
    update = amendment()
    currency = replace(update.header["currency"], extracted=ev("USD", "USD"), parsed="USD")
    update = replace(update, header={**update.header, "currency": currency})
    state = reconcile([first, event(update, 1, parent=first)])
    assert "changed_commercial_basis_requires_replacement" in state.issues
    assert state.quote.parsed("currency") == "EUR"
    assert state.quote.lines[0].needs_review


def test_processing_older_message_late_never_overwrites_newer_offer() -> None:
    first = event(validate(output()))
    second = event(amendment(), 1, parent=first)
    forward = reconcile([first, second])
    backward = reconcile([second, first])
    assert backward == forward
    assert backward.active_message_id == second.message_id
    assert backward.quote.lines[0].parsed("lead_time_days") == 15


def test_reply_ancestry_beats_supplier_date() -> None:
    first = event(validate(output()))
    second = replace(event(amendment(), 1, parent=first), occurred_at=START - timedelta(days=1))
    state = reconcile([second, first])
    assert state.active_message_id == second.message_id
    assert state.quote.lines[0].parsed("lead_time_days") == 15


def test_date_receipt_disagreement_is_held_without_ancestry() -> None:
    first = event(validate(output()))
    second = event(validate(output()), 1)
    first = replace(first, received_at=START + timedelta(hours=2))
    state = reconcile([second, first])
    assert state.active_message_id == second.message_id
    assert "uncertain_message_chronology" in state.issues
    assert state.quote.lines[0].needs_review


def test_withdrawal_removes_offer_but_keeps_audit_events() -> None:
    first = event(validate(output()))
    withdrawal = validate(
        output(
            lines=[],
            reply_kind="withdrawal",
            update_evidence=ev("We withdraw our entire offer"),
            currency=NONE,
            valid_until=NONE,
            incoterm=NONE,
            payment_terms=NONE,
        ),
        body="We withdraw our entire offer",
    )
    second = event(withdrawal, 1, parent=first)
    state = reconcile([second, first])
    assert state.quote.lines == ()
    assert state.message_ids == (first.message_id, second.message_id)
    assert state.reply_kind == "withdrawal"


def test_conflicting_sources_block_recommendation() -> None:
    current = validate(
        output(
            conflicts=[
                OfferConflict(
                    field="unit_price",
                    rfq_line_position=1,
                    first=SourceRef(kind="email_body", quote="0,45 €"),
                    second=SourceRef(kind="pdf", attachment="offer.pdf", page=1, quote="EUR 0.50"),
                )
            ]
        )
    )
    state = reconcile([event(current)])
    assert "conflicting_sources" in state.issues
    assert all(offer.needs_review for offer in state.quote.lines)


def test_technical_question_holds_existing_commercial_state() -> None:
    first = event(validate(output()))
    question = validate(
        output(
            lines=[],
            is_quote=False,
            reply_kind="clarification",
            technical_questions=["Which qualification grade do you require?"],
        )
    )
    state = reconcile([first, event(question, 1, parent=first)])
    assert state.quote.lines[0].parsed("unit_price") == Decimal("0.45")
    assert state.quote.lines[0].needs_review
    assert state.provenance()["requires_clarification"] is True


def test_negated_or_questioned_preservation_cannot_authorize_inheritance() -> None:
    first = event(validate(output()))
    for statement in ("Not all other terms unchanged", "All other terms unchanged?"):
        update = amendment()
        update = replace(
            update, output=update.output.model_copy(update={"unchanged_terms": ev(statement)})
        )
        state = reconcile([first, event(update, 1, parent=first)])
        assert "amendment_without_explicit_preservation" in state.issues


def test_partial_withdrawal_preserves_other_requested_lines() -> None:
    from tests.quotes.test_validation import LINE_2

    first_quote = validate(output(line(), line(rfq_line_position=2, offered_mpn=NONE)))
    first = event(first_quote)
    withdrawal = validate(
        output(
            lines=[],
            reply_kind="withdrawal",
            withdrawn_line_positions=[1],
            update_evidence=ev("We withdraw line 1"),
            currency=NONE,
            valid_until=NONE,
            incoterm=NONE,
            payment_terms=NONE,
        ),
        body="We withdraw line 1",
    )
    state = reconcile([first, event(withdrawal, 1, parent=first)])
    assert len(state.quote.lines) == 1
    assert state.quote.lines[0].rfq_line_id == LINE_2.id
    assert not state.quote.lines[0].needs_review
    assert state.provenance()["fully_withdrawn"] is False
    assert state.provenance()["withdrawn_line_positions"] == [1]


def test_new_price_currency_cannot_be_hidden_by_omitting_header_currency() -> None:
    first = event(validate(output()))
    update = amendment()
    delta = update.lines[0]
    new_price = replace(
        delta.fields["unit_price"], extracted=ev("0.40", "USD 0.40"), parsed=Decimal("0.40")
    )
    update = replace(
        update, lines=(replace(delta, fields={**delta.fields, "unit_price": new_price}),)
    )
    state = reconcile([first, event(update, 1, parent=first)])
    assert "changed_commercial_basis_requires_replacement" in state.issues
    assert state.quote.lines[0].parsed("unit_price") == Decimal("0.45")
