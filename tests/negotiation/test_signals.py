"""The interfaces the planner reads from other workstreams, with their defaults."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4

from bido_back.followups.calendar import add_business_days
from bido_back.negotiation.decision import DecisionLine, build_decision, writer_task
from bido_back.negotiation.planner import EmailPlan, PositionPlan
from bido_back.negotiation.signals import (
    ClarificationState,
    QuotedLeadTime,
    calendar_expected_delivery,
    latest_order_date,
    offer_signals,
)

FRIDAY = date(2026, 10, 9)


def business_days(line: QuotedLeadTime, reference: date) -> tuple[date, bool] | None:
    """A stand-in for the extraction's helper: `days` business days in the supplier's
    country, approximate."""
    if line.days is None:
        return None
    start = add_business_days(datetime.combine(reference, time(12)), line.days, line.country)
    return start.date(), False


def test_the_default_reads_lead_times_as_calendar_days() -> None:
    line = QuotedLeadTime(days=12)
    assert calendar_expected_delivery(line, FRIDAY) == (FRIDAY + timedelta(days=12), True)
    assert calendar_expected_delivery(QuotedLeadTime(days=None), FRIDAY) is None


def test_the_latest_order_date_works_with_any_helper() -> None:
    need_by = date(2026, 10, 22)
    calendar = QuotedLeadTime(days=7)
    assert latest_order_date(calendar_expected_delivery, calendar, need_by, FRIDAY) == date(
        2026, 10, 15
    )
    # Twelve Brazilian business days from Friday 9/10 (12/10 is a holiday) land on 28/10:
    # late whatever happens, so there is no order date that meets the need-by date.
    shafts = QuotedLeadTime(days=12, country="BR")
    assert business_days(shafts, FRIDAY) == (date(2026, 10, 28), False)
    assert latest_order_date(business_days, shafts, need_by, FRIDAY) is None
    bearings = QuotedLeadTime(days=7, country="BR")
    latest = latest_order_date(business_days, bearings, need_by, FRIDAY)
    assert latest is not None
    found = business_days(bearings, latest)
    assert found is not None and found[0] <= need_by
    later = business_days(bearings, latest + timedelta(days=1))
    assert later is not None and later[0] > need_by


def test_offer_signals_read_the_offer_state() -> None:
    assert offer_signals({}).final_offer is False
    signals = offer_signals(
        {
            "_offer_state": {
                "final_offer": True,
                "declined_line_positions": [3, "2", True],
                "technical_questions": ["Which drawing revision?"],
            }
        }
    )
    assert (signals.final_offer, signals.declined_positions, signals.questions) == (
        True,
        frozenset({3}),
        1,
    )


def test_a_clarification_blocks_while_open() -> None:
    assert ClarificationState(to_ask=("valid_until",)).blocking
    assert ClarificationState(pending=True).blocking
    assert ClarificationState(awaiting=True).blocking
    assert not ClarificationState(exhausted=("pos2.lead_time",)).blocking


def test_the_stored_decision_is_what_the_writer_gets() -> None:
    line_id = uuid4()
    plan = EmailPlan(
        kind="delivery_date",
        round=1,
        reply_by=date(2026, 10, 14),
        cutoff_at=None,
        order_date=FRIDAY,
        positions=(
            PositionPlan(
                rfq_line_id=line_id,
                ask_price=None,
                best_price=False,
                deliver_by=date(2026, 10, 22),
                offered_price=Decimal("89.00"),
                lead_time_days=17,
                offered_delivery=date(2026, 10, 26),
                delivery_exact=False,
                better_offer_exists=False,
                conceded=False,
            ),
        ),
        mention_competition=False,
    )
    decision = build_decision(
        plan,
        sequence=1,
        currency="BRL",
        offer_version=2,
        offer_received_on=FRIDAY,
        language="pt",
        lines={
            line_id: DecisionLine(
                position=2,
                description="Eixo motor MTR-100",
                part_number="000102",
                quantity=Decimal("15.000"),
                unit="pcs",
                need_by=date(2026, 10, 22),
            )
        },
    )
    [position] = decision.positions
    assert (position.deliver_by, position.need_by, position.target_unit_price) == (
        date(2026, 10, 22),
        date(2026, 10, 22),
        None,
    )
    assert str(position.quantity) == "15"
    task = writer_task(
        decision,
        rfq_ref="RFQ-6",
        rfq_title="Componentes",
        buyer_company="Biduuuuu",
        buyer_name="Biduuuuu",
        supplier_name="MetalNova Teste Ltda",
        supplier_language="en",
    )
    # The language pinned in the decision wins over the supplier's current setting.
    assert (task.action, task.supplier_language, task.reply_by) == (
        "counter",
        "pt",
        date(2026, 10, 14),
    )
    [ask] = task.lines
    # The writer's v1 contract takes days: from the order date to the deliver-by date.
    assert (ask.required_lead_time_days, ask.target_unit_price) == (13, None)
    assert "E+" not in task.model_dump_json()
