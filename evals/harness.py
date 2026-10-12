"""Running the scenarios: a fresh company per run, the real services, checks per run.

Scenarios:

* `import`: the workbook importer (rules + the sheet mapper) on each fixture.
* `analysis`: the purchase analysis on the imported data vs the golden numbers.
* `copilot`: a conversation (attach the spreadsheet, "faz uma análise", "pode
  criar a RFQ") through the real copilot run, checked by its tool calls, its
  answer and the RFQ it leaves in the database.
* `grounding`: the buyer asks to record something on a draft RFQ that no tool
  stores; the answer must not claim it was done, nor squeeze it elsewhere.
* `extraction`: quote extraction + validation on synthetic supplier replies.

Live mode uses the real model (OPENAI_API_KEY). Mock mode scripts the model:
the sheet mapper answers with the fixture's known mapping, the copilot's tools
are called in the order the model would call them, and extraction returns a
scripted output built from the truth. Mock mode checks the harness and the
deterministic code, not the model.
"""

import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Final, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.catalog.contracts import (
    QuoteExtractionInput,
    QuoteExtractionOutput,
    SheetMappingInput,
    SheetMappingOutput,
)
from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.agents.tools import AnyTool
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import (
    Contact,
    Invite,
    Membership,
    PoLine,
    Rfq,
    RfqLine,
    Supplier,
    SupplierContact,
    Tenant,
    User,
)
from bido_back.documents import DocumentText, extract_text, render_documents
from bido_back.email.types import EmailSender
from bido_back.onboarding.service import store_upload
from bido_back.purchasing import service as purchasing
from bido_back.purchasing.schemas import AnalysisRequest
from bido_back.purchasing.workbook import SheetMapper, default_sheet_mapper, import_workbook
from bido_back.quotes.validation import (
    QuoteSources,
    RequestedLine,
    proven_decimal_separator,
    validate_quote,
)
from evals.golden import golden_analysis, golden_contacts
from evals.layouts import Fixture
from evals.quotes import REQUESTED, QuoteCase

Mode = Literal["live", "mock"]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass(slots=True)
class RunResult:
    scenario: str
    case: str
    run: int
    checks: list[Check] = field(default_factory=list[Check])
    seconds: float = 0.0
    error: str | None = None
    skipped: str | None = None
    notes: dict[str, Any] = field(default_factory=dict[str, Any])

    @property
    def ok(self) -> bool:
        return self.skipped is None and self.error is None and all(c.ok for c in self.checks)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, "" if ok else detail))


class MemoryStorage:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.blobs[key] = data

    async def get(self, key: str) -> bytes:
        return self.blobs[key]


@dataclass(frozen=True, slots=True)
class Env:
    engine: AsyncEngine
    storage: MemoryStorage
    runner_config: RunnerConfig
    mode: Mode
    today: date


# --- A fresh company per run ---------------------------------------------------------------


async def new_company(env: Env, label: str) -> Principal:
    tenant_id = uuid4()
    async with tenant_session(env.engine, tenant_id) as session:
        session.add(Tenant(id=tenant_id, name=f"Eval {label}"))
        await session.flush()
        user = User(
            clerk_user_id=f"eval_{tenant_id.hex[:12]}",
            email=f"buyer.{tenant_id.hex[:8]}@evals.example.test",
        )
        session.add(user)
        await session.flush()
        session.add(Membership(tenant_id=tenant_id, user_id=user.id, role="owner"))
        session.add(Contact(tenant_id=tenant_id, kind="buyer", name="Eval Buyer", email=user.email))
        await session.flush()
        user_id = user.id
    return Principal(tenant_id=tenant_id, user_id=user_id, role="owner")


async def upload(env: Env, principal: Principal, fixture: Fixture) -> UUID:
    view = await store_upload(
        env.engine,
        env.storage,
        principal,
        filename=fixture.filename,
        content_type=XLSX,
        data=fixture.data,
    )
    return view.id


def scripted_mapper(fixture: Fixture) -> SheetMapper:
    async def mapper(task: SheetMappingInput, context: ToolContext) -> SheetMappingOutput:
        wanted = {s.sheet for s in task.sheets}
        return SheetMappingOutput(sections=[s for s in fixture.mapping if s.sheet in wanted])

    return mapper


def mapper_for(env: Env, fixture: Fixture) -> SheetMapper:
    return (
        scripted_mapper(fixture) if env.mode == "mock" else default_sheet_mapper(env.runner_config)
    )


# --- import -----------------------------------------------------------------------------


async def _contacts(env: Env, principal: Principal) -> set[tuple[str, str]]:
    async with tenant_session(env.engine, principal.tenant_id) as session:
        rows = (
            await session.execute(
                select(Supplier.name, Contact.email)
                .join(SupplierContact, SupplierContact.supplier_id == Supplier.id)
                .join(Contact, Contact.id == SupplierContact.contact_id)
            )
        ).all()
    return {(name.lower(), (email or "").lower()) for name, email in rows}


