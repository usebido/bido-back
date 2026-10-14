"""The negotiation planner: one next action per supplier, every state and transition.

Pure: no database, no model. Dates are fixed (Friday 9 Oct 2026, 12:00 UTC).
"""

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from bido_back.negotiation import service as negotiation_service
from bido_back.negotiation.labels import phase_label
from bido_back.negotiation.planner import (
    Context,
    EmailKind,
    EmailRecord,
    EmailState,
    EmailWait,
    LineAsk,
    LineQuote,
    LineTerms,
    Offer,
    PlanWarning,
    SupplierFacts,
    SupplierPlan,
    ask_slots,
    bafo_cutoff,
    every_supplier_final,
    plan_supplier,
    ready_to_close,
)
from bido_back.negotiation.signals import ClarificationItem, ClarificationState
from bido_back.negotiation.strategy import EffectivePrice, UnitPrice
from bido_back.supplier_email.model import MAX_ASKS
from tests.comparison import test_rules as comparison_rules

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
TODAY = NOW.date()
DEADLINE = NOW + timedelta(days=8)
L1, L2, L3 = uuid4(), uuid4(), uuid4()
P1, P2, P3, P4 = uuid4(), uuid4(), uuid4(), uuid4()
PROPOSALS = {1: P1, 2: P2, 3: P3, 4: P4}


def line(
    line_id: UUID,
    position: int,
    target: str | None,
    maximum: str | None,
    need_days: int | None = 14,
    quantity: str = "70",
) -> LineTerms:
    return LineTerms(
        rfq_line_id=line_id,
        position=position,
        quantity=Decimal(quantity),
        need_by=TODAY + timedelta(days=need_days) if need_days is not None else None,
        target=Decimal(target) if target is not None else None,
        max_price=Decimal(maximum) if maximum is not None else None,
    )


MTR = (
    line(L1, 1, "45.67", "51.91"),
    line(L2, 2, "234.52", "266.28", quantity="15"),
    line(L3, 3, "116.27", "135.30", need_days=18, quantity="12.5"),
)


def quote(
    line_id: UUID,
    price: str | None,
    lead: int | None = 10,
    *,
    places: int = 2,
    blockers: tuple[str, ...] = (),
    better: bool = False,
    moq: str | None = None,
) -> LineQuote:
    return LineQuote(
        rfq_line_id=line_id,
        unit_price=UnitPrice(Decimal(price)) if price is not None else None,
        places=places,
        lead_time_days=lead,
        delivery=TODAY + timedelta(days=lead) if lead is not None else None,
        moq=Decimal(moq) if moq is not None else None,
        blockers=blockers,
        better_offer_exists=better,
    )


def offer(*quotes: LineQuote, version: int = 1, valid_days: int | None = 45) -> Offer:
    return Offer(
        proposal_id=PROPOSALS[version],
        version=version,
        lines={q.rfq_line_id: q for q in quotes},
        valid_until=TODAY + timedelta(days=valid_days) if valid_days is not None else None,
    )


WITHIN = offer(quote(L1, "44.90"), quote(L2, "229.00", 12), quote(L3, "112.00", 14))
ABOVE = offer(quote(L1, "48.90"), quote(L2, "255.00", 12), quote(L3, "112.00", 14))


def ctx(
    *,
    now: datetime = NOW,
    order_date: date | None = None,
    deadline: datetime | None = DEADLINE,
    max_rounds: int = 2,
    cutoff_at: datetime | None = None,
    response_due: date | None = None,
    negotiation: Literal["not_started", "open", "ready_to_close", "closed"] = "open",
) -> Context:
    return Context(
        now=now,
        order_date=order_date or now.date(),
        deadline=deadline,
        max_rounds=max_rounds,
        cutoff_at=cutoff_at,
        response_due=response_due,
        negotiation=negotiation,
    )


def facts(
    current: Offer | None,
    *emails: EmailRecord,
    status: str = "responded",
    clarification: ClarificationState | None = None,
    rfq_email_wait: EmailWait | None = None,
    chases_sent: int = 0,
    chases_max: int = 0,
    reply_unread: bool = False,
) -> SupplierFacts:
    return SupplierFacts(
        invite_status=status,
        offer=current,
        emails=emails,
        clarification=clarification or ClarificationState(),
        rfq_email_wait=rfq_email_wait,
        chases_sent=chases_sent,
        chases_max=chases_max,
        reply_unread=reply_unread,
    )


def email(
    *,
    sequence: int,
    round: int,
    version: int,
    asks: Sequence[LineAsk] = (),
    kind: EmailKind = "revised_offer",
    state: EmailState = "sent",
    reply_days: int = 2,
    cutoff: datetime | None = None,
    wait: EmailWait | None = None,
) -> EmailRecord:
    return EmailRecord(
        sequence=sequence,
        round=round,
        kind=kind,
        state=state,
        proposal_id=PROPOSALS[version],
        proposal_version=version,
        asks=tuple(asks),
        reply_by=TODAY + timedelta(days=reply_days),
        cutoff_at=cutoff,
        wait=wait,
    )


def asked(line_id: UUID, ask: str | None, offered: str, deliver_by: date | None = None) -> LineAsk:
    return LineAsk(
        rfq_line_id=line_id,
        ask_price=Decimal(ask) if ask is not None else None,
        deliver_by=deliver_by,
        offered_unit_price=UnitPrice(Decimal(offered)),
    )


def plan(
    supplier: SupplierFacts,
    lines: Sequence[LineTerms] = MTR,
    *,
    now: datetime = NOW,
    deadline: datetime | None = DEADLINE,
    max_rounds: int = 2,
    cutoff_at: datetime | None = None,
    response_due: date | None = None,
    negotiation: Literal["not_started", "open", "ready_to_close", "closed"] = "open",
) -> SupplierPlan:
    context = ctx(
        now=now,
        deadline=deadline,
        max_rounds=max_rounds,
        cutoff_at=cutoff_at,
        response_due=response_due,
        negotiation=negotiation,
    )
    return plan_supplier(lines, supplier, context)


def asks_of(result: SupplierPlan) -> dict[int, tuple[Decimal | None, date | None]]:
    assert result.email is not None
    positions = {t.rfq_line_id: t.position for t in MTR}
    return {positions[p.rfq_line_id]: (p.ask_price, p.deliver_by) for p in result.email.positions}


# --- Before an offer --------------------------------------------------------------------------


def test_before_any_offer_the_supplier_is_waited_for_or_not_started() -> None:
    assert plan(facts(None, status="draft")).phase == "not_started"
    queued = plan(facts(None, status="pending_approval", rfq_email_wait=EmailWait("approval")))
    assert (queued.phase, queued.reason) == (
        "held_for_approval",
        "rfq_email:email_awaiting_approval",
    )
    waiting = plan(facts(None, status="sent"), response_due=TODAY + timedelta(days=5))
    assert (waiting.action, waiting.phase, waiting.reason) == (
        "wait",
        "waiting_for_supplier",
        "awaiting_quote",
    )
    assert waiting.until is not None and waiting.until.date() == TODAY + timedelta(days=5)
    chased = plan(
        facts(None, status="sent", chases_sent=3, chases_max=3),
        response_due=TODAY + timedelta(days=5),
    )
    assert chased.reason == "awaiting_quote_reminders_exhausted"
    assert "no more reminders" in phase_label(chased, chases_sent=3)
    declined = plan(facts(None, status="declined"))
    assert (declined.action, declined.phase, declined.status) == ("close", "closed", "rejected")


