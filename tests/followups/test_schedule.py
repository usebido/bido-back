"""Business days, auto-reply pauses, the chase and reminder plans and the open items. No
database."""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from bido_back.agents.catalog.contracts import FollowupEmailInput, FollowupEmailOutput
from bido_back.agents.catalog.definitions import followup_email_problems
from bido_back.followups.autoreply import paused_until, stated_return_date
from bido_back.followups.calendar import add_business_days, is_business_day
from bido_back.followups.ledger import OpenRequest
from bido_back.followups.missing import OfferedLine, RequestedLine, open_items
from bido_back.followups.service import (
    ChasePlan,
    ChaseRow,
    Schedule,
    first_chase_due,
    plan_chase,
    plan_reminder,
    reply_subject,
)

# Thursday 24 Dec 2026, 10:00 UTC.
CHRISTMAS_EVE = datetime(2026, 12, 24, 10, 0, tzinfo=UTC)


def test_business_days_skip_weekends_and_the_suppliers_holidays() -> None:
    # Germany: 25 and 26 Dec are holidays, then the weekend.
    assert add_business_days(CHRISTMAS_EVE, 1, "DE") == datetime(2026, 12, 28, 10, 0, tzinfo=UTC)
    # Without a country only weekends count.
    assert add_business_days(CHRISTMAS_EVE, 1, None) == datetime(2026, 12, 25, 10, 0, tzinfo=UTC)
    # Brazil: Christmas Day only.
    assert add_business_days(CHRISTMAS_EVE, 1, "BR") == datetime(2026, 12, 28, 10, 0, tzinfo=UTC)
    assert add_business_days(CHRISTMAS_EVE, 0, "DE") == CHRISTMAS_EVE
    assert not is_business_day(datetime(2026, 7, 4).date(), "US")
    # An unknown country falls back to weekends only.
    assert is_business_day(datetime(2026, 12, 25).date(), "ZZ")


def test_an_auto_reply_pauses_until_the_day_after_the_stated_return() -> None:
    received = datetime(2026, 10, 7, 9, 30, tzinfo=UTC)  # Wednesday
    text = "Ich bin vom 06.10.2026 bis zum 14.10.2026 nicht im Büro."

    assert stated_return_date(text, received) == datetime(2026, 10, 14).date()
    assert paused_until(text, received, "DE") == datetime(2026, 10, 15, 9, 30, tzinfo=UTC)
    # No date (or only past ones): three business days.
    assert paused_until("Out of office", received, "DE") == datetime(
        2026, 10, 12, 9, 30, tzinfo=UTC
    )


SCHEDULE = Schedule(enabled=True, first_after=2, every=2, max_chases=3)
MONDAY = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)


def plan(
    *chases: ChaseRow,
    now: datetime,
    paused: datetime | None = None,
    invite_status: str = "sent",
    schedule: Schedule = SCHEDULE,
) -> ChasePlan:
    return plan_chase(
        invite_status=invite_status,
        rfq_status="open",
        sent_at=MONDAY,
        chases=list(chases),
        schedule=schedule,
        country="DE",
        paused=paused,
        now=now,
    )


def test_the_chase_plan() -> None:
    first = plan(now=MONDAY)
    assert (first.state, first.due_at, first.used) == (
        "scheduled",
        datetime(2026, 10, 7, 9, 0, tzinfo=UTC),
        0,
    )

    sent_wed = ChaseRow(
        "sent", datetime(2026, 10, 7, 9, 5, tzinfo=UTC), datetime(2026, 10, 7, 10, 0, tzinfo=UTC)
    )
    second = plan(sent_wed, now=MONDAY)
    assert second.due_at == datetime(2026, 10, 9, 10, 0, tzinfo=UTC) and second.used == 1

    waiting = plan(ChaseRow("proposed", MONDAY, None), now=MONDAY)
    assert waiting.state == "awaiting_approval" and waiting.due_at is None

    rejected = ChaseRow("rejected", MONDAY, None)
    assert plan(rejected, rejected, rejected, now=MONDAY).state == "done"
    # Canceled ones do not count.
    assert plan(ChaseRow("canceled", MONDAY, None), now=MONDAY).used == 0


def test_a_chase_whose_draft_was_refused_is_used_not_retried_every_sweep() -> None:
    refused_wed = ChaseRow(
        "canceled", datetime(2026, 10, 7, 9, 5, tzinfo=UTC), None, "draft_refused"
    )
    after = plan(refused_wed, now=datetime(2026, 10, 7, 9, 20, tzinfo=UTC))
    # Not due again at the next sweep: the next chase follows the schedule from it.
    assert after.used == 1
    assert (after.state, after.due_at) == ("scheduled", datetime(2026, 10, 9, 9, 5, tzinfo=UTC))
    assert plan(refused_wed, refused_wed, refused_wed, now=MONDAY).state == "done"

    paused = plan(now=MONDAY, paused=datetime(2026, 10, 20, tzinfo=UTC))
    assert paused.state == "paused" and paused.due_at == datetime(2026, 10, 20, tzinfo=UTC)

    assert plan(now=MONDAY, invite_status="responded").state == "not_applicable"
    off = Schedule(enabled=False, first_after=2, every=2, max_chases=3)
    assert plan(now=MONDAY, schedule=off).state == "disabled"