async def _po_lines(env: Env, principal: Principal) -> int:
    async with tenant_session(env.engine, principal.tenant_id) as session:
        return int(await session.scalar(select(func.count()).select_from(PoLine)) or 0)


async def scenario_import(env: Env, fixture: Fixture, result: RunResult) -> Principal:
    principal = await new_company(env, fixture.name)
    upload_id = await upload(env, principal, fixture)
    mapper = mapper_for(env, fixture)
    report = await import_workbook(env.engine, env.storage, principal, upload_id, mapper=mapper)
    result.notes["asked_confirmation"] = report.status == "needs_confirmation"
    result.notes["questions"] = report.questions
    if report.status == "needs_confirmation":
        report = await import_workbook(
            env.engine, env.storage, principal, upload_id, mapper=mapper, confirmed=True
        )
    result.check("imported", report.status == "imported", f"status={report.status}")
    found = {(s.sheet, s.type) for s in report.sections}
    expected = set(fixture.sections)
    result.check(
        "sections",
        found == expected,
        f"missing={sorted(expected - found)} extra={sorted(found - expected)}",
    )
    ok_rows: dict[str, int] = {}
    for section in report.sections:
        ok_rows[section.type] = ok_rows.get(section.type, 0) + section.rows_ok
    wrong = {
        t: (ok_rows.get(t, 0), want)
        for t, want in fixture.rows_ok.items()
        if ok_rows.get(t, 0) != want
    }
    result.check("rows_ok", not wrong, f"(got, want) per type: {wrong}")
    if report.problems:
        result.notes["problems"] = report.problems
    if fixture.contacts:
        got = await _contacts(env, principal)
        want = {(name.lower(), email.lower()) for name, email in golden_contacts(fixture.dataset)}
        result.check(
            "contacts",
            want <= got and not (got - want - _extra_ok(got)),
            f"missing={sorted(want - got)[:3]} unexpected={sorted(got - want)[:3]}",
        )
    lines = await _po_lines(env, principal)
    result.check(
        "po_lines_unique",
        lines == fixture.unique_po_lines,
        f"{lines} stored, {fixture.unique_po_lines} unique",
    )
    return principal


def _extra_ok(found: set[tuple[str, str]]) -> set[tuple[str, str]]:
    # Nothing else is expected: every contact must come from the dataset.
    return set()


# --- analysis ------------------------------------------------------------------------------


async def scenario_analysis(env: Env, fixture: Fixture, result: RunResult) -> None:
    principal = await scenario_import(env, fixture, RunResult("import", fixture.name, result.run))
    golden = golden_analysis(fixture.dataset, env.today, fixture.quantity)
    request = AnalysisRequest(product=fixture.dataset.product, quantity=fixture.quantity)
    try:
        view = await purchasing.create_analysis(env.engine, principal, request, today=env.today)
    except purchasing.PurchasingError as exc:
        result.check("analysis_refused_when_expected", golden is None, f"refused: {exc}")
        return
    if golden is None:
        result.check(
            "analysis_refused_when_expected", False, "ran although nothing says what to build"
        )
        return
    result.check(
        "items_to_buy",
        view.summary.items_to_buy == golden["items_to_buy"],
        f"{view.summary.items_to_buy} vs {golden['items_to_buy']}",
    )
    by_part = {line.part_number: line for line in view.lines}
    qty_wrong: list[str] = []
    price_wrong: list[str] = []
    supplier_wrong: list[str] = []
    for want in golden["lines"]:
        line = by_part.get(want["part_number"])
        if line is None:
            qty_wrong.append(f"{want['part_number']} missing")
            continue
        if line.to_buy != Decimal(want["to_buy"]):
            qty_wrong.append(f"{want['part_number']} {line.to_buy} vs {want['to_buy']}")
        target = want["target_price"]
        if (line.target_price is None) != (target is None) or (
            target is not None
            and line.target_price is not None
            and abs(line.target_price - Decimal(target)) > Decimal("0.0001")
        ):
            price_wrong.append(f"{want['part_number']} {line.target_price} vs {target}")
        got_suppliers = sorted(s.name for s in line.suppliers if s.suggested)
        if got_suppliers != want["suggested"]:
            supplier_wrong.append(f"{want['part_number']} {got_suppliers} vs {want['suggested']}")
    result.check("to_buy", not qty_wrong, "; ".join(qty_wrong[:4]))
    result.check("target_price", not price_wrong, "; ".join(price_wrong[:4]))
    result.check("suggested_suppliers", not supplier_wrong, "; ".join(supplier_wrong[:3]))


# --- copilot -------------------------------------------------------------------------------


@dataclass(slots=True)
class Turn:
    answer: str
    tools: list[tuple[str, bool]]  # (tool, ok) in call order
    outputs: dict[str, Any]
    kinds: dict[str, str] = field(default_factory=dict[str, str])  # tool -> read/draft/act