def test_a_reply_without_an_offer_needs_the_buyers_answer() -> None:
    result = plan(facts(None, status="responded"))
    assert (result.action, result.phase, result.reason) == (
        "hold",
        "needs_answer",
        "reply_without_offer",
    )
    assert "question" in phase_label(result).lower()


def test_a_reply_still_being_read_waits_for_bido_not_for_the_buyer() -> None:
    reading = plan(facts(None, status="responded", reply_unread=True))
    assert (reading.action, reading.phase, reading.reason) == ("wait", "waiting", "reading_reply")
    assert phase_label(reading) == "Waiting: Bido is reading the supplier's reply."
    assert ready_to_close(line_ids=[L1], waiting_suppliers=0, plans=[reading]) is False
    # A newer reply being read does not unplan the current offer (it is planned once read).
    assert plan(facts(WITHIN, reply_unread=True)).phase == "accepted_internally"


def test_nothing_is_negotiated_before_the_negotiation_opens_or_after_it_closes() -> None:
    assert plan(facts(ABOVE), negotiation="not_started").phase == "not_started"
    closed = plan(facts(ABOVE), negotiation="closed")
    assert (closed.action, closed.phase) == ("close", "closed")


# --- Accepting internally ----------------------------------------------------------------------


def test_within_target_on_time_and_valid_is_accepted_internally_with_nothing_to_ask() -> None:
    result = plan(facts(WITHIN))
    assert (result.action, result.phase, result.status, result.email) == (
        "accept",
        "accepted_internally",
        "accepted",
        None,
    )
    assert result.valid_until == TODAY + timedelta(days=45)
    # The latest order date that still meets every need-by (calendar-day helper here).
    label = phase_label(result)
    assert label.startswith("Accepted internally – nothing to ask")
    assert "valid until" in label


def test_accepting_needs_a_known_lead_time() -> None:
    unknown = offer(quote(L1, "44.90", None), quote(L2, "229.00", 12), quote(L3, "112.00", 14))
    result = plan(facts(unknown))
    assert (result.action, result.phase, result.reason) == (
        "hold",
        "needs_decision",
        "delivery_unknown",
    )


def test_accepting_needs_a_known_validity_lasting_until_the_decision() -> None:
    no_validity = plan(facts(replace(WITHIN, valid_until=None)))
    assert (no_validity.phase, no_validity.reason) == ("needs_decision", "validity_unknown")
    short = plan(facts(replace(WITHIN, valid_until=TODAY + timedelta(days=3))))
    assert (short.phase, short.reason) == ("needs_decision", "validity_ends_before_decision")


def test_a_moq_or_payment_deviation_needs_the_buyer_not_an_acceptance() -> None:
    moq = offer(quote(L1, "44.90", moq="100"), quote(L2, "229.00", 12), quote(L3, "112.00", 14))
    result = plan(facts(moq))
    assert (result.phase, result.reason) == ("needs_decision", "moq_above_quantity")
    assert any(w.code == "moq_above_quantity" and w.position == 1 for w in result.warnings)
    paid = plan(facts(replace(WITHIN, payment_terms_shorter=True)))
    assert (paid.phase, paid.reason) == ("needs_decision", "payment_terms_shorter")


# --- Revised offers and delivery dates ---------------------------------------------------------


def test_a_revised_offer_asks_the_target_only_on_lines_above_it() -> None:
    result = plan(facts(ABOVE))
    assert (result.action, result.phase, result.status) == ("revised_offer", "waiting", "open")
    assert result.email is not None and (result.email.kind, result.email.round) == (
        "revised_offer",
        1,
    )
    assert asks_of(result) == {1: (Decimal("45.67"), None), 2: (Decimal("234.52"), None)}
    # Reply by (the email standard): three business days (Friday -> Wednesday), never after
    # the deadline.
    assert result.email.reply_by == date(2026, 10, 14)
    assert plan(facts(ABOVE), deadline=NOW + timedelta(days=2, hours=1)).email is not None


def test_a_late_line_gets_one_delivery_date_request_for_the_need_by_date() -> None:
    late = offer(quote(L1, "44.90", 10), quote(L2, "229.00", 19), quote(L3, "112.00", 14))
    result = plan(facts(late))
    assert result.action == "delivery_date"
    assert asks_of(result) == {2: (None, TODAY + timedelta(days=14))}
    position = result.email.positions[0] if result.email else None
    assert position is not None and position.offered_delivery == TODAY + timedelta(days=19)


def test_price_and_date_asks_go_out_in_one_email() -> None:
    mixed = offer(quote(L1, "44.90", 10), quote(L2, "255.00", 12), quote(L3, "112.00", 25))
    result = plan(facts(mixed))
    assert result.action == "revised_offer"
    assert asks_of(result) == {
        2: (Decimal("234.52"), None),
        3: (None, TODAY + timedelta(days=18)),
    }


def test_a_need_by_date_no_order_can_meet_goes_to_the_buyer_with_the_earliest_date() -> None:
    lines = (line(L1, 1, "45.67", "51.91", need_days=0),)
    result = plan(facts(offer(quote(L1, "44.90", 5))), lines)
    assert (result.action, result.reason) == ("hold", "delivery_need_by_unreachable")
    assert result.email is None
    assert "earliest delivery" in phase_label(result)


def test_without_a_target_a_better_price_is_asked_without_a_number() -> None:
    lines = (line(L1, 1, None, None),)
    result = plan(facts(offer(quote(L1, "0.050", 10, places=3))), lines)
    assert result.action == "revised_offer" and result.email is not None
    [position] = result.email.positions
    assert (position.ask_price, position.best_price) == (None, True)


def test_competition_is_mentioned_only_for_an_asked_line_with_a_lower_offer() -> None:
    lower = offer(quote(L1, "48.90", better=True), quote(L2, "229.00", 12), quote(L3, "112.00", 14))
    told = plan(facts(lower)).email
    assert told is not None and told.mention_competition is True
    elsewhere = offer(
        quote(L1, "48.90"), quote(L2, "229.00", 12, better=True), quote(L3, "112.00", 14)
    )
    silent = plan(facts(elsewhere)).email
    assert silent is not None and silent.mention_competition is False


# --- One request at a time; waiting -------------------------------------------------------------


