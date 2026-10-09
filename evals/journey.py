"""The buyer's journey end to end, graded step by step (up to the final quotation, no PO).

    uv run python -m evals.journey --mock
    uv run python -m evals.journey --live -k 1 --workbook PATH [--report-dir DIR]
    uv run python -m evals.journey --live --price-style code   # suppliers write "46,90 BRL"

Each run is a fresh company inside `evals.world.World`: the real copilot tools,
approval guard and executor, negotiation mandates, quote extraction, the
negotiation sweep, supplier-reply intake and the recording outbox under the
real recipient guard. Live mode runs the real models (OPENAI_API_KEY; the
copilot keeps its conversation in AGNO_DATABASE_URL). Mock mode scripts them:
the copilot is a correct one played by code (its tool calls, through the real
tools, and an answer rendered from their results), the source mapper answers
with the generated workbook's known mapping (`evals.journey_workbook`), and
quote extraction and the writers answer from `evals.journey_suppliers` and a
Portuguese negotiation script. Mock mode checks the harness and the
deterministic code, not a model.

The buyer, in Portuguese, in one conversation:

1. analysis: attaches the ERP export and asks what to buy for the Motor
   MTR-100 in Planta A (export date to 14 days later, no extra safety stock),
   with quantities, internal price references, previous suppliers and
   evidence, without contacting suppliers.
2. rfq: "Salve a análise e crie a RFQ em BRL ..." (and confirms the preview).
3. mandate: "Pode negociar com os fornecedores; prazo ...". The harness then
   approves the mandate as the buyer, through the approval guard; the RFQ
   e-mails go out to the three suppliers.
4. replies: the scripted suppliers answer over four simulated days
   (`evals.journey_suppliers.SCRIPT`): the extracted state must follow the
   business rules after every e-mail. A negotiation sweep runs after each day's
   replies; the buyer's view of the negotiation is read right before and right
   after each sweep.
5. negotiation: what the sweeps decided, sent and showed under the mandate,
   graded against the buyer's negotiation rules as properties
   (`evals.journey_negotiation`: one request per round, one best-and-final
   request with a common cut-off, asks within the limits, no concession without
   a move, the no-progress stop, the late-line date ask, the buyer-visible
   state), not against a golden sequence: which day a supplier's offer becomes
   usable varies in live runs.
6. comparison: "Feche e me mostre a comparação final": the outcome per line,
   the extra kept apart, explicit gaps, and no award, PO or PR.

Every step is graded on its own. When the product does not get somewhere, the
harness continues from a deterministic fallback, listed under the step
(`fallback`), so a failure early does not hide later ones. Each check names
the workstream that owns it when it fails (A: scope resolution and the
copilot; B: supplier statistics and internal prices; C: assessment-to-RFQ
handoff; D1: source parsing; extraction, negotiation, comparison: not yet
assigned; core: everything else).

Reports: evals/reports/<stamp>-journey-<mode>/ (report.md, report.json).
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final, Protocol, cast
from uuid import UUID, uuid4

from pydantic import BaseModel, JsonValue, ValidationError
from sqlalchemy import func, select, update

from bido_back.agents.catalog.contracts import NegotiationEmailInput
from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.agents.tools import AnyTool, ToolError
from bido_back.awards.send import AWARD_RFQ, SEND_PURCHASE_ORDER
from bido_back.copilot.journey_tools import (
    AssessmentRfqOut,
    ExtraOffersOut,
    OutcomeOut,
    ProposeMandateOut,
)
from bido_back.copilot.tools import ComparisonOut
from bido_back.db import create_engine, tenant_session
from bido_back.db.models import (
    Approval,
    Award,
    Contact,
    FollowupMessage,
    Invite,
    IssuedPurchaseOrder,
    MandateSend,
    Message,
    NegotiationMandate,
    NegotiationMessage,
    Proposal,
    ProposalLine,
    Rfq,
    RfqLine,
    Supplier,
    SupplierContact,
)
from bido_back.email.addresses import make_message_id
from bido_back.email.mime import build_mime
from bido_back.email.recipients import is_test_domain
from bido_back.email.types import OutboundEmail
from bido_back.intake.service import IngestResult
from bido_back.mandates import service as mandates
from bido_back.mandates.policy import commitment_language
from bido_back.mandates.schemas import ApproveMandateArgs, ProposeMandateRequest
from bido_back.negotiation.decision import decision_of
from bido_back.negotiation.schemas import LineLimitsUpdate, RfqNegotiation
from bido_back.negotiation.service import get_negotiation
from bido_back.negotiation.signals import offer_signals
from bido_back.onboarding.service import store_upload
from bido_back.purchasing.sources.contracts import PlanningScope, SourceAssessment
from bido_back.purchasing.sources.mapping import default_mapper
from bido_back.purchasing.sources.service import analyze_uploads
from bido_back.purchasing.sources.snapshots import AssessmentRfqRequest, draft_from_snapshot
from bido_back.quotes.parsing import detect_currencies
from bido_back.rfqs import service as rfq_service
from bido_back.rfqs import suppliers as supplier_service
from bido_back.rfqs.agents import RfqAgents
from bido_back.rfqs.schemas import ContactIn, CreateSupplierRequest
from bido_back.settings import get_settings
from evals import journey_negotiation as rules
from evals.harness import Env, MemoryStorage, Mode
from evals.journey_suppliers import (
    DAYS,
    EXTRA_PART,
    SCRIPT,
    Offer,
    PriceStyle,
    Requested,
    SupplierEmail,
    expected_recommendations,
    expected_states,
    write,
)
from evals.journey_workbook import (
    PLANT,
    PRODUCT,
    SUPPLIERS,
    XLSX,
    Case,
    PartGolden,
    SupplierRef,
    mock_case,
    real_case,
)
from evals.scripted import ScriptedModel
from evals.world import Captured, World

REPORTS: Final = Path(__file__).parent / "reports"

ANALYSIS_MESSAGE: Final = (
    "Estou cuidando das compras de motores. Analise este arquivo inteiro e cruze os relatórios "
    "para identificar o que precisamos comprar para o Motor MTR-100 na Planta A, considerando "
    "as necessidades entre a data da exportação e 14 dias depois, sem estoque de segurança "
    "adicional. Mostre quantidades, referências internas de preço, fornecedores anteriores e "
    "evidências. Separe informações de outras áreas, não conte registros repetidos duas vezes e "
    "sinalize o que não conseguir concluir. Ainda não contate fornecedores."
)
CLOSE_MESSAGE: Final = "Feche e me mostre a comparação final."
CONFIRM_MESSAGE: Final = "Sim, confirmo. Pode seguir."
RFQ_TITLE: Final = "Componentes do Motor MTR-100 - Planta A"
DUE_DAYS: Final = 5
DEADLINE_DAYS: Final = 7
MAX_CONFIRMATIONS: Final = 2
MAX_ROUNDS: Final = 3


def rfq_message(due: date) -> str:
    return f"Salve a análise e crie a RFQ em BRL para esses itens, com resposta até {due:%d/%m/%Y}."


def mandate_message(deadline: date) -> str:
    return f"Pode negociar com os fornecedores; prazo {deadline:%d/%m/%Y}."


# Checks that measure what is still being added (or a product gap not assigned yet):
# they may fail on the base code even in mock mode. Every other check must pass in mock
# mode (tests/evals/test_journey.py). Empty: every check passes in mock mode.
PENDING: Final[dict[str, str]] = {}


# --- Results ----------------------------------------------------------------------------------


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    owner: str = ""


@dataclass(slots=True)
class Step:
    key: str
    title: str
    checks: list[Check] = field(default_factory=list[Check])
    notes: dict[str, object] = field(default_factory=dict[str, object])
    fallback: list[str] = field(default_factory=list[str])
    error: str | None = None
    skipped: str | None = None
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.skipped is None and self.error is None and all(c.ok for c in self.checks)

    def check(self, name: str, ok: bool, detail: str = "", *, owner: str = "core") -> bool:
        self.checks.append(Check(name, ok, "" if ok else detail[:600], owner))
        return ok

    def failing(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


@dataclass(slots=True)
class Call:
    tool: str
    kind: str
    args: dict[str, JsonValue]
    ok: bool = False
    output: dict[str, JsonValue] | None = None
    error: str | None = None


@dataclass(slots=True)
class Turn:
    step: str
    message: str
    answer: str = ""
    calls: list[Call] = field(default_factory=list[Call])
    failed: str | None = None
    seconds: float = 0.0

    def calls_to(self, *tools: str) -> list[Call]:
        return [c for c in self.calls if c.tool in tools]

    def ok_calls(self, *tools: str) -> list[Call]:
        return [c for c in self.calls if c.tool in tools and c.ok]


@dataclass(slots=True)
class JourneyRun:
    run: int
    mode: Mode
    case: str
    workbook: str
    export: date
    steps: list[Step]
    turns: list[Turn]
    seconds: float = 0.0
    notes: dict[str, object] = field(default_factory=dict[str, object])

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)


STEPS: Final = (
    ("analysis", "1. Analysis: what to buy, from whom, at what price"),
    ("rfq", "2. RFQ from the saved analysis"),
    ("mandate", "3. Negotiation mandate, approval, RFQ e-mails"),
    ("replies", "4. Supplier replies: extracted state"),
    ("negotiation", "5. Negotiation rounds under the mandate"),
    ("comparison", "6. Final comparison (no award, PO or PR)"),
)


# --- Helpers on tool outputs ------------------------------------------------------------------


def parsed[M: BaseModel](model: type[M], call: Call | None) -> M | None:
    if call is None or call.output is None:
        return None
    try:
        return model.model_validate(call.output)
    except ValidationError:
        return None


def last[T](items: Sequence[T]) -> T | None:
    return items[-1] if items else None


def awaiting(turn: Turn) -> bool:
    """A gated tool proposed something and waits for the buyer's go-ahead."""
    return any(c.ok and (c.output or {}).get("awaiting_confirmation") is True for c in turn.calls)


def _json(value: object) -> JsonValue:
    return cast(JsonValue, json.loads(json.dumps(value, default=str)))


def _short(args: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Arguments for the report, without long free text."""
    return {k: (v[:80] + "…" if isinstance(v, str) and len(v) > 80 else v) for k, v in args.items()}


# --- Text checks (Portuguese answers) ---------------------------------------------------------

_NUMBER: Final = re.compile(r"\d+(?:[.,]\d+)*")
_CODE: Final = re.compile(r"\b0\d{5}\b")


def plain(text: str) -> str:
    """Lower case, accents removed: for matching words and names."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", plain(text))


def readings(token: str) -> set[Decimal]:
    """Every value a written number may stand for, either decimal separator."""
    values: set[Decimal] = set()
    for decimal_sep, group_sep in ((".", ","), (",", ".")):
        candidate = token.replace(group_sep, "").replace(decimal_sep, ".")
        try:
            values.add(Decimal(candidate).normalize())
        except InvalidOperation:
            continue
    return values


def numbers(text: str) -> set[Decimal]:
    found: set[Decimal] = set()
    for match in _NUMBER.finditer(text):
        found |= readings(match.group(0).rstrip(".,"))
    return found


def decimals(text: str) -> set[Decimal]:
    """Numbers written with a decimal part only (prices, not quantities or dates)."""
    found: set[Decimal] = set()
    for match in re.finditer(r"\d+(?:\.\d{3})*,\d+|\d+(?:,\d{3})*\.\d+", text):
        found |= readings(match.group(0))
    return found


