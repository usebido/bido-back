"""The simulated supplier's numbers: decided in code, reproducible."""

from datetime import date
from decimal import Decimal

from bido_back.simulator.offers import (
    LineSpec,
    article_code,
    concession_for,
    decide_offer,
    is_final_request,
    reference_price,
    response_delay_minutes,
    should_answer,
)
from bido_back.simulator.personas import Persona

TODAY = date(2026, 10, 7)
LINES = [
    LineSpec(
        position=1,
        description="Lamierini statore M270-35A",
        mpn="LAM-270",
        quantities=(Decimal(5000), Decimal(10000)),
        need_by=date(2026, 12, 1),
        incoterm="DAP",
    ),
    LineSpec(
        position=2, description="Cuscinetto 6205-2RS", mpn="6205-2RS", quantities=(Decimal(2000),)
    ),
    LineSpec(position=3, description="Connettore 8 poli", mpn="CN-8", quantities=(Decimal(1000),)),
]


def persona(**fields: object) -> Persona:
    return Persona.model_validate(fields)


def test_the_same_inputs_give_the_same_offer() -> None:
    first = decide_offer(persona(), LINES, round_number=1, currency="EUR", today=TODAY)
    second = decide_offer(persona(), LINES, round_number=1, currency="EUR", today=TODAY)

    assert first == second
    assert first.stance == "initial"
    assert first.incoterm == "DAP"
    assert first.valid_until == date(2026, 11, 6)


def test_larger_tiers_are_cheaper_and_the_anchor_scales_prices() -> None:
    cheap = decide_offer(
        persona(price_anchor="0.8"), LINES, round_number=1, currency="EUR", today=TODAY
    )
    normal = decide_offer(persona(), LINES, round_number=1, currency="EUR", today=TODAY)

    tiers = normal.lines[0].tiers
    assert tiers[0].quantity == 5000 and tiers[1].quantity == 10000
    assert tiers[1].unit_price < tiers[0].unit_price
    assert cheap.lines[0].tiers[0].unit_price < tiers[0].unit_price


def test_reference_prices_win_over_history_and_the_fallback() -> None:
    line = LINES[1]
    with_reference = persona(reference_prices={"cuscinett": "3.90"})

    assert reference_price(line, with_reference, {"6205-2rs": Decimal("9")}) == Decimal("3.90")
    assert reference_price(line, persona(), {"6205-2rs": Decimal("9")}) == Decimal("9")
    assert reference_price(line, persona()) == reference_price(line, persona())


def test_concession_schedules() -> None:
    tough = persona(behaviors=["tough_negotiator"])
    reseller = persona(behaviors=["reseller"])

    assert [concession_for(tough, r) for r in (1, 2, 3, 4, 5)] == [
        Decimal(0),
        Decimal(0),
        Decimal("0.03"),
        Decimal("0.05"),
        Decimal("0.08"),
    ]
    assert concession_for(reseller, 4) == 0
    assert concession_for(persona(), 2) == Decimal("0.02")
    assert concession_for(persona(), 9) == Decimal("0.06")


def test_stances_follow_the_rounds() -> None:
    tough = persona(behaviors=["tough_negotiator"])
    r1 = decide_offer(tough, LINES, round_number=1, currency="EUR", today=TODAY)
    r2 = decide_offer(tough, LINES, round_number=2, currency="EUR", today=TODAY, previous=r1)
    r3 = decide_offer(tough, LINES, round_number=3, currency="EUR", today=TODAY, previous=r2)
    r5 = decide_offer(tough, LINES, round_number=5, currency="EUR", today=TODAY, previous=r3)

    assert [r1.stance, r2.stance, r3.stance, r5.stance] == [
        "initial",
        "firm",
        "concession",
        "final",
    ]
    assert r3.lines[0].tiers[0].unit_price < r2.lines[0].tiers[0].unit_price


def test_behaviors_shape_the_offer() -> None:
    late = decide_offer(
        persona(behaviors=["cheap_but_late"], lead_time_days=20),
        LINES,
        round_number=1,
        currency="EUR",
        today=TODAY,
    )
    declines = decide_offer(
        persona(behaviors=["declines_some_lines"]),
        LINES,
        round_number=1,
        currency="EUR",
        today=TODAY,
    )
    alternatives = decide_offer(
        persona(behaviors=["offers_alternatives"]),
        LINES,
        round_number=1,
        currency="EUR",
        today=TODAY,
    )
    broker = decide_offer(
        persona(behaviors=["reseller"]), LINES, round_number=1, currency="EUR", today=TODAY
    )
    noisy = decide_offer(
        persona(behaviors=["noisy"], language="it"),
        LINES,
        round_number=1,
        currency="EUR",
        today=TODAY,
    )

    # Three weeks after the need-by date.
    assert late.lines[0].lead_time_days == (date(2026, 12, 1) - TODAY).days + 21
    assert [line.declined for line in declines.lines] == [False, False, True]
    assert declines.lines[2].tiers == []
    assert (
        alternatives.lines[1].is_alternative and alternatives.lines[1].offered_mpn == "6205-2RS-EQ"
    )
    assert not alternatives.lines[0].is_alternative
    assert not broker.authorized and broker.origin_country == "various"
    assert len(noisy.noise) == 3 and noisy.noise[0].department == "Reparto utensili"


def test_a_slow_responder_waits_to_be_chased() -> None:
    slow = persona(behaviors=["slow_responder"], response_delay_minutes=10)

    assert not should_answer(slow, is_first_email=True)
    assert should_answer(slow, is_first_email=False)
    assert should_answer(persona(), is_first_email=True)
    assert response_delay_minutes(slow) == 30


def test_a_bafo_request_gets_the_last_concession_at_once() -> None:
    tough = persona(behaviors=["tough_negotiator"])
    r1 = decide_offer(tough, LINES, round_number=1, currency="EUR", today=TODAY)
    bafo = decide_offer(
        tough, LINES, round_number=2, currency="EUR", today=TODAY, previous=r1, final_request=True
    )
    first = decide_offer(
        tough, LINES, round_number=1, currency="EUR", today=TODAY, final_request=True
    )

    assert bafo.stance == "final" and bafo.concession == Decimal("0.08")
    assert first.stance == "initial" and first.concession == 0
    assert is_final_request("Vi chiediamo la vostra offerta finale entro venerdì")
    assert is_final_request("Please send your BAFO")
    assert is_final_request("Bitte senden Sie Ihr letztes Angebot")
    assert not is_final_request("Can you do better on price?")


def test_lines_outside_the_portfolio_are_declined_and_moq_is_always_stated() -> None:
    steel = persona(portfolio=["lamierin"])
    offer = decide_offer(steel, LINES, round_number=1, currency="EUR", today=TODAY)

    assert [line.declined for line in offer.lines] == [False, True, True]
    # No MOQ on the persona: the smallest requested tier.
    assert offer.lines[0].moq == Decimal(5000)
    assert decide_offer(
        persona(moq="250"), LINES, round_number=1, currency="EUR", today=TODAY
    ).lines[1].moq == Decimal(250)


def test_every_quoted_line_carries_a_part_number() -> None:
    no_mpn = LineSpec(
        position=4, description="Lamierini acciaio M270-35A", quantities=(Decimal(1),)
    )
    plain = LineSpec(position=5, description="Carcassa pressofusa", quantities=(Decimal(1),))

    assert article_code(LINES[0], "AL") == "LAM-270"
    assert article_code(no_mpn, "AL") == "M270-35A"
    assert article_code(plain, "FV") == "FV-005"
