"""The clarification ledger end to end, on a real RFQ thread with a pinned clock.

The production pieces run (intake, quote extraction with validation and offer
reconciliation, the follow-up sweep with the code-rendered emails, approvals, the
send tool) inside `evals.world.World`; only the models are scripted: extraction
returns a careful model's output for each supplier email (prompt v5,
`tests.quotes.rfq6`), the writer no note.

The RFQ-6 replay follows the real thread (2026-10-08, MetalNova Teste Ltda), with
the lead-time-to-date helper the product uses (`missing.DEFAULT_EXPECTED_DELIVERY`):
- `historical`: Bido's actual clarification (validity, quantity tier and lead time
  for every line) went out, the supplier answered everything;
- `new_rules`: the clarification the missing-field rules ask today: only whether
  the bearings and the shafts can arrive by 22 Oct ("approximately 10 business
  days" lands on 26 Oct).
Both end with the second reply applied as answers, nothing asked twice, no
duplicate a day later, and the shafts' late delivery (12 business days: 28 Oct)
typed for the negotiation's delivery-date request.
"""

from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.catalog.contracts import (
    ExtractedValue,
    LeadTimeValue,
    ProposalLineDraft,
    QuoteExtractionOutput,
    SourceRef,
)
from bido_back.agents.config import RunnerConfig
from bido_back.auth import Principal
from bido_back.corrections import service as corrections
from bido_back.corrections.schemas import UseCurrentOfferRequest
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    AuditEvent,
    ClarificationAsk,
    FollowupMessage,
    Invite,
    Membership,
    Proposal,
    ProposalLine,
    SupplierQuestion,
    Tenant,
    User,
)
from bido_back.followups import ledger, questions, service
from bido_back.followups import missing as missing_rules
from bido_back.followups.missing import OpenItem
from bido_back.followups.schemas import AnswerQuestionsRequest, DismissQuestionRequest
from bido_back.followups.send import SEND_SUPPLIER_ANSWER
from bido_back.mandates.service import AUTO_TOOLS
from bido_back.rfqs import service as rfq_service
from bido_back.rfqs.errors import Forbidden, NotFound
from bido_back.rfqs.service import NewRfqLine
from evals.harness import Env, MemoryStorage, new_company
from evals.personas import Persona
from evals.scripted import ScriptedModel
from evals.world import RfqRef, World
from tests.db.conftest import app_engine, migrator_engine
from tests.quotes.rfq6 import REPLY_1, REPLY_2, first_reply, second_reply

__all__ = ["app_engine", "migrator_engine"]

pytestmark = pytest.mark.db

# Friday 9 Oct 2026, 02:50 UTC: Monday 12 Oct is a holiday in Brazil.
START = datetime(2026, 10, 9, 2, 50, tzinfo=UTC)
NEED_BY = date(2026, 10, 22)
WIRE_NEED_BY = date(2026, 10, 26)
METALNOVA = Persona(
    key="metalnova",
    name="MetalNova Teste Ltda",
    language="en",
    country="BR",
    kind="distributor",
    quotes=(),
    domain="metalnova.example.test",
    contact="Sales Team",
)

NONE = ExtractedValue(value=None, source=None, confidence=0)


def ev(value: str, quote: str, confidence: float = 0.95) -> ExtractedValue:
    return ExtractedValue(
        value=value, source=SourceRef(kind="email_body", quote=quote), confidence=confidence
    )


def days(value: str, quote: str) -> LeadTimeValue:
    """A lead time in calendar days, as prompt v5 writes it."""
    return LeadTimeValue(
        value=value,
        source=SourceRef(kind="email_body", quote=quote),
        confidence=0.95,
        unit="calendar_days",
    )


def draft(position: int, **values: Any) -> ProposalLineDraft:
    fields: dict[str, Any] = {
        name: NONE
        for name in (
            "offered_mpn",
            "min_quantity",
            "unit_price",
            "unit",
            "tooling_cost",
            "moq",
            "origin_country",
            "qualification",
        )
    }
    fields.update(values)
    return ProposalLineDraft.model_validate({"rfq_line_position": position, **fields})