def segments(answer: str, codes: Sequence[str], *, window: int = 700) -> dict[str, str]:
    """Per part code: what the answer says about it.

    For each mention: the text up to the next mention of another code, and the start
    of its line when no other code comes before it there ("Coberto pelo estoque:
    000103"). A table row, a bullet or a paragraph per part all read the same way.
    """
    marks = [(m.start(), m.group(0)) for m in _CODE.finditer(answer)]
    result: dict[str, list[str]] = {code: [] for code in codes}
    for index, (start, code) in enumerate(marks):
        if code not in result:
            continue
        end = len(answer)
        for later, other in marks[index + 1 :]:
            if other != code:
                end = later
                break
        line_start = answer.rfind("\n", 0, start) + 1
        if any(line_start <= at < start and other != code for at, other in marks):
            line_start = start
        result[code].append(answer[line_start : min(end, start + window)])
    return {code: "\n".join(parts) for code, parts in result.items()}


_LANGUAGE_WORDS: Final[dict[str, frozenset[str]]] = {
    "pt": frozenset(
        "não você para com uma são está estão também itens peças preço preços fornecedor "
        "fornecedores compra comprar estoque pedido pedidos linha linhas foi foram nenhum "
        "nenhuma ainda aguardando aprovação negociação cotação proposta até após pelo pela "
        "isso esse essa este esta quantidade necessidade prazo entrega então já".split()
    ),
    "en": frozenset(
        "the and with for was were is are this that supplier suppliers price order quantity "
        "delivery stock not have has".split()
    ),
    "es": frozenset(
        "el los las y proveedor proveedores precio cantidad pedido también está están "
        "entrega existencias".split()
    ),
    "it": frozenset(
        "della delle sono nessuna anche fornitori fornitore prezzo consegna quantità ordine".split()
    ),
}


def language_of(text: str) -> str | None:
    """The language most of the text's marker words belong to (None: too little text)."""
    words = re.findall(r"[a-zà-ÿ]+", text.casefold())
    scores = {lang: sum(w in markers for w in words) for lang, markers in _LANGUAGE_WORDS.items()}
    best = max(scores, key=lambda lang: scores[lang])
    return best if scores[best] >= 3 else None


def mentions(text: str, supplier: SupplierRef) -> bool:
    return supplier.key in compact(text)


_COVERED: Final = re.compile(
    r"cobert|estoque (?:atual )?(?:suficiente|cobre|atende)|nao (?:precisa|ha necessidade)"
    r"|nada a comprar|sem (?:necessidade|compra)|zero|atendid"
)
_CANNOT: Final = re.compile(
    r"nao (?:foi|e|sera) possivel (?:calcular|concluir|determinar|definir|identificar)"
    r"|nao (?:consegui|conseguimos|consigo|da para) (?:calcular|concluir|determinar|definir)"
    r"|nao (?:foi|foram) calculad|sem (?:conseguir|poder) calcular|nao calculad"
    r"|impossivel (?:calcular|concluir|determinar)|nao (?:ha|existem?) dados suficientes"
)
_ABOUT_BUYING: Final = (
    "quantidad", "necessidade", "compra", "comprar", "bom", "estrutura", "ordem", "mtr-100",
    "calculo", "motor", "componente",
)  # fmt: skip
_SENTENCES: Final = re.compile(r"(?<=[.!?;])\s+|\n+")


def cannot_conclude(answer: str) -> list[str]:
    """Sentences saying the purchase itself could not be worked out."""
    found: list[str] = []
    for sentence in _SENTENCES.split(plain(answer)):
        if _CANNOT.search(sentence) and any(word in sentence for word in _ABOUT_BUYING):
            found.append(sentence.strip()[:200])
    return found


def quantity_shown(segment: str, part: PartGolden) -> bool:
    text = plain(segment)
    if not part.buy:
        return Decimal(0) in numbers(text) or bool(_COVERED.search(text))
    if part.unit == "kg":
        amount = format(part.to_buy.normalize(), "f")
        return bool(re.search(rf"\b{amount}(?:[.,]0+)?\s*kg\b", text))
    return part.to_buy.normalize() in numbers(text)


# An internal price reference with its amount ("alvo R$ 45,67"), not "sem referência interna".
_INTERNAL_PRICE: Final = re.compile(
    r"(?<!sem )(?:alvo|maximo|teto|referencia interna|preco de referencia)\D{0,30}\d"
)


_GAP_WORDS: Final = (
    "frete", "imposto", "tribut", "pagamento", "historico", "lacuna", "nao informad",
    "sem informacao", "validade", "custo total", "landed",
)  # fmt: skip
_NOT_REQUESTED: Final = re.compile(
    r"nao solicitad|nao (?:foi|era) pedid|fora (?:do|da) (?:escopo|comparacao|rfq|pedido)"
    r"|extra|adiciona|a parte|separad|outra area|outro departamento|manutencao"
)


# --- The scripted model ----------------------------------------------------------------------


class JourneyModel(ScriptedModel):
    """The flow evals' scripted model. Code renders the negotiation emails in the supplier's
    language (`bido_back.supplier_email`); the scripted writer adds no note."""


# --- The copilot, live or scripted ------------------------------------------------------------


class Copilot(Protocol):
    async def send(self, message: str, attachments: Sequence[UUID], *, step: str) -> Turn: ...


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A supplier's latest stored offer (its latest proposal version)."""

    version: int | None
    lines: dict[str, tuple[Decimal | None, int | None, bool]]  # part -> (price, lead, review)
    issues: tuple[str, ...]
    reply_kind: str | None
    extras: tuple[str, ...]
    invite_status: str


@dataclass(slots=True)
class SweepRecord:
    """One negotiation sweep: who had replied that day, what was drafted, what went out,
    and what the buyer saw of the negotiation right before and right after it."""

    day: int
    at: datetime
    replied: list[str]
    tasks: list[NegotiationEmailInput]
    # The writers' optional notes (code renders the emails).
    notes: list[str]
    sent: list[Captured]
    before: RfqNegotiation | None = None
    after: RfqNegotiation | None = None
    # The negotiation e-mails (`negotiation_messages` ids) this sweep decided.
    decided: list[UUID] = field(default_factory=list[UUID])
    # Why the buyer's view could not be read (None: it was).
    view_error: str | None = None


@dataclass(slots=True)
class Journey:
    env: Env
    world: World
    case: Case
    model: JourneyModel | None
    upload_id: UUID
    due: date
    deadline: date
    today: Callable[[], date]
    price_style: PriceStyle = "symbol"
    copilot: Copilot | None = None
    turns: list[Turn] = field(default_factory=list[Turn])
    rfq_id: UUID | None = None
    rfq_ref: str | None = None
    lines: dict[str, UUID] = field(default_factory=dict[str, UUID])
    positions: dict[str, int] = field(default_factory=dict[str, int])
    invites: dict[str, UUID] = field(default_factory=dict[str, UUID])
    limits: dict[str, tuple[Decimal, Decimal]] = field(
        default_factory=dict[str, tuple[Decimal, Decimal]]
    )
    max_rounds: int = MAX_ROUNDS
    # The approved mandate's negotiation deadline.
    mandate_deadline: datetime | None = None
    t0: datetime | None = None
    rfq_emails: list[Captured] = field(default_factory=list[Captured])
    snapshots: dict[str, Snapshot] = field(default_factory=dict[str, Snapshot])
    deliveries: dict[str, str] = field(default_factory=dict[str, str])
    sweeps: list[SweepRecord] = field(default_factory=list[SweepRecord])

    async def say(self, message: str, attachments: Sequence[UUID] = (), *, step: str) -> Turn:
        assert self.copilot is not None
        started = time.perf_counter()
        turn = await self.copilot.send(message, attachments, step=step)
        turn.seconds = round(time.perf_counter() - started, 1)
        self.turns.append(turn)
        return turn

    def ctx(self) -> ToolContext:
        principal = self.world.principal
        return ToolContext(tenant_id=principal.tenant_id, user_id=principal.user_id, run_id=uuid4())

    def agents(self) -> RfqAgents:
        return RfqAgents(intake=self.world.intake, email_writer=self.world.email_writer)

    def tools(
        self,
        *,
        attached: Sequence[UUID] = (),
        earlier: Sequence[UUID] = (),
        conversation: UUID | None = None,
    ) -> dict[str, AnyTool]:
        """The copilot's tools as `copilot.service` binds them for one message."""
        from bido_back.copilot.tools import CopilotToolDeps, copilot_tools

        deps = CopilotToolDeps(
            db=self.world.engine,
            agents=self.agents(),
            mailboxes=self.world.mailboxes,
            attachment_ids=tuple(attached),
            earlier_attachment_ids=tuple(earlier),
            storage=self.env.storage,
            source_mapper=self.case.mapper,
            today=self.today,
            conversation_id=conversation,
        )
        return {tool.name: tool for tool in copilot_tools(deps)}

    async def invoke(self, tools: Mapping[str, AnyTool], name: str, **args: object) -> Call:
        """Call one tool as the model would (arguments validated, errors kept as text)."""
        tool = tools[name]
        call = Call(name, tool.kind, cast(dict[str, JsonValue], _json(args)))
        try:
            output = await tool.invoke(self.ctx(), tool.parse_input(args))
        except ToolError as exc:
            call.error = str(exc)
            return call
        except ValidationError as exc:
            call.error = f"invalid arguments: {exc.error_count()} error(s)"
            return call
        call.ok = True
        call.output = cast(dict[str, JsonValue], output.model_dump(mode="json"))
        return call


class LiveCopilot:
    """The real copilot, through `copilot.service` (one conversation per run)."""

    def __init__(self, journey: Journey) -> None:
        self.journey = journey
        self.conversation: UUID | None = None

    async def send(self, message: str, attachments: Sequence[UUID], *, step: str) -> Turn:
        from bido_back.copilot import service as copilot
        from bido_back.copilot.contracts import CopilotMessageRequest

        j = self.journey
        deps = copilot.CopilotDeps(
            db=j.world.engine,
            storage=j.env.storage,
            agents=j.agents(),
            mailboxes=j.world.mailboxes,
            runner_config=j.env.runner_config,
        )
        prepared = await copilot.prepare_message(
            deps,
            j.world.principal,
            CopilotMessageRequest(
                message=message,
                attachment_ids=list(attachments),
                conversation_id=self.conversation,
            ),
        )
        self.conversation = prepared.conversation_id
        turn = Turn(step, message)
        calls: dict[str, Call] = {}
        async for item in copilot.stream_message(deps, prepared):
            data = cast(
                dict[str, JsonValue],
                item if isinstance(item, dict) else item.model_dump(mode="json"),
            )
            kind = data.get("type")
            if kind == "tool_called":
                call = Call(
                    str(data.get("tool")),
                    str(data.get("kind")),
                    cast(dict[str, JsonValue], data.get("args") or {}),
                )
                calls[str(data.get("call_id"))] = call
                turn.calls.append(call)
            elif kind == "tool_result":
                found = calls.get(str(data.get("call_id")))
                if found is None:
                    continue
                found.ok = bool(data.get("ok"))
                output = data.get("output")
                found.output = (
                    cast(dict[str, JsonValue], output) if isinstance(output, dict) else None
                )
                error = data.get("error")
                found.error = str(error) if error is not None else None
            elif kind == "answer":
                turn.answer = str(data.get("text") or "")
            elif kind == "run_failed":
                turn.failed = str(data.get("code"))
        return turn


