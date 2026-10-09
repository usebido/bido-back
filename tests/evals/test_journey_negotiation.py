"""The negotiation rules of the buyer journey (`evals.journey_negotiation`) on small records:
a clean negotiation passes every rule, and each broken business rule is caught."""

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from evals import journey_negotiation as rules
from evals.journey_negotiation import (
    Email,
    LineAsk,
    LineView,
    Negotiation,
    Quote,
    Request,
    SupplierView,
    Sweep,
)

T0 = datetime(2026, 10, 9, 3, 0, tzinfo=UTC)
NEED_BY = date(2026, 10, 19)
REPLY_BY = date(2026, 10, 14)
CUTOFF = datetime.combine(REPLY_BY, time(23, 59), tzinfo=UTC)
LIMITS = rules.Limits(
    targets={"p1": Decimal("10.00")},
    maxima={"p1": Decimal("12.00")},
    need_by={"p1": NEED_BY},
    max_rounds=2,
    deadline=datetime(2026, 10, 16, 23, 59, tzinfo=UTC),
)


def view(
    phase: str, reason: str, label: str, action: str, status: str, version: int, **kw: object
) -> SupplierView:
    ask = kw.get("ask")
    line = LineView(
        "p1",
        "priced",
        price="in_range",
        delivery=str(kw.get("delivery", "on_time")),
        ask=ask if isinstance(ask, Decimal) else None,
        deliver_by=NEED_BY if kw.get("delivery") == "late" else None,
    )
    until = kw.get("until")
    return SupplierView(
        phase, reason, label, action, status,
        until=until if isinstance(until, datetime) else None,
        offer_version=version,
        lines=(line,),
    )  # fmt: skip


def request(seq: int, kind: str, version: int, offered: str, ask: str, sweep: int) -> Request:
    return Request(
        "a", seq, seq, kind, "sent", version, (T0 + timedelta(days=sweep)).date(),
        reply_by=REPLY_BY,
        cutoff_at=CUTOFF if kind == "bafo" else None,
        asks=(LineAsk("p1", Decimal(offered), 5, Decimal(ask)),),
        message_id=f"m{seq}",
        sweep=sweep,
    )  # fmt: skip


def clean() -> Negotiation:
    """A revised-offer request on the first offer, then the BAFO once the supplier moved."""
    counter = request(1, "revised_offer", 1, "11.00", "10.00", 0)
    bafo = request(2, "bafo", 2, "10.80", "10.40", 1)
    waiting = "Waiting: Bido is preparing the next email"
    return Negotiation(
        limits=LIMITS,
        suppliers=("a",),
        requests={"a": (counter, bafo)},
        offers={
            "a": {1: {"p1": Quote(Decimal("11.00"), 5)}, 2: {"p1": Quote(Decimal("10.80"), 5)}}
        },
        sweeps=(
            Sweep(
                0,
                T0,
                before={
                    "a": view(
                        "waiting",
                        "revised_offer_due",
                        waiting,
                        "revised_offer",
                        "open",
                        1,
                        ask=Decimal("10.00"),
                    )
                },
                after={
                    "a": view(
                        "waiting_for_supplier",
                        "revised_offer_sent",
                        "Waiting for the supplier's revised offer",
                        "wait",
                        "countered",
                        1,
                        ask=Decimal("10.00"),
                        until=datetime.combine(REPLY_BY, time(23, 59), tzinfo=UTC),
                    )
                },
            ),
            Sweep(
                1,
                T0 + timedelta(days=1),
                before={
                    "a": view(
                        "waiting", "bafo_due", waiting, "bafo", "countered", 2, ask=Decimal("10.40")
                    )
                },
                after={
                    "a": view(
                        "waiting_for_supplier",
                        "bafo_sent",
                        "Waiting for the supplier's best and final offer",
                        "wait",
                        "bafo_requested",
                        2,
                        ask=Decimal("10.40"),
                        until=CUTOFF,
                    )
                },
                cutoff=CUTOFF,
            ),
        ),  # fmt: skip
        emails={
            "a": (
                Email(
                    "a", "m1", "Sua proposta: R$ 11,00 Preço-alvo: R$ 10,00. Até 14/10/2026.", T0
                ),
                Email(
                    "a",
                    "m2",
                    "Sua proposta: R$ 10,80 Preço-alvo: R$ 10,40. Até 14/10/2026 às 23:59.",
                    T0 + timedelta(days=1),
                ),
            )
        },
    )


