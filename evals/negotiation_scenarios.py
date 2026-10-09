"""Negotiation scenarios: multi-stage supplier threads, run and graded step by step.

    uv run python -m evals.negotiation_scenarios --mock
    uv run python -m evals.negotiation_scenarios --live -k 1 [--scenario rfq6_replay]
        [--report-dir DIR] [--concurrency 4]

Each run of a scenario is a fresh company inside `evals.world.World`: the real
quote extraction and validation, the offer state, follow-ups (chases and
clarifications), the negotiation sweep under an approved mandate, the recipient
guard and the recording outbox, on a simulated clock. Sweeps run the way the
worker runs them (`FollowupSweepHandler`, `NegotiationSweepHandler`: each
sweep, then the mandate's automatic approval, then the sends), at the times a
step names. The timestamps the product reads back as "when" (follow-up and
negotiation emails sent, mandate decisions) are moved to the simulated clock
after each sweep, so business-day schedules and daily caps follow it.

The supplier side is scripted (`evals.negotiation_threads`): every number a
supplier writes is decided by code. A scenario is a sequence of steps
(supplier emails, sweeps at given times, the buyer closing), and after every
step design-independent properties are checked on what Bido sent and on what
the buyer can see:

* every Bido email (`email:*`): in the supplier's language for the whole
  thread, the RFQ reference in the subject, signed once, no competitor name or
  price, never the buyer's maximum, no accepting or ordering language, the
  numbers the code decided and no other amounts, and a reply-by date whenever
  it asks for an answer;
* per step: what is asked (and what never is), when Bido stops, what the
  offer state is, and what the Copilot tools tell the buyer
  (`get_negotiation_status`, `get_negotiation_outcome`, `what_is_missing`,
  `get_rfq_status`, `list_extra_offers`).

Steps go on after a failed check. Every failing check points at where its fix
belongs (`diagnosis`: the `GAPS` it shows, by check and scenario). Mock mode scripts the models
(`evals.negotiation_threads.ScenarioModel`: a correct extraction, the writers
following their released prompts); live mode runs the real extraction and
writer models on the same synthetic emails (OPENAI_API_KEY; set
AGNO_TELEMETRY=false). `EXPECTED_MOCK_FAILURES` lists the checks that fail on
the current code in mock mode (the tests mark them non-strict xfail).

Reports: evals/reports/<stamp>-negotiation-<mode>/ (report.md, report.json).
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal, cast
from uuid import UUID, uuid4

import holidays
from pydantic import JsonValue, ValidationError
from sqlalchemy import select, update

from bido_back.agents.catalog.contracts import FollowupEmailInput, NegotiationEmailInput
from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.agents.tools import AnyTool, ToolError
from bido_back.auth import Principal
from bido_back.db import create_engine, tenant_session
from bido_back.db.models import (
    Approval,
    Contact,
    FollowupMessage,
    Invite,
    MailboxConnection,
    MandateSend,
    Membership,
    Message,
    NegotiationMessage,
    Proposal,
    ProposalLine,
    Rfq,
    RfqLine,
    Supplier,
    SupplierContact,
    Tenant,
    User,
)
from bido_back.email.addresses import make_message_id
from bido_back.email.mime import build_mime
from bido_back.email.recipients import is_test_domain
from bido_back.email.types import OutboundEmail
from bido_back.followups.jobs import FollowupSweepHandler
from bido_back.mandates import service as mandates
from bido_back.mandates.schemas import ProposeMandateRequest
from bido_back.negotiation.jobs import NegotiationSweepHandler
from bido_back.negotiation.schemas import LineLimitsUpdate
from bido_back.rfqs import service as rfq_service
from bido_back.rfqs import suppliers as supplier_service
from bido_back.rfqs.agents import RfqAgents
from bido_back.rfqs.schemas import ContactIn, CreateSupplierRequest
from bido_back.rfqs.service import NewRfqLine
from bido_back.settings import get_settings
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import FOLLOWUPS_SWEEP, NEGOTIATION_SWEEP
from evals.harness import Env, MemoryStorage, Mode
from evals.negotiation_text import (
    amounts,
    commitment,
    compact,
    language_of,
    norm,
    numbers,
    signature_problem,
    text_items,
    words,
    written_dates,
)
from evals.negotiation_threads import (
    DE_FINAL,
    DE_OFFER,
    DE_REVISED,
    EXTRA_PART,
    EXTRA_PRICE,
    FINAL,
    OFFER,
    QUESTION_WORDS,
    REVISED,
    RFQ6_BUSINESS_DAYS,
    RFQ6_PRICES,
    RFQ6_SHAFT_CONFIRMED,
    SAME,
    Email,
    Part,
    Quoted,
    ScenarioModel,
    Thread,
    de_offer,
    pt_alternative,
    pt_answers,
    pt_completion,
    pt_lead_change,
    pt_offer,
    pt_offer_with_extra,
    pt_partial,
    pt_price_change,
    pt_question,
    q,
    rfq6_first_reply,
    rfq6_second_reply,
    rfq6_shaft_lead_time,
)
from evals.world import Captured, World

REPORTS: Final = Path(__file__).parent / "reports"
MAX_ROUNDS: Final = 3
# The buyer's name `evals.harness.new_company` puts on file.
DEFAULT_BUYER: Final = "Eval Buyer"
MINUTE: Final = timedelta(minutes=1)
HOUR: Final = timedelta(hours=1)
DAY: Final = timedelta(days=1)


# --- What a scenario is -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SupplierSpec:
    key: str
    name: str
    # The language every Bido email to this supplier must be in.
    language: str
    country: str | None
    contact: str = "Equipe comercial"
    # "api": the buyer's supplier master (country, language); "copilot": created by the
    # Copilot's add_supplier_contact, as the deployed RFQ-6 test created MetalNova.
    created: Literal["api", "copilot"] = "api"

    @property
    def email(self) -> str:
        return f"vendas@{self.key}.example.test"


@dataclass(frozen=True, slots=True)
class Say:
    """A supplier email delivered at `at` (from the moment the RFQ emails went out)."""

    at: timedelta
    supplier: str
    write: Callable[[Thread], Email]
    # Its Date header this much before delivery (an older revision delayed in transit).
    written_earlier: timedelta = timedelta(0)


@dataclass(frozen=True, slots=True)
class Tick:
    """The worker's sweeps at `at`: follow-ups, then negotiation, each with its sends."""

    at: timedelta


@dataclass(frozen=True, slots=True)
class Close:
    """The buyer closes the negotiation through the Copilot (`close_negotiation`)."""

    at: timedelta


Action = Say | Tick | Close


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    owner: str = "core"


