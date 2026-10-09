"""The negotiation strategy: per-line rules, pure decisions, no database, no model."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from bido_back.agents.catalog import negotiation_email_problems
from bido_back.agents.catalog.contracts import (
    NegotiationEmailInput,
    NegotiationEmailOutput,
    NegotiationLineAsk,
)
from bido_back.negotiation.strategy import (
    ask_places,
    decimals,
    delivery_position,
    floor_to,
    minor_unit,
    plain,
    price_position,
    quantize_ask,
    required_by,
)

TODAY = date(2026, 10, 9)


# --- Numbers ------------------------------------------------------------------------------


def test_asks_are_quantized_down_to_the_currency_minor_unit() -> None:
    assert quantize_ask(Decimal("45.6677"), 2) == Decimal("45.66")
    assert str(quantize_ask(Decimal("45.6677"), 2)) == "45.66"
    # A round target is written with its decimals, never in exponent notation.
    assert str(quantize_ask(Decimal("90"), 2)) == "90.00"
    assert str(floor_to(Decimal("90.000000"), 2)) == "90.00"
    assert str(plain(Decimal("90.000000"))) == "90"
    assert str(plain(Decimal("1E+2"))) == "100"
    assert str(plain(Decimal("12.50"))) == "12.5"
    # A price below one minor unit keeps its stored precision instead of becoming zero.
    assert quantize_ask(Decimal("0.004"), 2) == Decimal("0.004000")


@pytest.mark.parametrize(
    ("currency", "quoted", "places"),
    [
        ("BRL", Decimal("46.90"), 2),
        ("EUR", Decimal("0.055"), 3),  # the supplier's own finer precision
        ("EUR", Decimal("0.04512345"), 4),  # never more than 4
        ("JPY", Decimal("1200"), 0),
        ("KWD", None, 3),
        ("brl", None, 2),
    ],
)
def test_ask_places_follow_the_currency_or_the_supplier(
    currency: str, quoted: Decimal | None, places: int
) -> None:
    assert ask_places(currency, quoted) == places


def test_decimals_and_minor_units() -> None:
    assert decimals(Decimal("46.90")) == 1
    assert decimals(Decimal("90")) == 0
    assert decimals(Decimal("1E+2")) == 0
    assert minor_unit("JPY") == 0 and minor_unit("USD") == 2


# --- Price --------------------------------------------------------------------------------


def test_within_target_nothing_is_asked() -> None:
    ask = price_position(
        offered=Decimal("44.90"), target=Decimal("45.67"), max_price=Decimal("51.91"), places=2
    )
    assert (ask.status, ask.ask, ask.asks) == ("in_target", None, False)


def test_the_first_ask_is_the_target_rounded_down() -> None:
    ask = price_position(
        offered=Decimal("46.90"), target=Decimal("45.6677"), max_price=Decimal("51.91"), places=2
    )
    assert (ask.status, ask.ask, ask.conceded) == ("in_range", Decimal("45.66"), False)


def test_above_the_maximum_the_target_is_asked_and_never_conceded() -> None:
    first = price_position(
        offered=Decimal("279.00"), target=Decimal("234.52"), max_price=Decimal("266.28"), places=2
    )
    assert (first.status, first.ask) == ("above_max", Decimal("234.52"))
    moved = price_position(
        offered=Decimal("270.00"),
        target=Decimal("234.52"),
        max_price=Decimal("266.28"),
        places=2,
        previous_ask=Decimal("234.52"),
        previous_offer=Decimal("279.00"),
        final_round=True,
    )
    assert (moved.status, moved.ask, moved.conceded) == ("above_max", Decimal("234.52"), False)


def test_a_supplier_that_moved_gets_a_quarter_step_then_half_in_the_final_round() -> None:
    counter = price_position(
        offered=Decimal("46.50"),
        target=Decimal("45.67"),
        max_price=Decimal("51.91"),
        places=2,
        previous_ask=Decimal("45.67"),
        previous_offer=Decimal("48.90"),
    )
    # 45.67 + (46.50 - 45.67) / 4 = 45.8775, rounded down.
    assert (counter.ask, counter.conceded) == (Decimal("45.87"), True)
    final = price_position(
        offered=Decimal("46.00"),
        target=Decimal("45.67"),
        max_price=Decimal("51.91"),
        places=2,
        previous_ask=Decimal("45.87"),
        previous_offer=Decimal("46.50"),
        final_round=True,
    )
    # 45.87 + (46.00 - 45.87) / 2 = 45.935, rounded down.
    assert (final.ask, final.conceded) == (Decimal("45.93"), True)


def test_no_concession_without_movement_and_never_at_or_above_the_offer() -> None:
    same = price_position(
        offered=Decimal("46.50"),
        target=Decimal("45.67"),
        max_price=Decimal("51.91"),
        places=2,
        previous_ask=Decimal("45.67"),
        previous_offer=Decimal("46.50"),
    )
    assert (same.ask, same.conceded) == (Decimal("45.67"), False)
    tiny = price_position(
        offered=Decimal("45.68"),
        target=Decimal("45.67"),
        max_price=Decimal("51.91"),
        places=2,
        previous_ask=Decimal("45.67"),
        previous_offer=Decimal("46.00"),
    )
    # The step would round to the old ask: the ask holds instead of reaching the offer.
    assert tiny.ask == Decimal("45.67")


def test_meeting_our_last_ask_ends_the_price_ask() -> None:
    met = price_position(
        offered=Decimal("45.85"),
        target=Decimal("45.67"),
        max_price=Decimal("51.91"),
        places=2,
        previous_ask=Decimal("45.87"),
        previous_offer=Decimal("46.50"),
    )
    assert (met.status, met.asks) == ("ask_met", False)


def test_without_a_target_a_better_price_is_asked_without_a_number() -> None:
    ask = price_position(offered=Decimal("0.050"), target=None, max_price=None, places=3)
    assert (ask.status, ask.ask, ask.best_price) == ("no_target", None, True)
    # The internal history reference accepts, but is never disclosed.
    history = price_position(
        offered=Decimal("0.041"), target=None, max_price=None, places=3, history=Decimal("0.042")
    )
    assert history.status == "in_target"
    over = price_position(
        offered=Decimal("0.050"), target=None, max_price=None, places=3, history=Decimal("0.042")
    )
    assert (over.ask, over.best_price) == (None, True)
    # A reference above the maximum is no reference at all.
    capped = price_position(
        offered=Decimal("0.080"),
        target=None,
        max_price=Decimal("0.060"),
        places=3,
        history=Decimal("0.100"),
    )
    assert (capped.status, capped.ask, capped.best_price) == ("above_max", None, True)


def test_an_ask_is_never_above_the_maximum() -> None:
    for offered in ("51.90", "51.91", "60.00"):
        ask = price_position(
            offered=Decimal(offered),
            target=Decimal("45.67"),
            max_price=Decimal("51.91"),
            places=2,
            previous_ask=Decimal("51.00"),
            previous_offer=Decimal("70.00"),
            final_round=True,
        )
        assert ask.ask is not None and ask.ask < Decimal("51.91")


# --- Delivery -----------------------------------------------------------------------------


def test_delivery_is_a_date_compared_with_the_need_by_date() -> None:
    need_by = TODAY + timedelta(days=13)
    on_time = delivery_position(
        required=need_by, expected=need_by, order_date=TODAY, asked_before=False
    )
    assert (on_time.status, on_time.acceptable) == ("on_time", True)
    late = delivery_position(
        required=need_by, expected=need_by + timedelta(days=6), order_date=TODAY, asked_before=False
    )
    assert (late.status, late.deliver_by, late.expected) == (
        "late",
        need_by,
        need_by + timedelta(days=6),
    )
    again = delivery_position(
        required=need_by, expected=need_by + timedelta(days=6), order_date=TODAY, asked_before=True
    )
    assert (again.status, again.deliver_by) == ("still_late", None)


def test_an_unreachable_need_by_date_is_the_buyers_not_a_hopeless_ask() -> None:
    gone = delivery_position(
        required=TODAY, expected=TODAY + timedelta(days=5), order_date=TODAY, asked_before=False
    )
    assert (gone.status, gone.deliver_by, gone.expected) == (
        "need_by_unreachable",
        None,
        TODAY + timedelta(days=5),
    )


def test_unknown_lead_time_is_not_acceptable_and_no_need_by_is() -> None:
    unknown = delivery_position(
        required=TODAY + timedelta(days=9), expected=None, order_date=TODAY, asked_before=False
    )
    assert (unknown.status, unknown.acceptable) == ("unknown", False)
    free = delivery_position(
        required=None, expected=TODAY + timedelta(days=30), order_date=TODAY, asked_before=False
    )
    assert (free.status, free.acceptable) == ("no_need_by", True)


def test_a_buyer_lead_time_limit_counts_from_the_order_date() -> None:
    need_by = TODAY + timedelta(days=60)
    assert required_by(need_by, 42, TODAY) == TODAY + timedelta(days=42)
    # Five days later the limit date moves; the need-by date does not.
    later = TODAY + timedelta(days=5)
    assert required_by(need_by, 42, later) == later + timedelta(days=42)
    assert required_by(TODAY + timedelta(days=10), 42, TODAY) == TODAY + timedelta(days=10)
    assert required_by(None, None, TODAY) is None


# --- The draft check (agents/catalog) -------------------------------------------------------


def _task(**overrides: object) -> NegotiationEmailInput:
    fields: dict[str, object] = {
        "action": "counter",
        "rfq_ref": "RFQ-7",
        "rfq_title": "Passives",
        "buyer_company": "Voltwerk",
        "buyer_name": "Anna",
        "supplier_name": "Rhein",
        "supplier_language": "de",
        "round": 1,
        "reply_by": None,
        "mention_competition": False,
        "lines": [
            NegotiationLineAsk(
                position=1,
                line="MLCC (GRM188)",
                quantity=Decimal(4000),
                unit="pcs",
                currency="EUR",
                offered_unit_price=Decimal("0.05"),
                target_unit_price=Decimal("0.04"),
            )
        ],
    }
    fields.update(overrides)
    return NegotiationEmailInput.model_validate(fields)


def test_negotiation_email_checks_the_numbers() -> None:
    task = _task()
    ok = "Sehr geehrte Rhein, zu RFQ-7: Ihr Preis 0,05 EUR, unser Zielpreis 0,04 EUR je Stück."
    assert negotiation_email_problems(task, NegotiationEmailOutput(body=ok)) == []
    changed = "Zu RFQ-7: unser Zielpreis ist 0,038 EUR."
    assert "target_missing:1" in negotiation_email_problems(
        task, NegotiationEmailOutput(body=changed)
    )
    invented = "Zu RFQ-7: Zielpreis 0,04 EUR, ein anderer Anbieter bietet 0,035 EUR."
    assert negotiation_email_problems(task, NegotiationEmailOutput(body=invented)) == [
        "unknown_amount"
    ]
    assert negotiation_email_problems(task, NegotiationEmailOutput(body="Zielpreis 0,04 EUR")) == [
        "rfq_ref_missing_in_body"
    ]
    assert negotiation_email_problems(task, NegotiationEmailOutput(body=" ")) == ["empty"]


def test_a_target_with_more_than_three_decimals_is_read_whole() -> None:
    """`45,7164` is one number, not the group `45,716` followed by a `4`."""
    ask = NegotiationLineAsk(
        position=1,
        line="Rolamento",
        quantity=Decimal(50),
        unit="pcs",
        currency="BRL",
        offered_unit_price=Decimal("46.90"),
        target_unit_price=Decimal("45.7164"),
    )
    task = _task(supplier_language="pt", lines=[ask])
    body = "Sobre a RFQ-7: Item 1 Rolamento: preço alvo R$ 45,7164 por pcs."
    assert negotiation_email_problems(task, NegotiationEmailOutput(body=body)) == []
    grouped = "Sobre a RFQ-7: preço alvo R$ 1.234,5 e R$ 45,7164."
    assert negotiation_email_problems(task, NegotiationEmailOutput(body=grouped)) == [
        "unknown_amount"
    ]
    reais = "Sobre a RFQ-7: preço alvo R$ 45,7164; outro: 44,00 BRL."
    assert negotiation_email_problems(task, NegotiationEmailOutput(body=reais)) == [
        "unknown_amount"
    ]


def test_an_old_ask_stored_in_exponent_notation_is_read_plainly() -> None:
    ask = price_position(
        offered=Decimal("60"),
        target=Decimal("45"),
        max_price=Decimal("70"),
        places=2,
        previous_ask=Decimal("5E+1"),
        previous_offer=Decimal("60"),
    )
    assert ask.ask == Decimal(50) and "E" not in str(ask.ask)


def test_a_last_ask_above_a_lowered_maximum_is_no_anchor() -> None:
    # Our last ask was 95; the buyer then lowered the maximum to 90.
    lowered = price_position(
        offered=Decimal("94"),
        target=Decimal("80"),
        max_price=Decimal("90"),
        places=2,
        previous_ask=Decimal("95"),
        previous_offer=Decimal("100"),
    )
    assert (lowered.status, lowered.ask) == ("above_max", Decimal("80.00"))
    # An offer meeting that old ask is above the maximum now: not "ask met".
    assert lowered.status != "ask_met"