class MockCopilot:
    """A correct copilot played by code: the calls the model should make, through the real
    tools, and an answer rendered from their results."""

    def __init__(self, journey: Journey) -> None:
        self.journey = journey
        self.conversation = uuid4()
        self.earlier: tuple[UUID, ...] = ()
        self.pending: list[Call] = []
        self.assessment_id: str | None = None

    async def send(self, message: str, attachments: Sequence[UUID], *, step: str) -> Turn:
        j = self.journey
        tools = j.tools(attached=attachments, earlier=self.earlier, conversation=self.conversation)
        self.earlier = tuple(dict.fromkeys([*self.earlier, *attachments]))
        uploads = [str(u) for u in self.earlier]
        turn = Turn(step, message)
        scope = j.case.scope()
        if step == "analysis":
            call = await j.invoke(
                tools, "analyze_procurement_sources", upload_ids=uploads, objective=message,
                scope=scope,
            )  # fmt: skip
            turn.calls.append(call)
            turn.answer = render_analysis(parsed(SourceAssessment, call), j.case, call.error)
        elif step == "rfq":
            saved = await j.invoke(
                tools, "save_procurement_assessment", upload_ids=uploads,
                objective=ANALYSIS_MESSAGE, scope=scope,
            )  # fmt: skip
            turn.calls.append(saved)
            assessment = parsed(SourceAssessment, saved)
            if assessment is None or assessment.assessment_id is None:
                turn.answer = f"Não consegui salvar a análise: {saved.error}."
                return turn
            self.assessment_id = str(assessment.assessment_id)
            proposed = await j.invoke(
                tools, "create_rfq_from_assessment", assessment_id=self.assessment_id,
                title=RFQ_TITLE, base_currency="BRL", response_due=j.due.isoformat(),
            )  # fmt: skip
            turn.calls.append(proposed)
            self.pending = [proposed] if awaiting(turn) else []
            turn.answer = render_rfq(parsed(AssessmentRfqOut, proposed), proposed.error)
        elif step == "confirm":
            for pending in self.pending:
                confirmed = await j.invoke(
                    tools, pending.tool, **pending.args,
                    proposal_id=str((pending.output or {}).get("proposal_id")),
                )  # fmt: skip
                turn.calls.append(confirmed)
            self.pending = []
            done = last(turn.ok_calls("create_rfq_from_assessment"))
            turn.answer = render_rfq(parsed(AssessmentRfqOut, done), done.error if done else None)
        elif step == "mandate":
            call = await j.invoke(
                tools, "propose_negotiation_mandate", rfq=j.rfq_ref or "",
                deadline=j.deadline.isoformat(),
            )  # fmt: skip
            turn.calls.append(call)
            turn.answer = render_mandate(parsed(ProposeMandateOut, call), call.error)
        elif step == "close":
            rfq = j.rfq_ref or ""
            for name in (
                "close_negotiation",
                "get_comparison",
                "get_negotiation_outcome",
                "list_extra_offers",
            ):
                turn.calls.append(await j.invoke(tools, name, rfq=rfq))
            outcome = parsed(OutcomeOut, last(turn.ok_calls("get_negotiation_outcome")))
            extras = parsed(ExtraOffersOut, last(turn.ok_calls("list_extra_offers")))
            turn.answer = render_comparison(outcome, extras, j.case)
        return turn


# --- Rendering the scripted copilot's answers (Portuguese) ------------------------------------


def _money(value: Decimal) -> str:
    text = f"{value:,.2f}"
    return "R$ " + text.replace(",", "_").replace(".", ",").replace("_", ".")


def _qty(value: Decimal) -> str:
    return format(value.normalize(), "f").replace(".", ",")


def render_analysis(assessment: SourceAssessment | None, case: Case, error: str | None) -> str:
    if assessment is None:
        return f"Não consegui analisar o arquivo: {error}."
    blockers = [i.code for i in assessment.issues if i.severity == "blocker"]
    if blockers or not assessment.lines:
        return (
            "Não consegui concluir o cálculo da compra: "
            + (", ".join(blockers) or "nenhuma linha avaliada")
            + "."
        )
    scope = assessment.scope
    head = (
        f"Para o Motor {PRODUCT} na {PLANT}, de {scope.as_of:%d/%m/%Y} a "
        f"{scope.horizon_end:%d/%m/%Y}, sem estoque de segurança adicional:"
        if scope is not None
        else "Resultado da análise:"
    )
    rows = [
        "| Código | Item | Comprar | Fornecedores anteriores (último preço recebido) "
        "| Referência interna |",
        "|---|---|---|---|---|",
    ]
    covered: list[str] = []
    for line in assessment.lines:
        if line.calculated_to_buy is not None and line.calculated_to_buy <= 0:
            covered.append(f"{line.part_number} {line.description or ''}".strip())
            continue
        history = "; ".join(
            f"{s.name} {_money(s.last_price)} ({s.ordered_at:%d/%m/%Y})"
            if s.last_price is not None and s.ordered_at is not None
            else f"{s.name}"
            for s in line.suppliers
        )
        price = (
            f"alvo {_money(line.price.target_price)}, máximo {_money(line.price.max_price)}"
            if line.price is not None
            else "sem referência interna"
        )
        quantity = _qty(line.calculated_to_buy) if line.calculated_to_buy is not None else "?"
        rows.append(
            f"| {line.part_number} | {line.description or ''} | {quantity} {line.unit or ''} "
            f"| {history} | {price} |"
        )
    tail = [f"Cobertos pelo estoque, nada a comprar: {', '.join(covered)}."] if covered else []
    tail.append(
        "Considerei só a ordem do motor dentro do período; manutenção e bombas ficaram fora. "
        "O estoque é a posição da data da exportação (a anterior não foi somada). "
        "Não contatei fornecedores."
    )
    return "\n".join([head, "", *rows, "", *tail])


def render_rfq(created: AssessmentRfqOut | None, error: str | None) -> str:
    if created is None:
        return f"Não consegui criar a RFQ: {error}."
    if created.awaiting_confirmation and created.preview is not None:
        lines = [
            f"- {line.part_number} {line.description}: {_qty(line.quantities[0])} {line.unit}, "
            f"para {line.need_by:%d/%m/%Y}"
            if line.need_by is not None
            else f"- {line.part_number} {line.description}: {_qty(line.quantities[0])} {line.unit}"
            for line in created.preview.lines
        ]
        return "\n".join(
            [
                f'Vou criar a RFQ "{created.preview.title}" em {created.preview.currency}:',
                *lines,
                "Confirma?",
            ]
        )
    return f"Criei a {created.ref} com {created.lines} linhas, em rascunho. Nada foi enviado."


def render_mandate(proposed: ProposeMandateOut | None, error: str | None) -> str:
    if proposed is None:
        return f"Não consegui preparar a negociação: {error}."
    recipients = "\n".join(f"- {r.address}" for r in proposed.recipients)
    return (
        f"Preparei a aprovação da negociação da {proposed.rfq} até "
        f"{proposed.deadline:%d/%m/%Y}, com {proposed.suppliers} fornecedores:\n{recipients}\n"
        "Todos são domínios de teste. Está aguardando sua aprovação na fila de aprovações; "
        "nada foi enviado."
    )


_GAP_TEXT: Final = {
    "freight_cost_unknown": "frete não informado",
    "tax_treatment_unknown": "impostos não informados",
    "payment_terms_unconfirmed": "condição de pagamento não confirmada",
    "delivery_history_insufficient": "histórico de entrega insuficiente",
    "quality_history_insufficient": "histórico de qualidade insuficiente",
    "validity_unconfirmed": "validade não confirmada",
    "delivery_after_required_date": "entrega após a data necessária",
}


def render_comparison(outcome: OutcomeOut | None, extras: ExtraOffersOut | None, case: Case) -> str:
    if outcome is None:
        return "Não consegui obter a comparação final."
    rows = [
        "| Código | Item | Fornecedor recomendado | Preço unitário | Prazo |",
        "|---|---|---|---|---|",
    ]
    gaps: set[str] = set()
    uncovered: list[str] = []
    for line in outcome.lines:
        if not line.covered or line.unit_price is None:
            uncovered.append(f"{line.part_number} {line.description}")
            continue
        gaps.update(line.evidence_gaps)
        rows.append(
            f"| {line.part_number} | {line.description} | {line.supplier} "
            f"| {_money(line.unit_price)} | {line.lead_time_days} dias |"
        )
    text = [f"Negociação da {outcome.rfq} fechada. Comparação final para revisão:", "", *rows, ""]
    if uncovered:
        text.append(f"Sem oferta elegível: {', '.join(uncovered)}.")
    if gaps:
        text.append(
            "Lacunas: "
            + ", ".join(sorted(_GAP_TEXT.get(g, g.replace("_", " ")) for g in gaps))
            + "."
        )
    if extras is not None and extras.offers:
        offered = "; ".join(f"{o.supplier}: {o.description}" for o in extras.offers)
        text.append(f"Ofertas não solicitadas, fora da comparação: {offered}.")
    text.append("Nenhuma adjudicação, pedido de compra ou requisição foi criado.")
    return "\n".join(text)


# --- Reading the company's data ---------------------------------------------------------------


async def snapshot(j: Journey, supplier_key: str) -> Snapshot:
    invite_id = j.invites[supplier_key]
    async with tenant_session(j.world.engine, j.world.tenant_id) as session:
        invite = await session.get(Invite, invite_id)
        proposal = await session.scalar(
            select(Proposal)
            .where(Proposal.invite_id == invite_id)
            .order_by(Proposal.version.desc())
            .limit(1)
        )
        status = invite.status if invite is not None else "missing"
        if proposal is None:
            return Snapshot(None, {}, (), None, (), status)
        rows = (
            await session.execute(
                select(ProposalLine, RfqLine.part_number)
                .outerjoin(RfqLine, RfqLine.id == ProposalLine.rfq_line_id)
                .where(ProposalLine.proposal_id == proposal.id)
            )
        ).all()
        state = cast(dict[str, object], proposal.fields.get("_offer_state") or {})
        raw_issues = state.get("issues")
        issues = (
            tuple(str(i) for i in cast(list[object], raw_issues))
            if isinstance(raw_issues, list)
            else ()
        )
        raw_extras = proposal.fields.get("extra_offers")
        extras: list[str] = []
        if isinstance(raw_extras, list):
            for entry in cast(list[object], raw_extras):
                if isinstance(entry, dict):
                    offer = cast(dict[str, object], entry)
                    extras.append(f"{offer.get('mpn') or ''} {offer.get('description') or ''}")
        lines: dict[str, tuple[Decimal | None, int | None, bool]] = {}
        for row, part in rows:
            key = part or f"unmatched:{row.offered_mpn or row.id}"
            lines[key] = (row.unit_price, row.lead_time_days, row.needs_review)
        kind = state.get("reply_kind")
        return Snapshot(
            proposal.version,
            lines,
            issues,
            str(kind) if kind is not None else None,
            tuple(extras),
            status,
        )


async def load_rfq(j: Journey, rfq_id: UUID) -> None:
    """The RFQ's reference, lines (by part number) and the suppliers invited to it."""
    async with tenant_session(j.world.engine, j.world.tenant_id) as session:
        rfq = await session.get(Rfq, rfq_id)
        if rfq is None:
            raise RuntimeError("the RFQ vanished")
        j.rfq_id, j.rfq_ref = rfq.id, rfq.ref
        rows = list(
            await session.scalars(
                select(RfqLine).where(RfqLine.rfq_id == rfq_id).order_by(RfqLine.position)
            )
        )
        j.lines = {row.part_number or row.description: row.id for row in rows}
        j.positions = {row.part_number or row.description: row.position for row in rows}
    j.invites = await invites_by_supplier(j)