PARTS = {1: "000101", 2: "000102", 3: "000104"}
RFQ6_LINES = [
    NewRfqLine("Rolamento motor MTR-100", Decimal(70), "pcs", PARTS[1], need_by=NEED_BY),
    NewRfqLine("Eixo motor MTR-100", Decimal(15), "pcs", PARTS[2], need_by=NEED_BY),
    NewRfqLine("Fio de cobre esmaltado", Decimal("12.5"), "kg", PARTS[3], need_by=WIRE_NEED_BY),
]
NAMES = {1: "bearings", 2: "shafts", 3: "copper wire"}
PRICES = {1: "19.50", 2: "89.00", 3: "95.00"}
UNITS = {1: "piece", 2: "piece", 3: "kg"}


@dataclass
class Thread:
    """One RFQ to MetalNova on the eval world's pinned clock."""

    world: World
    model: ScriptedModel
    rfq: RfqRef

    @property
    def invite_id(self) -> UUID:
        return self.world.invites[METALNOVA.key]

    def at(self, delta: timedelta) -> None:
        self.world.at(START + delta)

    async def reply(self, body: str, truth: QuoteExtractionOutput, delta: timedelta) -> None:
        self.model.extraction[body.strip()] = truth
        self.at(delta)
        await self.world.deliver(METALNOVA, subject=f"Re: [{self.rfq.ref}]", body=body)
        await self.world.drain()

    async def sweep(self, delta: timedelta) -> service.SweepResult:
        self.at(delta)
        return await self.world.sweep_followups()

    async def followups(self) -> list[FollowupMessage]:
        async with tenant_session(self.world.engine, self.world.tenant_id) as session:
            return list(
                await session.scalars(
                    select(FollowupMessage)
                    .where(FollowupMessage.invite_id == self.invite_id)
                    .order_by(FollowupMessage.created_at, FollowupMessage.sequence)
                )
            )

    async def approve_pending(self) -> None:
        for row in await self.followups():
            if row.status == "proposed" and row.approval_id is not None:
                await self.world.approve(row.approval_id)

    async def asks(self) -> dict[tuple[int | None, str], str]:
        async with tenant_session(self.world.engine, self.world.tenant_id) as session:
            rows = await session.scalars(
                select(ClarificationAsk).where(ClarificationAsk.invite_id == self.invite_id)
            )
            return {(row.position, row.field): row.status for row in rows}

    async def latest(self) -> tuple[Proposal, dict[int, ProposalLine]]:
        async with tenant_session(self.world.engine, self.world.tenant_id) as session:
            proposal = await session.scalar(
                select(Proposal)
                .where(Proposal.invite_id == self.invite_id)
                .order_by(Proposal.version.desc())
                .limit(1)
            )
            assert proposal is not None
            positions = {line_id: p for p, line_id in self.rfq.lines.items()}
            lines: dict[int, ProposalLine] = {}
            for row in await session.scalars(
                select(ProposalLine).where(ProposalLine.proposal_id == proposal.id)
            ):
                if row.rfq_line_id is not None and row.rfq_line_id in positions:
                    lines[positions[row.rfq_line_id]] = row
            return proposal, lines

    async def state(self) -> ledger.SupplierFollowup:
        async with tenant_session(self.world.engine, self.world.tenant_id) as session:
            states = await ledger.supplier_followups(
                session,
                self.rfq.id,
                now=self.world.now,
                expected_delivery=missing_rules.DEFAULT_EXPECTED_DELIVERY,
            )
        return states[self.invite_id]

    async def body(self, followup: FollowupMessage) -> str:
        """The text of the email a follow-up proposes (its approval's arguments)."""
        async with tenant_session(self.world.engine, self.world.tenant_id) as session:
            approval = await session.get(Approval, followup.approval_id)
            assert approval is not None
            return str(approval.args["body"])

    def clarification_tasks(self) -> list[list[tuple[int | None, str, str]]]:
        return [
            [(i.position, i.field, i.question) for i in task.open_items]
            for task, _ in self.world.followup_writer.calls
            if task.kind == "clarification"
        ]


@pytest.fixture
def scripted() -> Iterator[ScriptedModel]:
    model = ScriptedModel()
    with model.installed():
        yield model


