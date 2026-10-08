"""Running the scenarios: a fresh company per run, the real services, checks per run.

Scenarios:

* `import`: the workbook importer (rules + the sheet mapper) on each fixture.
* `analysis`: the purchase analysis on the imported data vs the golden numbers.
* `copilot`: a conversation (attach the spreadsheet, "faz uma análise", "pode
  criar a RFQ") through the real copilot run, checked by its tool calls, its
  answer and the RFQ it leaves in the database.
* `extraction`: quote extraction + validation on synthetic supplier replies.

Live mode uses the real model (OPENAI_API_KEY). Mock mode scripts the model:
the sheet mapper answers with the fixture's known mapping, the copilot's tools
are called in the order the model would call them, and extraction returns a
scripted output built from the truth. Mock mode checks the harness and the
deterministic code, not the model.
"""

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal, cast
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
from bido_back.quotes.parsing import decimal_separator_for
from bido_back.quotes.validation import QuoteSources, RequestedLine, validate_quote
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
    async for item in copilot.stream_message(deps, prepared):
        data = item if isinstance(item, dict) else item.model_dump(mode="json")
        kind = data.get("type")
        if kind == "tool_called":
            names[str(data["call_id"])] = str(data["tool"])
        elif kind == "tool_result":
            tool = str(data["tool"])
            tools.append((tool, bool(data.get("ok"))))
            if data.get("output") is not None:
                outputs[tool] = data["output"]
        elif kind == "answer":
            answer = str(data.get("text", ""))
    return prepared.conversation_id, Turn(answer, tools, outputs)


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


async def _mock_conversation(
    env: Env, principal: Principal, fixture: Fixture, upload_id: UUID, turns: list[Turn]
) -> None:
    from bido_back.copilot.tools import CopilotToolDeps, copilot_tools
    from bido_back.rfqs.agents import default_rfq_agents

    tools = {
        t.name: t
        for t in copilot_tools(
            CopilotToolDeps(
                db=env.engine,
                agents=default_rfq_agents(),
                mailboxes=_no_mailbox,
                attachment_ids=(upload_id,),
                storage=env.storage,
                sheet_mapper=scripted_mapper(fixture),
                today=lambda: env.today,
            )
        )
    }
    ctx = ToolContext(tenant_id=principal.tenant_id, user_id=principal.user_id, run_id=uuid4())
    calls: list[tuple[str, bool]] = []

    async def call(name: str, **args: object) -> Any:
        tool = tools[name]
        try:
            out = await tool.invoke(ctx, tool.parse_input(args))
        except Exception:  # a ToolError: the model would see it and answer
            calls.append((name, False))
            return None
        calls.append((name, True))
        return out

    await call("import_spreadsheet", upload_id=str(upload_id), confirmed=True)
    analysis = await call(
        "run_purchase_analysis",
        product=fixture.dataset.product,
        quantity=int(fixture.quantity) if fixture.quantity is not None else None,
    )
    if analysis is not None:
        await call("create_rfq_from_analysis", analysis_id=str(analysis.analysis_id))
    turns.append(Turn("", calls, {}))


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
    validated = validate_quote(
        output,
        requested=[
            RequestedLine(id=ids[r.position], position=r.position, mpn=r.mpn) for r in REQUESTED
        ],
        sources=QuoteSources(
            email_subject=case.subject,
            email_body=case.body,
            documents={d.filename: d for d in documents},
        ),
        today=env.today,
        decimal=decimal_separator_for(case.language),
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