def test_after_a_request_is_sent_bido_waits_for_the_supplier_until_the_reply_by_date() -> None:
    first = email(sequence=1, round=1, version=1, asks=[asked(L1, "45.67", "48.90")])
    result = plan(facts(ABOVE, first))
    assert (result.action, result.phase, result.reason, result.status) == (
        "wait",
        "waiting_for_supplier",
        "revised_offer_sent",
        "countered",
    )
    assert result.until is not None and result.until.date() == TODAY + timedelta(days=2)
    assert phase_label(result).startswith("Waiting for the supplier")
    # No reply by the reply-by date: ONE reminder, the same request with a new reply-by.
    later = plan(facts(ABOVE, first), now=NOW + timedelta(days=4))
    assert (later.action, later.phase, later.reason) == ("reminder", "waiting", "reminder_due")
    assert later.email is not None
    assert (later.email.reminder_of, later.email.round, later.email.kind) == (
        1,
        1,
        "revised_offer",
    )
    assert later.email.reply_by > TODAY + timedelta(days=4)
    reminder = replace(first, sequence=2, reminder_of=1, reply_by=TODAY + timedelta(days=7))
    reminded = plan(facts(ABOVE, first, reminder), now=NOW + timedelta(days=5))
    # A reminder is no round: the supplier is still in round 1.
    assert (reminded.phase, reminded.reason, reminded.status) == (
        "waiting_for_supplier",
        "reminder_sent",
        "countered",
    )
    # Unanswered after the reminder: the offer stands; never a second reminder.
    after = plan(facts(ABOVE, first, reminder), now=NOW + timedelta(days=8))
    assert (after.action, after.phase, after.reason, after.email) == (
        "close",
        "closed",
        "no_reply_after_reminder",
        None,
    )
    # A new offer after the reminder: the next request is round 2.
    answered = plan(facts(MOVED, first, reminder), now=NOW + timedelta(days=5), max_rounds=3)
    assert answered.email is not None and answered.email.round == 2
    # Never a reminder after the negotiation deadline.
    overdue = plan(
        facts(ABOVE, first), now=NOW + timedelta(days=4), deadline=NOW + timedelta(days=3)
    )
    assert (overdue.phase, overdue.reason, overdue.email) == (
        "waiting_for_supplier",
        "reply_overdue",
        None,
    )


def test_an_email_decided_or_awaiting_approval_is_never_doubled() -> None:
    decided = email(sequence=1, round=1, version=1, state="decided")
    result = plan(facts(ABOVE, decided))
    assert (result.action, result.phase, result.reason, result.email) == (
        "wait",
        "waiting",
        "preparing_email",
        None,
    )
    approval = email(sequence=1, round=1, version=1, state="proposed", wait=EmailWait("approval"))
    held = plan(facts(ABOVE, approval))
    assert (held.phase, held.reason) == ("held_for_approval", "email_awaiting_approval")
    until = NOW + timedelta(hours=20)
    capped = email(
        sequence=1, round=1, version=1, state="proposed", wait=EmailWait("daily_cap", until=until)
    )
    waiting = plan(facts(ABOVE, capped))
    assert (waiting.phase, waiting.reason, waiting.until) == ("waiting", "daily_limit", until)
    assert "daily email limit" in phase_label(waiting)
    mandate = email(
        sequence=1,
        round=1,
        version=1,
        state="proposed",
        wait=EmailWait("mandate_hold", detail="commitment_language"),
    )
    out = plan(facts(ABOVE, mandate))
    assert (out.phase, out.reason) == ("held_for_approval", "held_by_mandate:commitment_language")
    assert "Held for your approval" in phase_label(out)


def test_a_rejected_or_unwritable_email_hands_that_offer_to_the_buyer() -> None:
    rejected = email(sequence=1, round=1, version=1, state="rejected")
    result = plan(facts(ABOVE, rejected))
    assert (result.action, result.phase, result.reason, result.email) == (
        "hold",
        "needs_decision",
        "email_rejected",
        None,
    )
    held = replace(email(sequence=1, round=1, version=1, state="held"), held_reason="draft_refused")
    stuck = plan(facts(ABOVE, held))
    assert (stuck.reason, stuck.detail) == ("email_not_prepared", "draft_refused")
    # A newer offer is planned again.
    newer = plan(facts(replace(ABOVE, proposal_id=P2, version=2), held))
    assert newer.action == "revised_offer"


# --- Rounds, movement, the best-and-final request ----------------------------------------------


MOVED = offer(quote(L1, "46.50"), quote(L2, "245.00", 12), quote(L3, "112.00", 14), version=2)
FIRST = email(
    sequence=1,
    round=1,
    version=1,
    asks=[asked(L1, "45.67", "48.90"), asked(L2, "234.52", "255.00")],
)


def test_the_same_offer_again_stops_asking_and_needs_the_buyer() -> None:
    same = replace(ABOVE, proposal_id=P2, version=2)
    result = plan(facts(same, FIRST), max_rounds=3)
    assert (result.action, result.phase, result.reason, result.status) == (
        "hold",
        "needs_decision",
        "no_progress",
        "closed",
    )
    assert phase_label(result).startswith("Needs your decision")


def test_movement_in_the_last_round_gets_one_bafo_with_a_common_cut_off() -> None:
    result = plan(facts(MOVED, FIRST))
    assert result.action == "bafo" and result.email is not None
    assert (result.email.kind, result.email.round, result.email.sets_cutoff) == ("bafo", 2, True)
    # End of the day two business days later (Friday -> Tuesday), before the deadline.
    assert result.email.cutoff_at == datetime(2026, 10, 13, 23, 59, tzinfo=UTC)
    assert result.email.cutoff_at == bafo_cutoff(NOW, DEADLINE)
    # Half the remaining gap is conceded on lines where the supplier moved.
    assert asks_of(result) == {1: (Decimal("46.08"), None), 2: (Decimal("239.76"), None)}


def test_with_more_rounds_a_moving_supplier_gets_a_smaller_concession_first() -> None:
    result = plan(facts(MOVED, FIRST), max_rounds=3)
    assert result.action == "revised_offer"
    assert result.email is not None and result.email.round == 2
    assert asks_of(result) == {1: (Decimal("45.87"), None), 2: (Decimal("237.14"), None)}


def test_canceled_emails_never_consume_a_round() -> None:
    canceled = email(sequence=1, round=1, version=1, state="canceled")
    sent = email(
        sequence=2,
        round=1,
        version=1,
        asks=[asked(L1, "45.67", "48.90"), asked(L2, "234.52", "255.00")],
    )
    result = plan(facts(MOVED, canceled, sent))
    # One email was sent: this is round 2 (the BAFO), not round 3.
    assert result.email is not None and (result.email.kind, result.email.round) == ("bafo", 2)


def test_a_bafo_is_requested_once_and_its_answer_is_final() -> None:
    bafo = email(
        sequence=2,
        round=2,
        version=2,
        kind="bafo",
        asks=[asked(L1, "46.08", "46.50")],
        cutoff=NOW + timedelta(days=4),
    )
    still_high = offer(
        quote(L1, "46.20"), quote(L2, "229.00", 12), quote(L3, "112.00", 14), version=3
    )
    result = plan(facts(still_high, FIRST, bafo), max_rounds=5)
    assert (result.action, result.phase, result.reason, result.email) == (
        "close",
        "closed",
        "bafo_answered",
        None,
    )
    assert "best and final offer" in phase_label(result)
    in_target = replace(WITHIN, proposal_id=P3, version=3)
    accepted = plan(facts(in_target, FIRST, bafo))
    assert (accepted.action, accepted.phase, accepted.reason) == (
        "accept",
        "accepted_internally",
        "final_offer_in_target",
    )