@pytest_asyncio.fixture
async def thread(app_engine: AsyncEngine, scripted: ScriptedModel) -> AsyncIterator[Thread]:
    env = Env(
        engine=app_engine,
        storage=MemoryStorage(),
        runner_config=RunnerConfig(),
        mode="mock",
        today=START.date(),
    )
    world = await World.create(env, "ledger", scripted)
    world.start, world.offset = START, timedelta(0)
    await world.supplier(METALNOVA)
    async with tenant_session(world.engine, world.tenant_id) as session:
        rfq, rows = await rfq_service.create_draft_rfq(
            session,
            world.principal,
            title="Cotação de componentes para motor MTR-100",
            response_due=date(2026, 10, 20),
            base_currency="BRL",
            lines=RFQ6_LINES,
        )
        ref = RfqRef(rfq.id, rfq.ref, {row.position: row.id for row in rows})
    await world.invite(ref, METALNOVA)
    prepared = await rfq_service.prepare_send(
        world.engine,
        world.principal,
        ref.id,
        world.email_writer,
        world.mailboxes,
        today=START.date(),
    )
    for item in prepared.approvals:
        await world.approve(item.approval_id)
    async with tenant_session(world.engine, world.tenant_id) as session:
        # Sent on the pinned clock: chases are scheduled from it.
        await session.execute(update(Invite).where(Invite.rfq_id == ref.id).values(sent_at=START))
    yield Thread(world, scripted, ref)


def legacy_items(lines: dict[int, UUID]) -> Callable[..., list[OpenItem]]:
    """What Bido actually asked on 2026-10-08: validity, and the quantity tier and the lead
    time of every line (the rules before the ledger)."""

    def items(*_: object, **__: object) -> list[OpenItem]:
        return [
            OpenItem(None, None, None, "valid_until", "value", "missing"),
            *(
                OpenItem(p, lines[p], f"line {p}", field, "value", "missing")
                for p in (1, 2, 3)
                for field in ("quantity", "lead_time")
            ),
        ]

    return items


async def test_rfq6_replay_the_historical_clarification_is_answered(
    thread: Thread, monkeypatch: pytest.MonkeyPatch
) -> None:
    await thread.reply(REPLY_1, first_reply(), timedelta(minutes=1))
    # Bido's real clarification #1 (the old rules), sent 3 minutes later.
    with monkeypatch.context() as patched:
        patched.setattr(ledger, "proposal_items", legacy_items(thread.rfq.lines))
        assert (await thread.sweep(timedelta(minutes=4))).clarifications == 1
    await thread.approve_pending()
    assert set((await thread.asks()).values()) == {"open"}

    await thread.reply(REPLY_2, second_reply(), timedelta(minutes=6))

    # The second reply is applied as answers: no 'other terms unchanged' needed, Net 30
    # and the MOQ carried over, nothing held.
    proposal, lines = await thread.latest()
    offer_state = proposal.fields["_offer_state"]
    assert offer_state["issues"] == [] and offer_state["reply_kind"] == "amendment"
    assert len(offer_state["answered_asks"]) == 7
    assert proposal.payment_terms == "Net 30"
    assert proposal.valid_until == date(2026, 10, 24)
    # 7, 12 and 8 business days from 9 Oct (12 Oct is a holiday), as calendar days.
    assert {p: line.lead_time_days for p, line in lines.items()} == {1: 12, 2: 19, 3: 13}
    assert {p: line.moq for p, line in lines.items()} == {
        1: Decimal(10),
        2: Decimal(10),
        3: Decimal(5),
    }
    # Every item it was asked is answered: validity (a duration), the quantity (one price
    # per line), the lead time (given).
    assert set((await thread.asks()).values()) == {"answered"}

    # Nothing is asked again: not now, not a day later (the duplicate RFQ-6 got).
    asked_before = len(thread.clarification_tasks())
    assert (await thread.sweep(timedelta(minutes=19))).clarifications == 0
    assert (await thread.sweep(timedelta(hours=24, minutes=20))).clarifications == 0
    assert len(thread.clarification_tasks()) == asked_before == 1
    assert [f.kind for f in await thread.followups()] == ["clarification"]

    state = await thread.state()
    assert state.to_ask == () and state.decisions == ()
    # The one targeted question left: the shafts' delivery date (12 business days from
    # 10 Oct is 28 Oct, after the 22 Oct need-by), typed for the negotiation's
    # delivery-date request, not a clarification.
    assert [(i.position, i.need_by, i.expected) for i in state.late] == [
        (2, NEED_BY, date(2026, 10, 28))
    ]
    assert state.phase == "nothing_to_ask"


