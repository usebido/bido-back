"""The interfaces the planner reads from other workstreams, and the email it decides."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from bido_back.negotiation.decision import (
    DecisionLine,
    QuotedTerms,
    build_decision,
    email_type,
    supplier_email,
    writer_task,
)
from bido_back.negotiation.planner import EmailKind, EmailPlan, PositionPlan
from bido_back.negotiation.signals import (
    ClarificationState,
    QuotedLeadTime,
    calendar_expected_delivery,
    extraction_expected_delivery,
    latest_order_date,
    offer_signals,
)
from bido_back.quotes.lead_time import LeadTime, encode_lead_time
from bido_back.supplier_email.builders import Header, RequestedLine
from bido_back.supplier_email.checks import GateContext, email_problems
from bido_back.supplier_email.locale import Locale
from bido_back.supplier_email.model import Signature
from bido_back.supplier_email.render import render

FRIDAY = date(2026, 10, 9)
SHAFT = uuid4()
BEARING = uuid4()


def stated(lead: LeadTime, *, offer_date: date = FRIDAY) -> dict[str, object]:
    """A proposal line's provenance as extraction v5 stores a lead time."""
    return {
        "lead_time_days": {"parsed": 19, "needs_review": False},
        "lead_time_terms": {"parsed": encode_lead_time(lead, offer_date)},
    }


def test_lead_times_are_read_as_stated_and_converted_from_the_order_date() -> None:
    # Twelve Brazilian business days from Friday 9/10 (12/10 is a holiday) land on 28/10.
    twelve = QuotedLeadTime(
        days=19,
        fields=stated(LeadTime(unit="business_days", amount=Decimal(12), holiday_calendar="BR")),
    )
    assert extraction_expected_delivery(twelve, FRIDAY) == (date(2026, 10, 28), True)
    # From a later order date it is counted again (never the stale day count of the offer).
    assert extraction_expected_delivery(twelve, date(2026, 10, 13)) == (date(2026, 10, 29), True)
    approximate = QuotedLeadTime(
        days=21,
        fields=stated(LeadTime(unit="weeks", amount=Decimal(3), approximate=True)),
    )
    assert extraction_expected_delivery(approximate, FRIDAY) == (date(2026, 10, 30), False)
    # A date stays that date, whenever the order is placed.
    by_date = QuotedLeadTime(
        days=9, fields=stated(LeadTime(unit="date", delivery_date=date(2026, 10, 18)))
    )
    assert extraction_expected_delivery(by_date, date(2026, 10, 12)) == (date(2026, 10, 18), True)
    # Before extraction v5 (or corrected by a person): calendar days, the fallback.
    legacy = QuotedLeadTime(days=12)
    assert extraction_expected_delivery(legacy, FRIDAY) == (FRIDAY + timedelta(days=12), True)
    assert calendar_expected_delivery(QuotedLeadTime(days=None), FRIDAY) is None


def test_the_latest_order_date_works_with_business_days() -> None:
    need_by = date(2026, 10, 22)
    calendar = QuotedLeadTime(days=7)
    assert latest_order_date(calendar_expected_delivery, calendar, need_by, FRIDAY) == date(
        2026, 10, 15
    )
    # Late whatever happens: no order date meets the need-by date.
    shafts = QuotedLeadTime(
        days=19,
        fields=stated(LeadTime(unit="business_days", amount=Decimal(12), holiday_calendar="BR")),
    )
    assert latest_order_date(extraction_expected_delivery, shafts, need_by, FRIDAY) is None
    bearings = QuotedLeadTime(
        days=9,
        fields=stated(LeadTime(unit="business_days", amount=Decimal(7), holiday_calendar="BR")),
    )
    latest = latest_order_date(extraction_expected_delivery, bearings, need_by, FRIDAY)
    assert latest is not None
    found = extraction_expected_delivery(bearings, latest)
    assert found is not None and found[0] <= need_by
    later = extraction_expected_delivery(bearings, latest + timedelta(days=1))
    assert later is not None and later[0] > need_by


def test_offer_signals_read_the_extractions_typed_signals() -> None:
    assert offer_signals({}).final_offer is False
    signals = offer_signals(
        {
            "_signals": {
                "final_offer": True,
                "final_offer_quote": "This is our best and final price.",
                "declined_lines": [{"rfq_line_position": 3, "quote": "We do not make these."}],
                "questions": [
                    {
                        "quote": "Which drawing revision?",
                        "topic": "technical",
                        "rfq_line_position": 2,
                        "grounded": True,
                    }
                ],
            },
            "_offer_state": {"technical_questions": ["Which drawing revision?"]},
        }
    )
    assert (signals.final_offer, signals.declined_positions, signals.questions) == (
        True,
        frozenset({3}),
        1,
    )
    # Questions the extraction kept only in the offer state still hold the offer.
    legacy = offer_signals({"_offer_state": {"technical_questions": ["Material?", "Finish?"]}})
    assert legacy.questions == 2 and not legacy.final_offer