def failing(n: Negotiation) -> dict[str, list[str]]:
    return {name: problems for name, problems in rules.grade(n) if problems}


def test_a_clean_negotiation_passes_every_rule() -> None:
    assert failing(clean()) == {}


def with_requests(n: Negotiation, *requests: Request) -> Negotiation:
    return replace(n, requests={"a": requests})


def test_one_request_per_round_and_one_bafo() -> None:
    n = clean()
    counter, bafo = n.requests["a"]
    again = replace(bafo, sequence=3, round=3, offer_version=3, message_id="m3")
    offers = {**n.offers["a"], 3: {"p1": Quote(Decimal("10.60"), 5)}}
    problems = rules.check_requests(
        replace(with_requests(n, counter, bafo, again), offers={"a": offers}), "a"
    )
    assert any("3 requests for 2 rounds" in p for p in problems)
    assert any("2 best-and-final requests" in p for p in problems)
    # Two requests for one offer version.
    twice = replace(bafo, offer_version=1, asks=counter.asks)
    problems = rules.check_requests(with_requests(n, counter, twice), "a")
    assert any("two for one offer" in p for p in problems)


def test_every_bafo_shares_one_cutoff() -> None:
    n = clean()
    other = replace(n.requests["a"][1], supplier="b", cutoff_at=CUTOFF + timedelta(days=1))
    two = replace(n, suppliers=("a", "b"), requests={**n.requests, "b": (other,)})
    assert any("cut-offs differ" in p for p in rules.check_cutoff(two))


def test_no_bafo_after_the_suppliers_final_offer() -> None:
    n = clean()
    assert rules.check_final_offers(replace(n, finals={"a": frozenset({2})}))
    assert rules.check_final_offers(replace(n, stored_finals={"a": frozenset({2})}))
    # Declared final at v1: the BAFO answers v2, which beats it (10.80 < 11.00) ...
    assert rules.check_final_offers(replace(n, finals={"a": frozenset({1})})) == [
        "a: a revised_offer (sent) answers v1, its declared final offer"
    ]
    # ... and nothing may answer a later offer that is no better than the final one.
    same = with_offer(replace(n, requests={"a": n.requests["a"][1:]}), 2, "11.00")
    found = rules.check_final_offers(replace(same, finals={"a": frozenset({1})}))
    assert found == ["a: a bafo (sent) answers v2, no better than its declared final offer v1"]


def test_asks_stay_within_the_maximum_in_cents() -> None:
    n = clean()
    counter, bafo = n.requests["a"]
    for price, problem in (("12.50", "above the maximum"), ("10.405", "more than 2 decimals")):
        asks = (replace(bafo.asks[0], price=Decimal(price)),)
        found = rules.check_limits(with_requests(n, counter, replace(bafo, asks=asks)))
        assert any(problem in p for p in found), found


def with_offer(n: Negotiation, version: int, price: str, lead: int = 5) -> Negotiation:
    offers = {**n.offers["a"], version: {"p1": Quote(Decimal(price), lead)}}
    return replace(n, offers={"a": offers})


def test_a_line_in_target_is_never_asked_lower() -> None:
    assert rules.check_in_target(with_offer(clean(), 2, "9.90"))


def test_no_concession_without_a_move() -> None:
    # The supplier kept 11.00, yet the BAFO conceded from 10.00 to 10.40.
    found = rules.check_concessions(with_offer(clean(), 2, "11.00"))
    assert any("did not move" in p for p in found), found
    # The first ask is the target.
    n = clean()
    counter, bafo = n.requests["a"]
    first = replace(counter, asks=(replace(counter.asks[0], price=Decimal("10.50")),))
    assert rules.check_concessions(with_requests(n, first, bafo))


def test_a_late_line_is_asked_for_its_need_by_date() -> None:
    n = with_offer(clean(), 1, "11.00", lead=20)  # ordered 09/10: 29/10, after 19/10
    sweep = n.sweeps[0]
    late = {
        "a": replace(
            sweep.before["a"], lines=(replace(sweep.before["a"].lines[0], delivery="late"),)
        )
    }
    n = replace(n, sweeps=(replace(sweep, before=late), *n.sweeps[1:]))
    assert any("no date asked" in p for p in rules.check_late_lines(n))
    counter, bafo = n.requests["a"]
    wrong = replace(counter, asks=(replace(counter.asks[0], deliver_by=NEED_BY + timedelta(1)),))
    assert any("the need-by is" in p for p in rules.check_late_lines(with_requests(n, wrong, bafo)))