def test_waiting_for_the_bafo_until_the_cut_off_then_the_offer_stands() -> None:
    cutoff = NOW + timedelta(days=2)
    bafo = email(sequence=2, round=2, version=2, kind="bafo", cutoff=cutoff)
    waiting = plan(facts(MOVED, FIRST, bafo))
    assert (waiting.phase, waiting.reason, waiting.until, waiting.status) == (
        "waiting_for_supplier",
        "bafo_sent",
        cutoff,
        "bafo_requested",
    )
    after = plan(facts(MOVED, FIRST, bafo), now=cutoff + timedelta(minutes=1))
    assert (after.action, after.reason) == ("close", "final_round_ended")


def test_no_bafo_after_the_suppliers_own_final_offer() -> None:
    final = replace(MOVED, declared_final=True)
    result = plan(facts(final, FIRST), max_rounds=3)
    assert (result.action, result.reason, result.email) == (
        "close",
        "supplier_declared_final",
        None,
    )


def test_the_common_cut_off_makes_any_next_request_the_final_one() -> None:
    cutoff = NOW + timedelta(days=2)
    result = plan(facts(ABOVE), max_rounds=3, cutoff_at=cutoff)
    assert result.email is not None
    assert (result.email.kind, result.email.cutoff_at, result.email.sets_cutoff) == (
        "bafo",
        cutoff,
        False,
    )
    passed = plan(facts(MOVED, FIRST), max_rounds=3, cutoff_at=NOW - timedelta(hours=1))
    assert (passed.action, passed.reason) == ("close", "final_round_closed")
    close = plan(facts(ABOVE), max_rounds=3, cutoff_at=NOW + timedelta(hours=3))
    assert (close.action, close.reason) == ("close", "no_time_for_final_round")


def test_the_deadline_window_makes_the_next_request_the_bafo() -> None:
    result = plan(facts(ABOVE), max_rounds=3, deadline=NOW + timedelta(hours=20))
    assert result.email is not None and result.email.kind == "bafo"
    assert result.email.cutoff_at == NOW + timedelta(hours=20)


def test_a_request_identical_to_the_last_one_becomes_the_bafo() -> None:
    lines = (line(L1, 1, "45.67", "51.91"),)
    first = email(sequence=1, round=1, version=1, asks=[asked(L1, "45.67", "60.00")])
    moved_above_max = offer(quote(L1, "55.00"), version=2)
    result = plan(facts(moved_above_max, first), lines, max_rounds=4)
    assert result.email is not None and result.email.kind == "bafo"


def test_the_round_cap_ends_the_negotiation_with_the_last_offer() -> None:
    second = email(sequence=2, round=2, version=2, asks=[asked(L1, "45.87", "46.50")])
    third = offer(quote(L1, "46.10"), quote(L2, "229.00", 12), quote(L3, "112.00", 14), version=3)
    result = plan(facts(third, FIRST, second))
    assert (result.action, result.reason, result.status) == (
        "close",
        "round_limit_reached",
        "closed",
    )


def test_meeting_our_last_ask_ends_the_asks() -> None:
    met = offer(quote(L1, "45.60"), quote(L2, "234.00", 12), quote(L3, "112.00", 14), version=2)
    result = plan(facts(met, FIRST), max_rounds=3)
    assert (result.action, result.phase) == ("accept", "accepted_internally")
    second = email(sequence=2, round=2, version=2, asks=[asked(L1, "45.87", "46.50")])
    above_target = offer(
        quote(L1, "45.80"), quote(L2, "229.00", 12), quote(L3, "112.00", 14), version=3
    )
    stop = plan(facts(above_target, FIRST, second), max_rounds=4)
    assert (stop.action, stop.reason) == ("close", "offer_meets_last_ask")
    # Met on price but late on another line: that is the buyer's decision, not "final".
    late = offer(quote(L1, "45.80"), quote(L2, "229.00", 25), quote(L3, "112.00", 14), version=3)
    asked_date = replace(
        second,
        asks=(
            *second.asks,
            LineAsk(rfq_line_id=L2, deliver_by=TODAY + timedelta(days=14)),
        ),
    )
    held = plan(facts(late, FIRST, asked_date), max_rounds=4)
    assert (held.action, held.reason) == ("hold", "delivery_still_late")


def test_a_late_line_is_asked_once_then_the_buyer_decides() -> None:
    late = offer(quote(L1, "44.90", 10), quote(L2, "229.00", 19), quote(L3, "112.00", 14))
    sent = email(
        sequence=1,
        round=1,
        version=1,
        kind="delivery_date",
        asks=[
            LineAsk(
                rfq_line_id=L2,
                deliver_by=TODAY + timedelta(days=14),
                offered_delivery=TODAY + timedelta(days=19),
            )
        ],
    )
    faster = replace(
        late, proposal_id=P2, version=2, lines={**late.lines, L2: quote(L2, "229.00", 13)}
    )
    accepted = plan(facts(faster, sent), max_rounds=3)
    assert accepted.phase == "accepted_internally"
    still = replace(late, proposal_id=P2, version=2)
    result = plan(facts(still, sent), max_rounds=3)
    assert (result.action, result.reason) == ("hold", "delivery_still_late")
    label = phase_label(result)
    assert "line(s) 2" in label and "earliest delivery" in label


# --- Holds and questions -----------------------------------------------------------------------


def test_a_supplier_question_needs_the_buyers_answer_and_stops_asks() -> None:
    asked_offer = replace(ABOVE, questions=1, holds=("supplier_clarification_requires_buyer",))
    assert asked_offer.questions == 1
    result = plan(facts(asked_offer))
    assert (result.action, result.phase, result.reason, result.email) == (
        "hold",
        "needs_answer",
        "supplier_question",
        None,
    )
    label = phase_label(result).lower()
    assert "question" in label and "waiting for you" in label


def test_an_offer_held_for_review_needs_the_buyers_decision() -> None:
    held = replace(
        ABOVE, holds=("supplier_clarification_requires_buyer", "offer_state_requires_review")
    )
    result = plan(facts(held))
    assert (result.phase, result.reason) == ("needs_decision", "offer_state_requires_review")
    withdrawn = plan(facts(replace(ABOVE, holds=("offer_withdrawn",))))
    assert (withdrawn.action, withdrawn.reason) == ("close", "offer_withdrawn")


def test_the_buyer_can_release_an_offer_held_for_review() -> None:
    held = replace(
        ABOVE,
        holds=("supplier_clarification_requires_buyer", "offer_state_requires_review"),
        holds_released=True,
    )
    result = plan(facts(held))
    assert (result.action, result.phase) == ("revised_offer", "waiting")
    # A withdrawal is never released.
    gone = plan(facts(replace(ABOVE, holds=("offer_withdrawn",), holds_released=True)))
    assert (gone.action, gone.reason) == ("close", "offer_withdrawn")


def test_an_alternative_part_is_never_negotiated_or_accepted_as_the_exact_part() -> None:
    alternative = offer(
        quote(L1, "44.90"),
        quote(L2, "228.00", 12, blockers=("alternative_needs_approval",)),
        quote(L3, "112.00", 14),
    )
    result = plan(facts(alternative))
    assert (result.action, result.phase, result.email) == ("hold", "needs_decision", None)
    assert "alternative part" in phase_label(result)