async def test_rfq6_replay_under_the_new_rules(thread: Thread) -> None:
    await thread.reply(REPLY_1, first_reply(), timedelta(minutes=1))
    assert (await thread.sweep(timedelta(minutes=4))).clarifications == 1
    # Never the validity ("15 days"), never a quantity tier (one price per line), never the
    # lead time already given: only whether "approximately 10 business days" (26 Oct)
    # meets the bearings' and the shafts' 22 Oct (the wire is needed by 26 Oct).
    assert thread.clarification_tasks() == [
        [(1, "delivery_date", "deliver_by"), (2, "delivery_date", "deliver_by")]
    ]
    [clarification] = await thread.followups()
    assert clarification.reply_by == date(2026, 10, 14)  # two business days, 12 Oct a holiday
    # One question, rendered by code in the thread's language (Portuguese, the RFQ email's)
    # under the lead time each position quoted, with the reply-by date.
    body = await thread.body(clarification)
    assert "Vocês conseguem entregar as posições 1 e 2 até 22/10/2026?" in body
    assert body.count("Prazo informado: aprox. 10 dias úteis após o pedido") == 2
    assert "14/10/2026" in body
    await thread.approve_pending()

    await thread.reply(REPLY_2, second_reply(), timedelta(minutes=6))
    proposal, lines = await thread.latest()
    assert proposal.fields["_offer_state"]["issues"] == []
    assert {p: line.lead_time_days for p, line in lines.items()} == {1: 12, 2: 19, 3: 13}
    # Both answered: the bearings arrive on 21 Oct; the shafts' answer is a date after
    # the need-by (a "no", for the negotiation).
    assert await thread.asks() == {
        (1, "delivery_date"): "answered",
        (2, "delivery_date"): "answered",
    }

    assert (await thread.sweep(timedelta(minutes=19))).clarifications == 0
    assert (await thread.sweep(timedelta(hours=24, minutes=20))).clarifications == 0
    assert len(thread.clarification_tasks()) == 1
    state = await thread.state()
    assert [(i.position, i.expected) for i in state.late] == [(2, date(2026, 10, 28))]
    assert state.phase == "nothing_to_ask"


# --- One open request per supplier, and one reminder -------------------------------------------

INCOMPLETE = """Hello, our prices: BRL 19.50 per piece for the bearings (000101), BRL 89.00 per piece for the shafts (000102) and BRL 95.00 per kg for the copper wire (000104). Lead time 10 days. Net 30."""  # noqa: E501


def incomplete() -> QuoteExtractionOutput:
    """Prices and a lead time for the whole offer that meets every need-by date (19 Oct),
    no validity."""
    return QuoteExtractionOutput(
        is_quote=True,
        currency=ev("BRL", "BRL 19.50"),
        valid_until=NONE,
        incoterm=NONE,
        payment_terms=ev("Net 30", "Net 30"),
        lead_time=days("10", "Lead time 10 days"),
        lines=[
            draft(
                p,
                unit_price=ev(PRICES[p], f"BRL {PRICES[p]} per {UNITS[p]} for the {NAMES[p]}"),
                unit=ev(UNITS[p], f"per {UNITS[p]} for the {NAMES[p]}"),
            )
            for p in (1, 2, 3)
        ],
    )


NOT_AN_OFFER = "Hello, we received your questions and will answer tomorrow. Sales Team"


def acknowledgement() -> QuoteExtractionOutput:
    return QuoteExtractionOutput(
        is_quote=False,
        currency=NONE,
        valid_until=NONE,
        incoterm=NONE,
        payment_terms=NONE,
        reply_kind="clarification",
    )