def test_no_request_after_an_answer_without_progress() -> None:
    n = with_offer(clean(), 2, "11.00")  # the answer to round 1 repeats the offer
    found = rules.check_progress(n)
    assert any("improved nothing" in p for p in found), found
    # Nothing sent, but the buyer is not told that it needs a decision.
    counter, _ = n.requests["a"]
    stopped = with_requests(n, counter)
    assert any("the buyer sees" in p for p in rules.check_progress(stopped))
    sweep = stopped.sweeps[1]
    decide = replace(sweep.after["a"], phase="needs_decision", reason="no_progress")
    ok = replace(stopped, sweeps=(stopped.sweeps[0], replace(sweep, after={"a": decide})))
    assert rules.check_progress(ok) == []


def test_the_final_round_is_common() -> None:
    n = clean()
    late = replace(n.requests["a"][0], supplier="b", sweep=1)  # a revised offer after a BAFO
    two = replace(n, suppliers=("a", "b"), requests={**n.requests, "b": (late,)})
    assert rules.check_final_round(two)


def test_the_buyer_sees_what_was_sent() -> None:
    n = clean()
    sweep = n.sweeps[1]
    stale = replace(sweep.after["a"], reason="revised_offer_sent", status="countered")
    found = rules.check_visible(
        replace(n, sweeps=(n.sweeps[0], replace(sweep, after={"a": stale})))
    )
    assert any("BAFO waits" in p for p in found), found
    assert any("make it bafo_requested" in p for p in found), found
    # Before the sweep, the view announced something else than what the sweep decided.
    quiet = replace(sweep.before["a"], phase="waiting_for_supplier", next_action="wait")
    found = rules.check_visible(
        replace(n, sweeps=(n.sweeps[0], replace(sweep, before={"a": quiet})))
    )
    assert any("the sweep decided" in p for p in found), found


def test_an_offer_above_target_gets_its_request() -> None:
    n = with_requests(clean())
    sweep = n.sweeps[0]
    accepted = replace(
        sweep.after["a"],
        phase="accepted_internally",
        reason="nothing_to_ask",
        label="Accepted internally – nothing to ask",
        status="accepted",
    )
    n = replace(n, sweeps=(replace(sweep, after={"a": accepted}),))
    assert any("got no request" in p for p in rules.check_request_due(n))
    held = replace(accepted, phase="needs_answer", reason="supplier_question")
    n = replace(n, sweeps=(replace(sweep, after={"a": held}),))
    assert rules.check_request_due(n) == []


def test_emails_show_only_decided_amounts() -> None:
    n = clean()
    leaked = replace(n.emails["a"][1], body=n.emails["a"][1].body + " Outro: R$ 9,75.")
    found = rules.check_emails(replace(n, emails={"a": (n.emails["a"][0], leaked)}))
    assert any("amounts not decided" in p for p in found), found


def test_a_price_left_for_the_next_email_is_no_repeat() -> None:
    """Questions first: the first email asked only the date, its price ask waited; the
    next email asks the price although the supplier did not move (a first ask)."""
    n = with_offer(clean(), 2, "11.00")
    counter, bafo = n.requests["a"]
    dated = LineAsk("p1", Decimal("11.00"), 5, deliver_by=NEED_BY)
    first = replace(counter, kind="delivery_date", asks=(dated,), questions=1)
    ask = (replace(bafo.asks[0], offered=Decimal("11.00"), price=Decimal("10.00")),)
    found = rules.check_progress(with_requests(n, first, replace(bafo, asks=ask)))
    assert any("improved nothing" in p for p in found), found
    deferred = replace(first, deferred=("p1",))
    assert rules.check_progress(with_requests(n, deferred, replace(bafo, asks=ask))) == []
    # The buyer's view plans the price ask; the email defers it: what was shown matches.
    sweep = n.sweeps[0]
    late = replace(sweep.before["a"].lines[0], delivery="late", deliver_by=NEED_BY)
    before = replace(sweep.before["a"], next_action="delivery_date", lines=(late,))
    shown = replace(n, sweeps=(replace(sweep, before={"a": before}), n.sweeps[1]))

    def predicted(first: Request) -> list[str]:
        found = rules.check_visible(with_requests(shown, first, replace(bafo, asks=ask)))
        return [p for p in found if p.startswith("a day 0: the buyer saw")]

    assert predicted(first)  # without the deferral the price ask was promised
    assert predicted(deferred) == []