def test_a_clarification_blocks_while_open() -> None:
    assert ClarificationState(to_ask=("valid_until",)).blocking
    assert ClarificationState(pending=True).blocking
    assert ClarificationState(awaiting=True).blocking
    assert not ClarificationState(exhausted=("pos2.lead_time",)).blocking


# --- The decision and the email it renders -------------------------------------------------

LINES = {
    SHAFT: DecisionLine(
        position=2,
        description="Eixo motor MTR-100",
        part_number="000102",
        quantity=Decimal("15.000"),
        unit="pcs",
        need_by=date(2026, 10, 22),
    ),
    BEARING: DecisionLine(
        position=1,
        description="Rolamento 6204",
        part_number="000101",
        quantity=Decimal("40"),
        unit="pcs",
        need_by=date(2026, 10, 22),
    ),
}
NEED_BY = date(2026, 10, 22)
REQUESTED = [
    RequestedLine(1, "Rolamento 6204", "000101", None, (Decimal(40),), "pcs", NEED_BY),
    RequestedLine(2, "Eixo motor MTR-100", "000102", None, (Decimal(15),), "pcs", NEED_BY),
]
FIRST_SUBJECT = "[RFQ-6] Solicitação de cotação: Componentes"
HEADER = Header(
    rfq_ref="RFQ-6",
    subject=f"Re: {FIRST_SUBJECT}",
    locale=Locale.of("pt", "BR"),
    supplier_name="MetalNova Teste Ltda",
    buyer_company="Biduuuuu",
    signature=Signature(name="Ana Souza", company="Biduuuuu"),
)
GATE = GateContext(
    thread_language="pt",
    first_subject=FIRST_SUBJECT,
    rfq_title="Componentes",
    other_suppliers=("Usinagem Alfa",),
)


def position(
    line: UUID,
    *,
    ask: str | None = None,
    best: bool = False,
    deliver_by: date | None = None,
    offered: str = "89.00",
) -> PositionPlan:
    return PositionPlan(
        rfq_line_id=line,
        ask_price=Decimal(ask) if ask is not None else None,
        best_price=best,
        deliver_by=deliver_by,
        offered_price=Decimal(offered),
        lead_time_days=19,
        offered_delivery=date(2026, 10, 28),
        delivery_exact=True,
        better_offer_exists=False,
        conceded=False,
    )


def plan(kind: EmailKind, *positions: PositionPlan, cutoff: datetime | None = None) -> EmailPlan:
    return EmailPlan(
        kind=kind,
        round=2 if kind == "bafo" else 1,
        reply_by=cutoff.date() if cutoff is not None else date(2026, 10, 14),
        cutoff_at=cutoff,
        order_date=FRIDAY,
        positions=positions,
        mention_competition=False,
    )


QUOTED = {
    SHAFT: QuotedTerms(
        unit_price=Decimal("89.00"), currency="BRL", lead_days=12, lead_basis="business"
    ),
    BEARING: QuotedTerms(unit_price=Decimal("21.50"), currency="BRL", lead_days=5),
}


def test_a_delivery_date_request_asks_to_deliver_by_the_need_by_date() -> None:
    decision = build_decision(
        plan("delivery_date", position(SHAFT, deliver_by=date(2026, 10, 22))),
        sequence=1,
        currency="BRL",
        offer_version=2,
        offer_received_on=FRIDAY,
        language="pt",
        lines=LINES,
        quoted=QUOTED,
    )
    [shaft] = decision.positions
    assert (shaft.deliver_by, shaft.need_by, shaft.target_unit_price) == (
        date(2026, 10, 22),
        date(2026, 10, 22),
        None,
    )
    assert (shaft.quoted_unit_price, shaft.quoted_lead_days, shaft.quoted_lead_basis) == (
        Decimal("89"),
        12,
        "business",
    )
    assert str(shaft.quantity) == "15"
    email = supplier_email(decision, header=HEADER, lines=REQUESTED)
    assert email_type(decision) == email.type == "delivery_date"
    assert [(a.kind, a.positions) for a in email.asks] == [("deliver_by", (2,))]
    rendered = render(email)
    assert email_problems(email, rendered, GATE) == []
    # A date, never "at most N days after order"; the supplier's quote as it wrote it.
    assert "Entrega necessária: até 22/10/2026" in rendered.text
    assert "Prazo necessário" not in rendered.text
    assert "R$\u00a089,00/pç · 12 dias úteis após o pedido" in rendered.text
    task = writer_task(
        decision,
        rfq_ref="RFQ-6",
        rfq_title="Componentes",
        supplier_name="MetalNova Teste Ltda",
        language=email.language,
    )
    assert (task.action, task.supplier_language, task.reply_by) == (
        "counter",
        "pt",
        date(2026, 10, 14),
    )
    [ask] = task.lines
    # The writer's contract takes days: from the order date to the deliver-by date.
    assert (ask.required_lead_time_days, ask.target_unit_price) == (13, None)
    assert "E+" not in task.model_dump_json()