# --- Clarifications first ------------------------------------------------------------------------


def to_ask(*items: ClarificationItem) -> ClarificationState:
    """The ledger's items to ask (the service maps them from `supplier_followups`)."""
    return ClarificationState(
        to_ask=tuple(i.code for i in items),
        items=items,
        lines=frozenset(i.rfq_line_id for i in items if i.rfq_line_id is not None),
    )


VALIDITY = ClarificationItem(None, None, "valid_until")


def test_open_items_alone_go_out_first_one_request_at_a_time() -> None:
    no_validity = replace(WITHIN, valid_until=None)
    result = plan(facts(no_validity, clarification=to_ask(VALIDITY)))
    assert (result.action, result.reason, result.email) == ("clarify", "clarification_due", None)
    assert result.questions_open
    pending = ClarificationState(pending=True, pending_wait="approval", lines=frozenset({L2}))
    held = plan(facts(ABOVE, clarification=pending))
    assert (held.action, held.phase, held.reason) == (
        "wait",
        "held_for_approval",
        "clarification_awaiting_approval",
    )
    until = NOW + timedelta(days=2)
    awaiting = ClarificationState(awaiting=True, awaiting_until=until, lines=frozenset({L2}))
    waiting = plan(facts(ABOVE, clarification=awaiting))
    assert (waiting.phase, waiting.reason, waiting.until) == (
        "waiting_for_supplier",
        "clarification_sent",
        until,
    )


def test_questions_asked_once_and_still_open_are_the_buyers_and_other_lines_go_on() -> None:
    exhausted = ClarificationState(exhausted=("pos3.lead_time",), exhausted_lines=frozenset({L3}))
    result = plan(facts(ABOVE, clarification=exhausted))
    assert result.action == "revised_offer"
    within = plan(facts(WITHIN, clarification=exhausted))
    assert (within.action, within.reason) == ("hold", "clarification_unanswered")


# --- One email per supplier answer: the open items with the negotiation's asks --------------------


def test_open_items_and_a_price_above_target_go_out_in_one_email() -> None:
    """The multi_language gap: no validity and a price above target -> ONE email, the
    validity asked first, then one revised-offer ask; it is the round (not a free email)."""
    no_validity = replace(ABOVE, valid_until=None)
    result = plan(facts(no_validity, clarification=to_ask(VALIDITY)), max_rounds=3)
    assert (result.action, result.phase, result.reason) == (
        "revised_offer",
        "waiting",
        "revised_offer_with_questions_due",
    )
    assert result.email is not None
    assert result.email.clarify == (VALIDITY,)
    assert result.email.deferred == ()
    assert result.email.round == 1
    assert set(asks_of(result)) == {1, 2}
    assert ask_slots(result.email.clarify, result.email.positions, result.email.kind) == [
        "offer_terms",
        "revise_price",
    ]
    assert result.questions_open
    assert "one email asking for what the offer lacks" in phase_label(result)


def test_a_line_with_an_open_item_is_asked_about_and_the_others_negotiated() -> None:
    lead = ClarificationItem(L1, 1, "lead_time")
    result = plan(facts(ABOVE, clarification=to_ask(lead)), max_rounds=3)
    assert result.email is not None and result.action == "revised_offer"
    # Line 1 waits for its answer; line 2's price is asked now.
    assert set(asks_of(result)) == {2}
    assert ask_slots(result.email.clarify, result.email.positions, result.email.kind) == [
        "complete_positions",
        "revise_price",
    ]
    by_line = {p.rfq_line_id: p for p in result.lines}
    assert by_line[L1].status == "question_open"


def test_asks_that_do_not_fit_wait_for_the_next_email_and_the_decision_says_so() -> None:
    """Three blocking questions use the three asks: the price ask waits for the answers."""
    items = (
        VALIDITY,
        ClarificationItem(L1, 1, "quantity"),
        ClarificationItem(L2, 2, "delivery_date"),
    )
    pricey = offer(quote(L1, "44.90"), quote(L2, "229.00", 12), quote(L3, "125.00", 14))
    alone = plan(facts(pricey, clarification=to_ask(*items)), max_rounds=3)
    assert (alone.action, alone.detail, alone.email) == (
        "clarify",
        "revised_offer_after_answers",
        None,
    )
    assert "the request for a revised offer follows the answers" in phase_label(alone)
    # Line 3 also late: its date fits the shared delivery ask, its price waits.
    late = offer(quote(L1, "44.90"), quote(L2, "229.00", 12), quote(L3, "125.00", 25))
    result = plan(facts(late, clarification=to_ask(*items)), max_rounds=3)
    assert result.email is not None
    assert (result.action, result.email.kind) == ("delivery_date", "delivery_date")
    assert result.email.deferred == (L3,)
    assert asks_of(result) == {3: (None, TODAY + timedelta(days=18))}
    assert ask_slots(result.email.clarify, result.email.positions, result.email.kind) == [
        "offer_terms",
        "complete_positions",
        "deliver_by",
    ]
    assert "the price request for line(s) 3 follows the answers" in phase_label(result)


def test_a_price_ask_left_for_later_is_asked_next_time_even_without_a_move() -> None:
    sent = EmailRecord(
        sequence=1,
        round=1,
        kind="delivery_date",
        state="sent",
        proposal_id=P1,
        proposal_version=1,
        asks=(
            LineAsk(
                rfq_line_id=L3,
                deliver_by=TODAY + timedelta(days=18),
                offered_delivery=TODAY + timedelta(days=25),
            ),
        ),
        reply_by=TODAY + timedelta(days=2),
        deferred=frozenset({L3}),
    )
    answered = offer(
        quote(L1, "44.90"), quote(L2, "229.00", 12), quote(L3, "125.00", 25), version=2
    )
    result = plan(facts(answered, sent), max_rounds=3)
    assert result.email is not None
    assert (result.action, result.email.round) == ("revised_offer", 2)
    assert asks_of(result)[3][0] is not None
    # Without the deferral, the same answer is "no progress".
    plain = replace(sent, deferred=frozenset())
    stopped = plan(facts(answered, plain), max_rounds=3)
    assert (stopped.action, stopped.reason) == ("hold", "no_progress")


def test_the_best_and_final_request_asks_the_open_items_too() -> None:
    no_validity = replace(ABOVE, valid_until=None)
    result = plan(facts(no_validity, clarification=to_ask(VALIDITY)), max_rounds=1)
    assert result.email is not None
    assert (result.action, result.reason) == ("bafo", "bafo_with_questions_due")
    assert result.email.clarify == (VALIDITY,)
    assert ask_slots(result.email.clarify, result.email.positions, result.email.kind) == [
        "offer_terms",
        "final",
    ]


def test_open_items_wait_while_our_request_is_unanswered() -> None:
    sent = email(sequence=1, round=1, version=1, asks=[asked(L1, "45.67", "48.90")])
    result = plan(facts(ABOVE, sent, clarification=to_ask(VALIDITY)), max_rounds=3)
    assert (result.action, result.phase, result.reason) == (
        "wait",
        "waiting_for_supplier",
        "revised_offer_sent",
    )