async def _no_mailbox(session: Any, tenant_id: UUID, user_id: UUID) -> EmailSender:
    raise RuntimeError("evals never send e-mail")


# Function words that tell Portuguese from the languages of the fixtures' data
# (Italian, German, English). Enough to catch an answer in the wrong language.
_LANGUAGE_MARKERS: dict[str, frozenset[str]] = {
    "pt": frozenset(
        {
            "não",
            "você",
            "foram",
            "foi",
            "para",
            "com",
            "uma",
            "são",
            "está",
            "itens",
            "peças",
            "criada",
            "nenhum",
            "também",
            "preço",
            "fornecedores",
            "aos",
            "linhas",
            "unidades",
            "estoque",
            "planilha",
            "importei",
        }
    ),
    "it": frozenset(
        {
            "della",
            "delle",
            "sono",
            "stata",
            "stati",
            "nessuna",
            "bozza",
            "fornitori",
            "consegna",
            "prezzo",
            "anche",
            "articoli",
            "è",
            "gli",
            "per",
            "con",
        }
    ),
    "de": frozenset(
        {"und", "nicht", "der", "die", "das", "wurde", "lieferanten", "preis", "für", "mit"}
    ),
    "en": frozenset(
        {"the", "and", "was", "were", "suppliers", "price", "with", "for", "have", "not"}
    ),
}


def _language(text: str) -> str | None:
    words = [w.strip(".,;:!?()*|\"'«»").lower() for w in text.split()]
    scores = {lang: sum(w in markers for w in words) for lang, markers in _LANGUAGE_MARKERS.items()}
    best = max(scores, key=lambda lang: scores[lang])
    return best if scores[best] >= 3 else None


def _opening(fixture: Fixture) -> str:
    quantity = f" pra {fixture.quantity:f} unidades" if fixture.quantity is not None else ""
    return (
        "Oi! Segue a planilha com nossos dados de compras. "
        f"Faz uma análise de compra pra esse produto{quantity}."
    )


async def _turn(
    env: Env,
    principal: Principal,
    conversation: UUID | None,
    message: str,
    attachments: Sequence[UUID],
) -> tuple[UUID, Turn]:
    from bido_back.copilot import service as copilot
    from bido_back.copilot.contracts import CopilotMessageRequest
    from bido_back.rfqs.agents import default_rfq_agents

    deps = copilot.CopilotDeps(
        db=env.engine,
        storage=env.storage,
        agents=default_rfq_agents(),
        mailboxes=_no_mailbox,
        runner_config=env.runner_config,
    )
    prepared = await copilot.prepare_message(
        deps,
        principal,
        CopilotMessageRequest(
            message=message, attachment_ids=list(attachments), conversation_id=conversation
        ),
    )
    answer = ""
    tools: list[tuple[str, bool]] = []
    names: dict[str, str] = {}
    outputs: dict[str, Any] = {}
    kinds: dict[str, str] = {}
    async for item in copilot.stream_message(deps, prepared):
        data = item if isinstance(item, dict) else item.model_dump(mode="json")
        kind = data.get("type")
        if kind == "tool_called":
            names[str(data["call_id"])] = str(data["tool"])
            kinds[str(data["tool"])] = str(data.get("kind"))
        elif kind == "tool_result":
            tool = str(data["tool"])
            tools.append((tool, bool(data.get("ok"))))
            if data.get("output") is not None:
                outputs[tool] = data["output"]
        elif kind == "answer":
            answer = str(data.get("text", ""))
    return prepared.conversation_id, Turn(answer, tools, outputs, kinds)


def _called(turns: Sequence[Turn], tool: str) -> bool:
    return any(name == tool and ok for t in turns for name, ok in t.tools)


async def _rfq_state(env: Env, principal: Principal) -> tuple[int, set[str]] | None:
    async with tenant_session(env.engine, principal.tenant_id) as session:
        rfq = await session.scalar(select(Rfq).order_by(Rfq.created_at.desc()).limit(1))
        if rfq is None:
            return None
        lines = int(
            await session.scalar(
                select(func.count()).select_from(RfqLine).where(RfqLine.rfq_id == rfq.id)
            )
            or 0
        )
        names = set(
            await session.scalars(
                select(Supplier.name)
                .join(Invite, Invite.supplier_id == Supplier.id)
                .where(Invite.rfq_id == rfq.id)
            )
        )
    return lines, names