def test_a_better_price_without_a_number_is_a_revised_offer_request() -> None:
    decision = build_decision(
        plan(
            "revised_offer",
            position(BEARING, best=True, offered="21.50"),
            position(SHAFT, ask="80.00", deliver_by=date(2026, 10, 22)),
        ),
        sequence=1,
        currency="BRL",
        offer_version=1,
        offer_received_on=FRIDAY,
        language="pt",
        lines=LINES,
        quoted=QUOTED,
    )
    email = supplier_email(decision, header=HEADER, lines=REQUESTED)
    assert email.type == "revised_offer" and email.round == 1
    assert [(a.kind, a.positions) for a in email.asks] == [
        ("revise_price", (1, 2)),
        ("deliver_by", (2,)),
    ]
    rendered = render(email)
    assert email_problems(email, rendered, GATE) == []
    # No number is invented for the line without a target; the other shows exactly ours.
    assert "R$ 80,00" in rendered.text
    assert "R$ 21,50" in rendered.text


def test_a_best_and_final_request_carries_the_common_cut_off() -> None:
    cutoff = datetime(2026, 10, 13, 23, 59, tzinfo=UTC)
    decision = build_decision(
        plan("bafo", position(SHAFT, ask="84.50"), cutoff=cutoff),
        sequence=3,
        currency="BRL",
        offer_version=3,
        offer_received_on=FRIDAY,
        language="pt",
        lines=LINES,
        quoted=QUOTED,
    )
    email = supplier_email(decision, header=HEADER, lines=REQUESTED)
    assert email.type == "bafo" and email.reply_by is not None
    assert (email.reply_by.day, email.reply_by.cutoff) == (date(2026, 10, 13), cutoff)
    assert [(a.kind, a.positions) for a in email.asks] == [("best_and_final", (2,))]
    rendered = render(email)
    assert email_problems(email, rendered, GATE) == []
    assert "23:59" in rendered.text and "R$ 84,50" in rendered.text


# --- The default clarification ledger -------------------------------------------------------


def test_an_unanswered_clarification_expires_into_the_buyers_items() -> None:
    from bido_back.db.models import (
        FollowupMessage,
        Invite,
        Proposal,
        ProposalLine,
        Rfq,
        RfqLine,
        Supplier,
    )
    from bido_back.negotiation import service
    from bido_back.negotiation.planner import LineTerms

    row = RfqLine(id=SHAFT, position=2, description="Eixo motor MTR-100", mpn=None)
    proposal = Proposal(id=uuid4(), version=1, currency="BRL", valid_until=None, fields={})
    offered = ProposalLine(
        proposal_id=proposal.id,
        rfq_line_id=SHAFT,
        unit_price=Decimal("89"),
        min_quantity=Decimal(15),
        lead_time_days=12,
        fields={},
    )
    sent_at = datetime(2026, 10, 9, 12, tzinfo=UTC)
    asked = FollowupMessage(
        kind="clarification", proposal_id=proposal.id, status="sent", sent_at=sent_at
    )
    item = service._Supplier(  # pyright: ignore[reportPrivateUsage]
        invite=Invite(status="responded"),
        supplier=Supplier(country="BR"),
        proposal=proposal,
        proposal_lines=[offered],
        followups=[asked],
    )
    loaded = service._Loaded(  # pyright: ignore[reportPrivateUsage]
        rfq=Rfq(),
        lines=[
            service._Line(  # pyright: ignore[reportPrivateUsage]
                row=row,
                terms=LineTerms(rfq_line_id=SHAFT, position=2, quantity=Decimal(15)),
                facts=LINES[SHAFT],
            )
        ],
        suppliers=[item],
        versions={},
        approvals={},
        waits={},
    )
    clarification = service._clarification  # pyright: ignore[reportPrivateUsage]
    # Two Brazilian business days to answer (12/10 is a holiday): until Wednesday 14/10.
    waiting = clarification(item, loaded, datetime(2026, 10, 14, 20, tzinfo=UTC))
    assert waiting.awaiting and waiting.awaiting_until is not None
    assert waiting.awaiting_until.date() == date(2026, 10, 14)
    # Unanswered past it: the validity is the buyer's now; the offer is negotiated as it is.
    expired = clarification(item, loaded, datetime(2026, 10, 15, 9, tzinfo=UTC))
    assert (expired.awaiting, expired.blocking, expired.exhausted) == (
        False,
        False,
        ("valid_until",),
    )