@dataclass(slots=True)
class StepResult:
    key: str
    title: str
    checks: list[Check] = field(default_factory=list[Check])
    notes: dict[str, object] = field(default_factory=dict[str, object])
    emails: list[str] = field(default_factory=list[str])
    error: str | None = None
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None and all(c.ok for c in self.checks)

    def check(self, name: str, ok: bool, detail: str = "", *, owner: str = "core") -> bool:
        self.checks.append(Check(name, ok, "" if ok else detail[:700], owner))
        return ok

    def failing(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


Grader = Callable[["View", StepResult], None]


@dataclass(frozen=True, slots=True)
class StepSpec:
    key: str
    title: str
    actions: tuple[Action, ...] = ()
    grade: Grader | None = None


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    title: str
    rfq_title: str
    currency: str
    parts: tuple[Part, ...]
    suppliers: tuple[SupplierSpec, ...]
    steps: tuple[StepSpec, ...]
    response_due_days: int
    deadline_days: int
    # Graded right after the RFQ emails went out under the approved mandate.
    rfq_grade: Grader | None = None
    max_rounds: int = MAX_ROUNDS
    # The company's name and the buyer's own name (None: none on file, as in RFQ-6).
    company: str | None = None
    buyer_name: str | None = DEFAULT_BUYER

    def supplier(self, key: str) -> SupplierSpec:
        return next(s for s in self.suppliers if s.key == key)


# --- What happened ----------------------------------------------------------------------------


def _plain(value: Decimal | None) -> str:
    return "-" if value is None else format(value.normalize(), "f")


@dataclass(slots=True)
class SentEmail:
    """One email Bido sent, with what the code decided to ask in it."""

    index: int
    supplier: str
    to: tuple[str, ...]
    subject: str
    body: str
    at: datetime
    step: str
    # rfq | chase | clarification | counter | bafo | other
    kind: str
    round: int | None = None
    price_asks: dict[int, Decimal] = field(default_factory=dict[int, Decimal])
    lead_asks: dict[int, int] = field(default_factory=dict[int, int])
    # The date a delivery ask names ("deliver by"), when the decision has one.
    lead_dates: dict[int, date] = field(default_factory=dict[int, date])
    # A clarification's open items, (position, field, problem), from its writer task.
    items: list[tuple[int | None, str, str]] = field(
        default_factory=list[tuple[int | None, str, str]]
    )
    # The body is exactly a writer's draft (so `items` are what it was asked to say).
    drafted: bool = False

    @property
    def tag(self) -> str:
        return f"{self.supplier}#{self.index} {self.kind}" + (
            f" r{self.round}" if self.round else ""
        )


@dataclass(frozen=True, slots=True)
class OfferLine:
    price: Decimal | None
    lead: int | None
    review: bool
    alternative: bool
    mpn: str | None


@dataclass(slots=True)
class SupplierState:
    invite_status: str = "missing"
    negotiation_status: str = "open"
    version: int | None = None
    lines: dict[int, OfferLine] = field(default_factory=dict[int, OfferLine])
    unmatched: int = 0
    issues: tuple[str, ...] = ()
    extras: tuple[str, ...] = ()
    rounds: list[str] = field(default_factory=list[str])
    followups: list[str] = field(default_factory=list[str])
    held: list[str] = field(default_factory=list[str])
    pending: int = 0

    def text(self) -> str:
        lines = ", ".join(
            f"{p}={_plain(line.price)}/{line.lead}d"
            + (" review" if line.review else "")
            + (" alt" if line.alternative else "")
            for p, line in sorted(self.lines.items())
        )
        return (
            f"invite={self.invite_status} negotiation={self.negotiation_status} "
            f"v{self.version}: {lines or '-'}"
            + (f" unmatched={self.unmatched}" if self.unmatched else "")
            + (f" issues={list(self.issues)}" if self.issues else "")
            + (f" extras={list(self.extras)}" if self.extras else "")
            + (f" rounds={self.rounds}" if self.rounds else "")
            + (f" followups={self.followups}" if self.followups else "")
            + (f" held={self.held}" if self.held else "")
            + (f" pending_approvals={self.pending}" if self.pending else "")
        )


@dataclass(slots=True)
class Run:
    """One scenario in one fresh company."""

    env: Env
    scenario: Scenario
    world: World
    model: ScenarioModel | None
    tools: dict[str, AnyTool] = field(default_factory=dict[str, AnyTool])
    rfq_id: UUID | None = None
    ref: str = ""
    lines: dict[int, UUID] = field(default_factory=dict[int, UUID])
    invites: dict[str, UUID] = field(default_factory=dict[str, UUID])
    names: dict[str, str] = field(default_factory=dict[str, str])
    t0: datetime | None = None
    lead_limits: dict[int, int | None] = field(default_factory=dict[int, int | None])
    signers: tuple[str, ...] = ()
    emails: list[SentEmail] = field(default_factory=list[SentEmail])
    # Every unit price each supplier wrote (for the competitor and maximum checks).
    offered: dict[str, set[Decimal]] = field(default_factory=dict[str, set[Decimal]])
    deliveries: list[str] = field(default_factory=list[str])
    # When each supplier email arrived (supplier key, eval time).
    replies: list[tuple[str, datetime]] = field(default_factory=list[tuple[str, datetime]])
    stamped: set[tuple[str, UUID]] = field(default_factory=set[tuple[str, UUID]])

    @property
    def start(self) -> date:
        assert self.t0 is not None
        return self.t0.date()

    def part(self, position: int) -> Part:
        return next(p for p in self.scenario.parts if p.position == position)

    def need_by(self, position: int) -> date:
        return self.start + timedelta(days=self.part(position).need_by_days)

    def thread(self, key: str) -> Thread:
        spec = self.scenario.supplier(key)
        return Thread(self.ref, self.start, self.scenario.parts, spec.name, spec.contact)

    def ctx(self) -> ToolContext:
        principal = self.world.principal
        return ToolContext(tenant_id=principal.tenant_id, user_id=principal.user_id, run_id=uuid4())


@dataclass(slots=True)
class View:
    """What a step's checks read: the emails, the offers, and what the buyer sees."""

    run: Run
    step: str
    new: list[SentEmail]
    states: dict[str, SupplierState]
    rfq_negotiation: str
    copilot: dict[str, JsonValue]
    now: datetime

    def to(self, key: str, *kinds: str) -> list[SentEmail]:
        return [e for e in self.new if e.supplier == key and (not kinds or e.kind in kinds)]

    def sent(self, key: str, *kinds: str) -> list[SentEmail]:
        return [e for e in self.run.emails if e.supplier == key and (not kinds or e.kind in kinds)]

    def state(self, key: str) -> SupplierState:
        return self.states.get(key, SupplierState())

    def visible(self, key: str, *families: str) -> tuple[bool, str]:
        """Whether the Copilot's outputs state each of `families` for this supplier."""
        text = buyer_view_text(self, key)
        missing = [f for f in families if not any(p in text for p in VISIBLE[f])]
        if not missing:
            return True, ""
        return False, f"no {'/'.join(missing)} statement in what the buyer sees: {text[:400]!r}"


# --- What the buyer must be able to read --------------------------------------------------------

# Phrases (normalized: lower case, no accents, punctuation as spaces) that state a
# supplier's situation. A Copilot output passes when the supplier's entry (or, with one
# supplier, the RFQ-level notes) contains one phrase of each expected family.
VISIBLE: Final[dict[str, tuple[str, ...]]] = {
    "waiting_supplier": (
        "waiting for the supplier", "waiting for supplier", "awaiting the supplier",
        "awaiting supplier", "waiting for a reply", "waiting for their reply",
        "waiting for the reply", "awaiting reply", "awaiting a reply", "awaiting response",
        "awaiting the response", "waiting on supplier", "waiting on the supplier",
        "waiting supplier", "aguardando fornecedor", "aguardando o fornecedor",
        "aguardando resposta",
    ),
    "accepted": (
        "accepted", "within target", "in target", "meets the target", "aceito", "aceita",
        "dentro do alvo",
    ),
    "nothing_to_ask": (
        "nothing to ask", "nothing to send", "nothing was sent", "nothing sent", "no email",
        "no e mail", "no message", "no counter", "nothing to negotiate", "no further email",
        "nada a pedir", "nada foi enviado", "nenhum e mail",
    ),
    "needs_buyer": (
        "needs your decision", "your decision", "buyer decision", "buyer s decision",
        "decision by the buyer", "needs buyer", "requires buyer", "buyer to decide",
        "waiting for the buyer", "waiting for buyer", "awaiting the buyer", "awaiting buyer",
        "waiting for you", "needs your review", "requires review", "needs review by",
        "decisao do comprador", "sua decisao",
    ),
    "question": ("question", "pergunta", "duvida", *(w.lower() for w in QUESTION_WORDS)),
    "held": (
        "held", "awaiting approval", "awaiting your approval", "pending approval",
        "approval queue", "needs approval", "requires approval", "aguardando aprovacao",
    ),
    "chases_exhausted": (
        "no reply after", "no answer after", "no response after", "reminders sent",
        "reminder limit", "chases exhausted", "chase limit", "stopped chasing",
        "no more reminders", "max chases", "follow ups stopped", "followups stopped",
        "chases used", "lembretes",
    ),
    "alternative": ("alternative", "alternativa"),
    "final": (
        "final offer", "best and final", "last round", "round limit", "rounds used up",
        "melhor e ultima",
    ),
}  # fmt: skip


def _walk(value: JsonValue, out: list[str], prefix: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _walk(item, out, key)
    elif isinstance(value, list):
        for item in value:
            _walk(item, out, prefix)
    elif value is not None:
        out.append(f"{prefix} {value}")


def _entries(value: JsonValue, name: str, path: str = "") -> list[tuple[str, JsonValue]]:
    """The dicts (at any depth) with a string value equal to the supplier's name, with the
    keys leading to them ("awaiting_suppliers" says something about the supplier too)."""
    found: list[tuple[str, JsonValue]] = []
    if isinstance(value, dict):
        if any(isinstance(v, str) and v.strip().casefold() == name for v in value.values()):
            found.append((path, value))
        else:
            for key, item in value.items():
                found.extend(_entries(item, name, f"{path} {key}"))
    elif isinstance(value, list):
        for item in value:
            found.extend(_entries(item, name, path))
    return found


def buyer_view_text(view: View, key: str) -> str:
    """What the Copilot's read tools say about one supplier, normalized."""
    name = view.run.names.get(key, key).strip().casefold()
    texts: list[str] = []
    single = len(view.run.scenario.suppliers) == 1
    for output in view.copilot.values():
        for path, entry in _entries(output, name):
            texts.append(path)
            _walk(entry, texts)
        if single and isinstance(output, dict):
            for top in ("note", "status", "negotiation_status", "readiness"):
                item = output.get(top)
                if isinstance(item, str):
                    texts.append(f"{top} {item}")
    return words(" | ".join(texts))


# --- Business days (the golden side) -----------------------------------------------------------


@lru_cache(maxsize=16)
def _holidays(country: str, year: int) -> frozenset[date]:
    return frozenset(holidays.country_holidays(country, years=year).keys())


def is_business_day(day: date, country: str | None) -> bool:
    return day.weekday() < 5 and (country is None or day not in _holidays(country, day.year))


def add_business_days(start: datetime, days: int, country: str | None) -> datetime:
    current, left = start, days
    while left > 0:
        current += DAY
        if is_business_day(current.date(), country):
            left -= 1
    return current


# --- The world: setting a scenario up, acting, reading back ------------------------------------


def end_of(day: date) -> datetime:
    return datetime.combine(day, clock(23, 59), tzinfo=UTC)


async def invoke(run: Run, name: str, **args: object) -> dict[str, JsonValue]:
    """One Copilot tool, as the model would call it; errors as {"error": ...}."""
    tool = run.tools[name]
    try:
        output = await tool.invoke(run.ctx(), tool.parse_input(args))
    except (ToolError, ValidationError) as exc:
        return {"error": str(exc)[:300]}
    return cast(dict[str, JsonValue], output.model_dump(mode="json"))


def copilot_tools(run: Run) -> dict[str, AnyTool]:
    from bido_back.copilot.tools import CopilotToolDeps
    from bido_back.copilot.tools import copilot_tools as tools

    world = run.world
    deps = CopilotToolDeps(
        db=world.engine,
        agents=RfqAgents(intake=world.intake, email_writer=world.email_writer),
        mailboxes=world.mailboxes,
        storage=run.env.storage,
        today=lambda: world.today,
    )
    return {tool.name: tool for tool in tools(deps)}


async def _supplier(run: Run, spec: SupplierSpec) -> tuple[UUID, UUID]:
    world = run.world
    if spec.created == "api":
        view = await supplier_service.create_supplier(
            world.engine,
            world.principal,
            CreateSupplierRequest(
                name=spec.name,
                country=spec.country,
                language=spec.language,
                email_domain=spec.email.rpartition("@")[2],
                contacts=[ContactIn(name=spec.contact, email=spec.email)],
            ),
        )
        return view.id, view.contacts[0].id
    added = await invoke(run, "add_supplier_contact", supplier=spec.name, email=spec.email)
    if "error" in added:
        raise RuntimeError(f"add_supplier_contact failed: {added['error']}")
    async with tenant_session(world.engine, world.tenant_id) as session:
        row = (
            await session.execute(
                select(Supplier.id, Contact.id)
                .join(SupplierContact, SupplierContact.supplier_id == Supplier.id)
                .join(Contact, Contact.id == SupplierContact.contact_id)
                .where(Supplier.name == spec.name, Contact.email == spec.email)
            )
        ).first()
    if row is None:
        raise RuntimeError(f"{spec.name} was not created")
    return row[0], row[1]


async def _world(env: Env, scenario: Scenario, model: ScenarioModel | None) -> World:
    """A fresh company, as `World.create` makes it, under the scenario's company and buyer
    names when it sets them (the deployed RFQ-6 company had no buyer name on file)."""
    label = f"negotiation {scenario.name}"
    if scenario.company is None and scenario.buyer_name == DEFAULT_BUYER:
        return await World.create(env, label, model)
    tenant_id = uuid4()
    async with tenant_session(env.engine, tenant_id) as session:
        session.add(Tenant(id=tenant_id, name=scenario.company or f"Eval {label}"))
        await session.flush()
        user = User(
            clerk_user_id=f"eval_{tenant_id.hex[:12]}",
            email=f"buyer.{tenant_id.hex[:8]}@evals.example.test",
        )
        session.add(user)
        await session.flush()
        session.add(Membership(tenant_id=tenant_id, user_id=user.id, role="owner"))
        session.add(
            Contact(tenant_id=tenant_id, kind="buyer", name=scenario.buyer_name, email=user.email)
        )
        session.add(
            MailboxConnection(
                tenant_id=tenant_id,
                user_id=user.id,
                provider="google",
                email_address=user.email,
                token_ciphertext=b"eval-not-a-real-token",
                status="active",
            )
        )
        user_id, address = user.id, user.email
    world = World(env, Principal(tenant_id=tenant_id, user_id=user_id, role="owner"), model)
    world.outbox.addresses[user_id] = address
    world.buyer_email = address
    return world


async def setup(env: Env, scenario: Scenario, model: ScenarioModel | None) -> Run:
    """The company, its suppliers, the RFQ, the mandate (approved): the RFQ emails go out."""
    world = await _world(env, scenario, model)
    run = Run(env=env, scenario=scenario, world=world, model=model)
    run.tools = copilot_tools(run)
    async with tenant_session(world.engine, world.tenant_id) as session:
        tenant = await session.get(Tenant, world.tenant_id)
        company = tenant.name if tenant is not None else ""
    run.signers = tuple(n for n in (scenario.buyer_name, company) if n)
    # Every scenario starts at 09:00 UTC (06:00 in Brazil): the hours a step adds stay on
    # the same day whatever time the eval runs.
    morning = datetime.combine(world.today, clock(9), tzinfo=UTC)
    world.at(morning if morning > world.now else morning + DAY)
    today = world.today
    async with tenant_session(world.engine, world.tenant_id) as session:
        rfq, rows = await rfq_service.create_draft_rfq(
            session,
            world.principal,
            title=scenario.rfq_title,
            response_due=today + timedelta(days=scenario.response_due_days),
            base_currency=scenario.currency,
            lines=[
                NewRfqLine(
                    description=part.description,
                    quantity=part.quantity,
                    unit=part.unit,
                    part_number=part.part_number,
                    need_by=today + timedelta(days=part.need_by_days),
                )
                for part in scenario.parts
            ],
        )
        run.rfq_id, run.ref = rfq.id, rfq.ref
        run.lines = {row.position: row.id for row in rows}
    for spec in scenario.suppliers:
        supplier_id, contact_id = await _supplier(run, spec)
        invite = await rfq_service.add_invite(
            world.engine, world.principal, rfq.id, supplier_id, contact_id
        )
        run.invites[spec.key] = invite.id
        run.names[spec.key] = spec.name
        run.offered[spec.key] = set()
    proposed = await mandates.propose_mandate(
        world.engine,
        world.principal,
        rfq.id,
        ProposeMandateRequest(
            deadline=end_of(today + timedelta(days=scenario.deadline_days)),
            max_rounds=scenario.max_rounds,
            lines=[
                LineLimitsUpdate(
                    rfq_line_id=run.lines[part.position],
                    target_unit_price=part.target,
                    max_unit_price=part.maximum,
                )
                for part in scenario.parts
            ],
        ),
        writer=world.email_writer,
        mailboxes=world.mailboxes,
        resolver=world.resolver,
        now=world.now,
    )
    await world.approve(proposed.approval_id)
    run.t0 = world.now
    await stamp(run)
    async with tenant_session(world.engine, world.tenant_id) as session:
        # Sent now on the eval clock: chases are scheduled from `invites.sent_at`.
        await session.execute(
            update(Invite)
            .where(Invite.rfq_id == rfq.id, Invite.sent_at.is_not(None))
            .values(sent_at=run.t0)
        )
        await session.execute(
            update(Message)
            .where(Message.direction == "out")
            .values(occurred_at=run.t0, created_at=run.t0)
        )
    async with tenant_session(world.engine, world.tenant_id) as session:
        run.lead_limits = {
            row.position: row.max_lead_time_days
            for row in await session.scalars(select(RfqLine).where(RfqLine.rfq_id == rfq.id))
        }
    return run


async def stamp(run: Run) -> None:
    """Move the product's "when" timestamps written during the last sweep to the eval clock.

    Follow-up and negotiation emails (created, sent) and mandate decisions carry the
    database's wall clock; the schedules (next chase, daily cap) compare them with the
    simulated `now`.
    """
    world = run.world
    now = world.now
    async with tenant_session(world.engine, world.tenant_id) as session:
        for model, table in ((FollowupMessage, "followup"), (NegotiationMessage, "negotiation")):
            for row in await session.scalars(select(model)):
                if (table, row.id) not in run.stamped:
                    row.created_at = now
                    run.stamped.add((table, row.id))
                if row.sent_at is not None and (f"{table}:sent", row.id) not in run.stamped:
                    row.sent_at = now
                    run.stamped.add((f"{table}:sent", row.id))
        for send in await session.scalars(select(MandateSend)):
            if ("mandate", send.id) not in run.stamped:
                send.created_at = now
                run.stamped.add(("mandate", send.id))


async def tick(run: Run) -> None:
    world = run.world

    def envelope(kind: str) -> Envelope:
        return Envelope(type=kind, tenant_id=world.tenant_id, idempotency_key=f"eval:{uuid4()}")

    await FollowupSweepHandler(
        db=world.engine, writer=world.followup_writer, now=world.now, jobs=world.jobs
    )(envelope(FOLLOWUPS_SWEEP))
    await world.drain()
    await stamp(run)
    await NegotiationSweepHandler(
        db=world.engine, writer=world.negotiation_writer, now=world.now, jobs=world.jobs
    )(envelope(NEGOTIATION_SWEEP))
    await world.drain()
    await stamp(run)


async def deliver(run: Run, say: Say) -> None:
    """The supplier's email, in the thread of the last email it received from Bido."""
    world = run.world
    spec = run.scenario.supplier(say.supplier)
    email = say.write(run.thread(say.supplier))
    if run.model is not None:
        run.model.extraction[email.body.strip()] = email.truth
    for line in email.truth.lines:
        value = line.unit_price.value
        if value is not None:
            try:
                run.offered[say.supplier].add(Decimal(value).normalize())
            except InvalidOperation:
                pass
    received = world.outbox.to(spec.email)
    if not received:
        run.deliveries.append(f"{say.supplier}: no thread (never received an email)")
        return
    thread = received[-1]
    first = received[0].subject
    subject = (first if first.lower().startswith("re:") else f"Re: {first}") + email.subject_suffix
    outbound = OutboundEmail(
        from_name=f"{spec.contact} | {spec.name}",
        from_address=spec.email,
        to=(world.buyer_email,),
        subject=subject,
        text_body=email.body,
        message_id=make_message_id(spec.email.rpartition("@")[2]),
        in_reply_to=thread.message_id,
        references=(*thread.references, thread.message_id),
    )
    message = build_mime(outbound, now=world.now - say.written_earlier)
    result = await world.inbound.ingest_for_invite(
        message.as_bytes(), tenant_id=world.tenant_id, invite_id=run.invites[say.supplier]
    )
    if result.message_id is not None:
        async with tenant_session(world.engine, world.tenant_id) as session:
            await session.execute(
                update(Message).where(Message.id == result.message_id).values(created_at=world.now)
            )
    await world.drain()
    async with tenant_session(world.engine, world.tenant_id) as session:
        status = (
            await session.scalar(select(Message.status).where(Message.id == result.message_id))
            if result.message_id is not None
            else None
        )
    run.deliveries.append(
        f"{say.supplier} at +{_offset(run, world.now)}: {result.outcome}/{status}"
    )
    run.replies.append((say.supplier, world.now))


def _offset(run: Run, moment: datetime) -> str:
    assert run.t0 is not None
    delta = moment - run.t0
    return f"{delta.days}d{delta.seconds // 3600:02d}h{delta.seconds % 3600 // 60:02d}m"


async def act(run: Run, action: Action) -> None:
    world = run.world
    assert run.t0 is not None
    world.at(max(run.t0 + action.at, world.now))
    if isinstance(action, Say):
        await deliver(run, action)
    elif isinstance(action, Tick):
        await tick(run)
    else:
        closed = await invoke(run, "close_negotiation", rfq=run.ref)
        if "error" in closed:
            raise RuntimeError(f"close_negotiation failed: {closed['error']}")


# --- Reading back ------------------------------------------------------------------------------


def _decimal(value: object) -> Decimal | None:
    if not isinstance(value, str | int | float):
        return None
    try:
        return Decimal(str(value)).normalize()
    except InvalidOperation:
        return None


async def classify(run: Run, captured: Sequence[Captured], step: str) -> list[SentEmail]:
    world = run.world
    by_address = {run.scenario.supplier(k).email: k for k in run.invites}
    lines = [(p.position, p.description, p.part_number) for p in run.scenario.parts]
    positions = {line_id: position for position, line_id in run.lines.items()}
    async with tenant_session(world.engine, world.tenant_id) as session:
        followup_rows = list(
            await session.scalars(
                select(FollowupMessage).order_by(FollowupMessage.created_at, FollowupMessage.id)
            )
        )
        negotiation_rows = list(
            await session.scalars(
                select(NegotiationMessage).order_by(
                    NegotiationMessage.created_at, NegotiationMessage.id
                )
            )
        )
    followups = {r.message_id_header: r for r in followup_rows if r.message_id_header}
    negotiations = {r.message_id_header: r for r in negotiation_rows if r.message_id_header}
    # Code renders every email; the writer is asked for its optional note once per proposed
    # email, just before the proposal is recorded. So a supplier's n-th writer call (per
    # follow-up kind) is the task of its n-th proposed row.
    names = {invite_id: run.names[key] for key, invite_id in run.invites.items()}
    followup_queues: dict[tuple[str, str], list[FollowupEmailInput]] = defaultdict(list)
    for task, _ in world.followup_writer.calls:
        followup_queues[(task.supplier_name, task.kind)].append(task)
    followup_tasks: dict[UUID, FollowupEmailInput] = {}
    for row in followup_rows:
        queue = followup_queues.get((names.get(row.invite_id, ""), row.kind))
        if queue:
            followup_tasks[row.id] = queue.pop(0)
    negotiation_queues: dict[str, list[NegotiationEmailInput]] = defaultdict(list)
    for task, _ in world.negotiation_writer.calls:
        negotiation_queues[task.supplier_name].append(task)
    negotiation_tasks: set[UUID] = set()
    for row in negotiation_rows:
        queue = negotiation_queues.get(names.get(row.invite_id, ""))
        if queue:
            queue.pop(0)
            negotiation_tasks.add(row.id)
    result: list[SentEmail] = []
    for item in captured:
        key = next((by_address[a] for a in item.to if a in by_address), "")
        earlier = [e for e in run.emails if e.supplier == key]
        email = SentEmail(
            index=len(run.emails) + len(result) + 1,
            supplier=key,
            to=item.to,
            subject=item.subject,
            body=item.body,
            at=item.at,
            step=step,
            kind="other",
        )
        followup = followups.get(item.message_id)
        negotiation = negotiations.get(item.message_id)
        body = item.body.strip()
        if followup is not None:
            email.kind = followup.kind
            task = followup_tasks.get(followup.id)
            email.drafted = task is not None
            if task is not None:
                email.items = [(i.position, i.field, i.problem) for i in task.open_items]
            elif followup.kind == "clarification":
                email.items = [(p, f, "read from the text") for p, f in text_items(body, lines)]
        elif negotiation is not None:
            email.kind = negotiation.action
            email.round = negotiation.round
            email.drafted = negotiation.id in negotiation_tasks
            raw = cast(dict[str, JsonValue], negotiation.terms).get("lines")
            for entry in raw if isinstance(raw, list) else []:
                if not isinstance(entry, dict):
                    continue
                line_id = entry.get("rfq_line_id")
                try:
                    position = positions.get(UUID(str(line_id)))
                except ValueError:
                    position = None
                if position is None:
                    continue
                ask = _decimal(entry.get("ask"))
                if ask is not None:
                    email.price_asks[position] = ask
                lead = entry.get("ask_lead_days")
                if isinstance(lead, int) and not isinstance(lead, bool):
                    email.lead_asks[position] = lead
                deliver_by = entry.get("deliver_by")
                if isinstance(deliver_by, str):
                    try:
                        email.lead_dates[position] = date.fromisoformat(deliver_by)
                    except ValueError:
                        pass
        elif not earlier and not [e for e in result if e.supplier == key]:
            email.kind = "rfq"
        result.append(email)
    return result


async def supplier_state(run: Run, key: str) -> SupplierState:
    world = run.world
    invite_id = run.invites[key]
    positions = {line_id: position for position, line_id in run.lines.items()}
    state = SupplierState()
    async with tenant_session(world.engine, world.tenant_id) as session:
        invite = await session.get(Invite, invite_id)
        if invite is None:
            return state
        state.invite_status = invite.status
        state.negotiation_status = invite.negotiation_status
        proposal = await session.scalar(
            select(Proposal)
            .where(Proposal.invite_id == invite_id)
            .order_by(Proposal.version.desc())
            .limit(1)
        )
        if proposal is not None:
            state.version = proposal.version
            for row in await session.scalars(
                select(ProposalLine).where(ProposalLine.proposal_id == proposal.id)
            ):
                position = positions.get(row.rfq_line_id) if row.rfq_line_id else None
                if position is None:
                    state.unmatched += 1
                    continue
                state.lines[position] = OfferLine(
                    row.unit_price.normalize() if row.unit_price is not None else None,
                    row.lead_time_days,
                    row.needs_review,
                    row.is_alternative,
                    row.offered_mpn,
                )
            offer = proposal.fields.get("_offer_state")
            if isinstance(offer, dict):
                issues = cast(dict[str, object], offer).get("issues")
                if isinstance(issues, list):
                    state.issues = tuple(str(i) for i in cast(list[object], issues))
            raw = proposal.fields.get("extra_offers")
            if isinstance(raw, list):
                for entry in cast(list[object], raw):
                    if isinstance(entry, dict):
                        extra = cast(dict[str, object], entry)
                        state.extras += (f"{extra.get('mpn') or ''} {extra.get('description')}",)
        for row in await session.scalars(
            select(NegotiationMessage)
            .where(NegotiationMessage.invite_id == invite_id)
            .order_by(NegotiationMessage.round)
        ):
            reason = f"({row.canceled_reason})" if row.canceled_reason else ""
            state.rounds.append(f"r{row.round} {row.action} {row.status}{reason}")
        for row in await session.scalars(
            select(FollowupMessage)
            .where(FollowupMessage.invite_id == invite_id)
            .order_by(FollowupMessage.created_at)
        ):
            reason = f"({row.canceled_reason})" if row.canceled_reason else ""
            state.followups.append(f"{row.kind}#{row.sequence} {row.status}{reason}")
        for tool, reason in (
            await session.execute(
                select(MandateSend.tool, MandateSend.reason).where(
                    MandateSend.invite_id == invite_id, MandateSend.outcome == "held"
                )
            )
        ).all():
            state.held.append(f"{tool}:{reason}")
        for args in await session.scalars(
            select(Approval.args).where(
                Approval.status == "pending", Approval.session_id == f"rfq:{run.rfq_id}"
            )
        ):
            if str(args.get("invite_id")) == str(invite_id):
                state.pending += 1
    return state


COPILOT_READS: Final = (
    "get_negotiation_status",
    "get_negotiation_outcome",
    "what_is_missing",
    "get_rfq_status",
    "list_extra_offers",
)


async def snapshot(run: Run, step: str, before: int) -> View:
    world = run.world
    new = await classify(run, world.outbox.sent[before:], step)
    run.emails.extend(new)
    states = {key: await supplier_state(run, key) for key in run.invites}
    async with tenant_session(world.engine, world.tenant_id) as session:
        rfq = await session.get(Rfq, run.rfq_id)
        status = rfq.negotiation_status if rfq is not None else "missing"
    copilot: dict[str, JsonValue] = {}
    for name in COPILOT_READS:
        copilot[name] = await invoke(run, name, rfq=run.ref)
    return View(run, step, new, states, status, copilot, world.now)


# --- Checks on every email ---------------------------------------------------------------------

_ASKING: Final = ("rfq", "chase", "clarification", "counter", "bafo")


def _quoted(values: set[Decimal]) -> str:
    return ", ".join(sorted(str(v) for v in values))


def grade_emails(view: View, step: StepResult) -> None:
    """The properties every email Bido sends must have, over this step's emails."""
    run = view.run
    if not view.new:
        return
    problems: dict[str, list[str]] = defaultdict(list)
    targets = norm(p.target for p in run.scenario.parts)
    maxima = norm(p.maximum for p in run.scenario.parts)
    for email in view.new:
        tag = email.tag
        if not email.supplier:
            problems["recipient"].append(f"{tag} to {email.to}")
            continue
        spec = run.scenario.supplier(email.supplier)
        language = language_of(email.body)
        if language != spec.language:
            problems["language"].append(f"{tag} reads as {language}, want {spec.language}")
        if run.ref not in email.subject:
            problems["ref_in_subject"].append(f"{tag}: {email.subject!r}")
        signed = signature_problem(email.body, run.signers)
        if signed is not None:
            problems["single_signature"].append(f"{tag}: {signed}")
        committing = commitment(email.body)
        if committing is not None:
            problems["no_commitment"].append(f"{tag}: {committing!r}")
        own = run.offered.get(email.supplier, set())
        rivals = {k: v for k, v in run.offered.items() if k != email.supplier}
        rival_prices: set[Decimal] = set()
        for prices in rivals.values():
            rival_prices |= prices
        rival_prices -= own | targets
        named = [
            run.names[k]
            for k in rivals
            if compact(_core(run.names[k])) and compact(_core(run.names[k])) in compact(email.body)
        ]
        written = numbers(email.body)
        leaked = written & rival_prices
        if named or leaked:
            problems["no_competitor"].append(f"{tag}: names {named} prices {_quoted(leaked)}")
        revealed = written & (maxima - own - targets)
        if revealed:
            problems["no_maximum"].append(f"{tag}: {_quoted(revealed)}")
        money = amounts(email.body)
        if email.kind in ("counter", "bafo"):
            asked = norm(email.price_asks.values())
            missing = [str(a) for a in asked if a not in written]
            dated = written_dates(email.body, email.at.date())
            for position, days in email.lead_asks.items():
                by = email.lead_dates.get(position)
                if by is not None:
                    # A delivery ask names the date ("deliver by"), not a day count.
                    if by not in dated:
                        missing.append(f"by {by}")
                elif Decimal(days) not in written:
                    missing.append(f"{days} days")
            allowed = asked | own
            unknown = [r for r in money if not r & allowed]
            if missing or unknown:
                problems["decided_numbers"].append(
                    f"{tag}: asks not written {missing}, amounts not decided "
                    f"{[_quoted(r) for r in unknown]}"
                )
        elif money:
            problems["decided_numbers"].append(
                f"{tag}: amounts in an email that asks no price {[_quoted(r) for r in money]}"
            )
        if email.kind in _ASKING:
            sent_on = email.at.date()
            dates = written_dates(email.body, sent_on)
            if email.kind == "rfq":
                due = run.start + timedelta(days=run.scenario.response_due_days)
                ok = due in dates
            else:
                ok = any(sent_on <= d <= sent_on + timedelta(days=120) for d in dates)
            if not ok:
                problems["reply_by_date"].append(f"{tag}: dates {sorted(map(str, dates))}")
    for name, owner in EMAIL_CHECKS:
        step.check(f"email:{name}", not problems[name], "; ".join(problems[name][:4]), owner=owner)


def _supplier_status(view: View, key: str) -> str | None:
    """The supplier's status as get_negotiation_status shows it to the buyer."""
    output = view.copilot.get("get_negotiation_status")
    name = view.run.names.get(key, "").casefold()
    suppliers = output.get("suppliers") if isinstance(output, dict) else None
    for entry in suppliers if isinstance(suppliers, list) else []:
        if isinstance(entry, dict):
            supplier = entry.get("supplier_name")
            status = entry.get("status")
            if isinstance(supplier, str) and supplier.casefold() == name:
                return status if isinstance(status, str) else None
    return None


def grade_pacing(view: View, step: StepResult) -> None:
    """One organized email per supplier answer: whatever Bido needs to ask after a reply
    (missing information, a counter-offer) goes out together, not as separate emails.
    Chases (no answer yet) are their own schedule."""
    crowded: list[str] = []
    for key in view.run.invites:
        replies = sorted(t for k, t in view.run.replies if k == key)
        answers: dict[datetime, list[SentEmail]] = defaultdict(list)
        for email in view.sent(key):
            if email.kind in ("rfq", "chase"):
                continue
            before = [t for t in replies if t <= email.at]
            if before:
                answers[before[-1]].append(email)
        for reply, emails in answers.items():
            if len(emails) > 1 and any(e.step == view.step for e in emails):
                crowded.append(
                    f"{len(emails)} emails after {key}'s reply at +{_offset(view.run, reply)}: "
                    f"{[e.tag for e in emails]}"
                )
    if any(view.run.replies):
        step.check("one_email_per_reply", not crowded, "; ".join(crowded), owner="followups")


def grade_status(view: View, step: StepResult) -> None:
    """What the buyer is told a supplier received matches what Bido actually sent: a
    supplier shown as "countered" got a counter-offer, one shown as "bafo_requested" got
    the best-and-final request."""
    wrong: list[str] = []
    for key in view.run.invites:
        status = _supplier_status(view, key)
        sent = {e.kind for e in view.sent(key)}
        if status == "countered" and "counter" not in sent:
            wrong.append(f"{key} shown as countered, no counter-offer was sent")
        if status == "bafo_requested" and "bafo" not in sent:
            wrong.append(f"{key} shown as bafo_requested, no best-and-final request was sent")
    step.check("status_matches_emails", not wrong, "; ".join(wrong), owner="visibility")


EMAIL_CHECKS: Final = (
    ("recipient", "core"),
    ("language", "language"),
    ("ref_in_subject", "writer"),
    ("single_signature", "writer"),
    ("no_commitment", "writer"),
    ("no_competitor", "negotiation"),
    ("no_maximum", "negotiation"),
    ("decided_numbers", "writer"),
    ("reply_by_date", "writer"),
)


def _core(name: str) -> str:
    """A company name without its legal form, for spotting it in a text."""
    kept = [
        w for w in name.split() if w.strip(".,").casefold() not in {"ltda", "gmbh", "s.a.", "sa"}
    ]
    return " ".join(kept)


# --- Helpers for the scenario checks -----------------------------------------------------------


def _items(emails: Sequence[SentEmail]) -> list[tuple[int | None, str, str]]:
    return [item for e in emails for item in e.items]


def _undrafted(emails: Sequence[SentEmail]) -> list[str]:
    return [e.tag for e in emails if not e.drafted]


def _negotiations(view: View, key: str) -> list[SentEmail]:
    return view.to(key, "counter", "bafo")


def _asks_text(emails: Sequence[SentEmail]) -> str:
    return "; ".join(
        f"{e.tag} price={ {p: str(v) for p, v in e.price_asks.items()} } lead={e.lead_asks}"
        for e in emails
    )


def check_offer(
    view: View, step: StepResult, key: str, prices: Mapping[int, Decimal], *, name: str
) -> None:
    """The supplier's current offer holds exactly these prices per line."""
    state = view.state(key)
    got = {p: line.price for p, line in state.lines.items()}
    want = {p: v.normalize() for p, v in prices.items()}
    step.check(name, got == want, f"got {got}, want {want}; {state.text()}", owner="extraction")


def check_usable(view: View, step: StepResult, key: str, *, name: str) -> None:
    """The current offer is not held for review (Bido can act on it)."""
    state = view.state(key)
    review = [p for p, line in state.lines.items() if line.review]
    step.check(
        name,
        not state.issues and not review,
        f"issues={list(state.issues)} lines needing review={review}",
        owner="extraction",
    )


def check_visible(view: View, step: StepResult, key: str, *families: str, name: str) -> None:
    ok, detail = view.visible(key, *families)
    step.check(name, ok, detail, owner="visibility")


def check_nothing_sent(view: View, step: StepResult, key: str, *, name: str, owner: str) -> None:
    sent = view.to(key)
    step.check(name, not sent, "; ".join(f"{e.tag}: {e.body[:160]!r}" for e in sent), owner=owner)


def check_asks(
    view: View,
    step: StepResult,
    key: str,
    *,
    prices: set[int],
    leads: set[int],
    name: str,
) -> None:
    """This step's negotiation emails ask a price on exactly `prices` and a lead time on
    exactly `leads`, every price below the offer and within the buyer's maximum
    (`name`); every lead time asked still meets the line's need-by date counted from the
    day it is asked (`lead_ask_meets_need_by`)."""
    emails = _negotiations(view, key)
    if not emails:
        step.check(name, False, "no negotiation email this step", owner="negotiation")
        return
    asked_prices = {p for e in emails for p in e.price_asks}
    asked_leads = {p for e in emails for p in e.lead_asks}
    problems: list[str] = []
    if asked_prices != prices:
        problems.append(f"price asked on {sorted(asked_prices)}, want {sorted(prices)}")
    if asked_leads != leads:
        problems.append(f"lead time asked on {sorted(asked_leads)}, want {sorted(leads)}")
    state = view.state(key)
    late: list[str] = []
    for email in emails:
        for position, ask in email.price_asks.items():
            part = view.run.part(position)
            offer = state.lines.get(position)
            if ask > part.maximum:
                problems.append(f"{email.tag} asks {ask} above the maximum on line {position}")
            if offer is not None and offer.price is not None and ask >= offer.price:
                problems.append(f"{email.tag} asks {ask}, not below the offer {offer.price}")
        for position, days in email.lead_asks.items():
            by = email.lead_dates.get(position)
            if by is not None:
                if by > view.run.need_by(position):
                    late.append(
                        f"{email.tag} asks delivery by {by} on line {position}: after the "
                        f"need-by date {view.run.need_by(position)}"
                    )
                continue
            left = (view.run.need_by(position) - email.at.date()).days
            if days > left:
                late.append(
                    f"{email.tag} asks at most {days} days on line {position} on "
                    f"{email.at.date()}: delivery after the need-by date "
                    f"{view.run.need_by(position)} ({left} days left)"
                )
    asked = f" [{_asks_text(emails)}]"
    step.check(name, not problems, "; ".join(problems) + asked, owner="negotiation")
    if asked_leads:
        step.check("lead_ask_meets_need_by", not late, "; ".join(late), owner="negotiation")


def outcome_lines(view: View) -> dict[int, tuple[str | None, Decimal | None, bool]]:
    """get_negotiation_outcome per line: (supplier, unit price, covered)."""
    output = view.copilot.get("get_negotiation_outcome")
    result: dict[int, tuple[str | None, Decimal | None, bool]] = {}
    lines = output.get("lines") if isinstance(output, dict) else None
    for line in lines if isinstance(lines, list) else []:
        if not isinstance(line, dict):
            continue
        position = line.get("position")
        supplier = line.get("supplier")
        if isinstance(position, int):
            result[position] = (
                supplier if isinstance(supplier, str) else None,
                _decimal(line.get("unit_price")),
                line.get("covered") is True,
            )
    return result


def check_outcome(
    view: View, step: StepResult, want: Mapping[int, tuple[str, Decimal]], *, name: str
) -> None:
    got = outcome_lines(view)
    wrong: list[str] = []
    for position, (key, price) in want.items():
        line = got.get(position)
        expected = view.run.names[key]
        if line is None or line[0] != expected or line[1] != price.normalize() or not line[2]:
            wrong.append(f"line {position}: got {line}, want {expected} at {price}")
    step.check(name, not wrong, "; ".join(wrong), owner="comparison")


def lateness(run: Run, position: int, business_days: int, country: str) -> bool:
    """Whether `business_days` from the RFQ day (an order placed at once, the supplier's
    calendar) miss the line's need-by date."""
    assert run.t0 is not None
    delivered = add_business_days(run.t0, business_days, country).date()
    return delivered > run.need_by(position)


# --- The parts the scenarios buy ---------------------------------------------------------------


def _part(position: int, part: str, description: str, qty: str, unit: str, need: int,
          target: str, maximum: str) -> Part:  # fmt: skip
    return Part(position, part, description, Decimal(qty), unit, need, Decimal(target),
                Decimal(maximum))  # fmt: skip


def mtr_parts(need: int = 21, wire_need: int = 25) -> tuple[Part, ...]:
    """The motor's bearing, shaft and copper wire, in BRL, with the buyer's internal limits."""
    return (
        _part(1, "000101", "Rolamento motor MTR-100", "70", "pcs", need, "45.67", "51.91"),
        _part(2, "000102", "Eixo motor MTR-100", "15", "pcs", need, "234.52", "266.28"),
        _part(3, "000104", "Fio de cobre esmaltado", "12.5", "kg", wire_need, "116.27",
              "135.30"),
    )  # fmt: skip


MTR: Final = mtr_parts()
# Within target on every line and on time (21/21/25 days).
WITHIN: Final = (q(1, "44.90", 10), q(2, "229.00", 12), q(3, "112.00", 14))


def br(key: str, name: str, contact: str = "Equipe comercial") -> SupplierSpec:
    return SupplierSpec(key, name, "pt", "BR", contact)


def say(
    at: timedelta,
    key: str,
    write: Callable[[Thread], Email],
    written_earlier: timedelta = timedelta(0),
) -> Say:
    return Say(at, key, write, written_earlier)


def offer(lines: Sequence[Quoted], opening: str = OFFER) -> Callable[[Thread], Email]:
    def write(t: Thread) -> Email:
        return pt_offer(t, lines, opening=opening)

    return write


# --- 1. rfq6_replay -----------------------------------------------------------------------------

RFQ6: Final = "metalnova"


def rfq6_clarification_items(view: View) -> list[tuple[int | None, str, str]]:
    return _items(view.to(RFQ6, "clarification"))


def rfq6_first(view: View, step: StepResult) -> None:
    check_offer(view, step, RFQ6, RFQ6_PRICES, name="offer_read")
    items = rfq6_clarification_items(view)
    asked = f"clarification items {items}; undrafted {_undrafted(view.to(RFQ6, 'clarification'))}"
    step.check(
        "no_validity_asked",
        not any(f == "valid_until" for _, f, _ in items),
        f"validity was given ('valid for 15 days'): {asked}",
        owner="followups",
    )
    step.check(
        "no_quantity_tier_asked",
        not any(f == "quantity" for _, f, _ in items),
        f"per-unit prices without tiers apply to the requested quantity: {asked}",
        owner="followups",
    )
    step.check(
        "no_answered_lead_time_asked",
        not any(f == "lead_time" and p in (1, 3) for p, f, _ in items),
        f"lines 1 and 3 have a lead time ('approximately 10 business days'): {asked}",
        owner="followups",
    )
    step.check(
        "no_answered_price_asked",
        not any(f in ("unit_price", "currency") for _, f, _ in items),
        asked,
        owner="followups",
    )
    priced = [e for e in _negotiations(view, RFQ6) if e.price_asks]
    step.check(
        "no_price_ask",
        not priced,
        f"prices are far below target: {_asks_text(priced)}",
        owner="negotiation",
    )


def rfq6_second(view: View, step: StepResult) -> None:
    check_offer(view, step, RFQ6, RFQ6_PRICES, name="offer_read")
    clarifications = view.to(RFQ6, "clarification")
    step.check(
        "nothing_asked_twice",
        not clarifications,
        f"everything was answered (validity, quantities, lead times per line): "
        f"{_items(clarifications)} {[e.body[:200] for e in clarifications]}",
        owner="followups",
    )
    late = {p for p, days in RFQ6_BUSINESS_DAYS.items() if lateness(view.run, p, days, "BR")}
    check_asks(view, step, RFQ6, prices=set(), leads=late, name="late_line_lead_time_asked")
    check_visible(view, step, RFQ6, "waiting_supplier", name="visible_waiting_for_supplier")


def rfq6_third(view: View, step: StepResult) -> None:
    check_nothing_sent(view, step, RFQ6, name="nothing_sent", owner="followups")
    check_visible(view, step, RFQ6, "accepted", "nothing_to_ask", name="visible_accepted")
    check_outcome(
        view,
        step,
        {p: (RFQ6, price) for p, price in RFQ6_PRICES.items()},
        name="outcome_every_line",
    )
    ready = view.rfq_negotiation == "ready_to_close"
    step.check("ready_to_close", ready, view.rfq_negotiation, owner="negotiation")


RFQ6_REPLAY: Final = Scenario(
    name="rfq6_replay",
    title="RFQ-6 replayed: Brazilian supplier, English thread, prices far below target",
    rfq_title="Cotação de componentes para motor MTR-100",
    currency="BRL",
    parts=mtr_parts(need=14, wire_need=18),
    suppliers=(
        SupplierSpec(RFQ6, "MetalNova Teste Ltda", "pt", None, "Sales Team", created="copilot"),
    ),
    response_due_days=12,
    deadline_days=12,
    company="Biduuuuu",
    buyer_name=None,
    steps=(
        StepSpec(
            "first_reply",
            "Prices per unit in BRL, 'valid for 15 days', Net 30, MOQ, ~10 business days",
            (say(MINUTE, RFQ6, rfq6_first_reply), Tick(4 * MINUTE)),
            rfq6_first,
        ),
        StepSpec(
            "second_reply",
            "Answers: validity, quantities, 7/12/8 business days (shaft late for need-by)",
            (say(6 * MINUTE, RFQ6, rfq6_second_reply), Tick(10 * MINUTE), Tick(DAY + 5 * MINUTE)),
            rfq6_second,
        ),
        StepSpec(
            "shaft_on_time",
            f"Shafts within {RFQ6_SHAFT_CONFIRMED} business days: complete, in target, on time",
            (
                say(DAY + 2 * HOUR, RFQ6, rfq6_shaft_lead_time),
                Tick(DAY + 2 * HOUR + 5 * MINUTE),
                Tick(2 * DAY + 5 * MINUTE),
            ),
            rfq6_third,
        ),
    ),
)


# --- 2. price_above_target_then_concession -----------------------------------------------------

PAULISTA, GAUCHA = "paulista", "gaucha"
GAUCHA_OFFER: Final = (q(1, "45.50", 10), q(2, "233.00", 12), q(3, "115.00", 14))
PAULISTA_ROUNDS: Final = (
    (q(1, "48.90", 10), q(2, "255.00", 12), q(3, "112.00", 14)),
    (q(1, "46.50", 10), q(2, "245.00", 12), q(3, "112.00", 14)),
    (q(1, "46.00", 10), q(2, "240.00", 12), q(3, "112.00", 14)),
    (q(1, "45.90", 10), q(2, "238.00", 12), q(3, "112.00", 14)),
)


def concession_first(view: View, step: StepResult) -> None:
    emails = _negotiations(view, PAULISTA)
    step.check("one_counter", len(emails) == 1, _asks_text(emails), owner="negotiation")
    check_asks(view, step, PAULISTA, prices={1, 2}, leads=set(), name="asks_above_target_only")
    check_nothing_sent(view, step, GAUCHA, name="within_target_not_asked", owner="negotiation")
    check_visible(view, step, GAUCHA, "accepted", "nothing_to_ask", name="visible_accepted")
    check_visible(view, step, PAULISTA, "waiting_supplier", name="visible_waiting_for_supplier")


def concession_next(view: View, step: StepResult) -> None:
    emails = _negotiations(view, PAULISTA)
    step.check("one_more_round", len(emails) == 1, _asks_text(emails), owner="negotiation")
    check_asks(view, step, PAULISTA, prices={1, 2}, leads=set(), name="asks_above_target_only")
    bafos = view.sent(PAULISTA, "bafo")
    step.check("bafo_at_most_once", len(bafos) <= 1, _asks_text(bafos), owner="negotiation")


def concession_final(view: View, step: StepResult) -> None:
    sent = view.sent(PAULISTA, "counter", "bafo")
    check_nothing_sent(view, step, PAULISTA, name="stops_after_final_offer", owner="negotiation")
    bafos = [e for e in sent if e.kind == "bafo"]
    step.check(
        "one_bafo_last",
        len(bafos) == 1 and sent[-1].kind == "bafo",
        f"rounds {[e.tag for e in sent]}",
        owner="negotiation",
    )
    rounds = view.run.scenario.max_rounds
    step.check("round_cap", len(sent) <= rounds, f"{len(sent)} > {rounds}", owner="negotiation")
    ok_buyer, detail_buyer = view.visible(PAULISTA, "needs_buyer")
    ok_final, detail_final = view.visible(PAULISTA, "final")
    step.check(
        "visible_final_offer",
        ok_buyer or ok_final,
        f"{detail_buyer}; {detail_final}",
        owner="visibility",
    )


def concession_close(view: View, step: StepResult) -> None:
    step.check("closed", view.rfq_negotiation == "closed", view.rfq_negotiation, owner="core")
    best = {1: (GAUCHA, Decimal("45.50")), 2: (GAUCHA, Decimal("233.00"))}
    check_outcome(view, step, {**best, 3: (PAULISTA, Decimal("112.00"))}, name="outcome")
    for key in (PAULISTA, GAUCHA):
        check_nothing_sent(view, step, key, name=f"silent_after_close:{key}", owner="core")


CONCESSION: Final = Scenario(
    name="price_above_target_then_concession",
    title="Above target, partial concessions, best-and-final, close (with a competitor)",
    rfq_title="Componentes do motor MTR-100",
    currency="BRL",
    parts=MTR,
    suppliers=(
        br(PAULISTA, "Usinagem Paulista Ltda"),
        br(GAUCHA, "Rolamentos Gaúcha Ltda"),
    ),
    response_due_days=10,
    deadline_days=8,
    steps=(
        StepSpec(
            "first_quotes",
            "A competitor within target; Paulista above target on lines 1 and 2",
            (
                say(2 * HOUR, GAUCHA, offer(GAUCHA_OFFER)),
                say(2 * HOUR + 10 * MINUTE, PAULISTA, offer(PAULISTA_ROUNDS[0])),
                Tick(2 * HOUR + 30 * MINUTE),
            ),
            concession_first,
        ),
        StepSpec(
            "partial_concession",
            "Paulista lowers both prices part of the way",
            (
                say(DAY + HOUR, PAULISTA, offer(PAULISTA_ROUNDS[1], REVISED)),
                Tick(DAY + 90 * MINUTE),
            ),
            concession_next,
        ),
        StepSpec(
            "more_concession",
            "Paulista lowers again, still above target",
            (
                say(2 * DAY + HOUR, PAULISTA, offer(PAULISTA_ROUNDS[2], REVISED)),
                Tick(2 * DAY + 90 * MINUTE),
            ),
            concession_next,
        ),
        StepSpec(
            "final_offer",
            "Paulista's best and final offer",
            (
                say(3 * DAY + HOUR, PAULISTA, offer(PAULISTA_ROUNDS[3], FINAL)),
                Tick(3 * DAY + 90 * MINUTE),
                Tick(4 * DAY + HOUR),
            ),
            concession_final,
        ),
        StepSpec(
            "close",
            "The buyer closes: the best offer per line",
            (Close(4 * DAY + 2 * HOUR), Tick(4 * DAY + 3 * HOUR)),
            concession_close,
        ),
    ),
)


# --- 3. supplier_asks_question --------------------------------------------------------------

EIXOS: Final = "eixos"


def question_asked(view: View, step: StepResult) -> None:
    check_nothing_sent(view, step, EIXOS, name="no_chase_or_invented_answer", owner="followups")
    check_visible(view, step, EIXOS, "question", name="visible_supplier_question")
    check_visible(view, step, EIXOS, "needs_buyer", name="visible_needs_buyer")


def question_then_quote(view: View, step: StepResult) -> None:
    check_offer(
        view, step, EIXOS, {quoted.position: quoted.price for quoted in WITHIN}, name="offer_read"
    )
    check_nothing_sent(view, step, EIXOS, name="nothing_to_ask", owner="negotiation")
    check_visible(view, step, EIXOS, "accepted", "nothing_to_ask", name="visible_accepted")


QUESTION: Final = Scenario(
    name="supplier_asks_question",
    title="A technical question and no prices: the buyer's to answer; no chase meanwhile",
    rfq_title="Componentes do motor MTR-100",
    currency="BRL",
    parts=MTR,
    suppliers=(br(EIXOS, "Eixos Precisão Ltda", "Engenharia de vendas"),),
    response_due_days=12,
    deadline_days=12,
    steps=(
        StepSpec(
            "question",
            "Drawing and material spec asked, nothing quoted; a week passes",
            (
                say(3 * HOUR, EIXOS, pt_question),
                Tick(3 * HOUR + 30 * MINUTE),
                *(Tick(d * DAY + MINUTE) for d in range(1, 8)),
            ),
            question_asked,
        ),
        StepSpec(
            "quote",
            "With the buyer's answer (outside Bido), a full quote within target",
            (say(8 * DAY + HOUR, EIXOS, offer(WITHIN)), Tick(8 * DAY + 90 * MINUTE)),
            question_then_quote,
        ),
    ),
)


# --- 4. partial_quote -----------------------------------------------------------------------

FIOSSUL: Final = "fiossul"


def partial_two(view: View, step: StepResult) -> None:
    status = view.rfq_negotiation
    step.check(
        "no_premature_close", status == "open", f"rfq negotiation {status}", owner="negotiation"
    )
    state = view.state(FIOSSUL)
    step.check(
        "supplier_not_final",
        state.negotiation_status not in ("accepted", "closed", "rejected"),
        state.text(),
        owner="negotiation",
    )
    asked = [e for e in _negotiations(view, FIOSSUL) if e.price_asks or e.lead_asks]
    step.check("quoted_lines_not_countered", not asked, _asks_text(asked), owner="negotiation")
    followups = view.to(FIOSSUL, "clarification", "chase")
    items = _items(followups)
    step.check(
        "asks_only_missing_line",
        all(p == 2 for p, _, _ in items) and not _undrafted(followups),
        f"items {items}; undrafted {_undrafted(followups)}",
        owner="followups",
    )
    check_visible(view, step, FIOSSUL, "waiting_supplier", name="visible_waiting_for_supplier")


def partial_complete(view: View, step: StepResult) -> None:
    prices = {quoted.position: quoted.price for quoted in WITHIN}
    check_offer(view, step, FIOSSUL, prices, name="offer_completed")
    check_usable(view, step, FIOSSUL, name="offer_usable")
    check_nothing_sent(view, step, FIOSSUL, name="nothing_to_ask", owner="negotiation")
    step.check(
        "ready_to_close",
        view.rfq_negotiation == "ready_to_close",
        view.rfq_negotiation,
        owner="negotiation",
    )
    check_visible(view, step, FIOSSUL, "accepted", "nothing_to_ask", name="visible_accepted")


def _partial(t: Thread) -> Email:
    return pt_partial(t, (WITHIN[0], WITHIN[2]), 2)


def _completion(t: Thread) -> Email:
    return pt_completion(t, WITHIN[1])


PARTIAL: Final = Scenario(
    name="partial_quote",
    title="Two of three lines now, the third tomorrow: no premature close, then complete",
    rfq_title="Componentes do motor MTR-100",
    currency="BRL",
    parts=MTR,
    suppliers=(br(FIOSSUL, "Fios e Cabos Sul Ltda"),),
    response_due_days=12,
    deadline_days=10,
    steps=(
        StepSpec(
            "two_of_three",
            "Lines 1 and 3 within target; the shaft promised for tomorrow",
            (say(3 * HOUR, FIOSSUL, _partial), Tick(3 * HOUR + 30 * MINUTE), Tick(6 * HOUR)),
            partial_two,
        ),
        StepSpec(
            "third_line",
            "The shaft's price alone, other conditions unchanged",
            (say(DAY + HOUR, FIOSSUL, _completion), Tick(DAY + 90 * MINUTE), Tick(2 * DAY)),
            partial_complete,
        ),
    ),
)


# --- 5. decline_then_alternative -----------------------------------------------------------

DELTA: Final = "delta"


def alternative_offered(view: View, step: StepResult) -> None:
    line = view.state(DELTA).lines.get(2)
    step.check(
        "alternative_recorded",
        line is not None and line.alternative,
        view.state(DELTA).text(),
        owner="extraction",
    )
    check_visible(view, step, DELTA, "alternative", name="visible_alternative")
    state = view.state(DELTA)
    step.check(
        "not_auto_accepted",
        state.negotiation_status != "accepted" and view.rfq_negotiation == "open",
        f"supplier {state.negotiation_status}, rfq {view.rfq_negotiation}",
        owner="negotiation",
    )
    asked = [
        e for e in view.sent(DELTA, "counter", "bafo") if 2 in e.price_asks or 2 in e.lead_asks
    ]
    step.check("not_negotiated_as_exact", not asked, _asks_text(asked), owner="negotiation")
    check_visible(view, step, DELTA, "needs_buyer", name="visible_needs_buyer")


def _alternative(t: Thread) -> Email:
    return pt_alternative(t, (WITHIN[0], WITHIN[2]), q(2, "228.00", 12))


ALTERNATIVE: Final = Scenario(
    name="decline_then_alternative",
    title="The shaft declined, another part offered: flagged, never accepted or countered",
    rfq_title="Componentes do motor MTR-100",
    currency="BRL",
    parts=MTR,
    suppliers=(br(DELTA, "Metalúrgica Delta Ltda"),),
    response_due_days=12,
    deadline_days=10,
    steps=(
        StepSpec(
            "alternative",
            "Lines 1 and 3 within target; line 2 only as an alternative part",
            (say(3 * HOUR, DELTA, _alternative), Tick(3 * HOUR + 30 * MINUTE), Tick(DAY)),
            alternative_offered,
        ),
        StepSpec(
            "days_later",
            "Two days pass",
            (Tick(2 * DAY + MINUTE), Tick(3 * DAY + MINUTE)),
            alternative_offered,
        ),
    ),
)


# --- 6. no_reply --------------------------------------------------------------------------------

NORTE: Final = "norte"
CHASE_EVERY: Final = 2  # business days, the RFQ's default schedule
MAX_CHASES: Final = 3


def no_reply(view: View, step: StepResult) -> None:
    run = view.run
    assert run.t0 is not None
    chases = view.sent(NORTE, "chase")
    expected: list[date] = []
    previous = run.t0
    for index in range(MAX_CHASES):
        due = add_business_days(previous, CHASE_EVERY, "BR")
        expected.append(due.date())
        previous = chases[index].at if index < len(chases) else due
    got = [c.at.date() for c in chases]
    step.check(
        "chases_on_schedule",
        got == expected,
        f"sent {[str(d) for d in got]}, want {[str(d) for d in expected]} "
        f"(every {CHASE_EVERY} business days, Brazil)",
        owner="followups",
    )
    weekend = [str(d) for d in got if not is_business_day(d, "BR")]
    step.check("chases_on_business_days", not weekend, f"{weekend}", owner="followups")
    step.check(
        "chases_capped", len(chases) <= MAX_CHASES, f"{len(chases)} chases", owner="followups"
    )
    others = view.sent(NORTE, "clarification", "counter", "bafo", "other")
    step.check("only_chases", not others, f"{[e.tag for e in others]}", owner="followups")
    check_visible(view, step, NORTE, "chases_exhausted", name="visible_chases_exhausted")


NO_REPLY: Final = Scenario(
    name="no_reply",
    title="No answer: chases every 2 business days (Brazil), at most 3, then the buyer knows",
    rfq_title="Componentes do motor MTR-100",
    currency="BRL",
    parts=MTR,
    suppliers=(br(NORTE, "Componentes Norte Ltda"),),
    response_due_days=20,
    deadline_days=21,
    steps=(
        StepSpec(
            "silence",
            "Sixteen days without an answer (a sweep every day)",
            tuple(Tick(d * DAY + MINUTE) for d in range(1, 17)),
            no_reply,
        ),
    ),
)


# --- 7. stubborn_supplier ----------------------------------------------------------------------

SERRA: Final = "serra"
SERRA_OFFER: Final = (q(1, "49.90", 10), q(2, "259.00", 12), q(3, "128.00", 14))


def stubborn_first(view: View, step: StepResult) -> None:
    emails = _negotiations(view, SERRA)
    step.check("one_counter", len(emails) == 1, _asks_text(emails), owner="negotiation")
    check_asks(view, step, SERRA, prices={1, 2, 3}, leads=set(), name="asks_above_target_only")


def stubborn_repeat_for(key: str) -> Grader:
    """After one unchanged repeat, one more ask is fine (no more than one)."""

    def grade(view: View, step: StepResult) -> None:
        emails = _negotiations(view, key)
        step.check("at_most_one_more", len(emails) <= 1, _asks_text(emails), owner="negotiation")

    return grade


def stubborn_stop(view: View, step: StepResult) -> None:
    check_nothing_sent(view, step, SERRA, name="stops_without_progress", owner="negotiation")
    sent = view.sent(SERRA, "counter", "bafo")
    rounds = view.run.scenario.max_rounds
    step.check("round_cap", len(sent) <= rounds, f"{[e.tag for e in sent]}", owner="negotiation")
    bafos = [e for e in sent if e.kind == "bafo"]
    step.check("bafo_at_most_once", len(bafos) <= 1, f"{[e.tag for e in sent]}",
               owner="negotiation")  # fmt: skip
    check_visible(view, step, SERRA, "needs_buyer", name="visible_needs_buyer")


STUBBORN: Final = Scenario(
    name="stubborn_supplier",
    title="The same offer above target, twice: stop asking, hand to the buyer",
    rfq_title="Componentes do motor MTR-100",
    currency="BRL",
    parts=MTR,
    suppliers=(br(SERRA, "Fundição Serra Ltda"),),
    response_due_days=10,
    deadline_days=8,
    steps=(
        StepSpec(
            "above_target",
            "Every line above target, within the maximum",
            (say(2 * HOUR, SERRA, offer(SERRA_OFFER)), Tick(2 * HOUR + 30 * MINUTE)),
            stubborn_first,
        ),
        StepSpec(
            "same_again",
            "The same offer, unchanged",
            (say(DAY + HOUR, SERRA, offer(SERRA_OFFER, SAME)), Tick(DAY + 90 * MINUTE)),
            stubborn_repeat_for(SERRA),
        ),
        StepSpec(
            "same_twice",
            "The same offer a second time",
            (
                say(2 * DAY + HOUR, SERRA, offer(SERRA_OFFER, SAME)),
                Tick(2 * DAY + 90 * MINUTE),
                Tick(3 * DAY + HOUR),
                Tick(4 * DAY + HOUR),
            ),
            stubborn_stop,
        ),
    ),
)


# --- 8. lead_time_only -------------------------------------------------------------------------

RAPIDA: Final = "rapida"
SLOW: Final = tuple(Quoted(x.position, x.price, 35) for x in WITHIN)
FASTER_DAYS: Final = 18


def lead_only_ask(view: View, step: StepResult) -> None:
    emails = _negotiations(view, RAPIDA)
    step.check("one_counter", len(emails) == 1, _asks_text(emails), owner="negotiation")
    check_asks(view, step, RAPIDA, prices=set(), leads={1, 2, 3}, name="lead_time_only_asked")
    targets = norm(p.target for p in view.run.scenario.parts)
    leaked = [e.tag for e in emails if numbers(e.body) & targets]
    step.check("no_target_price_written", not leaked, f"{leaked}", owner="writer")
    check_visible(view, step, RAPIDA, "waiting_supplier", name="visible_waiting_for_supplier")


def lead_only_done(view: View, step: StepResult) -> None:
    state = view.state(RAPIDA)
    leads = {p: line.lead for p, line in state.lines.items()}
    step.check(
        "offer_updated", leads == {1: FASTER_DAYS, 2: FASTER_DAYS, 3: FASTER_DAYS}, state.text(),
        owner="extraction",
    )  # fmt: skip
    check_nothing_sent(view, step, RAPIDA, name="nothing_to_ask", owner="negotiation")
    step.check(
        "ready_to_close",
        view.rfq_negotiation == "ready_to_close",
        view.rfq_negotiation,
        owner="negotiation",
    )
    check_visible(view, step, RAPIDA, "accepted", "nothing_to_ask", name="visible_accepted")


def _faster(t: Thread) -> Email:
    return pt_lead_change(t, (1, 2, 3), FASTER_DAYS)


LEAD_ONLY: Final = Scenario(
    name="lead_time_only",
    title="Prices within target, lead time too long: ask the lead time only",
    rfq_title="Componentes do motor MTR-100",
    currency="BRL",
    parts=MTR,
    suppliers=(br(RAPIDA, "Rápida Rolamentos Ltda"),),
    response_due_days=10,
    deadline_days=8,
    steps=(
        StepSpec(
            "late_offer",
            "Every price within target, 35 days on every line (need-by: 21/21/25)",
            (say(2 * HOUR, RAPIDA, offer(SLOW)), Tick(2 * HOUR + 30 * MINUTE)),
            lead_only_ask,
        ),
        StepSpec(
            "faster",
            f"{FASTER_DAYS} days on every line, other conditions unchanged",
            (say(DAY + HOUR, RAPIDA, _faster), Tick(DAY + 90 * MINUTE), Tick(2 * DAY)),
            lead_only_done,
        ),
    ),
)


# --- 9. extras_and_out_of_order ---------------------------------------------------------------

LESTE: Final = "leste"
LESTE_OFFER: Final = (q(1, "44.90", 10), q(2, "240.00", 12), q(3, "112.00", 14))
REVISION_2: Final = q(2, "236.00", 12)
REVISION_1: Final = q(2, "238.00", 12)


def _extra_listed(view: View) -> bool:
    output = view.copilot.get("list_extra_offers")
    texts: list[str] = []
    if output is not None:
        _walk(output, texts)
    return EXTRA_PART in " ".join(texts)


def extras_first(view: View, step: StepResult) -> None:
    extras = view.copilot.get("list_extra_offers")
    step.check("extra_listed", _extra_listed(view), f"{extras}", owner="extraction")
    state = view.state(LESTE)
    step.check(
        "extra_not_an_offer_line",
        state.unmatched == 0 and set(state.lines) <= {1, 2, 3},
        state.text(),
        owner="extraction",
    )
    priced = [
        e.tag for e in view.to(LESTE) if EXTRA_PART in e.body or EXTRA_PRICE in numbers(e.body)
    ]
    step.check("extra_not_negotiated", not priced, f"{priced}", owner="negotiation")
    check_asks(view, step, LESTE, prices={2}, leads=set(), name="asks_above_target_only")


def extras_revisions(view: View, step: StepResult) -> None:
    line = view.state(LESTE).lines.get(2)
    step.check(
        "newest_revision_kept",
        line is not None and line.price == REVISION_2.price.normalize(),
        view.state(LESTE).text(),
        owner="extraction",
    )
    check_usable(view, step, LESTE, name="offer_usable")
    extras = view.copilot.get("list_extra_offers")
    step.check("extra_still_listed", _extra_listed(view), f"{extras}", owner="extraction")
    priced = [
        e.tag for e in view.to(LESTE) if EXTRA_PART in e.body or EXTRA_PRICE in numbers(e.body)
    ]
    step.check("extra_not_negotiated", not priced, f"{priced}", owner="negotiation")
    stale = [e for e in _negotiations(view, LESTE) if e.price_asks.get(2) is None and e.price_asks]
    step.check("asks_follow_newest", not stale, _asks_text(stale), owner="negotiation")


def _with_extra(t: Thread) -> Email:
    return pt_offer_with_extra(t, LESTE_OFFER)


def _revision(quoted: Quoted, number: int) -> Callable[[Thread], Email]:
    def write(t: Thread) -> Email:
        return pt_price_change(t, quoted, revision=number)

    return write


EXTRAS: Final = Scenario(
    name="extras_and_out_of_order",
    title="An unrequested item, then revision 2 before an older revision 1",
    rfq_title="Componentes do motor MTR-100",
    currency="BRL",
    parts=MTR,
    suppliers=(br(LESTE, "Distribuidora Leste Ltda"),),
    response_due_days=10,
    deadline_days=8,
    steps=(
        StepSpec(
            "offer_with_extra",
            "A whole offer plus another department's belt (000302)",
            (say(2 * HOUR, LESTE, _with_extra), Tick(2 * HOUR + 30 * MINUTE)),
            extras_first,
        ),
        StepSpec(
            "revisions_out_of_order",
            "Revisão 2 (written 10:00) arrives before Revisão 1 (written 09:00)",
            (
                say(DAY + HOUR, LESTE, _revision(REVISION_2, 2)),
                say(
                    DAY + HOUR + 20 * MINUTE,
                    LESTE,
                    _revision(REVISION_1, 1),
                    written_earlier=HOUR + 20 * MINUTE,
                ),
                Tick(DAY + 90 * MINUTE),
            ),  # fmt: skip
            extras_revisions,
        ),
    ),
)


# --- 10. multi_language (German) ---------------------------------------------------------------

RHEIN: Final = "rhein"
DE_PARTS: Final = (
    _part(1, "000101", "Rolamento motor MTR-100", "70", "pcs", 30, "8.50", "9.80"),
    _part(2, "000102", "Eixo motor MTR-100", "15", "pcs", 30, "42.00", "48.00"),
)


def _de(lines: Sequence[Quoted], opening: str, valid: int | None) -> Callable[[Thread], Email]:
    def write(t: Thread) -> Email:
        return de_offer(t, lines, opening=opening, valid_days=valid)

    return write


def german_first(view: View, step: StepResult) -> None:
    clarifications = view.to(RHEIN, "clarification")
    items = _items(clarifications)
    step.check(
        "asks_only_validity",
        bool(clarifications)
        and all(f == "valid_until" for _, f, _ in items)
        and not _undrafted(clarifications),
        f"items {items}; undrafted {_undrafted(clarifications)}",
        owner="followups",
    )
    check_asks(view, step, RHEIN, prices={2}, leads=set(), name="asks_above_target_only")


def german_revised(view: View, step: StepResult) -> None:
    emails = _negotiations(view, RHEIN)
    step.check("one_more_round", len(emails) == 1, _asks_text(emails), owner="negotiation")
    check_asks(view, step, RHEIN, prices={2}, leads=set(), name="asks_above_target_only")


def german_final(view: View, step: StepResult) -> None:
    check_nothing_sent(view, step, RHEIN, name="nothing_to_ask", owner="negotiation")
    check_visible(view, step, RHEIN, "accepted", "nothing_to_ask", name="visible_accepted")
    languages = {language_of(e.body) for e in view.sent(RHEIN)}
    step.check("thread_in_german", languages == {"de"}, f"{languages}", owner="language")


GERMAN: Final = Scenario(
    name="multi_language",
    title="A German supplier: every Bido email in German through the thread",
    rfq_title="Componentes do motor MTR-100",
    currency="EUR",
    parts=DE_PARTS,
    suppliers=(SupplierSpec(RHEIN, "Rhein Antriebstechnik GmbH", "de", "DE", "Vertrieb"),),
    response_due_days=12,
    deadline_days=10,
    steps=(
        StepSpec(
            "first_offer",
            "Line 2 above target, no validity stated",
            (
                say(3 * HOUR, RHEIN, _de((q(1, "8.40", 20), q(2, "46.50", 25)), DE_OFFER, None)),
                Tick(3 * HOUR + 30 * MINUTE),
                Tick(DAY + 5 * MINUTE),
            ),
            german_first,
        ),
        StepSpec(
            "revised",
            "Validity stated, line 2 lowered part of the way",
            (
                say(
                    2 * DAY + HOUR,
                    RHEIN,
                    _de((q(1, "8.40", 20), q(2, "44.00", 25)), DE_REVISED, 40),
                ),
                Tick(2 * DAY + 90 * MINUTE),
            ),  # fmt: skip
            german_revised,
        ),
        StepSpec(
            "final",
            "Best and final: line 2 at target",
            (
                say(
                    3 * DAY + HOUR, RHEIN, _de((q(1, "8.40", 20), q(2, "42.00", 25)), DE_FINAL, 40)
                ),
                Tick(3 * DAY + 90 * MINUTE),
                Tick(4 * DAY + HOUR),
            ),  # fmt: skip
            german_final,
        ),
    ),
)


# --- 11. reply_while_pending -------------------------------------------------------------------

VALE: Final = "vale"
VALE_ROUNDS: Final = (
    (q(1, "49.90", 10), q(2, "259.00", 12), q(3, "128.00", 14)),
    (q(1, "48.90", 10), q(2, "255.00", 12), q(3, "126.00", 14)),
    (q(1, "47.90", 10), q(2, "250.00", 12), q(3, "124.00", 14)),
    (q(1, "46.90", 10), q(2, "245.00", 12), q(3, "120.00", 14)),
)


def pending_first(view: View, step: StepResult) -> None:
    emails = _negotiations(view, VALE)
    step.check("one_counter", len(emails) == 1, _asks_text(emails), owner="negotiation")


def pending_quick(view: View, step: StepResult) -> None:
    emails = _negotiations(view, VALE)
    step.check(
        "one_counter_for_quick_replies", len(emails) == 1, _asks_text(emails), owner="negotiation"
    )


def pending_last(view: View, step: StepResult) -> None:
    sent = view.sent(VALE, "counter", "bafo")
    bafos = [e for e in sent if e.kind == "bafo"]
    step.check(
        "last_round_sent",
        len(bafos) == 1,
        f"rounds {[e.tag for e in sent]}; {view.state(VALE).text()}",
        owner="negotiation",
    )
    rounds = view.run.scenario.max_rounds
    step.check("round_cap", len(sent) <= rounds, f"{[e.tag for e in sent]}", owner="negotiation")
    held = view.state(VALE).held
    step.check("nothing_held", not held, f"held for a person: {held}", owner="mandate")
    if held:
        check_visible(view, step, VALE, "held", name="visible_held")


REPLY_WHILE_PENDING: Final = Scenario(
    name="reply_while_pending",
    title="Quick concessions while a counter waits (daily cap): one email per round, BAFO once",
    rfq_title="Componentes do motor MTR-100",
    currency="BRL",
    parts=MTR,
    suppliers=(br(VALE, "Torneados Vale Ltda"),),
    response_due_days=10,
    deadline_days=8,
    steps=(
        StepSpec(
            "first_offer",
            "Every line above target",
            (say(HOUR, VALE, offer(VALE_ROUNDS[0])), Tick(90 * MINUTE)),
            pending_first,
        ),
        StepSpec(
            "quick_replies",
            "Two small concessions within two hours, before the next counter can go out",
            (
                say(2 * HOUR, VALE, offer(VALE_ROUNDS[1], REVISED)),
                Tick(150 * MINUTE),
                say(3 * HOUR, VALE, offer(VALE_ROUNDS[2], REVISED)),
                Tick(210 * MINUTE),
                Tick(DAY + 5 * MINUTE),
            ),
            pending_quick,
        ),
        StepSpec(
            "next_concession",
            "Another concession: the last allowed round is due",
            (
                say(DAY + 3 * HOUR, VALE, offer(VALE_ROUNDS[3], REVISED)),
                Tick(DAY + 210 * MINUTE),
                Tick(2 * DAY + 5 * MINUTE),
            ),
            pending_last,
        ),
    ),
)


# --- 12. above_maximum -------------------------------------------------------------------------

OESTE: Final = "oeste"
OESTE_OFFER: Final = (q(1, "44.90", 10), q(2, "279.00", 12), q(3, "112.00", 14))


def above_max_first(view: View, step: StepResult) -> None:
    emails = _negotiations(view, OESTE)
    step.check("one_counter", len(emails) == 1, _asks_text(emails), owner="negotiation")
    check_asks(view, step, OESTE, prices={2}, leads=set(), name="asks_within_maximum")


def above_max_stop(view: View, step: StepResult) -> None:
    check_nothing_sent(view, step, OESTE, name="stops_without_progress", owner="negotiation")
    line = outcome_lines(view).get(2)
    step.check(
        "above_maximum_not_recommended",
        line is not None and not line[2],
        f"line 2 in the outcome: {line}",
        owner="comparison",
    )
    check_visible(view, step, OESTE, "needs_buyer", name="visible_needs_buyer")


ABOVE_MAXIMUM: Final = Scenario(
    name="above_maximum",
    title="One line above the buyer's maximum, unchanged twice: ask the target, never the max",
    rfq_title="Componentes do motor MTR-100",
    currency="BRL",
    parts=MTR,
    suppliers=(br(OESTE, "Aços Oeste Ltda"),),
    response_due_days=10,
    deadline_days=8,
    steps=(
        StepSpec(
            "above_max",
            "Lines 1 and 3 within target; line 2 above the maximum",
            (say(2 * HOUR, OESTE, offer(OESTE_OFFER)), Tick(2 * HOUR + 30 * MINUTE)),
            above_max_first,
        ),
        StepSpec(
            "same_again",
            "The same offer, unchanged",
            (say(DAY + HOUR, OESTE, offer(OESTE_OFFER, SAME)), Tick(DAY + 90 * MINUTE)),
            stubborn_repeat_for(OESTE),
        ),
        StepSpec(
            "same_twice",
            "The same offer a second time",
            (
                say(2 * DAY + HOUR, OESTE, offer(OESTE_OFFER, SAME)),
                Tick(2 * DAY + 90 * MINUTE),
                Tick(3 * DAY + HOUR),
            ),
            above_max_stop,
        ),
    ),
)


# --- 13. clarification_answered ---------------------------------------------------------------

LITORAL: Final = "litoral"
ANSWER_LEAD: Final = 10


def answered_asked(view: View, step: StepResult) -> None:
    clarifications = view.to(LITORAL, "clarification")
    asked = {(p, f) for p, f, _ in _items(clarifications)}
    want = {(None, "valid_until"), (1, "lead_time"), (2, "lead_time"), (3, "lead_time")}
    step.check(
        "asks_exactly_what_is_missing",
        asked == want and len(clarifications) == 1,
        f"asked {sorted(asked, key=str)} in {len(clarifications)} email(s), want "
        f"{sorted(want, key=str)}",
        owner="followups",
    )


def answered_done(view: View, step: StepResult) -> None:
    state = view.state(LITORAL)
    leads = {p: line.lead for p, line in state.lines.items()}
    step.check(
        "answers_kept",
        leads == dict.fromkeys((1, 2, 3), ANSWER_LEAD) and not state.issues,
        state.text(),
        owner="extraction",
    )
    check_usable(view, step, LITORAL, name="offer_usable")
    check_nothing_sent(view, step, LITORAL, name="nothing_asked_twice", owner="followups")
    step.check(
        "ready_to_close",
        view.rfq_negotiation == "ready_to_close",
        view.rfq_negotiation,
        owner="negotiation",
    )
    check_visible(view, step, LITORAL, "accepted", "nothing_to_ask", name="visible_accepted")


def _incomplete(t: Thread) -> Email:
    return pt_offer(t, WITHIN, opening=OFFER, valid_days=None, with_lead=False)


def _answers(t: Thread) -> Email:
    return pt_answers(t, valid_days=45, lead=ANSWER_LEAD, positions=(1, 2, 3))


ANSWERED: Final = Scenario(
    name="clarification_answered",
    title="Validity and lead times missing, asked once, answered: the answers complete the offer",
    rfq_title="Componentes do motor MTR-100",
    currency="BRL",
    parts=MTR,
    suppliers=(br(LITORAL, "Rolamentos Litoral Ltda"),),
    response_due_days=10,
    deadline_days=8,
    steps=(
        StepSpec(
            "incomplete_offer",
            "Prices within target; no validity, no lead times",
            (say(2 * HOUR, LITORAL, _incomplete), Tick(2 * HOUR + 30 * MINUTE)),
            answered_asked,
        ),
        StepSpec(
            "answers",
            "Only the answers: validity and 10 days for every line",
            (say(DAY + HOUR, LITORAL, _answers), Tick(DAY + 90 * MINUTE), Tick(2 * DAY)),
            answered_done,
        ),
    ),
)


SCENARIOS: Final = (
    RFQ6_REPLAY,
    CONCESSION,
    QUESTION,
    PARTIAL,
    ALTERNATIVE,
    NO_REPLY,
    STUBBORN,
    LEAD_ONLY,
    EXTRAS,
    GERMAN,
    REPLY_WHILE_PENDING,
    ABOVE_MAXIMUM,
    ANSWERED,
)
NAMES: Final = tuple(s.name for s in SCENARIOS)


# --- Where each failure's fix belongs ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Gap:
    owner: str
    where: str
    why: str


# The product gaps the baseline found (2026-10-09, main at 77c6130), by id.
GAPS: Final[dict[str, Gap]] = {
    "supplier_language": Gap(
        "language",
        "copilot/journey_tools.py add_supplier_contact; rfqs/language.py email_language",
        "A supplier added from the chat (the Copilot's add_supplier_contact; the RFQ-6 emails "
        "went out in English, which this reproduces) gets no country and the default 'en', which "
        "cannot be told from a real 'en'; email_language then writes English (RFQ email, "
        "clarifications, counters) to a Brazilian supplier of a Brazilian buyer. Create it "
        "with the buyer's language like purchasing/sources/handoff.new_supplier_language "
        "(country, else the buyer's template language), and fall back to the buyer's or "
        "template's language, not English, when the language was never set.",
    ),
    "double_signature": Gap(
        "writer",
        "prompts/rfq_followup_writer.v1.md, rfq_negotiation_writer.v1.md; "
        "rfqs/service.py buyer_display_name",
        "The writers are told to 'sign with the buyer's name and company'. Without a buyer "
        "name on file buyer_display_name returns the company, so buyer_name == buyer_company "
        "('Biduuuuu') and the email is signed twice. The RFQ email is right because code "
        "renders it (templates/render.signature dedupes). Give the writers the signature "
        "lines from templates.render.signature (new prompt version), or append them in code.",
    ),
    "clarification_reply_by": Gap(
        "followups",
        "followups/service.py _build (FollowupEmailInput); prompts/rfq_followup_writer.v1.md",
        "A clarification asks the supplier for an answer without saying by when: the v1 "
        "prompt mentions the response date only for chases, and nothing passes a reply-by "
        "date for clarifications. Pass one (the RFQ's response date, or the negotiation "
        "deadline) and require it in a new prompt version.",
    ),
    "negotiation_reply_by_unchecked": Gap(
        "writer (live)",
        "agents/catalog/definitions.py negotiation_email_problems; "
        "prompts/rfq_negotiation_writer.v1.md",
        "The negotiation task carries reply_by and the prompt says to ask for the revised "
        "offer by that date, but the draft check does not require it: the live writer "
        "sometimes ends with 'Poderiam enviar uma oferta revisada?' and no date, and it goes "
        "out. Check the date in the draft (or write it in code).",
    ),
    "relative_validity": Gap(
        "extraction",
        "agents/catalog/contracts.py QuoteExtractionOutput.valid_until; quotes/validation.py; "
        "followups/missing.py",
        "'Valid for 15 days' cannot be stored: valid_until is an ISO date and the extraction "
        "is never given the message date, so it stays empty and missing.open_items asks for "
        "the validity the supplier gave (twice in RFQ-6). Extract a validity period with its "
        "evidence and resolve it in code against the message's Date (validation), and treat "
        "a stated relative validity as answered.",
    ),
    "quantity_tier": Gap(
        "followups",
        "followups/missing.py (_LINE min_quantity -> 'quantity')",
        "Every line whose offer does not restate the quantity gets 'the quantity or quantity "
        "tier the price applies to' asked, although a per-unit price without tiers applies to "
        "the requested quantity (the comparison already reads a missing min_quantity as the "
        "first tier, comparison.compute.pick_break). Ask only with several tiers or an "
        "unclear price basis.",
    ),
    "business_days": Gap(
        "extraction/negotiation",
        "quotes/validation.py _grounding ('working_days'); comparison/rules.py blocking "
        "flags; negotiation/strategy.py decide_line",
        "A lead time in business days ('approximately 10 business days', '7/12/8 business "
        "days') is flagged working_days, so the field needs review: the clarification asks "
        "to 'confirm the lead time' of every line again, and the comparison blocks every "
        "line (needs_review, low_confidence), so the strategy decides 'blocked' and nothing "
        "is negotiated: the shaft's 12 business days (~17 calendar days, need-by in 14) is "
        "never asked about, the offer never becomes acceptable and the outcome stays empty. "
        "Convert business days to calendar days in code with the supplier's calendar "
        "(followups/calendar.add_business_days, country) and keep the line usable.",
    ),
    "asked_again": Gap(
        "followups",
        "followups/service.py _due_followups (one clarification per proposal version)",
        "Each new proposal version gets a new clarification from scratch: nothing remembers "
        "what was asked and answered, so the supplier's answers (validity, per-line lead "
        "times) are asked again the next day, and once more after the next reply. Track "
        "asked items per invite and never re-ask one the supplier answered.",
    ),
    "proposed_status": Gap(
        "negotiation/visibility",
        "negotiation/service.py _propose; negotiation/hooks.py cancel_on_new_offer; "
        "mandates/service.py _hold",
        "invites.negotiation_status becomes 'countered'/'bafo_requested' when the email is "
        "PROPOSED and is not reverted when the proposal is canceled (newer offer while it "
        "waited for the daily cap) or held by the mandate, so the buyer reads 'countered' "
        "(rounds_used 0) or 'bafo_requested' for emails that never went out. Show the state "
        "of what was sent, and say when an email waits (daily cap) or is held.",
    ),
    "copilot_status": Gap(
        "visibility",
        "copilot/tools.py get_negotiation_status (NegotiationStatusOut)",
        "The Copilot shows per supplier only status, rounds_used and line decisions, and a "
        "generic RFQ note ('Negotiating.'): it drops the strategy reasons the API view has "
        "(RfqNegotiation.strategy_reasons: stalled_negotiation_requires_buyer, "
        "partial_coverage_requires_review, round_limit_reached, "
        "supplier_clarification_requires_buyer) and knows nothing of follow-ups (chase state "
        "is only in the follow-ups API), held approvals or supplier questions. The buyer "
        "cannot tell 'waiting for the supplier' from 'accepted internally, nothing to ask', "
        "'held for your approval' or 'needs your decision'. Add a per-supplier next step "
        "with its reason.",
    ),
    "question_dropped": Gap(
        "extraction/visibility",
        "quotes/extract_job.py _persist; copilot/tools.py get_negotiation_status",
        "A first reply that only asks questions (is_quote false, reply_kind clarification, "
        "no earlier offer) stores no proposal: its technical_questions are dropped and the "
        "invite just becomes 'responded' (which also stops chases). Nothing tells the buyer "
        "that the supplier waits for their answer. Record the question for the invite and "
        "show it as needing the buyer.",
    ),
    "amendment_adds_line": Gap(
        "extraction",
        "quotes/state.py _amend",
        "An amendment that quotes a line the previous offer lacked (the promised third "
        "line, 'other conditions unchanged') matches no previous line, so the whole offer is "
        "held (ambiguous_amendment_line_or_tier): lines 1 and 3 go to review, the shaft "
        "never enters the offer, and a new clarification asks for what the supplier just "
        "sent. Let an amendment add a requested line that had no offer.",
    ),
    "canceled_round_numbers": Gap(
        "negotiation/mandate",
        "negotiation/service.py evaluate_rfq (round = max(round)+1 over every row); "
        "mandates/service.py _check (rounds_exceeded)",
        "Rounds are numbered over every negotiation row, canceled ones included, while "
        "rounds_used excludes them: a counter canceled because the supplier answered while "
        "it waited for the daily cap pushes the best-and-final request to round 4 > "
        "max_rounds, and the mandate holds it (rounds_exceeded). The supplier never gets the "
        "last round automatically, the status says bafo_requested, and re-proposals call the "
        "writer for a second BAFO (the journey's double BAFO). Number rounds by counted "
        "messages, or compare counted rounds in the mandate check.",
    ),
    "answers_need_preservation": Gap(
        "extraction",
        "quotes/state.py _amend (_has_preservation); prompts/quote_extraction.v4.md reply_kind",
        "A reply that only answers the clarification (validity, lead times) is an amendment "
        "under the current contract, and _amend holds the whole offer unless the supplier "
        "also wrote 'other conditions unchanged' (amendment_without_explicit_preservation): "
        "the answers are dropped, every line goes to review, and the same questions are sent "
        "again the next day. The live model classifies RFQ-6's second reply this way too. "
        "Fields that were missing (asked by Bido) and contradict nothing should merge without "
        "a preservation sentence; or the contract needs a reply kind for answers.",
    ),
    "lead_time_as_condition": Gap(
        "extraction (live)",
        "prompts/quote_extraction.v4.md (lead_time_days, conditional_terms); "
        "quotes/validation.py _check_line",
        "The live extraction files 'Lead time is approximately 10 business days after order "
        "confirmation' (and the shafts' 'few additional days') under conditional_terms, not "
        "lead_time_days: the contract has no place for an approximate or business-day lead "
        "time. conditional_terms puts every line on review (conditional_terms_require_review), "
        "so nothing is negotiated, and the lead time counts as missing and is asked again. "
        "Give the contract an approximate / business-day lead time with its unit and let "
        "code convert it; keep conditional_terms for real price conditions.",
    ),
    "refused_drafts": Gap(
        "writer (live)",
        "agents/catalog/definitions.py negotiation_email_problems; "
        "negotiation/service.py evaluate_rfq; prompts/rfq_negotiation_writer.v1.md",
        "Seen in 1 of 2 live runs: the writer's best-and-final drafts were refused by the "
        "draft check (unknown_amount) and dropped silently; nothing records the round, the "
        "next sweep tries again, and the request went out late (after the supplier had sent "
        "its final offer, price_above_target_then_concession) or never "
        "(reply_while_pending). Nobody is told. Record refused drafts, retry with the "
        "problem as feedback, and show it; the run's unsent_drafts note keeps the texts.",
    ),
    "uncoordinated_emails": Gap(
        "followups/negotiation",
        "followups/service.py sweep_tenant; negotiation/service.py evaluate_rfq",
        "The follow-up sweep and the negotiation sweep each write their own email for the "
        "same supplier answer: a clarification (validity) and a counter-offer (price) go out "
        "separately, the second one delayed to the next day by the daily cap (or canceled "
        "when the supplier answers meanwhile). One organized email per answer would ask for "
        "the missing information and the counter-offer together.",
    ),
    "stale_lead_limit": Gap(
        "negotiation",
        "mandates/service.py lead_time_until",
        "The lead-time limit from a need-by date is computed once, from the mandate's "
        "proposal date; asked days later, 'at most N days' lands after the need-by date. "
        "Ask from the days left (or for the date itself) when the email is written.",
    ),
    "questions_before_counter": Gap(
        "email rendering/followups",
        "negotiation/planner.py (one request per supplier); negotiation/decision.py; "
        "followups/service.py",
        "One request per supplier: when an answer leaves open questions (the validity) and a "
        "price above target, the planner asks the questions first and the revised-offer "
        "request follows the answer, never two emails for one answer. Asking both in ONE "
        "email needs the clarification items in the negotiation email: the code renderer of "
        "the email standard (N3) rendering the planner's decision with the ledger's items "
        "(N2b), instead of today's two writers.",
    ),
}

# A check that means the same thing in every scenario -> its gaps.
CHECK_GAPS: Final[dict[str, tuple[str, ...]]] = {
    "email:language": ("supplier_language",),
    "email:single_signature": ("double_signature",),
    # A clarification (deterministic: no date given) or a live counter (no date checked).
    "email:reply_by_date": ("clarification_reply_by", "negotiation_reply_by_unchecked"),
    "status_matches_emails": ("proposed_status",),
    "one_email_per_reply": ("uncoordinated_emails",),
    "lead_ask_meets_need_by": ("stale_lead_limit",),
    "no_validity_asked": ("relative_validity",),
    "no_quantity_tier_asked": ("quantity_tier",),
    "no_answered_lead_time_asked": ("business_days",),
    "nothing_asked_twice": ("asked_again", "relative_validity", "business_days"),
    "late_line_lead_time_asked": ("business_days",),
    "visible_waiting_for_supplier": ("copilot_status",),
    "visible_accepted": ("copilot_status",),
    "visible_needs_buyer": ("copilot_status",),
    "visible_final_offer": ("copilot_status",),
    "visible_chases_exhausted": ("copilot_status",),
    "visible_held": ("copilot_status", "proposed_status"),
    "visible_supplier_question": ("question_dropped",),
    "last_round_sent": ("canceled_round_numbers",),
    "nothing_held": ("canceled_round_numbers",),
}
# Where a check fails for a scenario's own reason.
_RFQ6_LEAD: Final = ("business_days", "lead_time_as_condition", "answers_need_preservation")
SCENARIO_GAPS: Final[dict[tuple[str, str], tuple[str, ...]]] = {
    # Mock: business days flagged; live: the approximate lead time filed as a condition
    # and the clarification's answers read as an amendment without preservation.
    ("rfq6_replay", "no_answered_lead_time_asked"): ("business_days", "lead_time_as_condition"),
    ("rfq6_replay", "nothing_asked_twice"): ("asked_again", "relative_validity", *_RFQ6_LEAD),
    ("rfq6_replay", "late_line_lead_time_asked"): _RFQ6_LEAD,
    ("rfq6_replay", "nothing_sent"): ("asked_again", "relative_validity", *_RFQ6_LEAD),
    ("rfq6_replay", "visible_accepted"): (*_RFQ6_LEAD, "copilot_status"),
    ("rfq6_replay", "outcome_every_line"): _RFQ6_LEAD,
    ("rfq6_replay", "ready_to_close"): _RFQ6_LEAD,
    ("price_above_target_then_concession", "one_more_round"): ("refused_drafts",),
    ("price_above_target_then_concession", "asks_above_target_only"): ("refused_drafts",),
    ("price_above_target_then_concession", "stops_after_final_offer"): ("refused_drafts",),
    ("price_above_target_then_concession", "one_bafo_last"): ("refused_drafts",),
    ("reply_while_pending", "last_round_sent"): ("canceled_round_numbers", "refused_drafts"),
    ("supplier_asks_question", "visible_needs_buyer"): ("question_dropped", "copilot_status"),
    ("partial_quote", "offer_completed"): ("amendment_adds_line",),
    ("partial_quote", "offer_usable"): ("amendment_adds_line",),
    ("partial_quote", "nothing_to_ask"): ("amendment_adds_line", "asked_again"),
    ("partial_quote", "ready_to_close"): ("amendment_adds_line",),
    ("partial_quote", "visible_accepted"): ("amendment_adds_line", "copilot_status"),
    ("clarification_answered", "answers_kept"): ("answers_need_preservation",),
    ("clarification_answered", "offer_usable"): ("answers_need_preservation",),
    ("clarification_answered", "nothing_asked_twice"): (
        "answers_need_preservation",
        "asked_again",
    ),
    ("clarification_answered", "ready_to_close"): ("answers_need_preservation",),
    ("multi_language", "asks_above_target_only"): ("questions_before_counter",),
    ("clarification_answered", "visible_accepted"): (
        "answers_need_preservation",
        "copilot_status",
    ),
}


def diagnosis(scenario: str, check: str) -> tuple[str, ...]:
    """The gaps (GAPS ids) a failing check points at; empty when not diagnosed."""
    return SCENARIO_GAPS.get((scenario, check)) or CHECK_GAPS.get(check, ())


# Checks that fail on the current code in mock mode (scenario, step, check): the tests
# mark them non-strict xfail. Remove an entry when its gap is fixed. Fixed by the code-
# rendered layout (`bido_back.supplier_email`): double_signature, clarification_reply_by.
# Fixed by the negotiation planner (`bido_back.negotiation.planner`): the per-supplier
# state the buyer reads (visible_*), status_matches_emails, canceled_round_numbers,
# refused_drafts and the lead-time ask as a date from the need-by date.
EXPECTED_MOCK_FAILURES: Final[frozenset[tuple[str, str, str]]] = frozenset(
    {
        *(
            ("rfq6_replay", step, "email:language")
            for step in ("rfq", "first_reply", "second_reply")
        ),
        ("rfq6_replay", "first_reply", "no_quantity_tier_asked"),
        ("partial_quote", "third_line", "offer_completed"),
        ("partial_quote", "third_line", "offer_usable"),
        ("partial_quote", "third_line", "nothing_to_ask"),
        ("partial_quote", "third_line", "ready_to_close"),
        ("partial_quote", "third_line", "visible_accepted"),
        # One request per supplier: the validity question first, the counter after it.
        ("multi_language", "first_offer", "asks_above_target_only"),
        *(
            ("clarification_answered", "answers", check)
            for check in (
                "answers_kept",
                "offer_usable",
                "nothing_asked_twice",
                "ready_to_close",
                "visible_accepted",
            )
        ),
    }
)


# --- Running ------------------------------------------------------------------------------------


@dataclass(slots=True)
class ScenarioRun:
    scenario: str
    run: int
    mode: Mode
    steps: list[StepResult] = field(default_factory=list[StepResult])
    error: str | None = None
    seconds: float = 0.0
    notes: dict[str, object] = field(default_factory=dict[str, object])

    @property
    def ok(self) -> bool:
        return self.error is None and all(s.ok for s in self.steps)


def _notes(view: View) -> dict[str, object]:
    notes: dict[str, object] = {
        "at": _offset(view.run, view.now),
        "rfq_negotiation": view.rfq_negotiation,
        "states": {k: s.text() for k, s in view.states.items()},
    }
    status = view.copilot.get("get_negotiation_status")
    if isinstance(status, dict):
        notes["copilot_status"] = {
            "status": status.get("status"),
            "note": status.get("note"),
            "suppliers": status.get("suppliers"),
        }
    return notes


async def run_scenario(env: Env, scenario: Scenario, number: int, model: ScenarioModel | None,
                       ) -> ScenarioRun:  # fmt: skip
    result = ScenarioRun(scenario.name, number, env.mode)
    started = time.perf_counter()
    try:
        run = await setup(env, scenario, model)
    except Exception as exc:  # report it; the scenario cannot go on without its RFQ
        result.error = f"setup: {type(exc).__name__}: {exc}"[:600]
        result.seconds = round(time.perf_counter() - started, 1)
        return result
    steps = (StepSpec("rfq", "RFQ emails under the approved mandate", (), scenario.rfq_grade),
             *scenario.steps)  # fmt: skip
    before = 0
    for spec in steps:
        step = StepResult(spec.key, spec.title)
        began = time.perf_counter()
        try:
            for action in spec.actions:
                await act(run, action)
            view = await snapshot(run, spec.key, before)
            before = len(run.world.outbox.sent)
            grade_emails(view, step)
            grade_status(view, step)
            grade_pacing(view, step)
            if spec.grade is not None:
                spec.grade(view, step)
            step.notes = _notes(view)
            step.emails = [
                f"{e.tag} +{_offset(run, e.at)} {e.subject!r}: {' '.join(e.body.split())[:900]}"
                for e in view.new
            ]
        except Exception as exc:  # report, and go on with the next step
            step.error = f"{type(exc).__name__}: {exc}"[:600]
            before = len(run.world.outbox.sent)
        step.seconds = round(time.perf_counter() - began, 1)
        result.steps.append(step)
    leaked = [c.to for c in run.world.outbox.sent if not all(
        is_test_domain(a.rpartition("@")[2]) for a in c.to)]  # fmt: skip
    if result.steps:
        result.steps[-1].check("only_test_recipients", not leaked, f"{leaked}", owner="core")
    world = run.world
    unsent = await _unsent(run)
    result.notes = {
        "rfq": run.ref,
        "start": str(run.start),
        "deliveries": run.deliveries,
        "emails": len(world.outbox.sent),
        "unsent_drafts": unsent,
        "unscripted_extractions": list(model.unscripted) if model is not None else [],
        "tenant_id": str(world.tenant_id),
        "lead_limits": {str(p): d for p, d in run.lead_limits.items()},
    }
    result.seconds = round(time.perf_counter() - started, 1)
    return result


async def _unsent(run: Run) -> list[str]:
    """Proposed emails that never went out: canceled (a newer offer), rejected, or still
    waiting (daily cap, a mandate hold) when the scenario ended."""
    names = {invite_id: run.names[key] for key, invite_id in run.invites.items()}
    async with tenant_session(run.world.engine, run.world.tenant_id) as session:
        negotiation = list(
            await session.scalars(
                select(NegotiationMessage)
                .where(NegotiationMessage.status != "sent")
                .order_by(NegotiationMessage.created_at)
            )
        )
        followup = list(
            await session.scalars(
                select(FollowupMessage)
                .where(FollowupMessage.status != "sent")
                .order_by(FollowupMessage.created_at)
            )
        )
    return [
        f"{r.action} r{r.round} to {names.get(r.invite_id, '?')}: {r.status}"
        + (f" ({r.canceled_reason})" if r.canceled_reason else "")
        for r in negotiation
    ] + [
        f"{r.kind} to {names.get(r.invite_id, '?')}: {r.status}"
        + (f" ({r.canceled_reason})" if r.canceled_reason else "")
        for r in followup
    ]


class UsageLog(logging.Handler):
    """The runner's `agent run` lines (tokens, estimated cost, duration), per tenant."""

    FIELDS: Final = ("agent", "model", "tenant_id", "duration_ms", "model_calls", "input_tokens",
                     "output_tokens", "cost_usd", "outcome")  # fmt: skip

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.records: list[dict[str, object]] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() == "agent run":
            self.records.append({k: getattr(record, k, None) for k in self.FIELDS})

    def totals(self, tenant_id: str | None = None) -> dict[str, object]:
        rows = [r for r in self.records if tenant_id is None or r.get("tenant_id") == tenant_id]
        calls: Counter[str] = Counter()
        cost = 0.0
        seconds = 0.0
        for row in rows:
            calls[str(row.get("agent"))] += 1
            value = row.get("cost_usd")
            cost += float(value) if isinstance(value, int | float) else 0.0
            duration = row.get("duration_ms")
            seconds += float(duration) / 1000 if isinstance(duration, int | float) else 0.0
        return {"runs": dict(calls), "cost_usd": round(cost, 4), "model_seconds": round(seconds, 1)}


async def run_suite(
    env: Env,
    *,
    k: int,
    names: Sequence[str] = NAMES,
    concurrency: int = 1,
    usage: UsageLog | None = None,
) -> list[ScenarioRun]:
    model = ScenarioModel() if env.mode == "mock" else None
    selected = [s for s in SCENARIOS if s.name in names]
    gate = asyncio.Semaphore(max(1, concurrency))

    async def one(scenario: Scenario, number: int) -> ScenarioRun:
        async with gate:
            result = await run_scenario(env, scenario, number, model)
        if usage is not None:
            result.notes["usage"] = usage.totals(str(result.notes.get("tenant_id")))
        return result

    async def suite() -> list[ScenarioRun]:
        return list(await asyncio.gather(*(one(s, n) for n in range(1, k + 1) for s in selected)))

    if model is not None:
        with model.installed():
            return await suite()
    return await suite()


# --- Report -------------------------------------------------------------------------------------


def _mark(ok: bool) -> str:
    return "✅" if ok else "❌"


def summary_rows(runs: Sequence[ScenarioRun]) -> list[dict[str, object]]:
    """Per scenario and step: pass^k, runs passed, failing checks with counts."""
    rows: list[dict[str, object]] = []
    for scenario in SCENARIOS:
        mine = [r for r in runs if r.scenario == scenario.name]
        if not mine:
            continue
        keys = ["setup"] if any(r.error for r in mine) else []
        keys += list(dict.fromkeys(s.key for r in mine for s in r.steps))
        for key in keys:
            steps = [s for r in mine for s in r.steps if s.key == key]
            failing: Counter[str] = Counter()
            for s in steps:
                if s.error:
                    failing["error"] += 1
                for c in s.failing():
                    failing[f"{c.name} [{c.owner}]"] += 1
            if key == "setup":
                failing.update({"setup error": sum(1 for r in mine if r.error)})
            passed = sum(s.ok for s in steps)
            rows.append(
                {
                    "scenario": scenario.name,
                    "step": key,
                    "runs": len(mine),
                    "passed": passed if key != "setup" else len(mine) - failing["setup error"],
                    "pass_k": bool(steps) and passed == len(mine) and key != "setup",
                    "failing": dict(failing),
                }
            )
    return rows


def report_markdown(
    runs: Sequence[ScenarioRun], mode: Mode, k: int, usage: Mapping[str, object] | None = None
) -> str:
    rows = summary_rows(runs)
    checks = [c for r in runs for s in r.steps for c in s.checks]
    out = [
        f"# Negotiation scenarios ({mode}, k={k}, {datetime.now(UTC):%Y-%m-%d %H:%M} UTC)",
        "",
        f"{len({r.scenario for r in runs})} scenarios, {len(runs)} runs, "
        f"{sum(1 for c in checks if c.ok)}/{len(checks)} checks passed, "
        f"{sum(r.seconds for r in runs):.0f} s of scenario time."
        + (f" Model usage: {json.dumps(usage, default=str)}." if usage else ""),
        "",
        "pass^k: every run passed every check of the step. Owners say where a fix belongs.",
        "",
        "| Scenario | Step | pass^k | Runs passed | Failing checks [owner] ×runs |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        failing = cast(dict[str, int], row["failing"])
        listed = ", ".join(f"{name} ×{count}" for name, count in failing.items()) or "—"
        out.append(
            f"| {row['scenario']} | {row['step']} | {_mark(bool(row['pass_k']))} "
            f"| {row['passed']}/{row['runs']} | {listed} |"
        )
    failing = Counter((r.scenario, c.name) for r in runs for s in r.steps for c in s.failing())
    by_gap: dict[str, list[str]] = defaultdict(list)
    for (scenario, name), count in sorted(failing.items()):
        for gap in diagnosis(scenario, name) or ("not_diagnosed",):
            by_gap[gap].append(f"{scenario}: `{name}` ×{count}")
    out += ["", "## Diagnosis: where each failure's fix belongs", ""]
    for gap_id, where in sorted(by_gap.items(), key=lambda item: -len(item[1])):
        gap = GAPS.get(gap_id)
        head = f"**{gap_id}** [{gap.owner}] `{gap.where}`" if gap else f"**{gap_id}**"
        out.append(f"- {head}: {gap.why if gap else 'not diagnosed yet'}")
        out.append(f"  - failing: {'; '.join(where)}")
    if not failing:
        out.append("Nothing fails.")
    out += ["", "## Runs"]
    for run in runs:
        out += ["", f"### {run.scenario}, run {run.run} ({run.seconds} s)", ""]
        if run.error:
            out.append(f"- error: `{run.error}`")
        for step in run.steps:
            out.append(f"#### {step.key}: {_mark(step.ok)} ({step.seconds} s) — {step.title}")
            if step.error:
                out.append(f"- error: `{step.error}`")
            for check in step.checks:
                detail = f": {check.detail}" if check.detail else ""
                out.append(f"- {_mark(check.ok)} `{check.name}` [{check.owner}]{detail}")
            for email in step.emails:
                out.append(f"- email {email}")
            for name, value in step.notes.items():
                out.append(
                    f"- note `{name}`: {json.dumps(value, default=str, ensure_ascii=False)[:1500]}"
                )
            out.append("")
        for name, value in run.notes.items():
            text = json.dumps(value, default=str, ensure_ascii=False)
            out.append(f"- run note `{name}`: {text[:6000]}")
    return "\n".join(out) + "\n"


def write_report(
    runs: Sequence[ScenarioRun],
    mode: Mode,
    k: int,
    folder: Path | None = None,
    usage: Mapping[str, object] | None = None,
) -> Path:
    stamp_ = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = folder or REPORTS / f"{stamp_}-negotiation-{mode}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "report.md").write_text(report_markdown(runs, mode, k, usage))
    (folder / "report.json").write_text(
        json.dumps(
            {
                "mode": mode,
                "k": k,
                "usage": usage,
                "summary": summary_rows(runs),
                "runs": [asdict(r) for r in runs],
            },
            indent=1,
            default=str,
            ensure_ascii=False,
        )
    )
    return folder


async def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the negotiation scenarios.")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--mock", action="store_true", help="scripted models (harness check)")
    modes.add_argument("--live", action="store_true", help="the real models")
    parser.add_argument("-k", type=int, default=1, help="runs per scenario (default 1)")
    parser.add_argument("--scenario", action="append", choices=NAMES, help="repeatable")
    parser.add_argument("--report-dir", type=Path, help="where report.md/report.json go")
    parser.add_argument(
        "--concurrency", type=int, default=1, help="scenario runs at once (default 1)"
    )
    args = parser.parse_args(argv)
    mode: Mode = "mock" if args.mock else "live"
    settings = get_settings()
    if mode == "live" and not (settings.openai_api_key or os.environ.get("OPENAI_API_KEY")):
        print("live evals need OPENAI_API_KEY; use --mock to check the harness", file=sys.stderr)
        return 2
    usage = UsageLog()
    runner_log = logging.getLogger("bido_back.agents.runner")
    runner_log.setLevel(logging.INFO)
    runner_log.addHandler(usage)
    engine = create_engine(settings)
    env = Env(
        engine=engine,
        storage=MemoryStorage(),
        runner_config=RunnerConfig.from_settings(settings),
        mode=mode,
        today=date.today(),
    )
    started = time.perf_counter()
    try:
        runs = await run_suite(
            env,
            k=args.k,
            names=args.scenario or NAMES,
            concurrency=args.concurrency,
            usage=usage,
        )
    finally:
        await engine.dispose()
        runner_log.removeHandler(usage)
    totals = {**usage.totals(), "wall_seconds": round(time.perf_counter() - started, 1)}
    folder = write_report(runs, mode, args.k, args.report_dir, totals)
    print((folder / "report.md").read_text().split("\n## Runs", 1)[0])
    print(f"report: {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