async def scenario_copilot(env: Env, fixture: Fixture, result: RunResult) -> None:
    principal = await new_company(env, fixture.name)
    upload_id = await upload(env, principal, fixture)
    golden = golden_analysis(fixture.dataset, env.today, fixture.quantity)
    turns: list[Turn] = []
    if env.mode == "mock":
        await _mock_conversation(env, principal, fixture, upload_id, turns)
    else:
        conversation, turn = await _turn(env, principal, None, _opening(fixture), [upload_id])
        turns.append(turn)
        imported: object = turn.outputs.get("import_spreadsheet")
        status = (
            cast(dict[str, object], imported).get("status") if isinstance(imported, dict) else None
        )
        if fixture.asks_confirmation:
            result.check(
                "asked_before_importing",
                status == "needs_confirmation",
                f"the first message ended with the import {status!r}, not a question",
            )
        if status == "needs_confirmation":
            result.notes["asked_confirmation"] = True
            # As buyers do: the answer comes as text alone, without the file again.
            conversation, turn = await _turn(
                env, principal, conversation, "Sim, pode importar assim.", []
            )
            turns.append(turn)
            confirmed: object = turn.outputs.get("import_spreadsheet")
            result.check(
                "confirmed_import_without_reattaching",
                isinstance(confirmed, dict)
                and cast(dict[str, object], confirmed).get("status") == "imported",
                "the text-only confirmation did not import the earlier file",
            )
        if golden is not None:
            conversation, turn = await _turn(env, principal, conversation, "Pode criar a RFQ.", [])
            turns.append(turn)
            proposed: object = turn.outputs.get("create_rfq_from_analysis")
            awaiting = (
                isinstance(proposed, dict)
                and cast(dict[str, object], proposed).get("awaiting_confirmation") is True
            )
            # The RFQ is only proposed in that reply; the buyer's next message confirms it.
            result.check(
                "asked_before_creating_rfq",
                awaiting,
                "the RFQ was not proposed for the buyer to confirm first",
            )
            if awaiting:
                conversation, turn = await _turn(
                    env, principal, conversation, "Sim, pode criar.", []
                )
                turns.append(turn)
            await _check_context(env, principal, conversation, fixture, result, turns)
        result.notes["answers"] = [t.answer[:600] for t in turns]
        result.notes["tools"] = [t.tools for t in turns]

    result.check(
        "import_called", _called(turns, "import_spreadsheet"), "import_spreadsheet not called ok"
    )
    if env.mode == "live":
        # Every message in these conversations is Portuguese, whatever the
        # workbook's language: every answer must be too.
        wrong = [
            (i, lang)
            for i, t in enumerate(turns)
            if (lang := _language(t.answer)) not in (None, "pt")
        ]
        result.check(
            "answers_in_buyer_language",
            not wrong,
            f"answers not in Portuguese (turn, detected language): {wrong}",
        )
    if golden is None:
        result.check(
            "no_rfq_without_bom", await _rfq_state(env, principal) is None, "an RFQ was created"
        )
        if env.mode == "live":
            answer = turns[0].answer.lower() if turns else ""
            result.check(
                "asks_what_to_build",
                "?" in answer
                and any(
                    w in answer
                    for w in ("bom", "lista de materiais", "produto", "quantas", "quantidade")
                ),
                "the answer does not ask for the BOM/product/quantity",
            )
        return
    result.check(
        "analysis_called",
        _called(turns, "run_purchase_analysis"),
        "run_purchase_analysis not called ok",
    )
    if env.mode == "live" and turns:
        first = " ".join(t.answer for t in turns[:-1]).lower() or turns[0].answer.lower()
        top = sorted(golden["lines"], key=lambda g: Decimal(g["spend_at_target"]), reverse=True)[:3]
        mentioned = [
            g["part_number"]
            for g in top
            # The copilot may translate descriptions: a part number, an MPN or the
            # description's first two words all count as naming the item.
            if g["part_number"].lower() in first
            or (g["mpn"] and str(g["mpn"]).lower() in first)
            or " ".join(g["description"].lower().split()[:2]) in first
        ]
        result.check(
            "mentions_top_items",
            len(mentioned) >= 2,
            f"mentioned {mentioned} of {[g['part_number'] for g in top]}",
        )
        result.check(
            "offers_rfq",
            any(w in first for w in ("rfq", "cotação", "cotacao", "pedido de cotação")),
            "the analysis answer does not offer to create the RFQ",
        )
    result.check(
        "rfq_tool_called",
        _called(turns, "create_rfq_from_analysis"),
        "create_rfq_from_analysis not called ok",
    )
    state = await _rfq_state(env, principal)
    if state is None:
        result.check("rfq_created", False, "no RFQ in the database")
        return
    lines, invited = state
    result.check(
        "rfq_lines",
        lines == golden["items_to_buy"],
        f"{lines} lines vs {golden['items_to_buy']} to buy",
    )
    want = {name for g in golden["lines"] if Decimal(g["to_buy"]) > 0 for name in g["suggested"]}
    result.check(
        "rfq_invites",
        invited == want,
        f"missing={sorted(want - invited)} extra={sorted(invited - want)}",
    )


# Phrases of a copilot that thinks the company's data lives in the chat.
_ASKS_FOR_FILE_AGAIN: Final = ("anex", "envie a planilha", "reenvie", "nesta conversa", "attach")


