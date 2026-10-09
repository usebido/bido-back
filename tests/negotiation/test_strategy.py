"""The negotiation strategy: pure decisions, no database, no model."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from bido_back.agents.catalog import negotiation_email_problems
from bido_back.agents.catalog.contracts import (
    NegotiationEmailInput,
    NegotiationEmailOutput,
    NegotiationLineAsk,
)
from bido_back.negotiation.strategy import (
    LineLimits,
    LineOffer,
    best_competing_prices,
    decide_line,
    decide_supplier,
    ready_to_close,
)

NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)
LINE = uuid4()
OTHER_LINE = uuid4()


def limits(
    target: str | None = "0.040",
    max_price: str | None = "0.060",
    max_lead: int | None = 42,
    history: str | None = None,
    line: UUID = LINE,
) -> LineLimits:
    return LineLimits(
        rfq_line_id=line,
        target=Decimal(target) if target else None,
        max_price=Decimal(max_price) if max_price else None,
        max_lead_days=max_lead,
        history_price=Decimal(history) if history else None,
    )


def offer(price: str | None, lead: int | None = 30, *, blockers: tuple[str, ...] = ()) -> LineOffer:
    return LineOffer(
        rfq_line_id=LINE,
        unit_price=Decimal(price) if price is not None else None,
        lead_time_days=lead,
        blockers=blockers,
    )


def test_within_target_and_lead_time_is_accepted() -> None:
    d = decide_line(limits(), offer("0.039"), None)
    assert (d.kind, d.ask_price, d.ask_lead_days) == ("accept", None, None)


def test_above_target_counters_with_the_target() -> None:
    d = decide_line(limits(), offer("0.050"), Decimal("0.045"))
    assert (d.kind, d.ask_price) == ("counter", Decimal("0.04"))
    assert d.better_offer_exists is True


def test_above_max_is_flagged_but_still_asked_the_target() -> None:
    d = decide_line(limits(), offer("0.070"), None)
    assert (d.kind, d.ask_price) == ("above_max", Decimal("0.04"))


def test_only_the_lead_time_is_asked_when_the_price_is_fine() -> None:
    d = decide_line(limits(max_lead=28), offer("0.035", lead=56), None)
    assert (d.kind, d.ask_price, d.ask_lead_days) == ("counter", None, 28)
    # An unknown lead time is not acceptable when a maximum is set.
    assert decide_line(limits(max_lead=28), offer("0.035", lead=None), None).kind == "counter"


def test_without_a_target_the_history_or_competition_or_five_percent_is_asked() -> None:
    no_target = limits(target=None, max_price=None, max_lead=None)
    assert decide_line(no_target, offer("0.050"), Decimal("0.044")).ask_price == Decimal("0.0475")
    assert decide_line(no_target, offer("0.050"), None).ask_price == Decimal("0.0475")
    history = limits(target=None, max_price=None, max_lead=None, history="0.042")
    assert decide_line(history, offer("0.050"), None).ask_price == Decimal("0.0475")
    assert decide_line(history, offer("0.041"), None).kind == "accept"


def test_blocked_and_missing_offers_are_not_negotiated_on_price() -> None:
    assert (
        decide_line(limits(), offer("0.050", blockers=("alternative_needs_approval",)), None).kind
        == "blocked"
    )
    assert decide_line(limits(), offer(None), None).kind == "no_offer"
    assert decide_line(limits(), None, None).kind == "no_offer"


def supplier(
    price: str,
    *,
    rounds_used: int = 0,
    bafo: bool = False,
    max_rounds: int = 3,
    deadline: datetime | None = None,
    best: str | None = None,
):  # noqa: ANN202 - test helper
    return decide_supplier(
        lines=[limits()],
        offers={LINE: offer(price)},
        best_competing={LINE: Decimal(best)} if best else {},
        rounds_used=rounds_used,
        bafo_already_requested=bafo,
        max_rounds=max_rounds,
        deadline=deadline,
        now=NOW,
    )


def test_supplier_actions_follow_rounds_and_deadline() -> None:
    assert (supplier("0.039").action, supplier("0.039").status) == ("accept", "accepted")
    first = supplier("0.050", best="0.045")
    assert (first.action, first.status, first.mention_competition) == (
        "counter",
        "countered",
        True,
    )
    assert supplier("0.050").mention_competition is False
    # The last allowed round is a best-and-final request.
    assert supplier("0.050", rounds_used=2).action == "bafo"
    # So is any round less than 24h before the deadline.
    near = supplier("0.050", deadline=NOW + timedelta(hours=10))
    assert (near.action, near.status) == ("bafo", "bafo_requested")
    assert supplier("0.050", deadline=NOW + timedelta(days=3)).action == "counter"


def test_after_a_best_and_final_answer_nothing_more_is_sent() -> None:
    still_high = supplier("0.050", rounds_used=1, bafo=True)
    assert (still_high.action, still_high.status, still_high.sends_email) == (
        "final",
        "closed",
        False,
    )
    over_max = supplier("0.070", rounds_used=3)
    assert (over_max.action, over_max.status) == ("final", "rejected")
    # Within target after the BAFO: accepted.
    assert supplier("0.040", rounds_used=3, bafo=True).action == "accept"


def test_nothing_priced_means_nothing_to_do() -> None:
    decision = decide_supplier(
        lines=[limits()],
        offers={},
        best_competing={},
        rounds_used=0,
        bafo_already_requested=False,
        max_rounds=3,
        deadline=None,
        now=NOW,
    )
    assert (decision.action, decision.status, decision.sends_email) == ("none", "open", False)


def test_best_competing_prices_ignore_the_supplier_itself_and_blocked_offers() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    best = best_competing_prices(
        {
            a: {LINE: offer("0.050")},
            b: {LINE: offer("0.045")},
            c: {LINE: offer("0.030", blockers=("unauthorized_source",))},
        }
    )
    assert best[a][LINE] == Decimal("0.045")
    assert best[b][LINE] == Decimal("0.050")


def test_ready_to_close_needs_every_answer_and_every_line_within_target() -> None:
    both = [limits(line=LINE), limits(line=OTHER_LINE)]

    def decision(prices: dict[UUID, str]):  # noqa: ANN202 - test helper
        return decide_supplier(
            lines=both,
            offers={
                line: LineOffer(rfq_line_id=line, unit_price=Decimal(p), lead_time_days=30)
                for line, p in prices.items()
            },
            best_competing={},
            rounds_used=0,
            bafo_already_requested=False,
            max_rounds=3,
            deadline=None,
            now=NOW,
        )

    covers_one = decision({LINE: "0.039", OTHER_LINE: "0.050"})
    covers_other = decision({LINE: "0.050", OTHER_LINE: "0.035"})
    ids = [LINE, OTHER_LINE]
    assert ready_to_close(line_ids=ids, waiting_suppliers=0, decisions=[covers_one, covers_other])
    assert not ready_to_close(
        line_ids=ids, waiting_suppliers=1, decisions=[covers_one, covers_other]
    )
    assert not ready_to_close(line_ids=ids, waiting_suppliers=0, decisions=[covers_one])


def _task(**overrides: object) -> NegotiationEmailInput:
    fields: dict[str, object] = {
        "action": "counter",
        "rfq_ref": "RFQ-7",
        "rfq_title": "Passives",
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
    # A target written differently, or an invented amount, is refused.
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


def test_stalled_sent_rounds_stop_instead_of_repeating_the_target() -> None:
    from bido_back.negotiation.strategy import PreviousRound

    decision = decide_supplier(
        lines=[limits()],
        offers={LINE: offer("0.050")},
        best_competing={},
        rounds_used=2,
        bafo_already_requested=False,
        max_rounds=8,
        deadline=None,
        now=NOW,
        previous_rounds=[
            PreviousRound({LINE: offer("0.050")}),
            PreviousRound({LINE: offer("0.050")}),
        ],
    )
    assert decision.action == "final"
    assert not decision.sends_email
    assert decision.stalled_rounds == 2
    assert decision.reasons == ("stalled_negotiation_requires_buyer",)


def test_evidenced_concession_or_delivery_improvement_keeps_negotiation_open() -> None:
    from bido_back.negotiation.strategy import PreviousRound

    for current in (offer("0.049"), offer("0.050", lead=20)):
        decision = decide_supplier(
            lines=[limits()],
            offers={LINE: current},
            best_competing={},
            rounds_used=2,
            bafo_already_requested=False,
            max_rounds=8,
            deadline=None,
            now=NOW,
            previous_rounds=[
                PreviousRound({LINE: offer("0.050")}),
                PreviousRound({LINE: offer("0.050")}),
            ],
        )
        assert decision.action == "counter"
        assert decision.stalled_rounds == 0


def test_supplier_question_holds_price_negotiation_until_buyer_clarifies() -> None:
    decision = decide_supplier(
        lines=[limits()],
        offers={LINE: offer("0.035")},
        best_competing={},
        rounds_used=0,
        bafo_already_requested=False,
        max_rounds=3,
        deadline=None,
        now=NOW,
        holds=["supplier_clarification_requires_buyer"],
    )
    assert decision.action == "none"
    assert not decision.sends_email
    assert not ready_to_close(line_ids=[LINE], waiting_suppliers=0, decisions=[decision])


def test_partial_offer_is_not_marked_as_supplier_acceptance() -> None:
    decision = decide_supplier(
        lines=[limits(), limits(line=OTHER_LINE)],
        offers={LINE: offer("0.035")},
        best_competing={},
        rounds_used=0,
        bafo_already_requested=False,
        max_rounds=3,
        deadline=None,
        now=NOW,
    )
    assert decision.status == "open"
    assert decision.reasons == ("partial_coverage_requires_review",)


def test_historical_reference_cannot_override_the_buyers_maximum() -> None:
    d = decide_line(limits(target=None, history="0.100", max_price="0.060"), offer("0.080"), None)
    assert d.kind == "above_max"
    assert d.ask_price is None


def test_competitor_price_never_becomes_supplier_facing_ask() -> None:
    d = decide_line(limits(target=None), offer("0.050"), Decimal("0.044"))
    assert d.better_offer_exists
    assert d.ask_price == Decimal("0.0475")
    assert d.ask_price != Decimal("0.044")


# --- Round asks and how the draft check reads amounts (email audit F1, F2) ----------------


def test_round_asks_reach_the_writer_as_plain_digits_never_in_exponent_form() -> None:
    """A target of 90.00 was asked as 9E+1: the writer copied it and the check refused."""
    for target, written in (("90.00", "90"), ("100", "100"), ("1000.00", "1000"), ("50", "50")):
        d = decide_line(limits(target=target, max_price="2000", max_lead=None), offer("1500"), None)
        assert d.ask_price == Decimal(target)
        assert str(d.ask_price) == written
        ask = NegotiationLineAsk(
            position=3,
            line="Fio de cobre esmaltado",
            quantity=Decimal("12.5"),
            unit="kg",
            currency="BRL",
            offered_unit_price=Decimal("1500"),
            target_unit_price=d.ask_price,
        )
        assert f'"target_unit_price":"{written}"' in ask.model_dump_json()
    no_target = limits(target=None, max_price=None, max_lead=None)
    # 5% off 100.00 is 95: still plain.
    assert str(decide_line(no_target, offer("100.00"), None).ask_price) == "95"


RFQ6_LINES = [
    NegotiationLineAsk(
        position=1,
        line="Rolamento motor MTR-100 (000101)",
        quantity=Decimal(70),
        unit="pcs",
        currency="BRL",
        offered_unit_price=Decimal("19.5"),
        target_unit_price=Decimal(18),
    ),
    NegotiationLineAsk(
        position=2,
        line="Eixo motor MTR-100 (000102)",
        quantity=Decimal(15),
        unit="pcs",
        currency="BRL",
        offered_unit_price=Decimal(89),
        target_unit_price=Decimal(85),
    ),
    NegotiationLineAsk(
        position=3,
        line="Fio de cobre esmaltado (000104)",
        quantity=Decimal("12.5"),
        unit="kg",
        currency="BRL",
        offered_unit_price=Decimal(95),
        target_unit_price=Decimal("89.9"),
    ),
]


def test_an_amount_never_continues_on_the_next_line_of_a_numbered_list() -> None:
    """The live BAFO "... BRL 18\\n2. Eixo ..." was read as the amount 18 2 and refused."""
    task = _task(action="bafo", supplier_language="pt", lines=RFQ6_LINES)
    body = (
        "Prezados, sobre a RFQ-7 pedimos sua melhor e última oferta:\n"
        "1. Rolamento motor MTR-100 — 70 pcs: BRL 18\n"
        "2. Eixo motor MTR-100 — 15 pcs: BRL 85\n"
        "3. Fio de cobre esmaltado — 12.5 kg: BRL 89.9\n"
        "Atenciosamente,"
    )
    assert negotiation_email_problems(task, NegotiationEmailOutput(body=body)) == []


def test_amounts_are_read_in_every_usual_spelling() -> None:
    task = _task(supplier_language="pt", lines=RFQ6_LINES)
    for spelling in ("R$ 18,00", "R$18,00", "BRL 18", "18,00 BRL", "18,00 R$", "18,00\u00a0R$"):
        body = (
            f"RFQ-7: rolamento {spelling} por peça (hoje R$ 19,50); eixo R$ 85,00; "
            "fio R$ 89,90 por kg."
        )
        assert negotiation_email_problems(task, NegotiationEmailOutput(body=body)) == [], spelling
        invented = body + f" Outro fornecedor cobra {spelling.replace('18', '17')}."
        assert negotiation_email_problems(task, NegotiationEmailOutput(body=invented)) == [
            "unknown_amount"
        ], spelling


def test_codes_are_not_amounts_and_do_not_stand_in_for_a_missing_target() -> None:
    ask = NegotiationLineAsk(
        position=2,
        line="Eixo motor MTR-100",
        quantity=Decimal(15),
        unit="pcs",
        currency="BRL",
        offered_unit_price=Decimal(105),
        target_unit_price=Decimal(100),
    )
    task = _task(supplier_language="pt", lines=[ask])
    # "MTR-100 BRL 100": the 100 of the code is no amount next to the currency.
    ok = "RFQ-7: Eixo motor MTR-100 BRL 100 por peça."
    assert negotiation_email_problems(task, NegotiationEmailOutput(body=ok)) == []
    # The target left out: the code's 100 does not count as written.
    missing = "RFQ-7: Eixo motor MTR-100, pedimos uma redução do preço."
    assert negotiation_email_problems(task, NegotiationEmailOutput(body=missing)) == [
        "target_missing:2"
    ]


def test_a_required_lead_time_must_be_asked() -> None:
    ask = NegotiationLineAsk(
        position=2,
        line="Eixo motor MTR-100",
        quantity=Decimal(15),
        unit="pcs",
        currency="BRL",
        offered_unit_price=Decimal(89),
        target_unit_price=None,
        offered_lead_time_days=12,
        required_lead_time_days=10,
    )
    task = _task(supplier_language="pt", lines=[ask])
    ok = "RFQ-7: para o eixo precisamos de um prazo de no máximo 10 dias (hoje 12)."
    assert negotiation_email_problems(task, NegotiationEmailOutput(body=ok)) == []
    vague = "RFQ-7: para o eixo precisamos de um prazo menor."
    assert negotiation_email_problems(task, NegotiationEmailOutput(body=vague)) == [
        "lead_time_missing:2"
    ]
