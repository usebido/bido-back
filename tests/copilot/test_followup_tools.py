"""What the Copilot's follow-up tools tell the buyer about an offer held for them."""

from bido_back.copilot.followup_tools import _decision  # pyright: ignore[reportPrivateUsage]
from bido_back.followups.ledger import Decision, HeldChange, offer_held_changes


def test_a_held_offer_names_the_real_reason_and_the_changed_terms() -> None:
    text = _decision(
        Decision(
            kind="offer_held",
            reasons=("changed_commercial_basis_requires_replacement",),
            changes=(HeldChange(code="pos1.unit", previous="piece", stated="kg", applied=False),),
        )
    )
    assert "commercial basis" in text
    assert 'line 1 unit from "piece" to "kg" (not applied)' in text
    # Releasing the hold takes the offer as shown: the values from before that reply.
    assert "the current offer keeps the values from before that reply" in text


def test_an_unasked_change_already_taken_is_named_for_confirmation() -> None:
    text = _decision(
        Decision(
            kind="offer_held",
            reasons=("changed_commercial_basis_requires_replacement",),
            changes=(
                HeldChange(
                    code="payment_terms", previous="30 days net", stated="15 days net", applied=True
                ),
            ),
        )
    )
    assert 'payment terms from "30 days net" to "15 days net" (applied, to confirm)' in text
    assert "keeps the values from before" not in text


def test_a_hold_without_a_change_reads_as_before() -> None:
    text = _decision(
        Decision(kind="offer_held", reasons=("amendment_without_explicit_preservation",))
    )
    assert text == (
        "the supplier's latest reply could not be applied as it was (a partial update that "
        "does not say the other terms are unchanged): check it, correct it, or use the current "
        "offer"
    )


def test_the_held_changes_are_read_from_the_stored_offer_state() -> None:
    fields: dict[str, object] = {
        "_offer_state": {
            "issues": ["changed_commercial_basis_requires_replacement"],
            "held_changes": [
                {"field": "unit", "position": 1, "previous": "piece", "stated": "kg"},
                {"field": "currency", "position": None, "previous": "BRL", "stated": "USD"},
                {"position": 1},
            ],
        }
    }
    assert offer_held_changes(fields) == (
        HeldChange(code="pos1.unit", previous="piece", stated="kg", applied=False),
        HeldChange(code="currency", previous="BRL", stated="USD", applied=False),
    )
    assert offer_held_changes({}) == ()