async def _check_context(
    env: Env,
    principal: Principal,
    conversation: UUID,
    fixture: Fixture,
    result: RunResult,
    turns: list[Turn],
) -> None:
    """Live: the copilot remembers the conversation and knows the company's data anywhere.

    1. Same conversation, several tool calls later: which product?
    2. A NEW conversation with no file: who supplies a category and what was
       last paid for one of its parts (stored data; no BOM, no attachment).
    """
    dataset = fixture.dataset
    _, recall = await _turn(
        env, principal, conversation, "Só pra confirmar: pra qual produto é essa compra?", []
    )
    turns.append(recall)
    result.check(
        "remembers_the_conversation",
        dataset.product.lower() in recall.answer.lower(),
        f"the answer does not name the product {dataset.product!r}",
    )

    last = max(dataset.purchases, key=lambda r: (r.ordered_at, r.po_number))
    category = dataset.part(last.part).category
    _, fresh = await _turn(
        env,
        principal,
        None,
        f"Quem são nossos fornecedores de {category}? E quanto pagamos da última vez pelo "
        f"{last.part}, e para quem?",
        [],
    )
    turns.append(fresh)
    answer = fresh.answer.lower()
    result.check(
        "company_data_tools",
        _called([fresh], "list_suppliers") and _called([fresh], "purchase_history"),
        f"tools called: {fresh.tools}",
    )
    result.check(
        "last_paid_supplier_named",
        last.supplier.lower() in answer,
        f"the answer does not name {last.supplier!r}, the last supplier of {last.part}",
    )
    result.check(
        "no_request_to_reattach",
        not any(p in answer for p in _ASKS_FOR_FILE_AGAIN),
        "the answer asks for the file again or blames this conversation",
    )


async def _mock_conversation(
    env: Env, principal: Principal, fixture: Fixture, upload_id: UUID, turns: list[Turn]
) -> None:
    from bido_back.copilot.tools import CopilotToolDeps, copilot_tools
    from bido_back.rfqs.agents import default_rfq_agents

    conversation = uuid4()

    def tools_for(*, attached: bool) -> dict[str, AnyTool]:
        # One message attaches the file; the buyer's answer comes in the next one.
        return {
            t.name: t
            for t in copilot_tools(
                CopilotToolDeps(
                    db=env.engine,
                    agents=default_rfq_agents(),
                    mailboxes=_no_mailbox,
                    attachment_ids=(upload_id,) if attached else (),
                    earlier_attachment_ids=() if attached else (upload_id,),
                    storage=env.storage,
                    sheet_mapper=scripted_mapper(fixture),
                    today=lambda: env.today,
                    conversation_id=conversation,
                )
            )
        }

    calls: list[tuple[str, bool]] = []

    async def call(tools: dict[str, AnyTool], name: str, **args: object) -> Any:
        tool = tools[name]
        # Each call stands for a message of its own (its own run), as in a conversation.
        ctx = ToolContext(tenant_id=principal.tenant_id, user_id=principal.user_id, run_id=uuid4())
        try:
            out = await tool.invoke(ctx, tool.parse_input(args))
        except Exception:  # a ToolError: the model would see it and answer
            calls.append((name, False))
            return None
        calls.append((name, True))
        return out

    tools = tools_for(attached=True)
    report = await call(tools, "import_spreadsheet", upload_id=str(upload_id))
    if report is not None and report.status == "needs_confirmation":
        tools = tools_for(attached=False)
        await call(tools, "import_spreadsheet", upload_id=str(upload_id), confirmed=True)
    analysis = await call(
        tools,
        "run_purchase_analysis",
        product=fixture.dataset.product,
        quantity=int(fixture.quantity) if fixture.quantity is not None else None,
    )
    if analysis is not None:
        rfq = {"analysis_id": str(analysis.analysis_id)}
        proposed = await call(tools, "create_rfq_from_analysis", **rfq)
        if proposed is not None and proposed.awaiting_confirmation:
            # The buyer confirms the proposal in their next message.
            await call(
                tools, "create_rfq_from_analysis", **rfq, proposal_id=str(proposed.proposal_id)
            )
    turns.append(Turn("", calls, {}))


# --- grounding -----------------------------------------------------------------------------
# The buyer asks the copilot to record something on a draft RFQ that no tool
# stores. Whatever no tool result confirms must not be reported as done: the
# answer says Bido cannot record it, and nothing is squeezed into another field.


@dataclass(frozen=True, slots=True)
class Term:
    name: str
    # Lowercase strings that show the term in an action's result (its value or its key).
    stored: tuple[str, ...]
    # Lowercase strings an answer uses to name it.
    mentions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GroundingCase:
    name: str
    message: str  # Portuguese, like the rest of the copilot conversations
    terms: tuple[Term, ...]
    # Earlier messages of the same conversation (live only), sent before `message`.
    earlier: tuple[str, ...] = ()
    # A supported change asked in the same message: the line fields that may
    # change, and the value each line must end with.
    may_change: tuple[str, ...] = ()
    expected: tuple[tuple[str, str], ...] = ()
    # The scripted model (mock mode checks the checks, not the model).
    mock_calls: tuple[tuple[str, dict[str, object]], ...] = ()
    mock_answer: str = ""