async def invites_by_supplier(j: Journey) -> dict[str, UUID]:
    if j.rfq_id is None:
        return {}
    async with tenant_session(j.world.engine, j.world.tenant_id) as session:
        rows = (
            await session.execute(
                select(Invite.id, Contact.email)
                .join(Contact, Contact.id == Invite.contact_id)
                .where(Invite.rfq_id == j.rfq_id)
            )
        ).all()
    by_email = {(email or "").lower(): invite_id for invite_id, email in rows}
    return {s.key: by_email[s.email] for s in SUPPLIERS if s.email in by_email}


@dataclass(frozen=True, slots=True)
class RfqRow:
    id: UUID
    ref: str
    currency: str
    response_due: date | None
    status: str
    negotiation_status: str
    lines: list[tuple[str, Decimal, str, date | None, Decimal | None, Decimal | None]]
    invited: list[tuple[str, str, str]]  # (supplier, email, status)


async def rfq_row(j: Journey, rfq_id: UUID) -> RfqRow:
    async with tenant_session(j.world.engine, j.world.tenant_id) as session:
        rfq = await session.get(Rfq, rfq_id)
        if rfq is None:
            raise RuntimeError("the RFQ vanished")
        lines = [
            (
                row.part_number or row.description,
                Decimal(str(row.quantities[0])) if row.quantities else Decimal(0),
                row.unit,
                row.need_by,
                row.target_unit_price,
                row.max_unit_price,
            )
            for row in await session.scalars(
                select(RfqLine).where(RfqLine.rfq_id == rfq_id).order_by(RfqLine.position)
            )
        ]
        invited = [
            (name, email or "", status)
            for name, email, status in (
                await session.execute(
                    select(Supplier.name, Contact.email, Invite.status)
                    .join(Supplier, Supplier.id == Invite.supplier_id)
                    .join(Contact, Contact.id == Invite.contact_id)
                    .where(Invite.rfq_id == rfq_id)
                )
            ).all()
        ]
        return RfqRow(
            rfq.id,
            rfq.ref,
            rfq.base_currency,
            rfq.response_due,
            rfq.status,
            rfq.negotiation_status,
            lines,
            invited,
        )


async def latest_rfq(j: Journey) -> UUID | None:
    async with tenant_session(j.world.engine, j.world.tenant_id) as session:
        return await session.scalar(select(Rfq.id).order_by(Rfq.created_at.desc()).limit(1))


async def mandate_terms(j: Journey) -> ApproveMandateArgs | None:
    async with tenant_session(j.world.engine, j.world.tenant_id) as session:
        row = await session.scalar(
            select(NegotiationMandate)
            .where(NegotiationMandate.rfq_id == j.rfq_id)
            .order_by(NegotiationMandate.created_at.desc())
            .limit(1)
        )
        return ApproveMandateArgs.model_validate(row.terms) if row is not None else None


# --- Fallbacks: where the product did not get, the harness goes on, deterministically ---------


async def fallback_rfq(j: Journey) -> tuple[UUID, str]:
    """A scoped assessment saved by code (no copilot) and its draft RFQ."""
    world = j.world
    mapper = j.case.mapper or default_mapper(j.env.runner_config)
    assessment = await analyze_uploads(
        world.engine,
        j.env.storage,
        world.principal,
        [j.upload_id],
        ANALYSIS_MESSAGE,
        j.ctx(),
        mapper,
        persist=True,
        scope=PlanningScope.model_validate(j.case.scope()),
    )
    if assessment.assessment_id is None:
        raise RuntimeError("the fallback assessment was not saved")
    result = await draft_from_snapshot(
        world.engine,
        world.principal,
        assessment.assessment_id,
        AssessmentRfqRequest(title=RFQ_TITLE, base_currency="BRL", response_due=j.due),
    )
    return result.rfq_id, result.ref


async def ensure_invites(j: Journey) -> list[str]:
    """Draft invites for the three suppliers the history names, where missing."""
    assert j.rfq_id is not None
    done: list[str] = []
    existing = await invites_by_supplier(j)
    for supplier in SUPPLIERS:
        if supplier.key in existing:
            continue
        supplier_id, contact_id = await supplier_contact(j, supplier)
        await rfq_service.add_invite(
            j.world.engine, j.world.principal, j.rfq_id, supplier_id, contact_id
        )
        done.append(f"invited {supplier.name} <{supplier.email}>")
    j.invites = await invites_by_supplier(j)
    return done


async def supplier_contact(j: Journey, supplier: SupplierRef) -> tuple[UUID, UUID]:
    """The company's supplier and its contact for this address, created when missing."""
    world = j.world
    async with tenant_session(world.engine, world.tenant_id) as session:
        found = await session.scalar(
            select(Supplier).where(func.lower(Supplier.name) == supplier.name.lower())
        )
        supplier_id = found.id if found is not None else None
        contact_id = (
            await session.scalar(
                select(Contact.id)
                .join(SupplierContact, SupplierContact.contact_id == Contact.id)
                .where(
                    SupplierContact.supplier_id == supplier_id,
                    func.lower(Contact.email) == supplier.email,
                )
            )
            if supplier_id is not None
            else None
        )
    if supplier_id is None:
        view = await supplier_service.create_supplier(
            world.engine,
            world.principal,
            CreateSupplierRequest(
                name=supplier.name,
                country="BR",
                language="pt",
                email_domain=supplier.domain,
                contacts=[ContactIn(name="Comercial", email=supplier.email)],
            ),
        )
        return view.id, view.contacts[0].id
    if contact_id is None:
        contact = await supplier_service.add_contact(
            world.engine, world.principal, supplier_id, ContactIn(email=supplier.email)
        )
        return supplier_id, contact.id
    return supplier_id, contact_id


def end_of(day: date) -> datetime:
    return datetime.combine(day, clock(23, 59), tzinfo=UTC)


async def fallback_mandate(j: Journey) -> tuple[UUID, list[str]]:
    """The mandate with the lines' own limits, or the reference prices where a line has none."""
    assert j.rfq_id is not None
    row = await rfq_row(j, j.rfq_id)
    missing = [
        (part, j.case.part(part)) for part, _, _, _, target, maximum in row.lines
        if target is None or maximum is None
    ]  # fmt: skip
    request = ProposeMandateRequest(
        deadline=end_of(j.deadline),
        max_rounds=MAX_ROUNDS,
        lines=[
            LineLimitsUpdate(
                rfq_line_id=j.lines[part],
                target_unit_price=golden.target,
                max_unit_price=golden.maximum,
            )
            for part, golden in missing
        ],
    )
    proposed = await mandates.propose_mandate(
        j.world.engine,
        j.world.principal,
        j.rfq_id,
        request,
        writer=j.world.email_writer,
        mailboxes=j.world.mailboxes,
        resolver=j.world.resolver,
    )
    notes = [
        f"limits {part}: target {g.target}, maximum {g.maximum} (reference prices)"
        for part, g in missing
    ]
    return proposed.approval_id, [*notes, "mandate proposed by the harness"]


# --- Step 1: analysis -------------------------------------------------------------------------


def scope_ok(scope: Mapping[str, JsonValue], case: Case) -> list[str]:
    """What differs from the buyer's request (empty: the scope is right)."""
    wrong: list[str] = []
    want: dict[str, object] = {
        "product_code": PRODUCT,
        "location": PLANT,
        "as_of": case.export.isoformat(),
        "horizon_end": case.horizon_end.isoformat(),
    }
    for key, value in want.items():
        got = scope.get(key)
        if str(got).strip().casefold() != str(value).casefold():
            wrong.append(f"{key}={got!r} (want {value})")
    safety = scope.get("safety_stock")
    try:
        zero = safety is not None and Decimal(str(safety)) == 0
    except InvalidOperation:
        zero = False
    if not zero:
        wrong.append(f"safety_stock={safety!r} (want 0)")
    return wrong


def grade_assessment(step: Step, assessment: SourceAssessment, case: Case) -> None:
    by_part = {line.part_number: line for line in assessment.lines}
    wrong: list[str] = []
    for part in case.parts:
        line = by_part.get(part.part)
        if line is None:
            wrong.append(f"{part.part} missing")
            continue
        if line.calculated_to_buy is None or line.calculated_to_buy != part.to_buy:
            wrong.append(f"{part.part} {line.calculated_to_buy} vs {part.to_buy}")
        if (line.unit or "").casefold() != part.unit:
            wrong.append(f"{part.part} unit {line.unit} vs {part.unit}")
        if not line.ready:
            wrong.append(f"{part.part} not ready ({[i.code for i in line.issues]})")
    others = sorted(set(by_part) - {p.part for p in case.parts})
    if others:
        wrong.append(f"other areas' parts listed: {others}")
    step.check(
        "tool_quantities",
        not wrong and assessment.status == "assessed",
        f"status={assessment.status}; " + "; ".join(wrong[:6]),
        owner="D1",
    )
    history: list[str] = []
    for part in case.to_buy:
        line = by_part.get(part.part)
        if line is None:
            continue
        got = {
            s.key: (sup.last_price, sup.last_price_basis)
            for sup in line.suppliers
            for s in SUPPLIERS
            if sup.name is not None and s.key in compact(sup.name)
        }
        for key, price in part.last_prices.items():
            found = got.get(key)
            if found is None or found[0] != price or found[1] != "received_order":
                history.append(f"{part.part}/{key}: {found} vs ({price}, received_order)")
    step.check("tool_suppliers_last_prices", not history, "; ".join(history[:6]), owner="D1")
    no_price = [p.part for p in case.to_buy if (line := by_part.get(p.part)) and line.price is None]
    step.check(
        "internal_price_estimate",
        not no_price,
        f"no internal target/maximum on {no_price}",
        owner="B",
    )
    unmeasured = [
        p.part
        for p in case.to_buy
        if (line := by_part.get(p.part))
        and not any(
            s.on_time_rate is not None or s.quality_rate is not None for s in line.suppliers
        )
    ]
    step.check(
        "supplier_performance",
        not unmeasured,
        f"no measured on-time/quality rate for {unmeasured}",
        owner="B",
    )


def grade_analysis_answer(step: Step, turn: Turn, case: Case) -> None:
    answer = turn.answer
    parts = segments(answer, [p.part for p in case.parts])
    wrong = [
        f"{p.part}: want {p.to_buy} {p.unit}" + ("" if parts[p.part] else " (code not named)")
        for p in case.parts
        if not quantity_shown(parts[p.part], p)
    ]
    step.check("answer_quantities", not wrong, "; ".join(wrong), owner="A")
    missing = [
        f"{p.part}: {key}"
        for p in case.to_buy
        for key in p.last_prices
        if not mentions(parts[p.part], next(s for s in SUPPLIERS if s.key == key))
    ]
    step.check("answer_previous_suppliers", not missing, "; ".join(missing[:6]), owner="A")
    prices = [
        f"{p.part}: none of {sorted(str(v) for v in p.last_prices.values())}"
        for p in case.to_buy
        if not ({v.normalize() for v in p.last_prices.values()} & numbers(parts[p.part]))
    ]
    step.check("answer_last_prices", not prices, "; ".join(prices), owner="A")
    open_as_last = [
        p.part
        for p in case.to_buy
        if p.open_price is not None
        and p.open_price.normalize() in numbers(parts[p.part])
        and "aberto" not in plain(parts[p.part])
    ]
    step.notes["open_order_price_mentioned"] = open_as_last
    internal = [p.part for p in case.to_buy if not _INTERNAL_PRICE.search(plain(parts[p.part]))]
    step.check(
        "answer_internal_price",
        not internal,
        f"no internal price reference for {internal}",
        owner="B",
    )
    claims = cannot_conclude(answer)
    step.check("answer_concludes", not claims, f"says it cannot conclude: {claims[:2]}", owner="A")
    language = language_of(answer)
    step.check("answer_in_portuguese", language == "pt", f"reads as {language}", owner="A")