async def test_one_open_request_then_one_reminder_after_its_reply_by(thread: Thread) -> None:
    await thread.reply(INCOMPLETE, incomplete(), timedelta(hours=1))
    assert (await thread.sweep(timedelta(hours=2))).clarifications == 1
    assert thread.clarification_tasks() == [[(None, "valid_until", "value")]]
    await thread.approve_pending()
    [clarification] = await thread.followups()
    assert clarification.reply_by == date(2026, 10, 14)

    # Waiting for the supplier: nothing else, and no reminder before its reply-by date.
    state = await thread.state()
    assert state.phase == "waiting_for_supplier"
    assert state.request is not None and state.request.kind == "clarification"
    assert (await thread.sweep(timedelta(days=4))).reminders == 0  # Tue 13 Oct
    assert (await thread.sweep(timedelta(days=5, hours=3))).reminders == 0  # 14 Oct, reply-by

    # The business day after, at the hour it went out: one reminder, asking the same.
    result = await thread.sweep(timedelta(days=6, hours=3))  # Thu 15 Oct, 05:50
    assert (result.reminders, result.clarifications) == (1, 0)
    [reminder] = [f for f in await thread.followups() if f.kind == "reminder"]
    assert reminder.regarding_message_id is not None and reminder.status == "proposed"
    task = thread.world.followup_writer.calls[-1][0]
    assert task.kind == "clarification" and task.reminder_of == date(2026, 10, 9)
    assert [(i.position, i.field) for i in task.open_items] == [(None, "valid_until")]
    await thread.approve_pending()

    # Once: never again, and after its own reply-by date the buyer decides.
    assert (await thread.sweep(timedelta(days=12))).reminders == 0
    state = await thread.state()
    assert [d.kind for d in state.decisions] == ["no_answer"]
    assert state.phase == "needs_your_decision"
    kinds = sorted((f.kind, f.status) for f in await thread.followups())
    assert kinds == [("clarification", "sent"), ("reminder", "sent")]


async def test_a_reply_without_an_offer_keeps_our_clarification_and_asks_nothing_twice(
    thread: Thread,
) -> None:
    await thread.reply(INCOMPLETE, incomplete(), timedelta(hours=1))
    await thread.sweep(timedelta(hours=2))
    # Proposed, not yet approved, when the supplier writes "we will answer tomorrow".
    await thread.reply(NOT_AN_OFFER, acknowledgement(), timedelta(hours=3))
    [clarification] = await thread.followups()
    assert clarification.status == "proposed"  # not killed by a reply without an offer
    await thread.approve_pending()
    [clarification] = await thread.followups()
    assert clarification.status == "sent"
    # No new offer version, the ask stays open.
    assert await thread.asks() == {(None, "valid_until"): "open"}
    proposal, _ = await thread.latest()
    assert proposal.version == 1

    # The supplier's next offer still has no validity: never asked twice, the buyer decides.
    await thread.reply(INCOMPLETE + " ", incomplete(), timedelta(days=1))
    assert await thread.asks() == {(None, "valid_until"): "unusable"}
    assert (await thread.sweep(timedelta(days=2))).clarifications == 0
    state = await thread.state()
    assert [(d.kind, d.field) for d in state.decisions] == [("unusable_answer", "valid_until")]
    assert state.phase == "needs_your_decision"


async def test_chases_wait_for_the_reply_by_and_survive_a_reply_without_an_offer(
    thread: Thread,
) -> None:
    # Quotes are due 20 Oct: no chase before.
    assert (await thread.sweep(timedelta(days=4))).chases == 0
    await thread.reply(NOT_AN_OFFER, acknowledgement(), timedelta(days=5))
    proposal_count = len(await thread.followups())
    assert proposal_count == 0
    state = await thread.state()
    assert state.phase == "awaiting_offer"
    # On the reply-by date (Tue 20 Oct, 02:50): the first chase, although it answered.
    assert (await thread.sweep(timedelta(days=11, minutes=1))).chases == 1
    [chase] = await thread.followups()
    # It asks for an answer by the RFQ's own date, today.
    assert chase.kind == "chase" and chase.reply_by == date(2026, 10, 20)
    assert "20/10/2026" in await thread.body(chase)


# --- Supplier questions ------------------------------------------------------------------------

QUESTION = (
    "Hello, before quoting: can you send the drawing of the shaft (000102) with its "
    "tolerances? Sales Team"
)