def test_chases_never_before_the_rfqs_reply_by_date() -> None:
    # Quotes due Friday 16 Oct: no chase on Wednesday 7 Oct, but on the 16th, at 09:00.
    due = plan_chase(
        invite_status="sent",
        rfq_status="open",
        sent_at=MONDAY,
        chases=[],
        schedule=SCHEDULE,
        country="DE",
        paused=None,
        now=MONDAY,
        response_due=date(2026, 10, 16),
    )
    assert due.due_at == datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
    # A reply-by date on a weekend: the next business day.
    weekend = plan_chase(
        invite_status="sent",
        rfq_status="open",
        sent_at=MONDAY,
        chases=[],
        schedule=SCHEDULE,
        country="DE",
        paused=None,
        now=MONDAY,
        response_due=date(2026, 10, 17),
    )
    assert weekend.due_at == datetime(2026, 10, 19, 9, 0, tzinfo=UTC)
    # A reply-by date already past: the usual 2 business days.
    assert first_chase_due(
        sent_at=MONDAY, first_after=2, response_due=date(2026, 10, 1), country="DE"
    ) == datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


def test_a_reply_without_an_offer_does_not_stop_chasing() -> None:
    replied = datetime(2026, 10, 7, 15, 0, tzinfo=UTC)  # "we will send it next week"
    chasing = plan_chase(
        invite_status="responded",
        rfq_status="open",
        sent_at=MONDAY,
        chases=[],
        schedule=SCHEDULE,
        country="DE",
        paused=None,
        now=replied,
        last_reply_at=replied,
        awaiting_offer=True,
    )
    # Never right after it wrote: two business days after its reply.
    assert (chasing.state, chasing.due_at) == (
        "scheduled",
        datetime(2026, 10, 9, 15, 0, tzinfo=UTC),
    )
    offer = plan_chase(
        invite_status="responded",
        rfq_status="open",
        sent_at=MONDAY,
        chases=[],
        schedule=SCHEDULE,
        country="DE",
        paused=None,
        now=replied,
        awaiting_offer=False,
    )
    assert offer.state == "not_applicable"


def request(**changes: object) -> OpenRequest:
    base = OpenRequest(
        kind="clarification",
        sent_at=datetime(2026, 10, 9, 14, 0, tzinfo=UTC),  # Friday
        reply_by=date(2026, 10, 14),
        message_row_id=uuid4(),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_one_reminder_the_business_day_after_the_reply_by_date() -> None:
    # Brazil: Monday 12 Oct is a holiday; reply by Wednesday 14 -> remind Thursday 15.
    assert plan_reminder(request(), country="BR", every=2, last_reply_at=None) == datetime(
        2026, 10, 15, 14, 0, tzinfo=UTC
    )
    # Reminded once already (sent, waiting or turned down): never again.
    assert plan_reminder(request(reminded=True), country="BR", every=2, last_reply_at=None) is None
    # Nothing to refer to, or no date asked: no reminder.
    assert plan_reminder(request(reply_by=None), country="BR", every=2, last_reply_at=None) is None
    assert (
        plan_reminder(request(message_row_id=None), country="BR", every=2, last_reply_at=None)
        is None
    )
    # The supplier wrote meanwhile without answering ("tomorrow"): give it two business days.
    wrote = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
    assert plan_reminder(request(), country="BR", every=2, last_reply_at=wrote) == datetime(
        2026, 10, 19, 9, 0, tzinfo=UTC
    )


def test_a_complete_offer_has_no_open_items() -> None:
    line = RequestedLine(id=uuid4(), position=1, description="MLCC", mpn=None)
    complete = OfferedLine(
        rfq_line_id=line.id,
        values={"unit_price": Decimal(1), "min_quantity": Decimal(10), "lead_time_days": 10},
        fields={},
    )
    # A second tier with a gap does not matter when one tier is complete.
    partial = OfferedLine(rfq_line_id=line.id, values={"unit_price": None}, fields={})
    assert (
        open_items(
            header_values={"currency": "EUR", "valid_until": "2026-12-31"},
            header_fields={},
            requested=[line],
            offered=[complete, partial],
        )
        == []
    )


def test_reply_subject_keeps_the_thread_and_the_marker() -> None:
    assert reply_subject("[RFQ-7] Request for quotation", "RFQ-7", "x") == (
        "Re: [RFQ-7] Request for quotation"
    )
    assert reply_subject("AW: [RFQ-7] Anfrage", "RFQ-7", "x") == "AW: [RFQ-7] Anfrage"
    assert reply_subject(None, "RFQ-7", "Capacitors") == "Re: [RFQ-7] Capacitors"
    assert reply_subject("Request\r\nfor quote", "RFQ-7", "x") == "Re: [RFQ-7] Request for quote"


def test_followup_drafts_are_checked() -> None:
    task = FollowupEmailInput(
        kind="chase",
        rfq_ref="RFQ-7",
        rfq_title="Caps",
        supplier_name="Elektron",
        supplier_language="de",
        chase_number=1,
    )
    ok = FollowupEmailOutput(body="Guten Tag, gibt es Neuigkeiten zu RFQ-7?")
    assert followup_email_problems(task, ok) == []
    bad = FollowupEmailOutput(body="Other offers are at 0,40 EUR.")
    assert followup_email_problems(task, bad) == ["rfq_ref_missing_in_body", "price_mentioned"]
    # A currency code ending one line and a numbered item on the next is no price.
    listed = FollowupEmailOutput(
        body="Zu RFQ-7 bitte noch:\n1. die Währung, z. B. EUR\n2. die Lieferzeit"
    )
    assert followup_email_problems(task, listed) == []
    assert followup_email_problems(task, FollowupEmailOutput(body="RFQ-7: 18,00 R$")) == [
        "price_mentioned"
    ]