_CONTACTING: Final = (
    "prepare_send", "propose_negotiation_mandate", "invite_suppliers", "propose_counteroffers",
    "create_rfq", "create_rfq_from_assessment", "create_rfq_from_analysis",
)  # fmt: skip


async def step_analysis(j: Journey, step: Step) -> None:
    turn = await j.say(ANALYSIS_MESSAGE, [j.upload_id], step="analysis")
    calls = turn.calls_to("analyze_procurement_sources", "save_procurement_assessment")
    step.notes["analysis_calls"] = [
        {"tool": c.tool, "ok": c.ok, "scope": c.args.get("scope"), "error": c.error} for c in calls
    ]
    scoped = [c for c in calls if isinstance(c.args.get("scope"), dict)]
    step.check(
        "analyze_called_with_scope",
        bool(scoped),
        f"{len(calls)} call(s), none with a scope",
        owner="A",
    )
    result = parsed(SourceAssessment, last([c for c in scoped if c.ok]))
    if scoped:
        # The copilot passes the buyer's words; code resolves the exact boundaries. Grade
        # the scope the tool resolved, not the request's spelling.
        resolved = result.scope if result is not None else None
        problems = (
            scope_ok(cast(dict[str, JsonValue], resolved.model_dump(mode="json")), j.case)
            if resolved is not None
            else ["no resolved scope came back"]
        )
        step.check("scope_matches_request", not problems, "; ".join(problems), owner="A")
    if result is None:
        result = parsed(SourceAssessment, last([c for c in calls if c.ok]))
        step.notes["graded_assessment"] = "unscoped" if result is not None else "none"
    if result is not None:
        step.notes["assessment_status"] = result.status
        step.notes["assessment_blockers"] = [
            i.code for i in result.issues if i.severity == "blocker"
        ]
        step.notes["assessment_lines"] = [
            f"{line.part_number}={line.calculated_to_buy} {line.unit or ''} ready={line.ready}"
            for line in result.lines
        ]
        grade_assessment(step, result, j.case)
    else:
        step.check("tool_quantities", False, "no assessment came back", owner="A")
    grade_analysis_answer(step, turn, j.case)
    contacted = [c.tool for c in turn.calls if c.tool in _CONTACTING]
    step.check("no_supplier_contact", not contacted, f"called {contacted}", owner="A")


# --- Step 2: RFQ ------------------------------------------------------------------------------


async def follow_up(j: Journey, goal: Callable[[], Awaitable[bool]]) -> int:
    """Confirm what the copilot proposed (as buyers do), until `goal` or nothing to confirm."""
    sent = 0
    while sent < MAX_CONFIRMATIONS and not await goal():
        previous = j.turns[-1]
        if not (awaiting(previous) or "?" in previous.answer[-400:]):
            break
        await j.say(CONFIRM_MESSAGE, step="confirm")
        sent += 1
    return sent


async def step_rfq(j: Journey, step: Step) -> None:
    turn = await j.say(rfq_message(j.due), step="rfq")
    start = len(j.turns) - 1

    async def created() -> bool:
        return await latest_rfq(j) is not None

    step.notes["confirmations"] = await follow_up(j, created)
    turns = j.turns[start:]
    calls = [c for t in turns for c in t.calls]
    saves = [c for c in calls if c.tool == "save_procurement_assessment"]
    step.check(
        "save_called_with_scope",
        any(c.ok and isinstance(c.args.get("scope"), dict) for c in saves),
        f"save_procurement_assessment: {[(c.ok, bool(c.args.get('scope'))) for c in saves]}",
        owner="A",
    )
    made = [
        out
        for c in calls
        if c.tool == "create_rfq_from_assessment"
        and (out := parsed(AssessmentRfqOut, c)) is not None
        and out.rfq_id is not None
    ]
    step.notes["rfq_calls"] = [
        {"tool": c.tool, "ok": c.ok, "args": _short(c.args), "error": c.error}
        for c in calls
        if c.tool.startswith("create_rfq") or c.tool == "invite_suppliers"
    ]
    step.check(
        "rfq_created",
        bool(made),
        f"no RFQ from the assessment; answer: {turn.answer[:200]!r}",
        owner="A",
    )
    rfq_id = made[-1].rfq_id if made else None
    golden = {p.part: p for p in j.case.to_buy}
    if rfq_id is not None:
        row = await rfq_row(j, rfq_id)
        lines = {part: (qty, unit) for part, qty, unit, *_ in row.lines}
        exact = {p: (g.to_buy, g.unit) for p, g in golden.items()}
        good = {p: v for p, v in lines.items() if exact.get(p) == (v[0].normalize(), v[1])}
        if not step.check(
            "rfq_lines",
            len(good) == len(exact) == len(lines),
            f"got {sorted((p, str(q), u) for p, (q, u) in lines.items())}",
            owner="A",
        ):
            rfq_id = None  # not the RFQ the journey needs
    if rfq_id is None:
        rfq_id, ref = await fallback_rfq(j)
        step.fallback.append(f"{ref} created from a scoped assessment saved by the harness")
    await load_rfq(j, rfq_id)
    row = await rfq_row(j, rfq_id)
    late = [f"{p}={d}" for p, _, _, d, _, _ in row.lines if d != j.case.need_by]
    step.check("rfq_need_by", not late, f"want {j.case.need_by}: {late}", owner="C")
    step.check("rfq_currency", row.currency == "BRL", row.currency, owner="A")
    step.check(
        "rfq_response_due", row.response_due == j.due, f"{row.response_due} vs {j.due}", owner="A"
    )
    step.notes["rfq"] = row.ref
    step.notes["invites"] = [f"{name} <{email}> {status}" for name, email, status in row.invited]
    step.notes["limits"] = [f"{p}: target={t} max={m}" for p, _, _, _, t, m in row.lines]
    invited = {email.lower() for _, email, _ in row.invited}
    expected = {s.email for s in SUPPLIERS}
    step.check(
        "draft_invites",
        invited == expected and all(is_test_domain(e.rpartition("@")[2]) for e in invited),
        f"invited {sorted(invited)}, want {sorted(expected)}",
        owner="C",
    )
    unset = [
        p for p, _, _, _, t, m in row.lines if t is None or m is None or not Decimal(0) < t <= m
    ]
    step.check("default_limits", not unset, f"no target/maximum on {unset}", owner="C")


# --- Step 3: mandate --------------------------------------------------------------------------


async def step_mandate(j: Journey, step: Step) -> None:
    if j.rfq_id is None:
        step.skipped = "no RFQ"
        return
    sent_before = len(j.world.outbox.sent)
    turn = await j.say(mandate_message(j.deadline), step="mandate")
    start = len(j.turns) - 1

    async def proposed_yet() -> bool:
        return any(c.ok for t in j.turns[start:] for c in t.calls_to("propose_negotiation_mandate"))

    step.notes["confirmations"] = await follow_up(j, proposed_yet)
    calls = [c for t in j.turns[start:] for c in t.calls]
    step.notes["calls"] = [
        {"tool": c.tool, "ok": c.ok, "args": _short(c.args), "error": c.error} for c in calls
    ]
    proposal = parsed(
        ProposeMandateOut,
        last([c for c in calls if c.tool == "propose_negotiation_mandate" and c.ok]),
    )
    errors = [c.error for c in calls if c.tool == "propose_negotiation_mandate" and c.error]
    step.check(
        "mandate_proposed_by_copilot",
        proposal is not None,
        f"errors: {errors}" if errors else f"not called; answer: {turn.answer[:200]!r}",
        owner="C",
    )
    if proposal is not None:
        listed = [s.email for s in SUPPLIERS if s.email in plain(turn.answer)]
        step.check(
            "answer_lists_recipients",
            len(listed) == len(SUPPLIERS),
            f"listed {listed}",
            owner="A",
        )
    step.check(
        "nothing_sent_before_approval",
        len(j.world.outbox.sent) == sent_before,
        f"{len(j.world.outbox.sent) - sent_before} e-mail(s) left before any approval",
    )
    approval_id = proposal.approval_id if proposal is not None else None
    if approval_id is None:
        added = await ensure_invites(j)
        step.fallback.extend(added)
        approval_id, notes = await fallback_mandate(j)
        step.fallback.extend(notes)
    elif missing := [s.key for s in SUPPLIERS if s.key not in await invites_by_supplier(j)]:
        step.notes["suppliers_not_invited"] = missing
    before = len(j.world.outbox.sent)
    await j.world.approve(approval_id)
    j.t0 = j.world.now
    j.invites = await invites_by_supplier(j)
    terms = await mandate_terms(j)
    step.check("mandate_active", terms is not None, "no mandate after the approval")
    if terms is not None:
        j.max_rounds = terms.max_rounds
        j.mandate_deadline = terms.deadline
        by_id = {line_id: part for part, line_id in j.lines.items()}
        j.limits = {
            by_id[line.rfq_line_id]: (line.target_unit_price, line.max_unit_price)
            for line in terms.lines
            if line.rfq_line_id in by_id
        }
        step.notes["mandate"] = {
            "deadline": terms.deadline.isoformat(),
            "max_rounds": terms.max_rounds,
            "suppliers": [f"{s.supplier_name} {s.to}" for s in terms.suppliers],
            "limits": {p: [str(t), str(m)] for p, (t, m) in j.limits.items()},
        }
    sent = j.world.outbox.sent[before:]
    j.rfq_emails = sent
    to = Counter(a.lower() for e in sent for a in (*e.to, *e.cc))
    want = Counter(s.email for s in SUPPLIERS)
    step.check(
        "rfq_emails_to_suppliers",
        to == want and all(is_test_domain(a.rpartition("@")[2]) for a in to),
        f"sent to {dict(to)}, want one each to {sorted(want)}",
    )
    codes = [p.part for p in j.case.to_buy]
    # The RFQ is the attached PDF (the body is a cover note); without one, the body.
    incomplete = [
        f"{e.to[0]} lacks {[c for c in codes if c not in (e.attachment_text or e.body)]}"
        for e in sent
        if any(c not in (e.attachment_text or e.body) for c in codes)
        or (j.rfq_ref or "") not in e.subject
    ]
    step.check("rfq_emails_complete", not incomplete, "; ".join(incomplete[:3]))
    languages = {e.to[0]: language_of(e.body) for e in sent if e.to}
    step.check(
        "rfq_email_language",
        all(lang == "pt" for lang in languages.values()),
        f"{languages}",
        owner="C",
    )
    if sent:
        step.notes["rfq_email_sample"] = f"{sent[0].subject}\n{sent[0].body[:1500]}"


# --- Step 4: supplier replies -----------------------------------------------------------------


def requested(j: Journey) -> list[Requested]:
    return [
        Requested(position, j.case.part(part))
        for part, position in sorted(j.positions.items(), key=lambda item: item[1])
        if part in {p.part for p in j.case.parts}
    ]