def question() -> QuoteExtractionOutput:
    return QuoteExtractionOutput(
        is_quote=False,
        currency=NONE,
        valid_until=NONE,
        incoterm=NONE,
        payment_terms=NONE,
        reply_kind="clarification",
        technical_questions=["Can you send the drawing of the shaft (000102)?"],
    )


async def test_a_question_waits_for_the_buyer_and_pauses_automatic_emails(
    thread: Thread,
) -> None:
    await thread.reply(QUESTION, question(), timedelta(hours=3))
    state = await thread.state()
    assert state.phase == "needs_your_answer"
    assert [q.text for q in state.open_questions] == [
        "Can you send the drawing of the shaft (000102)?"
    ]
    # Nothing automatic, even once a chase would be due.
    assert (await thread.sweep(timedelta(days=12))).chases == 0
    assert await thread.followups() == []

    answered = await questions.answer_questions(
        thread.world.engine,
        thread.world.principal,
        thread.rfq.id,
        thread.invite_id,
        AnswerQuestionsRequest(answer="The drawing is attached to the RFQ email. Best regards"),
    )
    async with tenant_session(thread.world.engine, thread.world.tenant_id) as session:
        approval = await session.get(Approval, answered.approval_id)
        assert approval is not None
        # Always a person's decision: no negotiation mandate can approve this tool.
        assert approval.tool == SEND_SUPPLIER_ANSWER and approval.tool not in AUTO_TOOLS
        assert approval.status == "pending"
        assert approval.args["body"] == "The drawing is attached to the RFQ email. Best regards"
        events = list(
            await session.scalars(
                select(AuditEvent.action).where(
                    AuditEvent.payload["invite_id"].astext == str(thread.invite_id)
                )
            )
        )
    assert {"rfq.supplier_question", "rfq.supplier_question_answered"} <= set(events)
    state = await thread.state()
    assert state.phase == "email_pending" and state.open_questions == ()

    # One email at a time: no chase while the answer waits for approval.
    assert (await thread.sweep(timedelta(days=12, hours=1))).chases == 0
    await thread.world.approve(answered.approval_id)
    sent = thread.world.outbox.to(METALNOVA.email)[-1]
    assert sent.body.startswith("The drawing is attached")
    assert sent.subject.startswith(f"Re: [{thread.rfq.ref}]")
    # Answered: the supplier is chased again on the schedule.
    assert (await thread.sweep(timedelta(days=15))).chases == 1


async def test_rejecting_the_answer_reopens_the_question_and_dismissing_closes_it(
    thread: Thread,
) -> None:
    await thread.reply(QUESTION, question(), timedelta(hours=3))
    answered = await questions.answer_questions(
        thread.world.engine,
        thread.world.principal,
        thread.rfq.id,
        thread.invite_id,
        AnswerQuestionsRequest(answer="See the drawing."),
    )
    from bido_back.approvals import decide_approval
    from bido_back.followups.send import FOLLOWUP_REJECT_HOOKS

    await decide_approval(
        thread.world.engine,
        thread.world.principal,
        answered.approval_id,
        "reject",
        jobs=thread.world.jobs,
        on_reject=FOLLOWUP_REJECT_HOOKS,
    )
    state = await thread.state()
    assert state.phase == "needs_your_answer"
    [question_id] = [q.id for q in state.open_questions]

    await questions.dismiss_question(
        thread.world.engine,
        thread.world.principal,
        thread.rfq.id,
        question_id,
        DismissQuestionRequest(reason="Answered by phone"),
    )
    state = await thread.state()
    assert state.open_questions == () and state.phase == "awaiting_offer"
    with pytest.raises(Exception, match="dismissed"):
        await questions.dismiss_question(
            thread.world.engine,
            thread.world.principal,
            thread.rfq.id,
            question_id,
            DismissQuestionRequest(),
        )