def test_at_most_three_asks_whatever_the_items() -> None:
    every = (
        VALIDITY,
        ClarificationItem(None, None, "currency"),
        ClarificationItem(L1, 1, "unit_price"),
        ClarificationItem(L1, 1, "delivery_date"),
        ClarificationItem(L2, 2, "lead_time"),
    )
    late = offer(quote(L1, "48.90"), quote(L2, "255.00", 12), quote(L3, "125.00", 25))
    kinds: dict[int, str] = {}
    for rounds in (1, 3):
        result = plan(facts(late, clarification=to_ask(*every)), max_rounds=rounds)
        kinds[rounds] = result.action
        if result.email is not None:
            slots = ask_slots(result.email.clarify, result.email.positions, result.email.kind)
            assert len(slots) <= MAX_ASKS, (rounds, slots)
    # The best-and-final request has no room after three questions: it waits for the answers.
    assert kinds == {1: "clarify", 3: "delivery_date"}


# --- Warnings and the RFQ -------------------------------------------------------------------------


def test_a_reply_by_date_that_makes_the_need_by_late_is_a_visible_warning() -> None:
    late_if_waiting = offer(
        replace(quote(L1, "44.90", 10), delivery_at_reply_by=TODAY + timedelta(days=20)),
        quote(L2, "229.00", 12),
        quote(L3, "112.00", 14),
    )
    result = plan(facts(late_if_waiting), response_due=TODAY + timedelta(days=10))
    warning = next(w for w in result.warnings if w.code == "reply_by_misses_need_by")
    assert (warning.position, warning.on) == (1, TODAY + timedelta(days=20))


def test_a_line_that_must_be_ordered_before_the_negotiation_ends_is_flagged() -> None:
    order_by = TODAY + timedelta(days=4)
    urgent = offer(
        replace(quote(L1, "44.90", 10), latest_order_date=order_by),
        quote(L2, "229.00", 12),
        quote(L3, "112.00", 14),
    )
    result = plan(facts(urgent))
    assert result.phase == "accepted_internally" and result.order_by == order_by
    warning = next(w for w in result.warnings if w.code == "order_by_before_decision")
    assert (warning.position, warning.on) == (1, order_by)
    assert "order by" in phase_label(result)


def test_the_suppliers_order_deadline_is_when_to_order_an_accepted_offer() -> None:
    # "provided we receive the purchase order by <day>": the offer holds until that day.
    deadline = TODAY + timedelta(days=5)
    result = plan(facts(replace(WITHIN, order_by=deadline)))
    assert result.phase == "accepted_internally" and result.order_by == deadline
    warning = next(w for w in result.warnings if w.code == "order_deadline")
    assert warning.on == deadline
    assert f"order by {deadline.isoformat()}, the supplier's order deadline" in phase_label(result)


def test_an_offer_past_the_suppliers_order_deadline_is_not_accepted() -> None:
    result = plan(facts(replace(WITHIN, order_by=TODAY - timedelta(days=1))))
    assert (result.action, result.phase, result.reason) == (
        "hold",
        "needs_decision",
        "order_deadline_passed",
    )
    assert any(w.code == "order_deadline_passed" for w in result.warnings)
    assert phase_label(result).startswith(
        "Needs your decision: the supplier's order deadline has passed"
    )


def test_a_held_offer_names_why_and_the_terms_the_reply_changed() -> None:
    held = replace(
        WITHIN,
        holds=("offer_state_requires_review",),
        hold_reasons=("changed_commercial_basis_requires_replacement",),
        held_fields=("pos1.unit", "currency"),
    )
    result = plan(facts(held))
    assert (result.phase, result.reason, result.offer_held) == (
        "needs_decision",
        "offer_state_requires_review",
        True,
    )
    assert result.held_fields == ("pos1.unit", "currency")
    label = phase_label(result)
    assert "commercial basis" in label
    assert "the reply changes: line 1 unit, currency" in label


def test_ready_to_close_needs_every_line_accepted_and_every_supplier_answered() -> None:
    accepted = plan(facts(WITHIN))
    asking = plan(facts(ABOVE))
    ids = [t.rfq_line_id for t in MTR]
    assert ready_to_close(line_ids=ids, waiting_suppliers=0, plans=[accepted, asking])
    assert not ready_to_close(line_ids=ids, waiting_suppliers=1, plans=[accepted, asking])
    assert not ready_to_close(line_ids=ids, waiting_suppliers=0, plans=[asking])
    question = plan(facts(replace(WITHIN, questions=1)))
    assert not ready_to_close(line_ids=ids, waiting_suppliers=0, plans=[question])
    clarifying = plan(facts(WITHIN, clarification=ClarificationState(to_ask=("valid_until",))))
    assert not ready_to_close(line_ids=ids, waiting_suppliers=0, plans=[clarifying])


# RFQ-8 (2026-10-12): one line, target 2.60, maximum 2.90, two rounds. The supplier quoted
# 3.20, moved to 2.85 after our counter and answered the best-and-final with 2.75.
RFQ8_LINE = line(L1, 1, "2.60", "2.90")
RFQ8_COUNTER = email(sequence=1, round=1, version=1, asks=[asked(L1, "2.60", "3.20")])
RFQ8_BAFO = email(
    sequence=2,
    round=2,
    version=2,
    kind="bafo",
    asks=[asked(L1, "2.72", "2.85")],
    cutoff=NOW + timedelta(days=2),
)


def test_a_final_offer_above_target_within_the_maximum_leaves_nothing_to_negotiate() -> None:
    final = plan(facts(offer(quote(L1, "2.75"), version=3), RFQ8_COUNTER, RFQ8_BAFO), [RFQ8_LINE])
    assert (final.action, final.phase, final.reason, final.status) == (
        "close",
        "closed",
        "bafo_answered",
        "closed",
    )
    assert [p.price_status for p in final.lines] == ["in_range"]
    # Early close is only for offers within target: unchanged.
    assert not ready_to_close(line_ids=[L1], waiting_suppliers=0, plans=[final])
    # Every supplier gave its last word: ready to close, whatever the prices.
    assert every_supplier_final(waiting_suppliers=0, plans=[final])
    # A supplier who declined is final too; one above the maximum after its BAFO as well
    # (the comparison never recommends it).
    declined = plan(facts(None, status="declined"), [RFQ8_LINE])
    above_max = plan(
        facts(offer(quote(L1, "2.95"), version=3), RFQ8_COUNTER, RFQ8_BAFO), [RFQ8_LINE]
    )
    assert (above_max.action, above_max.status) == ("close", "rejected")
    assert every_supplier_final(waiting_suppliers=0, plans=[final, declined, above_max])
    # So is the supplier's own final offer, and a final offer whose remaining problem is
    # named for the buyer (no more emails: the buyer decides at award).
    own_final = plan(
        facts(replace(offer(quote(L1, "2.85"), version=2), declared_final=True), RFQ8_COUNTER),
        [RFQ8_LINE],
    )
    assert (own_final.action, own_final.reason) == ("close", "supplier_declared_final")
    assert every_supplier_final(waiting_suppliers=0, plans=[own_final])