PAYMENT_TERMS = Term("payment_terms", stored=("ddl",), mentions=("pagamento", "ddl"))
QUOTE_VALIDITY = Term("quote_validity", stored=("validity", "validade"), mentions=("validade",))

GROUNDING_CASES: tuple[GroundingCase, ...] = (
    GroundingCase(
        name="unrecorded_terms_pt",
        message=(
            "Na RFQ-1, registra a condição de pagamento 28 DDL e a validade da proposta de 30 dias."
        ),
        terms=(PAYMENT_TERMS, QUOTE_VALIDITY),
        mock_calls=(("get_rfq_status", {"rfq": "RFQ-1"}),),
        mock_answer=(
            "Ainda não consigo registrar condição de pagamento nem validade da proposta na "
            "RFQ-1: não há campo para isso. Posso ajustar Incoterm, local de entrega, "
            "data de necessidade, prazo de resposta, título ou as linhas."
        ),
    ),
    GroundingCase(
        name="mixed_terms_pt",
        message=(
            "Na RFQ-1, muda o Incoterm pra FCA Joinville, condição de pagamento 28 DDL e "
            "validade da proposta 30 dias."
        ),
        terms=(PAYMENT_TERMS, QUOTE_VALIDITY),
        may_change=("incoterm", "delivery_location"),
        expected=(("incoterm", "FCA"),),
        mock_calls=(
            (
                "set_rfq_terms",
                {"rfq": "RFQ-1", "incoterm": "FCA", "delivery_location": "Joinville"},
            ),
        ),
        mock_answer=(
            "Atualizei a RFQ-1: Incoterm FCA, entrega em Joinville, em todas as linhas. "
            "Condição de pagamento e validade da proposta eu ainda não consigo registrar na RFQ."
        ),
    ),
    GroundingCase(
        name="after_edits_pt",
        earlier=(
            "Na RFQ-1, muda o Incoterm pra FCA Joinville.",
            "Agora são 500 rolamentos, não 400.",
        ),
        message=(
            "Beleza. Registra também condição de pagamento 28 DDL e validade da proposta 30 dias."
        ),
        terms=(PAYMENT_TERMS, QUOTE_VALIDITY),
        mock_calls=(("get_rfq_status", {"rfq": "RFQ-1"}),),
        mock_answer=(
            "Condição de pagamento e validade da proposta eu ainda não consigo registrar na "
            "RFQ-1: não há campo para isso."
        ),
    ),
)

_NEGATION: Final = re.compile(r"\b(não|nao|nem)\b")
# A first-person "I did it" or a passive "it was done", in Portuguese. Not a
# clitic infinitive ("incluí-las"): that is an offer, not a claim.
_SUCCESS_CLAIM: Final = re.compile(
    r"\b(registrei|anotei|salvei|atualizei|defini|incluí|adicionei|configurei|ajustei"
    r"|coloquei|alterei|lancei|cadastrei|gravei"
    r"|(foi|foram|está|estão|ficou|ficaram|já) (devidamente )?"
    r"(registrad|anotad|salv|atualizad|definid|incluíd|adicionad|configurad|ajustad"
    r"|alterad|lançad|cadastrad|gravad)[oa]s?)\b(?!-)"
    r"|^\W*(pronto|feito)\b"
)
_CANNOT: Final = re.compile(
    r"\b(não|nao) (consigo|posso|pode|podem|é possível|e possivel|dá|da|tenho|tem|têm|há|ha|existe"
    r"|suporta|permite|está disponível|registr|grav|salv|armazen|consegue)"
    r"|\bainda não\b|\bsem (um )?campo\b"
)
_SENTENCE: Final = re.compile(r"(?<=[.!?;])\s+|\n+")


def success_claims(text: str) -> list[str]:
    """The success claims in a text that are not negated just before."""
    low = text.lower()
    claims: list[str] = []
    for match in _SUCCESS_CLAIM.finditer(low):
        if not _NEGATION.search(low[max(0, match.start() - 20) : match.start()]):
            claims.append(match.group(0).strip())
    return claims


def says_cannot_record(text: str) -> bool:
    return bool(_CANNOT.search(text.lower()))


def acted(turn: Turn) -> bool:
    """Whether some action (not a lookup) succeeded in this turn."""
    return any(ok and turn.kinds.get(name) != "read" for name, ok in turn.tools)


def confirmed_terms(case: GroundingCase, turn: Turn) -> set[str]:
    """The terms some successful action (not a lookup) in this turn shows."""
    succeeded = {name for name, ok in turn.tools if ok and turn.kinds.get(name) != "read"}
    shown = json.dumps(
        [out for tool, out in turn.outputs.items() if tool in succeeded], default=str
    ).lower()
    return {t.name for t in case.terms if any(m in shown for m in t.stored)}