async def deliver(j: Journey, email: SupplierEmail) -> str:
    """The supplier's e-mail, in the thread of the last one it received; Date as written."""
    world = j.world
    supplier = email.supplier
    received = world.outbox.to(supplier.email)
    if not received:
        return "no_thread"
    thread = received[-1]
    written = write(
        email,
        rfq_ref=j.rfq_ref or "",
        thread_subject=received[0].subject,
        requested=requested(j),
        valid_until=world.today + timedelta(days=30),
        style=j.price_style,
    )
    if j.model is not None:
        j.model.extraction[written.body.strip()] = written.truth
    outbound = OutboundEmail(
        from_name=f"Comercial | {supplier.name}",
        from_address=supplier.email,
        to=(world.buyer_email,),
        subject=written.subject,
        text_body=written.body,
        message_id=make_message_id(supplier.domain),
        in_reply_to=thread.message_id,
        references=(*thread.references, thread.message_id),
    )
    message = build_mime(outbound, now=world.now - timedelta(hours=email.written_earlier))
    result: IngestResult = await world.inbound.ingest_for_invite(
        message.as_bytes(), tenant_id=world.tenant_id, invite_id=j.invites[supplier.key]
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
    return f"{result.outcome}/{status}"


async def negotiation_view(j: Journey) -> tuple[RfqNegotiation | None, str | None]:
    """What the buyer sees of the negotiation now (`get_negotiation`), or why it failed."""
    if j.rfq_id is None:
        return None, "no RFQ"
    try:
        view = await get_negotiation(j.world.engine, j.world.principal, j.rfq_id, now=j.world.now)
    except Exception as exc:  # graded in step 5: the buyer could not see the negotiation
        return None, f"{type(exc).__name__}: {exc}"[:300]
    return view, None


async def negotiation_message_ids(j: Journey) -> set[UUID]:
    """Every negotiation e-mail decided so far (`negotiation_messages`)."""
    async with tenant_session(j.world.engine, j.world.tenant_id) as session:
        return set(await session.scalars(select(NegotiationMessage.id)))


async def run_timeline(j: Journey) -> None:
    """Four simulated days: the day's replies, then the negotiation sweep, with the buyer's
    view of the negotiation read right before and right after it."""
    assert j.t0 is not None
    world = j.world
    for day in range(DAYS):
        today = [e for e in SCRIPT if e.day == day]
        for email in sorted(today, key=lambda e: e.hour):
            world.at(j.t0 + timedelta(days=day, hours=email.hour))
            j.deliveries[email.key] = await deliver(j, email)
            j.snapshots[email.key] = await snapshot(j, email.supplier.key)
        world.at(j.t0 + timedelta(days=day, hours=max(e.hour for e in today) + 0.5))
        calls = len(world.negotiation_writer.calls)
        sent = len(world.outbox.sent)
        before, before_error = await negotiation_view(j)
        known = await negotiation_message_ids(j)
        await world.sweep_negotiation()
        decided = await negotiation_message_ids(j) - known
        after, after_error = await negotiation_view(j)
        new = world.negotiation_writer.calls[calls:]
        j.sweeps.append(
            SweepRecord(
                day=day,
                at=world.now,
                replied=sorted({e.supplier.key for e in today}),
                tasks=[task for task, _ in new],
                notes=[out.note for _, out in new if out is not None and out.note],
                sent=world.outbox.sent[sent:],
                before=before,
                after=after,
                decided=sorted(decided),
                view_error=before_error or after_error,
            )
        )


def _state_text(state: Mapping[str, tuple[Decimal | None, int | None]]) -> str:
    return ", ".join(f"{p}={pr}/{ld}d" for p, (pr, ld) in sorted(state.items()))


def compare_state(snap: Snapshot | None, want: Mapping[str, Offer]) -> str | None:
    """None when the stored offer is the expected one, else what differs."""
    if snap is None or snap.version is None:
        return "no stored offer"
    got = {p: (price, lead) for p, (price, lead, _) in snap.lines.items()}
    expected: dict[str, tuple[Decimal | None, int | None]] = {
        p: (o.price, o.lead) for p, o in want.items()
    }

    def same(part: str) -> bool:
        mine, theirs = got.get(part), expected.get(part)
        if mine is None or theirs is None or mine[0] is None or theirs[0] is None:
            return False
        return mine[0].normalize() == theirs[0].normalize() and mine[1] == theirs[1]

    differs = sorted(p for p in set(got) | set(expected) if not same(p))
    if not differs:
        return None
    return (
        f"got {_state_text({p: got[p] for p in differs if p in got}) or 'nothing'}; "
        f"want {_state_text({p: expected[p] for p in differs if p in expected}) or 'nothing'}"
    )


async def step_replies(j: Journey, step: Step) -> None:
    if j.t0 is None or len(j.invites) < len(SUPPLIERS):
        step.skipped = "the RFQ e-mails did not go out to the three suppliers"
        return
    await run_timeline(j)
    expected = expected_states()
    step.notes["deliveries"] = dict(j.deliveries)
    stored = [k for k, v in j.deliveries.items() if not v.startswith("stored/extracted")]
    step.check(
        "replies_stored", not stored, f"not stored and extracted: {stored}", owner="extraction"
    )
    for email in SCRIPT:
        snap = j.snapshots.get(email.key)
        problem = compare_state(snap, expected[email.key])
        step.check(f"state:{email.key}", problem is None, problem or "", owner="extraction")
    first = j.snapshots.get("multipecas.r0")
    extras = first.extras if first is not None else ()
    step.check(
        "extra_captured",
        any(EXTRA_PART in e or "correia" in plain(e) for e in extras),
        f"extras: {extras}",
        owner="extraction",
    )
    priced_extra = [p for p in (first.lines if first else {}) if p not in j.lines]
    step.check(
        "extra_not_priced",
        not priced_extra,
        f"lines outside the RFQ: {priced_extra}",
        owner="extraction",
    )
    step.check(
        "partial_decline_kept_open",
        first is not None and "000104" not in first.lines and first.invite_status != "declined",
        f"lines={sorted(first.lines) if first else None} "
        f"invite={first.invite_status if first else None}",
        owner="extraction",
    )
    named = {
        "lead_only_change_keeps_prices": "metalnova.r2",
        "one_price_change_keeps_rest": "precisaosul.r1",
        "repeat_keeps_offer": "multipecas.r2",
    }
    for name, key in named.items():
        problem = compare_state(j.snapshots.get(key), expected[key])
        step.check(name, problem is None, problem or "", owner="extraction")
    late = j.snapshots.get("multipecas.rev1")
    newest = late.lines.get("000102", (None, None, False))[0] if late is not None else None
    step.check(
        "older_revision_no_regression",
        newest is not None and newest.normalize() == Decimal("238"),
        f"000102 at {newest} after the older revision arrived (want 238.00)",
        owner="extraction",
    )
    step.check(
        "older_revision_usable",
        late is not None and not late.issues and not any(r for *_, r in late.lines.values()),
        f"held: issues={late.issues if late else None}",
        owner="extraction",
    )
    held = {
        key: snap.issues
        for key, snap in j.snapshots.items()
        if not key.startswith("multipecas.rev")
        and (snap.issues or any(r for *_, r in snap.lines.values()))
    }
    step.check("no_hold_on_clean_replies", not held, f"{held}", owner="extraction")
    step.check(
        "currency_symbol_reais",
        detect_currencies("R$ 46,90") == {"BRL"},
        f"'R$ 46,90' reads as {sorted(detect_currencies('R$ 46,90'))}: a price quoted as R$ is "
        "a currency mismatch against BRL",
        owner="extraction",
    )
    step.notes["states"] = {
        key: f"v{snap.version} {snap.reply_kind}: "
        + _state_text({p: (pr, ld) for p, (pr, ld, _) in snap.lines.items()})
        + (f" | review={[p for p, (*_, r) in snap.lines.items() if r]}" if snap.lines else "")
        + (f" | issues={list(snap.issues)}" if snap.issues else "")
        for key, snap in j.snapshots.items()
    }


def last_key(supplier_key: str) -> str:
    return [e.key for e in SCRIPT if e.supplier.key == supplier_key][-1]


# --- Step 5: negotiation ----------------------------------------------------------------------

# A negotiation e-mail not sent yet (being written or awaiting approval).
_UNSENT: Final = frozenset({"decided", "proposed"})


def supplier_views(
    view: RfqNegotiation | None, suppliers: Mapping[UUID, str], parts: Mapping[int, str]
) -> dict[str, rules.SupplierView]:
    """Each supplier as the buyer sees it in one negotiation view (keyed by supplier)."""
    found: dict[str, rules.SupplierView] = {}
    for item in view.invites if view is not None else ():
        key = suppliers.get(item.invite_id)
        if key is None or item.state is None:
            continue
        lines = tuple(
            rules.LineView(
                part=parts.get(line.position, str(line.position)),
                status=line.status,
                price=line.price,
                delivery=line.delivery,
                ask=line.ask_unit_price,
                best_price=line.asks_best_price,
                deliver_by=line.deliver_by,
                acceptable=line.acceptable,
            )
            for line in item.line_plans
        )
        found[key] = rules.SupplierView(
            phase=item.state.phase,
            reason=item.state.reason,
            label=item.state.label,
            next_action=item.state.next_action,
            status=item.status,
            until=item.state.until,
            offer_version=item.latest_proposal_version,
            lines=lines,
            pending=sum(1 for m in item.messages if m.state in _UNSENT),
        )
    return found


def _request(
    row: NegotiationMessage,
    supplier: str,
    version: int,
    parts: Mapping[int, str],
    sweep: int | None,
) -> rules.Request:
    """One decided negotiation e-mail as the rules read it (its typed decision)."""
    decision = decision_of(cast(dict[str, object], row.terms))
    # A refused draft waiting for a person is "held" (as the planner reads it).
    state = "held" if row.status == "canceled" and row.held_reason else row.status
    if decision is None:
        return rules.Request(
            supplier, row.sequence, row.round, row.kind or row.action, state, version,
            row.created_at.date(), message_id=row.message_id_header, sweep=sweep,
        )  # fmt: skip
    asks = tuple(
        rules.LineAsk(
            part=parts.get(p.position, p.part_number or str(p.position)),
            offered=p.offered_unit_price,
            offered_lead=p.offered_lead_time_days,
            price=p.target_unit_price,
            best_price=p.best_price,
            deliver_by=p.deliver_by,
        )
        for p in decision.positions
    )
    reminder = row.kind == "reminder" or decision.reminder_of is not None
    return rules.Request(
        supplier=supplier,
        sequence=decision.sequence,
        round=decision.round,
        kind="reminder" if reminder else decision.kind,
        state=state,
        offer_version=decision.offer_version,
        order_date=decision.order_date,
        reply_by=decision.reply_by,
        cutoff_at=decision.cutoff_at,
        asks=asks,
        reminder_of=decision.reminder_of,
        message_id=row.message_id_header,
        sweep=sweep,
    )


def declared_finals(j: Journey) -> dict[str, frozenset[int]]:
    """Per supplier, the stored offer versions its "melhor e última oferta" e-mails made
    (the script's truth): an e-mail that stored no new version marks nothing."""
    finals: dict[str, set[int]] = defaultdict(set)
    previous: dict[str, int | None] = {}
    for email in sorted(SCRIPT, key=lambda e: (e.day, e.hour)):
        snap = j.snapshots.get(email.key)
        version = snap.version if snap is not None else None
        key = email.supplier.key
        if email.kind == "final" and version is not None and version != previous.get(key):
            finals[key].add(version)
        previous[key] = version
    return {key: frozenset(versions) for key, versions in finals.items()}


async def negotiation_record(j: Journey) -> rules.Negotiation:
    """What the sweeps decided, sent and showed, read from the run (`evals.journey_negotiation`)."""
    assert j.rfq_id is not None
    suppliers = {invite: key for key, invite in j.invites.items()}
    parts = {position: part for part, position in j.positions.items()}
    sweep_of = {row_id: index for index, s in enumerate(j.sweeps) for row_id in s.decided}
    requests: dict[str, list[rules.Request]] = defaultdict(list)
    offers: dict[str, dict[int, dict[str, rules.Quote]]] = defaultdict(dict)
    async with tenant_session(j.world.engine, j.world.tenant_id) as session:
        rows = (
            await session.execute(
                select(NegotiationMessage, Proposal.version)
                .join(Proposal, Proposal.id == NegotiationMessage.proposal_id)
                .where(NegotiationMessage.invite_id.in_(list(suppliers)))
            )
        ).all()
        for row, version in rows:
            key = suppliers[row.invite_id]
            requests[key].append(_request(row, key, version, parts, sweep_of.get(row.id)))
        lines = (
            await session.execute(
                select(Proposal.invite_id, Proposal.version, ProposalLine, RfqLine.part_number)
                .join(ProposalLine, ProposalLine.proposal_id == Proposal.id)
                .join(RfqLine, RfqLine.id == ProposalLine.rfq_line_id)
                .where(Proposal.invite_id.in_(list(suppliers)))
            )
        ).all()
        for invite_id, version, line, part in lines:
            offers[suppliers[invite_id]].setdefault(version, {})[part or str(line.rfq_line_id)] = (
                rules.Quote(line.unit_price, line.lead_time_days, line.needs_review)
            )
        stored_finals: dict[str, set[int]] = defaultdict(set)
        for invite_id, version, fields in (
            await session.execute(
                select(Proposal.invite_id, Proposal.version, Proposal.fields).where(
                    Proposal.invite_id.in_(list(suppliers))
                )
            )
        ).all():
            if offer_signals(fields).final_offer:
                stored_finals[suppliers[invite_id]].add(version)
        followups = set(
            await session.scalars(
                select(FollowupMessage.message_id_header).where(
                    FollowupMessage.message_id_header.is_not(None)
                )
            )
        )
        need_by = {
            row.part_number or row.description: row.need_by
            for row in await session.scalars(select(RfqLine).where(RfqLine.rfq_id == j.rfq_id))
            if row.need_by is not None
        }
    return rules.Negotiation(
        limits=rules.Limits(
            targets={p: t for p, (t, _) in j.limits.items()},
            maxima={p: m for p, (_, m) in j.limits.items()},
            need_by=need_by,
            max_rounds=j.max_rounds,
            deadline=j.mandate_deadline,
        ),
        suppliers=tuple(s.key for s in SUPPLIERS if s.key in j.invites),
        requests={k: tuple(v) for k, v in requests.items()},
        offers=offers,
        sweeps=tuple(
            rules.Sweep(
                day=s.day,
                at=s.at,
                before=supplier_views(s.before, suppliers, parts),
                after=supplier_views(s.after, suppliers, parts),
                cutoff=s.after.cutoff_at if s.after is not None else None,
            )
            for s in j.sweeps
        ),
        emails={
            s.key: tuple(
                rules.Email(s.key, e.message_id, e.body, e.at)
                for e in j.world.outbox.to(s.email)
                if e not in j.rfq_emails and e.message_id not in followups
            )
            for s in SUPPLIERS
        },
        finals=declared_finals(j),
        stored_finals={k: frozenset(v) for k, v in stored_finals.items()},
    )


def supplier_of(name: str) -> SupplierRef | None:
    return next((s for s in SUPPLIERS if s.key in compact(name)), None)


def view_text(view: rules.SupplierView | None) -> str:
    return f"{view.phase}:{view.reason} ({view.status})" if view is not None else "-"


def last_view(view: rules.SupplierView) -> str:
    """The supplier's label and its lines that are not acceptable (status/price/delivery)."""
    off = [f"{x.part} {x.status}/{x.price}/{x.delivery}" for x in view.lines if not x.acceptable]
    return f"{view.label} | not acceptable: {', '.join(off) or '-'}"


_CONTENT_CHECKS: Final = (
    "html_part",
    "no_max_revealed",
    "no_competitor_leak",
    "no_commitment_language",
    "negotiation_language",
)


async def check_email_content(j: Journey, step: Step, n: rules.Negotiation) -> None:
    """What every negotiation e-mail may say: never the buyer's maximum, another supplier's
    name or price, or commitment language; Portuguese, with an HTML part. Nothing held."""
    states = expected_states()
    own: dict[str, set[Decimal]] = defaultdict(set)
    for email in SCRIPT:
        own[email.supplier.key] |= {o.price.normalize() for o in states[email.key].values()}
    disclosed = {t.normalize() for t in n.limits.targets.values()}
    problems: dict[str, list[str]] = defaultdict(list)
    for supplier in SUPPLIERS:
        others = [s for s in SUPPLIERS if s.key != supplier.key]
        # Our own asks to this supplier are decided by code from its prices and the target
        # (`no_concession_without_move` checks how): never another supplier's price.
        asked = {
            a.price.normalize()
            for r in n.requests.get(supplier.key, ())
            for a in r.asks
            if a.price is not None
        }
        rival_prices = {p for s in others for p in own[s.key]} - own[supplier.key] - disclosed
        rival_prices -= asked
        hidden = {m.normalize() for m in n.limits.maxima.values()} - own[supplier.key] - disclosed
        for email in [e for e in j.world.outbox.to(supplier.email) if e not in j.rfq_emails]:
            body = email.body
            if email.html is None:
                problems["html_part"].append(supplier.key)
            if numbers(body) & hidden:
                problems["no_max_revealed"].append(
                    f"{supplier.key}: {sorted(numbers(body) & hidden)}"
                )
            leaked = sorted(decimals(body) & rival_prices)
            names = [s.name for s in others if s.key in compact(body)]
            if leaked or names:
                problems["no_competitor_leak"].append(f"{supplier.key}: {leaked} {names}")
            if commitment_language(body):
                problems["no_commitment_language"].append(supplier.key)
            if language_of(body) != "pt":
                problems["negotiation_language"].append(f"{supplier.key}: {language_of(body)}")
    for name in _CONTENT_CHECKS:
        step.check(name, not problems[name], "; ".join(problems[name][:4]), owner="negotiation")
    async with tenant_session(j.world.engine, j.world.tenant_id) as session:
        held = [
            f"{tool}:{reason}"
            for tool, reason in (
                await session.execute(
                    select(MandateSend.tool, MandateSend.reason).where(
                        MandateSend.outcome == "held"
                    )
                )
            ).all()
        ]
    step.check("nothing_held", not held, f"held for a person: {held}", owner="negotiation")


async def step_negotiation(j: Journey, step: Step) -> None:
    if not j.sweeps or j.rfq_id is None:
        step.skipped = "no negotiation ran"
        return
    unread = [f"day {s.day}: {s.view_error}" for s in j.sweeps if s.view_error]
    step.check("buyer_view_read", not unread, "; ".join(unread), owner="negotiation")
    n = await negotiation_record(j)
    for name, problems in rules.grade(n):
        step.check(name, not problems, "; ".join(problems[:4]), owner="negotiation")
    step.notes["requests"] = {
        s: [rules.describe(r) for r in sorted(n.requests.get(s, ()), key=lambda r: r.sequence)]
        for s in n.suppliers
    }
    step.notes["final_offers"] = {
        s: f"written v{sorted(n.finals.get(s, ()))}, stored v{sorted(n.stored_finals.get(s, ()))}"
        for s in n.suppliers
    }
    step.notes["buyer_sees"] = [
        f"day {sweep.day} {s}: {view_text(sweep.before.get(s))} -> {view_text(sweep.after.get(s))}"
        for sweep in n.sweeps
        for s in n.suppliers
    ]
    if n.sweeps:
        step.notes["last_view"] = {s: last_view(v) for s, v in n.sweeps[-1].after.items()}
    await check_email_content(j, step, n)
    step.notes["emails"] = [
        f"day {sweep.day} -> {', '.join(e.to)}: {e.body[:700]}"
        for sweep in j.sweeps
        for e in sweep.sent
    ]


# --- Step 6: comparison -----------------------------------------------------------------------


async def step_comparison(j: Journey, step: Step) -> None:
    if j.rfq_id is None or not j.sweeps:
        step.skipped = "no negotiation to close"
        return
    turn = await j.say(CLOSE_MESSAGE, step="close")
    calls = turn.calls
    step.notes["calls"] = [{"tool": c.tool, "ok": c.ok, "error": c.error} for c in calls]
    row = await rfq_row(j, j.rfq_id)
    step.check("closed", row.negotiation_status == "closed", row.negotiation_status, owner="A")
    outcome_call = last(turn.ok_calls("get_negotiation_outcome")) or last(
        turn.ok_calls("close_negotiation")
    )
    comparison_call = last(turn.ok_calls("get_comparison"))
    step.check(
        "comparison_tools",
        outcome_call is not None and comparison_call is not None,
        f"called {[(c.tool, c.ok) for c in calls]}",
        owner="A",
    )
    outcome = parsed(OutcomeOut, outcome_call)
    tools = j.tools()
    if outcome is None:
        harness = await j.invoke(tools, "get_negotiation_outcome", rfq=j.rfq_ref or "")
        outcome = parsed(OutcomeOut, harness)
        step.notes["outcome_from"] = "harness"
    comparison = parsed(ComparisonOut, comparison_call)
    if comparison is None:
        comparison = parsed(
            ComparisonOut, await j.invoke(tools, "get_comparison", rfq=j.rfq_ref or "")
        )
    today = j.today()
    need_days = (j.case.need_by - today).days
    maxima = {p: m for p, (_, m) in j.limits.items()} or {p.part: p.maximum for p in j.case.parts}
    expected = expected_recommendations(j.case.parts, need_days, maxima=maxima)
    step.notes["expected"] = {
        p: f"{e.supplier} {e.price} ({e.lead}d): {e.why}" for p, e in expected.items()
    }
    lines = {line.part_number: line for line in outcome.lines} if outcome is not None else {}
    step.notes["outcome"] = {
        p: f"{line.supplier} {line.unit_price} ({line.lead_time_days}d) "
        f"readiness={line.readiness} gaps={line.evidence_gaps} excluded="
        f"{[(x.supplier_name, x.reasons) for x in line.excluded]}"
        for p, line in lines.items()
        if p is not None
    }
    for part, want in expected.items():
        line = lines.get(part)
        got_key = supplier_of(line.supplier or "") if line is not None else None
        ok = (
            line is not None
            and got_key is not None
            and got_key.key == want.supplier
            and line.unit_price is not None
            and line.unit_price.normalize() == want.price.normalize()
        )
        step.check(
            f"recommendation:{part}",
            ok,
            f"got {line.supplier if line else None} at {line.unit_price if line else None}; "
            f"want {want.supplier} at {want.price} ({want.why})",
            owner="comparison",
        )
    offers = [
        offer
        for rec in (comparison.recommendations if comparison is not None else [])
        for offer in (rec.offer, *(alt.offer for alt in rec.alternatives))
        if offer is not None
    ]
    compared = [
        f"{line.part_number}: {line.supplier}"
        for line in (outcome.lines if outcome is not None else [])
        if line.part_number == EXTRA_PART
    ] + [f"offer of {o.offered_mpn}" for o in offers if (o.offered_mpn or "") == EXTRA_PART]
    step.check(
        "extra_not_compared",
        not compared,
        f"the unrequested item reached the comparison: {compared}",
        owner="comparison",
    )
    extras = parsed(ExtraOffersOut, last(turn.ok_calls("list_extra_offers")))
    if extras is None:
        extras = parsed(
            ExtraOffersOut, await j.invoke(tools, "list_extra_offers", rfq=j.rfq_ref or "")
        )
    listed = [o for o in (extras.offers if extras is not None else []) if
              (o.mpn or "") == EXTRA_PART or "correia" in plain(o.description)]  # fmt: skip
    step.check(
        "extra_still_listed",
        bool(listed),
        "list_extra_offers no longer shows MultiPecas' maintenance offer (000302): only the "
        "latest proposal version's extras are read",
        owner="extraction",
    )
    covered = [line for line in lines.values() if line.covered]
    gapless = [line.part_number for line in covered if not line.evidence_gaps]
    step.check(
        "gaps_exposed",
        bool(covered) and not gapless,
        f"no evidence gaps on {gapless} although taxes were never quoted",
        owner="comparison",
    )
    answer = turn.answer
    parts = segments(answer, list(expected))
    wrong = [
        f"{p}: want {e.supplier} {e.price}"
        for p, e in expected.items()
        if not (e.supplier in compact(parts[p]) and e.price.normalize() in numbers(parts[p]))
    ]
    step.check("answer_winners", not wrong, "; ".join(wrong), owner="A")
    text = plain(answer)
    step.check(
        "answer_gaps",
        any(word in text for word in _GAP_WORDS),
        "the answer names no gap (freight, taxes, payment terms, history)",
        owner="A",
    )
    mention = [s for s in _SENTENCES.split(text) if EXTRA_PART in s or "correia" in s]
    step.check(
        "answer_extra_separate",
        all(_NOT_REQUESTED.search(s) for s in mention),
        f"the extra is mentioned as if requested: {mention[:2]}",
        owner="A",
    )
    step.notes["answer_mentions_extra"] = bool(mention)
    language = language_of(answer)
    step.check("answer_in_portuguese", language == "pt", f"reads as {language}", owner="A")
    forbidden = [c.tool for c in calls if c.tool in ("prepare_award",)]
    async with tenant_session(j.world.engine, j.world.tenant_id) as session:
        awards = await session.scalar(select(func.count()).select_from(Award)) or 0
        orders = await session.scalar(select(func.count()).select_from(IssuedPurchaseOrder)) or 0
        approvals = (
            await session.scalar(
                select(func.count())
                .select_from(Approval)
                .where(Approval.tool.in_((AWARD_RFQ, SEND_PURCHASE_ORDER)))
            )
            or 0
        )
    step.check(
        "no_award_po_pr",
        not forbidden and not awards and not orders and not approvals,
        f"tools {forbidden}, awards {awards}, POs {orders}, approvals {approvals}",
    )


# --- The run ----------------------------------------------------------------------------------

StepRunner = Callable[[Journey, Step], Awaitable[None]]
RUNNERS: Final[dict[str, StepRunner]] = {
    "analysis": step_analysis,
    "rfq": step_rfq,
    "mandate": step_mandate,
    "replies": step_replies,
    "negotiation": step_negotiation,
    "comparison": step_comparison,
}


async def run_journey(
    env: Env,
    *,
    run: int,
    workbook: Path | None,
    model: JourneyModel | None,
    price_style: PriceStyle = "symbol",
) -> JourneyRun:
    started = time.perf_counter()
    world = await World.create(env, f"journey {run}", model)
    case = real_case(workbook) if workbook is not None else mock_case(world.today)
    upload = await store_upload(
        env.engine, env.storage, world.principal, filename=case.filename, content_type=XLSX,
        data=case.data,
    )  # fmt: skip
    journey = Journey(
        env=env,
        world=world,
        case=case,
        model=model,
        upload_id=upload.id,
        due=world.today + timedelta(days=DUE_DAYS),
        deadline=world.today + timedelta(days=DEADLINE_DAYS),
        price_style=price_style,
        today=(lambda: world.today) if env.mode == "mock" else (lambda: datetime.now(UTC).date()),
    )
    journey.copilot = MockCopilot(journey) if env.mode == "mock" else LiveCopilot(journey)
    steps: list[Step] = []
    for key, title in STEPS:
        step = Step(key, title)
        began = time.perf_counter()
        try:
            await RUNNERS[key](journey, step)
        except Exception as exc:  # report, and go on with what can still be graded
            step.error = f"{type(exc).__name__}: {exc}"[:600]
        step.seconds = round(time.perf_counter() - began, 1)
        steps.append(step)
    leaked = [
        c.to
        for c in world.outbox.sent
        if not all(is_test_domain(a.rpartition("@")[2]) for a in c.to)
    ]
    notes: dict[str, object] = {
        "emails_captured": len(world.outbox.sent),
        "non_test_recipients": leaked,
        "rfq": journey.rfq_ref,
        "today": journey.today().isoformat(),
        "need_by": case.need_by.isoformat(),
        "price_style": price_style,
    }
    return JourneyRun(
        run=run,
        mode=env.mode,
        case=case.name,
        workbook=case.filename,
        export=case.export,
        steps=steps,
        turns=journey.turns,
        seconds=round(time.perf_counter() - started, 1),
        notes=notes,
    )


async def run_journeys(
    env: Env, *, k: int, workbook: Path | None = None, price_style: PriceStyle = "symbol"
) -> list[JourneyRun]:
    model = JourneyModel() if env.mode == "mock" else None
    runs: list[JourneyRun] = []

    async def suite() -> None:
        for run in range(1, k + 1):
            runs.append(
                await run_journey(
                    env, run=run, workbook=workbook, model=model, price_style=price_style
                )
            )

    if model is not None:
        with model.installed():
            await suite()
    else:
        await suite()
    return runs


# --- Report -----------------------------------------------------------------------------------


def _mark(ok: bool) -> str:
    return "✅" if ok else "❌"


def _flat(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    return " ".join(text.split())[:800]


def note_lines(name: str, value: object) -> list[str]:
    """A step note for the report: lists and mappings one item per line."""
    if isinstance(value, list) and value:
        items = cast(list[object], value)
        return [f"- note `{name}`:", *(f"  - {_flat(item)}" for item in items)]
    if isinstance(value, dict) and value:
        entries = cast(dict[object, object], value)
        return [f"- note `{name}`:", *(f"  - {k}: {_flat(v)}" for k, v in entries.items())]
    return [f"- note `{name}`: {_flat(cast(object, value))}"]


def journey_markdown(runs: Sequence[JourneyRun], mode: Mode, k: int) -> str:
    head = runs[0] if runs else None
    out = [
        f"# Buyer journey eval ({mode}, k={k}, {date.today().isoformat()})",
        "",
        f"Workbook: {head.workbook if head else '-'} (export {head.export if head else '-'}). "
        "pass^k = every run passed every check of the step. Owners: A scope/copilot, B "
        "supplier stats/prices, C assessment-to-RFQ handoff, D1 source parsing; extraction, "
        "negotiation, comparison: not assigned yet.",
        "",
        "| Step | pass^k | Runs passed | Failing checks [owner] ×runs | Fallbacks |",
        "|---|---|---|---|---|",
    ]
    for key, title in STEPS:
        steps = [s for r in runs for s in r.steps if s.key == key]
        failing: Counter[str] = Counter()
        for s in steps:
            if s.error:
                failing["error"] += 1
            if s.skipped:
                failing[f"skipped: {s.skipped}"] += 1
            for c in s.failing():
                failing[f"{c.name} [{c.owner}]"] += 1
        fallbacks = sum(bool(s.fallback) for s in steps)
        passed = sum(s.ok for s in steps)
        listed = ", ".join(f"{n} ×{c}" for n, c in failing.items()) or "—"
        out.append(
            f"| {title} | {_mark(bool(steps) and passed == len(steps))} | {passed}/{len(steps)} "
            f"| {listed} | {fallbacks or '—'} |"
        )
    for run in runs:
        out += ["", f"## Run {run.run} ({run.seconds} s)", ""]
        for step in run.steps:
            state = "skipped" if step.skipped else _mark(step.ok)
            out.append(f"### {step.title}: {state} ({step.seconds} s)")
            if step.error:
                out.append(f"- error: `{step.error}`")
            if step.skipped:
                out.append(f"- skipped: {step.skipped}")
            for fallback in step.fallback:
                out.append(f"- fallback: {fallback}")
            for check in step.checks:
                detail = f": {check.detail}" if check.detail else ""
                out.append(f"- {_mark(check.ok)} `{check.name}` [{check.owner}]{detail}")
            for name, value in step.notes.items():
                out += note_lines(name, value)
            out.append("")
        for turn in run.turns:
            out.append(f"#### Copilot turn ({turn.step}, {turn.seconds} s): {turn.message[:120]}")
            for call in turn.calls:
                state = "ok" if call.ok else f"error: {call.error}"
                args = json.dumps(_short(call.args), default=str, ensure_ascii=False)[:400]
                out.append(f"- `{call.tool}` {args} -> {state}")
            if turn.failed:
                out.append(f"- run failed: {turn.failed}")
            out += ["", "```text", turn.answer[:3000], "```", ""]
    return "\n".join(out) + "\n"


def write_journey_report(
    runs: Sequence[JourneyRun], mode: Mode, k: int, folder: Path | None = None
) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = folder or REPORTS / f"{stamp}-journey-{mode}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "report.md").write_text(journey_markdown(runs, mode, k))
    (folder / "report.json").write_text(
        json.dumps(
            {"mode": mode, "k": k, "runs": [asdict(r) for r in runs]},
            indent=1,
            default=str,
            ensure_ascii=False,
        )
    )
    return folder


async def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the buyer journey eval.")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--mock", action="store_true", help="scripted models (harness check)")
    modes.add_argument("--live", action="store_true", help="the real models")
    parser.add_argument("-k", type=int, default=1, help="runs (default 1)")
    parser.add_argument(
        "--workbook",
        type=Path,
        help="the synthetic MTR-100 ERP export (live only); default: the generated one",
    )
    parser.add_argument("--report-dir", type=Path, help="where report.md/report.json go")
    parser.add_argument(
        "--price-style",
        choices=("symbol", "code"),
        default="symbol",
        help='suppliers write "R$ 46,90" (default, as in Brazil) or "46,90 BRL"',
    )
    args = parser.parse_args(argv)
    mode: Mode = "mock" if args.mock else "live"
    if args.workbook is not None and mode == "mock":
        parser.error("--workbook needs --live: mock mode maps only the generated workbook")
    settings = get_settings()
    if mode == "live":
        if not (settings.openai_api_key or os.environ.get("OPENAI_API_KEY")):
            print(
                "live evals need OPENAI_API_KEY; use --mock to check the harness", file=sys.stderr
            )
            return 2
        if not os.environ.get("AGNO_DATABASE_URL"):
            print("live evals need AGNO_DATABASE_URL (the copilot's memory)", file=sys.stderr)
            return 2
    engine = create_engine(settings)
    env = Env(
        engine=engine,
        storage=MemoryStorage(),
        runner_config=RunnerConfig.from_settings(settings),
        mode=mode,
        today=date.today(),
    )
    try:
        runs = await run_journeys(
            env, k=args.k, workbook=args.workbook, price_style=args.price_style
        )
    finally:
        await engine.dispose()
    folder = write_journey_report(runs, mode, args.k, args.report_dir)
    print((folder / "report.md").read_text())
    print(f"report: {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