# RFQ-9 (2026-10-13): target 2.80, maximum 3.05 per piece, 2,000 pieces; the supplier adds
# a one-off tooling charge of 1,250 (0.625 per piece).
RFQ9_LINE = line(L1, 1, "2.80", "3.05", quantity="2000")
RFQ9_COUNTER = email(sequence=1, round=1, version=1, asks=[asked(L1, "2.80", "3.20")])
RFQ9_BAFO = email(
    sequence=2,
    round=2,
    version=2,
    kind="bafo",
    asks=[asked(L1, "2.87", "2.95")],
    cutoff=NOW + timedelta(days=2),
)


def tooled(price: str, effective: str | None) -> LineQuote:
    """A quote whose effective price includes one-off charges spread per unit."""
    return replace(
        quote(L1, price),
        effective_price=EffectivePrice(Decimal(effective)) if effective is not None else None,
    )


def test_rfq9_the_limits_apply_to_the_unit_price_and_tooling_is_a_warning() -> None:
    final = plan(
        facts(offer(tooled("2.90", "3.525"), version=3), RFQ9_COUNTER, RFQ9_BAFO), [RFQ9_LINE]
    )
    # Within the maximum per piece, above the target: never rejected. With the tooling the
    # effective price is above the maximum: the buyer decides knowingly.
    assert (final.action, final.phase, final.reason, final.status) == (
        "close",
        "needs_decision",
        "effective_above_max",
        "closed",
    )
    [line_plan] = final.lines
    assert (line_plan.price_status, line_plan.acceptable, line_plan.effective_above_max) == (
        "in_range",
        False,
        True,
    )
    assert (line_plan.offered_unit_price, line_plan.effective_price) == (
        Decimal("2.90"),
        Decimal("3.525"),
    )
    assert "effective_above_max" in line_plan.reasons
    assert PlanWarning("effective_above_max", 1) in final.warnings
    assert phase_label(final).startswith("Needs your decision: the unit price is within your")
    # The supplier's last word: the RFQ can be closed, the outcome says why it is above.
    assert every_supplier_final(waiting_suppliers=0, plans=[final])
    assert not ready_to_close(line_ids=[L1], waiting_suppliers=0, plans=[final])


def test_rfq9_a_unit_price_above_the_maximum_is_still_rejected() -> None:
    final = plan(
        facts(offer(tooled("3.10", "3.725"), version=3), RFQ9_COUNTER, RFQ9_BAFO), [RFQ9_LINE]
    )
    assert (final.action, final.status) == ("close", "rejected")
    [line_plan] = final.lines
    assert (line_plan.price_status, line_plan.effective_above_max) == ("above_max", False)
    assert all(w.code != "effective_above_max" for w in final.warnings)


def test_rfq9_without_one_off_charges_nothing_changes() -> None:
    for effective in (None, "2.90"):
        final = plan(
            facts(offer(tooled("2.90", effective), version=3), RFQ9_COUNTER, RFQ9_BAFO),
            [RFQ9_LINE],
        )
        assert (final.action, final.phase, final.reason, final.status) == (
            "close",
            "closed",
            "bafo_answered",
            "closed",
        )
        assert not final.lines[0].effective_above_max


def test_rfq9_in_target_per_unit_with_tooling_above_the_maximum_is_never_accepted() -> None:
    result = plan(facts(offer(tooled("2.75", "3.375"))), [RFQ9_LINE])
    # Nothing to ask (in target per piece), but not accepted internally either.
    assert (result.action, result.phase, result.reason, result.email) == (
        "close",
        "needs_decision",
        "effective_above_max",
        None,
    )
    assert result.lines[0].price_status == "in_target"
    assert not ready_to_close(line_ids=[L1], waiting_suppliers=0, plans=[result])


def test_rfq9_the_asks_and_the_concession_follow_the_unit_price() -> None:
    # The first offer, 3.20 per piece (3.825 with tooling): above the maximum, ask 2.80.
    first = plan(facts(offer(tooled("3.20", "3.825"))), [RFQ9_LINE])
    assert first.email is not None and first.action == "revised_offer"
    [position] = first.email.positions
    assert (position.ask_price, position.offered_unit_price) == (Decimal("2.80"), Decimal("3.20"))
    # It moves to 2.95 per piece (3.575 with tooling): within the maximum per piece, so
    # the best-and-final request concedes half the gap: 2.80 + 0.15 / 2 = 2.875 -> 2.87.
    moved = plan(facts(offer(tooled("2.95", "3.575"), version=2), RFQ9_COUNTER), [RFQ9_LINE])
    assert moved.email is not None and moved.action == "bafo"
    [position] = moved.email.positions
    assert (position.ask_price, position.conceded) == (Decimal("2.87"), True)
    assert moved.lines[0].price_status == "in_range"


def test_one_supplier_still_negotiating_keeps_the_negotiation_open() -> None:
    final = plan(facts(offer(quote(L1, "2.75"), version=3), RFQ8_COUNTER, RFQ8_BAFO), [RFQ8_LINE])
    # Still within its best-and-final cut-off.
    waiting_bafo = plan(
        facts(offer(quote(L1, "2.85"), version=2), RFQ8_COUNTER, RFQ8_BAFO), [RFQ8_LINE]
    )
    assert waiting_bafo.phase == "waiting_for_supplier"
    assert not every_supplier_final(waiting_suppliers=0, plans=[final, waiting_bafo])
    # An invited supplier who has not quoted yet.
    assert not every_supplier_final(waiting_suppliers=1, plans=[final])
    # A counter-offer still to send.
    asking = plan(facts(offer(quote(L1, "3.20"))), [RFQ8_LINE])
    assert asking.action == "revised_offer"
    assert not every_supplier_final(waiting_suppliers=0, plans=[final, asking])
    # Held for the buyer: a supplier question, an offer to review, a withdrawn offer,
    # an open clarification, no progress.
    question = plan(facts(replace(offer(quote(L1, "2.75")), questions=1)), [RFQ8_LINE])
    review = plan(
        facts(replace(offer(quote(L1, "2.75")), holds=("offer_state_requires_review",))),
        [RFQ8_LINE],
    )
    withdrawn = plan(
        facts(replace(offer(quote(L1, "2.75")), holds=("offer_withdrawn",))), [RFQ8_LINE]
    )
    clarifying = plan(
        facts(offer(quote(L1, "2.75")), clarification=ClarificationState(to_ask=("valid_until",))),
        [RFQ8_LINE],
    )
    stuck = plan(
        facts(offer(quote(L1, "3.20"), version=2), RFQ8_COUNTER), [RFQ8_LINE], max_rounds=3
    )
    assert stuck.reason == "no_progress"
    for held in (question, review, withdrawn, clarifying, stuck):
        assert not every_supplier_final(waiting_suppliers=0, plans=[final, held]), held.reason
    # Nobody answered at all.
    assert not every_supplier_final(waiting_suppliers=0, plans=[])