async def test_a_question_after_an_offer_pauses_the_clarification_until_answered(
    thread: Thread,
) -> None:
    await thread.reply(INCOMPLETE, incomplete(), timedelta(hours=1))
    await thread.sweep(timedelta(hours=2))
    question_after = QuoteExtractionOutput(
        is_quote=False,
        currency=NONE,
        valid_until=NONE,
        incoterm=NONE,
        payment_terms=NONE,
        reply_kind="clarification",
        technical_questions=["Do you need RoHS certificates?"],
    )
    body = "Do you need RoHS certificates? Sales Team"
    await thread.reply(body, question_after, timedelta(hours=3))
    [clarification] = await thread.followups()
    assert clarification.status == "canceled"
    assert clarification.canceled_reason == "supplier_question"
    assert await thread.asks() == {(None, "valid_until"): "withdrawn"}
    # The offer is held for the buyer (the negotiation waits too), and nothing is asked.
    proposal, _ = await thread.latest()
    assert proposal.fields["_offer_state"]["technical_questions"] == [
        "Do you need RoHS certificates?"
    ]
    assert (await thread.sweep(timedelta(hours=4))).clarifications == 0

    [open_question] = (await thread.state()).open_questions
    await questions.dismiss_question(
        thread.world.engine,
        thread.world.principal,
        thread.rfq.id,
        open_question.id,
        DismissQuestionRequest(),
    )
    proposal, _ = await thread.latest()
    assert proposal.fields["_offer_state"]["technical_questions"] == []
    assert proposal.fields["_offer_state"]["requires_clarification"] is False
    # Never asked (the email was canceled): asked now.
    assert (await thread.sweep(timedelta(hours=5))).clarifications == 1


# --- The buyer takes a held offer as shown -----------------------------------------------------

UPDATE = "Hello, the lead time is now 25 days. Sales Team"


def update_without_statement() -> QuoteExtractionOutput:
    return QuoteExtractionOutput(
        is_quote=True,
        reply_kind="amendment",
        currency=NONE,
        valid_until=NONE,
        incoterm=NONE,
        payment_terms=NONE,
        lines=[draft(1, lead_time=days("25", "the lead time is now 25 days"))],
    )


async def add_member(engine: AsyncEngine, tenant_id: UUID, role: str) -> Principal:
    async with tenant_session(engine, tenant_id) as session:
        user = User(clerk_user_id=f"ledger_{role}_{tenant_id.hex[:8]}", email=f"{role}@x.test")
        session.add(user)
        await session.flush()
        session.add(Membership(tenant_id=tenant_id, user_id=user.id, role=role))
        return Principal(tenant_id=tenant_id, user_id=user.id, role=role)  # type: ignore[arg-type]


async def test_use_the_current_offer_releases_its_holds_for_good(thread: Thread) -> None:
    world = thread.world
    await thread.reply(INCOMPLETE, incomplete(), timedelta(hours=1))
    # A partial update without "other terms unchanged": held for the buyer.
    await thread.reply(UPDATE, update_without_statement(), timedelta(hours=2))
    proposal, lines = await thread.latest()
    assert proposal.fields["_offer_state"]["issues"] == ["amendment_without_explicit_preservation"]
    assert all(line.needs_review for line in lines.values())
    state = await thread.state()
    assert [d.kind for d in state.decisions] == ["offer_held"]
    # Held: nothing is asked meanwhile.
    assert (await thread.sweep(timedelta(hours=3))).clarifications == 0

    member = await add_member(world.engine, world.tenant_id, "member")
    with pytest.raises(Forbidden):
        await corrections.use_current_offer(
            world.engine, member, proposal.id, UseCurrentOfferRequest()
        )
    view = await corrections.use_current_offer(
        world.engine, world.principal, proposal.id, UseCurrentOfferRequest(reason="checked")
    )
    assert view.holds == [] and view.released_at is not None and not view.needs_review
    proposal, lines = await thread.latest()
    assert not any(line.needs_review for line in lines.values())
    assert proposal.fields["_offer_state"]["released"]["issues"] == [
        "amendment_without_explicit_preservation"
    ]
    async with tenant_session(world.engine, world.tenant_id) as session:
        released = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "proposal.hold_released")
        )
        assert released is not None and released.actor_user_id == world.principal.user_id
        assert "checked" not in str(released.payload)  # ids and codes only
    # Released for good: a later reply that changes nothing (a question) rebuilds the
    # offer from every reply, and the old hold does not come back.
    await thread.reply(QUESTION, question(), timedelta(hours=4))
    proposal, _ = await thread.latest()
    assert proposal.version == 3
    assert proposal.fields["_offer_state"]["issues"] == []
    # Nothing left to release.
    with pytest.raises(Exception, match="not held"):
        await corrections.use_current_offer(
            world.engine, world.principal, proposal.id, UseCurrentOfferRequest()
        )