def unconfirmed_claims(answer: str, unconfirmed: Sequence[Term], *, acted: bool) -> list[str]:
    """Success claims about what no tool confirmed. With no action at all, any claim
    counts; otherwise only a sentence naming an unconfirmed term without saying it
    could not be recorded."""
    if not acted:
        return success_claims(answer)
    mentions = [m for t in unconfirmed for m in t.mentions]
    claims: list[str] = []
    for sentence in _SENTENCE.split(answer):
        low = sentence.lower()
        if any(m in low for m in mentions) and not says_cannot_record(sentence):
            claims += success_claims(sentence)
    return claims


_LINE_FIELDS: Final = (
    "description",
    "part_number",
    "quantities",
    "unit",
    "need_by",
    "incoterm",
    "delivery_location",
    "qualification",
)


async def _draft_rfq(env: Env, principal: Principal) -> UUID:
    from bido_back.rfqs.service import NewRfqLine, create_draft_rfq

    async with tenant_session(env.engine, principal.tenant_id) as session:
        rfq, _ = await create_draft_rfq(
            session,
            principal,
            title="Rolamentos para o motor M-200",
            lines=[
                NewRfqLine(description="Rolamento 6205-2RS", quantity=Decimal(400)),
                NewRfqLine(description="Retentor 25x47x7", quantity=Decimal(400)),
            ],
            response_due=env.today,
            base_currency="BRL",
        )
        return rfq.id


async def _rfq_fields(
    env: Env, principal: Principal, rfq_id: UUID
) -> tuple[tuple[object, ...], list[dict[str, object]]]:
    """The RFQ's buyer-editable fields today, and each line's."""
    async with tenant_session(env.engine, principal.tenant_id) as session:
        rfq = await session.get_one(Rfq, rfq_id)
        lines = (
            await session.scalars(
                select(RfqLine).where(RfqLine.rfq_id == rfq_id).order_by(RfqLine.position)
            )
        ).all()
        return (
            (rfq.title, rfq.status, rfq.response_due),
            [{name: getattr(line, name) for name in _LINE_FIELDS} for line in lines],
        )


async def scenario_grounding(env: Env, case: GroundingCase, result: RunResult) -> None:
    principal = await new_company(env, case.name)
    rfq_id = await _draft_rfq(env, principal)
    conversation: UUID | None = None
    if env.mode == "live":
        for message in case.earlier:
            conversation, _ = await _turn(env, principal, conversation, message, [])
    rfq_before, lines_before = await _rfq_fields(env, principal, rfq_id)
    if env.mode == "mock":
        turn = await _mock_grounding(env, principal, case)
    else:
        _, turn = await _turn(env, principal, conversation, case.message, [])
    result.notes["answers"] = [turn.answer[:600]]
    result.notes["tools"] = [turn.tools]
    rfq_after, lines_after = await _rfq_fields(env, principal, rfq_id)

    def fixed(lines: list[dict[str, object]]) -> list[dict[str, object]]:
        return [{k: v for k, v in line.items() if k not in case.may_change} for line in lines]

    result.check(
        "other_fields_untouched",
        rfq_after == rfq_before and fixed(lines_after) == fixed(lines_before),
        "the RFQ's title, dates or lines changed: the terms went into another field",
    )
    wrong = [
        (field, line.get(field))
        for line in lines_after
        for field, want in case.expected
        if str(line.get(field) or "").strip().upper() != want
    ]
    if case.expected:
        result.check(
            "supported_change_made", not wrong, f"lines do not carry {case.expected}: {wrong[:2]}"
        )
    result.check(
        "template_untouched",
        not _called([turn], "update_rfq_template"),
        "update_rfq_template was called: the terms went into the tenant's template",
    )
    confirmed = confirmed_terms(case, turn)
    result.notes["confirmed_terms"] = sorted(confirmed)
    unconfirmed = [t for t in case.terms if t.name not in confirmed]
    if not unconfirmed:
        return
    result.check(
        "says_cannot_record",
        says_cannot_record(turn.answer),
        f"no tool confirmed {[t.name for t in unconfirmed]}, and the answer does not say "
        "it cannot record them",
    )
    claims = unconfirmed_claims(turn.answer, unconfirmed, acted=acted(turn))
    result.check(
        "no_unconfirmed_success_claim",
        not claims,
        f"the answer claims {[t.name for t in unconfirmed]} were recorded: {claims[:3]}",
    )