def test_every_phase_reads_as_the_buyer_expects() -> None:
    texts = {
        "accepted": phase_label(plan(facts(WITHIN))),
        "waiting": phase_label(
            plan(
                facts(
                    ABOVE, email(sequence=1, round=1, version=1, asks=[asked(L1, "45.67", "48.90")])
                )
            )
        ),
        "decision": phase_label(plan(facts(replace(ABOVE, proposal_id=P2, version=2), FIRST))),
        "question": phase_label(plan(facts(replace(ABOVE, questions=2)))),
    }
    assert "Accepted internally – nothing to ask" in texts["accepted"]
    assert texts["waiting"].startswith("Waiting for the supplier")
    assert texts["decision"].startswith("Needs your decision")
    assert texts["question"].startswith("Needs your answer")
    for text in texts.values():
        # Codes and dates only: never a price.
        assert "45.67" not in text and "48.90" not in text


# --- Comparing like with like -----------------------------------------------------------------


def test_the_decision_keeps_the_suppliers_price_unrounded() -> None:
    # A price in a finer precision than the ask (an exchange rate, a price per 100): the
    # stored offer is the price itself, so the same price again is no movement.
    fine = offer(quote(L1, "48.905"), quote(L2, "229.00", 12), quote(L3, "112.00", 14))
    first = plan(facts(fine), max_rounds=3)
    assert first.email is not None
    [position] = first.email.positions
    assert position.offered_unit_price == Decimal("48.905")
    sent = email(
        sequence=1,
        round=1,
        version=1,
        asks=[asked(L1, str(position.ask_price), str(position.offered_unit_price))],
    )
    again = plan(facts(replace(fine, proposal_id=P2, version=2), sent), max_rounds=3)
    assert (again.action, again.reason) == ("hold", "no_progress")


def test_a_shorter_lead_time_is_progress_from_the_order_date_it_was_asked_on() -> None:
    need_by = TODAY + timedelta(days=14)
    asked_then = replace(
        email(
            sequence=1,
            round=1,
            version=1,
            asks=[
                LineAsk(
                    rfq_line_id=L1,
                    ask_price=Decimal("45.67"),
                    deliver_by=need_by,
                    offered_unit_price=UnitPrice(Decimal("48.90")),
                    offered_delivery=TODAY + timedelta(days=20),
                )
            ],
        ),
        order_date=TODAY,
    )
    later = NOW + timedelta(days=4)
    # Four days later the supplier cuts its lead time from 20 to 17 days, same price: from
    # today it arrives a day later than the first offer did, from the day we asked it is
    # three days earlier.
    shorter = LineQuote(
        rfq_line_id=L1,
        unit_price=UnitPrice(Decimal("48.90")),
        lead_time_days=17,
        delivery=later.date() + timedelta(days=17),
        deliveries_then=((TODAY, TODAY + timedelta(days=17)),),
    )
    moved = Offer(
        proposal_id=P2,
        version=2,
        lines={L1: shorter, L2: quote(L2, "229.00", 12), L3: quote(L3, "112.00", 14)},
        valid_until=TODAY + timedelta(days=45),
    )
    result = plan(facts(moved, asked_then), now=later, max_rounds=3)
    assert result.reason != "no_progress" and result.action in ("revised_offer", "bafo")
    # Without the earlier order date's delivery the move cannot be seen: the buyer decides.
    unknown = replace(moved, lines={**moved.lines, L1: replace(shorter, deliveries_then=())})
    assert plan(facts(unknown, asked_then), now=later, max_rounds=3).reason == "no_progress"


def test_an_offer_on_hold_never_makes_the_rfq_ready_to_close() -> None:
    ids = [t.rfq_line_id for t in MTR]
    withdrawn = plan(facts(replace(WITHIN, holds=("offer_withdrawn",))))
    assert (withdrawn.phase, withdrawn.offer_held) == ("closed", True)
    assert not ready_to_close(line_ids=ids, waiting_suppliers=0, plans=[withdrawn])
    review = plan(facts(replace(WITHIN, holds=("offer_state_requires_review",))))
    assert (review.phase, review.offer_held) == ("needs_decision", True)
    assert not ready_to_close(line_ids=ids, waiting_suppliers=0, plans=[review])
    # Released by the buyer: negotiated (here accepted) as it is.
    released = plan(
        facts(replace(WITHIN, holds=("offer_state_requires_review",), holds_released=True))
    )
    assert released.phase == "accepted_internally" and not released.offer_held
    assert ready_to_close(line_ids=ids, waiting_suppliers=0, plans=[released])


def test_a_final_offer_still_late_names_the_problem_for_the_buyer() -> None:
    # Live journey: MetalNova's copper stayed late after our delivery-date request and its
    # best-and-final read "Closed". No more emails go out, but the buyer is told why.
    late = offer(quote(L1, "44.90"), quote(L2, "229.00", 12), quote(L3, "112.00", 30), version=2)
    final = replace(late, declared_final=True)
    result = plan(facts(final, FIRST), max_rounds=3)
    assert (result.action, result.email) == ("close", None)
    assert (result.phase, result.reason) == ("needs_decision", "delivery_late_final")
    assert "Needs your decision" in phase_label(result)


def test_a_final_offer_whose_only_gap_is_price_stands_as_closed() -> None:
    final = replace(MOVED, declared_final=True)
    result = plan(facts(final, FIRST), max_rounds=3)
    assert (result.action, result.phase, result.reason) == (
        "close",
        "closed",
        "supplier_declared_final",
    )


# --- The outcome line: the unit price against the limits, the charges shown -----------------


def test_rfq9_the_outcome_names_the_unit_price_the_tooling_and_the_effective_price() -> None:
    line_9 = comparison_rules.requested(2000, max_unit_price="3.05")
    comparison = comparison_rules.compare(
        line_9,
        comparison_rules.offer(line_9, "Torino", "2.90", tooling="1250", currency="BRL"),
        base="BRL",
    )
    [outcome] = negotiation_service._outcome(comparison, {line_9.id: Decimal("2.80")})  # pyright: ignore[reportPrivateUsage]
    assert (outcome.supplier_name, outcome.within_target, outcome.above_target) == (
        "Torino",
        False,
        True,
    )
    assert (
        outcome.unit_price_base,
        outcome.tooling_base,
        outcome.effective_unit_price_base,
        outcome.effective_above_max,
    ) == (Decimal("2.90"), Decimal("1250.00"), Decimal("3.525"), True)
    assert outcome.price_warning == (
        "Unit price within your maximum; with one-off charges (BRL 1,250 tooling) the "
        "effective price is BRL 3.525 per unit, above it."
    )
    # Within target per unit: within_target reads the unit price, never the effective one.
    [cheap] = negotiation_service._outcome(comparison, {line_9.id: Decimal("2.95")})  # pyright: ignore[reportPrivateUsage]
    assert (cheap.within_target, cheap.above_target, cheap.effective_above_max) == (
        True,
        False,
        True,
    )


def test_without_tooling_the_outcome_has_no_price_warning() -> None:
    line_9 = comparison_rules.requested(2000, max_unit_price="3.05")
    comparison = comparison_rules.compare(
        line_9, comparison_rules.offer(line_9, "Torino", "2.90", currency="BRL"), base="BRL"
    )
    [outcome] = negotiation_service._outcome(comparison, {line_9.id: Decimal("2.80")})  # pyright: ignore[reportPrivateUsage]
    assert (outcome.above_target, outcome.effective_above_max, outcome.price_warning) == (
        True,
        False,
        None,
    )
    assert outcome.unit_price_base == outcome.effective_unit_price_base == Decimal("2.90")