async def test_a_four_eyes_company_needs_a_second_person_to_release(
    thread: Thread, migrator_engine: AsyncEngine
) -> None:
    world = thread.world
    await thread.reply(INCOMPLETE, incomplete(), timedelta(hours=1))
    await thread.reply(UPDATE, update_without_statement(), timedelta(hours=2))
    proposal, _ = await thread.latest()
    # Four-eyes is set at creation; the app role cannot change tenants.
    async with tenant_session(migrator_engine, world.tenant_id) as session:
        tenant = await session.get(Tenant, world.tenant_id)
        assert tenant is not None
        tenant.allow_self_approval = False
    # The RFQ's own buyer (an owner) cannot; an approver who is someone else can.
    with pytest.raises(Forbidden):
        await corrections.use_current_offer(
            world.engine, world.principal, proposal.id, UseCurrentOfferRequest()
        )
    approver = await add_member(world.engine, world.tenant_id, "approver")
    view = await corrections.use_current_offer(
        world.engine, approver, proposal.id, UseCurrentOfferRequest()
    )
    assert view.holds == []


# --- Tenancy -----------------------------------------------------------------------------------


async def test_the_ledger_and_questions_are_invisible_to_another_tenant(
    thread: Thread, app_engine: AsyncEngine
) -> None:
    await thread.reply(INCOMPLETE, incomplete(), timedelta(hours=1))
    await thread.sweep(timedelta(hours=2))
    await thread.reply(QUESTION, question(), timedelta(hours=3))
    async with tenant_session(app_engine, thread.world.tenant_id) as session:
        own_asks = list(await session.scalars(select(ClarificationAsk.id)))
        own_questions = list(await session.scalars(select(SupplierQuestion)))
    assert own_asks and own_questions

    other = await new_company(thread.world.env, "ledger-other")
    async with tenant_session(app_engine, other.tenant_id) as session:
        assert list(await session.scalars(select(ClarificationAsk.id))) == []
        assert list(await session.scalars(select(SupplierQuestion.id))) == []
    with pytest.raises(NotFound):
        await questions.dismiss_question(
            app_engine, other, thread.rfq.id, own_questions[0].id, DismissQuestionRequest()
        )
    with pytest.raises(NotFound):
        await corrections.use_current_offer(
            app_engine,
            other,
            (await thread.latest())[0].id,
            UseCurrentOfferRequest(),
        )
    # Writing another tenant's row fails closed (forced RLS).
    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_session(app_engine, other.tenant_id) as session:
            session.add(
                SupplierQuestion(
                    tenant_id=thread.world.tenant_id,
                    invite_id=thread.invite_id,
                    message_id=own_questions[0].message_id,
                    ordinal=9,
                    question="x",
                )
            )
            await session.flush()


async def test_each_item_is_asked_at_most_once_by_the_database(thread: Thread) -> None:
    await thread.reply(INCOMPLETE, incomplete(), timedelta(hours=1))
    await thread.sweep(timedelta(hours=2))
    [clarification] = await thread.followups()
    with pytest.raises(DBAPIError, match="clarification_asks_item_key"):
        async with tenant_session(thread.world.engine, thread.world.tenant_id) as session:
            session.add(
                ClarificationAsk(
                    tenant_id=thread.world.tenant_id,
                    invite_id=thread.invite_id,
                    followup_id=clarification.id,
                    field="valid_until",
                    question="value",
                    status="proposed",
                )
            )
            await session.flush()


async def test_the_new_tables_force_row_level_security(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname IN ('clarification_asks', 'supplier_questions')"
                )
            )
        ).all()
        # Without a tenant, nothing is visible.
        counts = [
            await conn.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            for table in ("clarification_asks", "supplier_questions")
        ]
    assert sorted(rows) == [
        ("clarification_asks", True, True),
        ("supplier_questions", True, True),
    ]
    assert counts == [0, 0]