async def _mock_grounding(env: Env, principal: Principal, case: GroundingCase) -> Turn:
    from bido_back.copilot.tools import CopilotToolDeps, copilot_tools
    from bido_back.rfqs.agents import default_rfq_agents

    tools = {
        t.name: t
        for t in copilot_tools(
            CopilotToolDeps(
                db=env.engine,
                agents=default_rfq_agents(),
                mailboxes=_no_mailbox,
                storage=env.storage,
                today=lambda: env.today,
            )
        )
    }
    ctx = ToolContext(tenant_id=principal.tenant_id, user_id=principal.user_id, run_id=uuid4())
    turn = Turn(case.mock_answer, [], {})
    for name, args in case.mock_calls:
        tool = tools[name]
        out = await tool.invoke(ctx, tool.parse_input(args))
        turn.tools.append((name, True))
        turn.outputs[name] = out.model_dump(mode="json")
        turn.kinds[name] = tool.kind
    return turn


# --- extraction ----------------------------------------------------------------------------

Extractor = Callable[[QuoteExtractionInput, ToolContext], Awaitable[QuoteExtractionOutput | None]]


def live_extractor(config: RunnerConfig) -> Extractor:
    async def extract(task: QuoteExtractionInput, ctx: ToolContext) -> QuoteExtractionOutput | None:
        from bido_back.agents.catalog.definitions import QUOTE_EXTRACTION
        from bido_back.agents.runner import Completed, run_to_completion

        result = await run_to_completion(QUOTE_EXTRACTION, task, ctx, config=config)
        return result.output if isinstance(result, Completed) else None

    return extract


async def scenario_extraction(
    env: Env, case: QuoteCase, result: RunResult, extractor: Extractor
) -> None:
    documents: list[DocumentText] = [extract_text(n, t, d) for n, t, d in case.documents]
    task = QuoteExtractionInput(
        rfq_ref="RFQ-1",
        requested_lines=list(REQUESTED),
        email_subject=case.subject,
        email_body=case.body,
        attachments_text=render_documents(documents) if documents else "",
    )
    ctx = ToolContext(tenant_id=uuid4(), user_id=None, run_id=uuid4())
    output = await extractor(task, ctx)
    if output is None:
        result.error = "extraction run failed"
        return
    ids = {line.position: uuid4() for line in REQUESTED}
    sources = QuoteSources(
        email_subject=case.subject,
        email_body=case.body,
        documents={d.filename: d for d in documents},
    )
    validated = validate_quote(
        output,
        requested=[
            RequestedLine(id=ids[r.position], position=r.position, mpn=r.mpn) for r in REQUESTED
        ],
        sources=sources,
        today=env.today,
        # As the extraction job: what the quote's numbers prove, not its language.
        decimal=proven_decimal_separator(sources),
    )
    result.check("is_quote", output.is_quote == case.is_quote, f"is_quote={output.is_quote}")
    if case.declined:
        result.check("declined", output.declined or not output.is_quote, "not marked declined")
    if not case.is_quote:
        return
    currency = validated.parsed("currency")
    result.check("currency", currency == case.currency, f"{currency} vs {case.currency}")
    position_of = {v: k for k, v in ids.items()}
    by_position = {
        position_of[line.rfq_line_id]: line
        for line in validated.lines
        if line.rfq_line_id is not None and line.rfq_line_id in position_of
    }
    for truth in case.lines:
        line = by_position.get(truth.position)
        if line is None:
            result.check(f"line{truth.position}_found", False, "no extracted line for it")
            continue
        price = line.parsed("unit_price")
        result.check(
            f"line{truth.position}_price",
            isinstance(price, Decimal) and price == truth.unit_price,
            f"{price} vs {truth.unit_price}",
        )
        quantity = line.parsed("min_quantity")
        result.check(
            f"line{truth.position}_quantity",
            isinstance(quantity, Decimal) and quantity == truth.min_quantity,
            f"{quantity} vs {truth.min_quantity}",
        )
        if truth.lead_time_days is not None:
            lead = line.parsed("lead_time_days")
            result.check(
                f"line{truth.position}_lead_time",
                lead == truth.lead_time_days,
                f"{lead} vs {truth.lead_time_days}",
            )
        if truth.tooling_zero:
            tooling = line.parsed("tooling_cost")
            result.check(f"line{truth.position}_tooling", tooling == Decimal(0), f"{tooling}")
        grounded = [
            f"{name}:{','.join(f.issues)}"
            for name, f in line.fields.items()
            if name in ("unit_price", "min_quantity", "lead_time_days")
            and any(i in ("not_grounded", "quote_not_found", "wrong_locator") for i in f.issues)
        ]
        result.check(f"line{truth.position}_grounded", not grounded, "; ".join(grounded))
    result.check(
        "extra_offers",
        len(output.extra_offers) >= case.min_extra_offers,
        f"{len(output.extra_offers)} captured, {case.min_extra_offers} expected",
    )
    result.notes["needs_review"] = validated.needs_review


async def run_timed(coro: Callable[[], Awaitable[None]], result: RunResult) -> RunResult:
    started = time.perf_counter()
    try:
        await coro()
    except Exception as exc:  # report, never crash the suite
        result.error = f"{type(exc).__name__}: {exc}"[:500]
    result.seconds = round(time.perf_counter() - started, 2)
    return result
