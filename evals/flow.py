"""Flow evals: the RFQ journey end to end, with scripted suppliers and the real services.

    uv run python -m evals.flow                 # live model, k=3
    uv run python -m evals.flow --mock          # scripted model (harness check, free)
    uv run python -m evals.flow -k 1 --scenario negotiation --case early_close

Scenarios (each case runs in a fresh company, inside `evals.world.World`):

* `intake`: an RFQ from free text, a forwarded email or a pasted sheet (pt, en,
  it) -> lines, quantities, need-by, Incoterm vs the golden lines.
* `rfq_email`: the buyer's Portuguese template + lines -> the RFQ per supplier
  language (de, it, fr, en, pt), as a PDF with a short cover note: in the PDF,
  required fields asked, numbers unchanged, `rfq_email_problems` clean, the
  supplier's language; in the cover, the reference, reply-by date, reply
  marker and item count, no items listed; test-domain recipients only.
* `followups`: no reply -> a chase after N business days in the supplier's
  language; a reply missing fields -> one clarification listing exactly what is
  missing; an out-of-office -> chases paused until the return date; a reply ->
  the pending chase canceled.
* `negotiation`: suppliers with personas under one mandate: counter-offers and
  a best-and-final request decided by code and sent automatically, never above
  the maximum, never revealing it or a competitor; the early close; the
  deadline close and its recommendation (broker rule); a committing draft held.
* `recipients`: a non-test domain is held until a person confirms it; test
  domains pass; the guarded sender is the net under every path.
* `award`: award the recommendation -> purchase orders (lines, totals), the PO
  email in the supplier's language, "not selected" notices without prices.

Every supplier number is decided by code (`evals.personas`); the golden
expectations below are written from the business rules, not read from the
code under test. Live mode uses the real models (OPENAI_API_KEY); mock mode
scripts them (`evals.scripted`). Reports: evals/reports/<stamp>-flow-<mode>/.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final
from uuid import UUID

from sqlalchemy import select

from bido_back.agents.catalog.contracts import (
    EmailNoteOutput,
    NegotiationEmailInput,
    RfqEmailInput,
    RfqEmailLine,
    RfqEmailOutput,
    RfqIntakeOutput,
    RfqLineDraft,
)
from bido_back.agents.catalog.definitions import rfq_cover_problems, rfq_email_problems
from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.approvals import ApprovalRecipientsUnconfirmed
from bido_back.awards import service as awards
from bido_back.awards.schemas import AwardRequest
from bido_back.db import create_engine, tenant_session
from bido_back.db.models import (
    Approval,
    FollowupMessage,
    Invite,
    MandateSend,
    NegotiationMandate,
    Rfq,
    RfqLine,
    RfqTemplate,
)
from bido_back.email.recipients import is_test_domain
from bido_back.email.types import OutboundEmail
from bido_back.followups.calendar import add_business_days
from bido_back.mandates import service as mandates
from bido_back.mandates.policy import commitment_language
from bido_back.mandates.schemas import ProposeMandateRequest
from bido_back.negotiation import service as negotiation
from bido_back.negotiation.schemas import LineLimitsUpdate
from bido_back.outbound.domains import RecipientDomainsNotConfirmed
from bido_back.rfqs import service as rfq_service
from bido_back.rfqs.document import document_text
from bido_back.rfqs.errors import InvalidState
from bido_back.rfqs.language import country_code
from bido_back.rfqs.send import SendRfqEmailArgs
from bido_back.settings import get_settings
from bido_back.supplier_email.locale import Locale
from bido_back.templates.render import display_quantity, format_date
from bido_back.templates.required import REQUIRED_FIELDS, covered_keys
from bido_back.templates.service import FOOTER_SEPARATOR
from evals.harness import Env, MemoryStorage, Mode, RunResult, run_timed
from evals.personas import (
    ABOVE,
    EAGER,
    ITEMS,
    NEGOTIATION_CAST,
    RESELLER,
    WITHIN,
    Item,
    Persona,
    Quote,
    Reply,
    out_of_office,
)
from evals.run import markdown, summarize
from evals.scripted import GREETING_PT, LABELS_PT, ScriptedModel
from evals.world import Captured, Recording, RfqRef, World

REPORTS: Final = Path(__file__).parent / "reports"

# --- Text checks (language-agnostic, deterministic) ----------------------------------------

_STOPWORDS: Final[dict[str, frozenset[str]]] = {
    "de": frozenset("und der die das wir sie ihr ihre ihren für mit nicht bitte zu von unser "
                    "unsere bis freundlichen grüßen vielen dank ist ein eine den dem im auf "
                    "uns ihnen sehr geehrte damen herren angebot".split()),
    "it": frozenset("il la di che per con non grazie vostra vostro nostra nostro della delle "
                    "sono una gli alla entro cordiali saluti vi del al ci buongiorno offerta "
                    "richiesta".split()),
    "fr": frozenset("le les de des et pour vous nous votre notre avec merci une est pas du au "
                    "sur cordialement bonjour nos vos offre demande".split()),
    "en": frozenset("the and for you we your our with please thank of to is this by best "
                    "regards hello on at us would offer request".split()),
    "pt": frozenset("que para com não por uma os dos das prezados obrigado segue nossa sua em "
                    "solicitamos cotação itens abaixo atenciosamente favor".split()),
}  # fmt: skip
_WORD: Final = re.compile(r"[a-zà-ÿß']+")
_NUMBER: Final = re.compile(r"\d+(?:[.,]\d+)+|\d+")
_DECIMAL: Final = re.compile(r"\d+[.,]\d+")
_MONEY: Final = re.compile(
    r"(?:[€$£]|\b(?:EUR|USD|GBP)\b)\s?\d|\d\s?(?:[€$£]|\b(?:EUR|USD|GBP)\b)", re.IGNORECASE
)


def guess_language(text: str) -> str:
    words = Counter(_WORD.findall(text.lower()))
    scores = {lang: sum(words[w] for w in stop) for lang, stop in _STOPWORDS.items()}
    return max(scores, key=lambda lang: scores[lang])


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


def written_numbers(text: str, pattern: re.Pattern[str] = _NUMBER) -> set[Decimal]:
    found: set[Decimal] = set()
    for match in pattern.finditer(text):
        found |= readings(match.group(0))
    return found


def _n(value: Decimal) -> Decimal:
    return value.normalize()


# --- Intake ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GoldenLine:
    mpn: str
    description: str
    quantities: tuple[Decimal, ...]
    need_by: date
    incoterm: str
    qualification: str | None = None


@dataclass(frozen=True, slots=True)
class IntakeCase:
    name: str
    text: str
    lines: tuple[GoldenLine, ...]


def intake_cases(today: date) -> list[IntakeCase]:
    need_by = today + timedelta(days=45)
    br = need_by.strftime("%d/%m/%Y")
    return [
        IntakeCase(
            "pt_free_text",
            "Olá, preciso de cotação para 10.000 un do capacitor MLCC 100nF 50V X7R 0603 "
            "Murata GRM188R71H104KA93D e 500 un do conector board-to-board 40 pinos Hirose "
            f"DF40C-40DS-0.4V. Entrega até {br}, DAP Curitiba.",
            (
                GoldenLine("GRM188R71H104KA93D", "MLCC", (Decimal(10000),), need_by, "DAP"),
                GoldenLine("DF40C-40DS-0.4V", "connector", (Decimal(500),), need_by, "DAP"),
            ),
        ),
        IntakeCase(
            "en_forwarded_email",
            "---------- Forwarded message ---------\n"
            "From: Engineering <eng@evals.example.test>\nSubject: parts for the pilot run\n\n"
            "Hi purchasing,\nplease get quotes for:\n"
            "- NTC thermistor 10k, TDK B57861S0103F040: 2,500 pcs and 5,000 pcs (two price "
            "breaks)\n"
            "- TVS diode SMBJ24A (Littelfuse), AEC-Q101 qualified: 1,200 pcs\n"
            f"We need them by {need_by.isoformat()}, FCA Shenzhen.\nThanks, Ana",
            (
                GoldenLine(
                    "B57861S0103F040", "NTC", (Decimal(2500), Decimal(5000)), need_by, "FCA"
                ),
                GoldenLine("SMBJ24A", "TVS", (Decimal(1200),), need_by, "FCA", "AEC-Q101"),
            ),
        ),
        IntakeCase(
            "it_pasted_sheet",
            "Richiesta d'offerta, consegna resa DAP Bologna.\n"
            "Codice\tDescrizione\tQuantità\tConsegna entro\n"
            f"STM32G474RET6\tMicrocontrollore STM32G4 LQFP64\t3000\t{br}\n"
            f"TPS7A4701RGWR\tRegolatore LDO 1A\t1500\t{br}\n",
            (
                GoldenLine("STM32G474RET6", "STM32", (Decimal(3000),), need_by, "DAP"),
                GoldenLine("TPS7A4701RGWR", "LDO", (Decimal(1500),), need_by, "DAP"),
            ),
        ),
    ]


def _intake_script(case: IntakeCase) -> RfqIntakeOutput:
    return RfqIntakeOutput(
        title=f"Eval {case.name}",
        lines=[
            RfqLineDraft(
                description=line.description,
                mpn=line.mpn,
                quantities=list(line.quantities),
                need_by=line.need_by,
                incoterm=line.incoterm,
                qualification=line.qualification,
            )
            for line in case.lines
        ],
    )


async def scenario_intake(world: World, case: IntakeCase, result: RunResult) -> None:
    if world.model is not None:
        world.model.intake[case.text.strip()] = _intake_script(case)
    created = await rfq_service.create_rfq_from_text(
        world.engine, world.principal, case.text, world.intake, currency="EUR"
    )
    async with tenant_session(world.engine, world.tenant_id) as session:
        rows = list(
            await session.scalars(
                select(RfqLine).where(RfqLine.rfq_id == created.id).order_by(RfqLine.position)
            )
        )
    result.check(
        "line_count", len(rows) == len(case.lines), f"{len(rows)} lines vs {len(case.lines)}"
    )
    by_mpn = {(r.mpn or "").replace(" ", "").upper(): r for r in rows}
    for golden in case.lines:
        row = by_mpn.get(golden.mpn.upper())
        if row is None:
            result.check(f"{golden.mpn}:found", False, f"mpns: {sorted(by_mpn)}")
            continue
        quantities = tuple(Decimal(str(q)) for q in row.quantities)
        result.check(
            f"{golden.mpn}:quantities",
            [_n(q) for q in quantities] == [_n(q) for q in golden.quantities],
            f"{list(map(str, quantities))} vs {list(map(str, golden.quantities))}",
        )
        result.check(f"{golden.mpn}:need_by", row.need_by == golden.need_by, f"{row.need_by}")
        incoterm = (row.incoterm or "").split()[0].upper() if row.incoterm else None
        result.check(f"{golden.mpn}:incoterm", incoterm == golden.incoterm, f"{row.incoterm}")
        if golden.qualification:
            result.check(
                f"{golden.mpn}:qualification",
                (row.qualification or "").replace(" ", "").upper()
                == golden.qualification.replace(" ", "").upper(),
                f"{row.qualification}",
            )


# --- RFQ email -------------------------------------------------------------------------------

RFQ_EMAIL_LANGUAGES: Final = ("de", "it", "fr", "en", "pt")


def _plain(language: str) -> Persona:
    names = {"de": "Hansa Bauteile GmbH", "it": "Elettronica Adriatica S.r.l.",
             "fr": "Électronique du Rhône SARL", "en": "Pennine Components Ltd",
             "pt": "Componentes Paulista Ltda"}  # fmt: skip
    country = {"de": "DE", "it": "IT", "fr": "FR", "en": "GB", "pt": "BR"}[language]
    return Persona(f"plain{language}", names[language], language, country,
                   "authorized_distributor", (Quote({}, 0),))  # fmt: skip


async def _pt_template(world: World) -> None:
    async with tenant_session(world.engine, world.tenant_id) as session:
        session.add(
            RfqTemplate(
                tenant_id=world.tenant_id,
                source="none",
                status="ready",
                language="pt",
                greeting=GREETING_PT,
                columns=list(LABELS_PT[:5]),
                footer=FOOTER_SEPARATOR.join(LABELS_PT[5:]),
                fields=list(LABELS_PT),
                is_default=True,
            )
        )


async def scenario_rfq_email(world: World, language: str, result: RunResult) -> None:
    await _pt_template(world)
    need_by = world.today + timedelta(days=60)
    rfq = await world.draft_rfq("Capacitores e conectores para o lote piloto", need_by)
    persona = _plain(language)
    await world.invite(rfq, persona)
    try:
        prepared = await rfq_service.prepare_send(
            world.engine, world.principal, rfq.id, world.email_writer, world.mailboxes
        )
    except InvalidState as exc:
        result.check("rendered", False, str(exc)[:300])
        return
    result.check("rendered", True)
    async with tenant_session(world.engine, world.tenant_id) as session:
        approval = await session.get(Approval, prepared.approvals[0].approval_id)
    assert approval is not None
    args = SendRfqEmailArgs.model_validate(approval.args)
    body = args.body
    task = RfqEmailInput(
        rfq_ref=rfq.ref,
        rfq_title="x",
        buyer_company="x",
        buyer_name="x",
        supplier_name=persona.name,
        supplier_language=language,
        lines=[
            RfqEmailLine(
                position=i.position, description=i.description, mpn=i.mpn,
                quantities=[i.quantity], unit="pcs",
            )
            for i in ITEMS
        ],
    )  # fmt: skip
    # The RFQ goes as the PDF; the email is a short cover note.
    document = args.document
    result.check("pdf_attached", document is not None, "no document: the full email went")
    if document is None:
        return
    pdf = document_text(document)
    problems = rfq_email_problems(task, RfqEmailOutput(subject=args.subject, body=pdf))
    result.check("problems_clean", not problems, ", ".join(problems))
    # Asked: a line of the PDF names the field. Bido's own labels count as synonyms, so this
    # is the same coverage rule the product uses. The line data is checked by value below.
    wanted = {f.key for f in REQUIRED_FIELDS if f.kind == "ask" and not f.conditional}
    covered = covered_keys(pdf.splitlines())
    result.check(
        "required_fields_asked", wanted <= covered, f"not asked: {sorted(wanted - covered)}"
    )
    # Quantities as the supplier's locale writes them (10.000 in German), like every email.
    locale = Locale.of(language, country_code(persona.country))
    missing = [
        text
        for i in ITEMS
        for text in (i.mpn, display_quantity(i.quantity, locale))
        # The PDF's text has its spaces normalized (French writes 10 000 with a narrow
        # no-break space), as the document's own checks read it.
        if " ".join(text.split()) not in pdf
    ]
    for text in (format_date(need_by, language), "DAP"):
        if text not in pdf:
            missing.append(text)
    result.check("numbers_unchanged", not missing, f"not in the PDF: {missing}")
    guessed = guess_language(pdf)
    result.check("language", guessed == language, f"PDF reads as {guessed}")
    if language != "pt":
        result.check("greeting_localized", GREETING_PT not in pdf, "Portuguese greeting kept")
    # The cover: the reference, the reply-by date, how to reply; no items, short.
    cover_problems = rfq_cover_problems(task, RfqEmailOutput(subject=args.subject, body=body))
    result.check("cover_clean", not cover_problems, ", ".join(cover_problems))
    reply_by = format_date(document.reply_by, language) if document.reply_by else "?"
    absent = [
        what
        for what, text in (
            ("reference", f"{rfq.ref} "),
            ("reply_by", reply_by),
            ("reply_marker", f"[{rfq.ref}]"),
            ("item_count", str(len(ITEMS))),
        )
        if text not in body
    ]
    result.check("cover_says", not absent, f"missing in the cover: {absent}")
    listed = [i.mpn for i in ITEMS if i.mpn in body]
    result.check("cover_short", not listed and len(body) < 1200, f"{len(body)} chars, {listed}")
    cover_language = guess_language(body)
    result.check("cover_language", cover_language == language, f"cover reads as {cover_language}")
    calls = len(world.email_writer.calls)
    result.check("localized_once", calls == (0 if language == "pt" else 1), f"{calls} writer calls")
    domains = [a.rpartition("@")[2] for a in [*args.to, *args.cc]]
    result.check(
        "test_domain_recipients",
        bool(domains) and all(is_test_domain(d) for d in domains),
        f"{domains}",
    )
    result.notes["subject"] = args.subject


# --- Follow-ups ------------------------------------------------------------------------------

FOLLOWUP_CASES: Final = ("chase", "clarification", "out_of_office", "reply_cancels_chase")
_FOLLOWUP_PERSONA: Final = {
    "chase": Persona("chaser", "Hansa Bauteile GmbH", "de", "DE", "authorized_distributor",
                     (Quote({1: Decimal("0.044"), 2: Decimal("1.12")}, 35),)),
    "clarification": Persona("vague", "Elettronica Adriatica S.r.l.", "it", "IT",
                             "authorized_distributor",
                             (Quote({1: Decimal("0.043"), 2: Decimal("1.08")}, 30),)),
    "out_of_office": Persona("away", "Électronique du Rhône SARL", "fr", "FR",
                             "authorized_distributor", (Quote({1: Decimal("0.046")}, 30),)),
    "reply_cancels_chase": Persona("late", "Pennine Components Ltd", "en", "GB",
                                   "authorized_distributor",
                                   (Quote({1: Decimal("0.041"), 2: Decimal("1.04")}, 21),)),
}  # fmt: skip


async def _followups(world: World) -> list[FollowupMessage]:
    async with tenant_session(world.engine, world.tenant_id) as session:
        return list(
            await session.scalars(select(FollowupMessage).order_by(FollowupMessage.created_at))
        )


async def _invite(world: World, persona: Persona) -> Invite:
    async with tenant_session(world.engine, world.tenant_id) as session:
        invite = await session.get(Invite, world.invites[persona.key])
    assert invite is not None
    return invite


def _check_followup_email(
    result: RunResult, prefix: str, email: Captured, persona: Persona, thread: Captured, ref: str
) -> None:
    result.check(f"{prefix}_to_supplier", email.to == (persona.email,), f"{email.to}")
    # In the RFQ email's thread, replying to the latest message with the supplier.
    result.check(
        f"{prefix}_threaded",
        bool(email.references)
        and email.references[0] == thread.message_id
        and email.in_reply_to == email.references[-1],
        f"in_reply_to={email.in_reply_to} references={email.references}",
    )
    guessed = guess_language(email.body)
    result.check(f"{prefix}_language", guessed == persona.language, f"reads as {guessed}")
    result.check(f"{prefix}_ref_in_body", ref in email.body, "RFQ reference missing")
    result.check(f"{prefix}_no_price", not _MONEY.search(email.body), "mentions an amount")


async def scenario_followups(world: World, case: str, result: RunResult) -> None:
    persona = _FOLLOWUP_PERSONA[case]
    rfq = await world.draft_rfq("MLCC and connectors", world.today + timedelta(days=60))
    await world.invite(rfq, persona)
    await world.send_rfq(rfq)
    thread = world.last_to(persona)
    invite = await _invite(world, persona)
    assert invite.sent_at is not None
    async with tenant_session(world.engine, world.tenant_id) as session:
        row = await session.get(Rfq, rfq.id)
    assert row is not None
    first_after = row.followup_first_after_days
    due = add_business_days(invite.sent_at, first_after, persona.country)

    if case == "chase":
        world.at(due - timedelta(hours=2))
        await world.sweep_followups()
        result.check("not_before_due", not await _followups(world), "chased early")
        world.at(due + timedelta(hours=1))
        await world.sweep_followups()
        rows = await _followups(world)
        result.check("one_chase", [r.kind for r in rows] == ["chase"], f"{[r.kind for r in rows]}")
        if not rows or rows[0].approval_id is None:
            return
        task = world.followup_writer.calls[-1][0]
        result.check(
            "chase_task",
            task.kind == "chase" and task.chase_number == 1 and task.supplier_language == "de",
            f"{task.kind} #{task.chase_number} {task.supplier_language}",
        )
        await world.approve(rows[0].approval_id)
        sent = world.outbox.to(persona.email)
        result.check("chase_sent", len(sent) == 2, f"{len(sent)} emails to the supplier")
        if len(sent) == 2:
            _check_followup_email(result, "chase", sent[-1], persona, thread, rfq.ref)

    elif case == "clarification":
        world.at(world.now + timedelta(hours=3))
        reply = await world.quote(persona, rfq, 0, drop=("lead", "valid"))
        await world.drain()
        offers = await world.offers(persona)
        latest = offers[-1] if offers else {}
        prices_ok = all(
            latest.get(p, (None, None))[0] == price for p, price in reply.quote.prices.items()
        )
        result.check("extraction_prices", prices_ok, f"{latest}")
        world.at(world.now + timedelta(hours=1))
        await world.sweep_followups()
        rows = await _followups(world)
        result.check(
            "one_clarification",
            [r.kind for r in rows] == ["clarification"],
            f"{[r.kind for r in rows]}",
        )
        if not rows or rows[0].approval_id is None:
            return
        task = world.followup_writer.calls[-1][0]
        missing = {(i.position, i.field) for i in task.open_items if i.problem == "missing"}
        golden = {(None, "valid_until"), (1, "lead_time"), (2, "lead_time")}
        result.check("missing_exactly", missing == golden, f"{sorted(missing, key=str)} vs golden")
        unclear = [(i.position, i.field) for i in task.open_items if i.problem == "unclear"]
        result.check("nothing_unclear", not unclear, f"also asked to confirm: {unclear}")
        await world.approve(rows[0].approval_id)
        sent = world.outbox.to(persona.email)
        if len(sent) == 2:
            email = sent[-1]
            _check_followup_email(result, "clarification", email, persona, thread, rfq.ref)
            named = [i.mpn for i in ITEMS if i.mpn not in email.body]
            result.check("clarification_names_lines", not named, f"lines not named: {named}")
        else:
            result.check("clarification_sent", False, f"{len(sent)} emails to the supplier")
        world.at(world.now + timedelta(hours=1))
        await world.sweep_followups()
        result.check("asked_once", len(await _followups(world)) == 1, "asked again")

    elif case == "out_of_office":
        world.at(world.now + timedelta(hours=2))
        back = (world.now + timedelta(days=8)).date()
        subject, body = out_of_office(persona, subject=thread.subject, back=back)
        stored = await world.deliver(persona, subject=subject, body=body, auto_reply=True)
        result.check(
            "quarantined_auto_reply",
            stored.outcome == "quarantined" and stored.reason == "auto_reply",
            f"{stored.outcome}/{stored.reason}",
        )
        invite = await _invite(world, persona)
        paused = invite.followups_paused_until
        result.check(
            "paused_until_return",
            paused is not None and paused.date() > back,
            f"paused until {paused}",
        )
        world.at(due + timedelta(hours=1))
        await world.sweep_followups()
        result.check("no_chase_while_away", not await _followups(world), "chased while away")
        if paused is not None:
            world.at(max(paused, world.now) + timedelta(hours=1))
            await world.sweep_followups()
            rows = await _followups(world)
            result.check(
                "chase_after_return",
                [r.kind for r in rows] == ["chase"],
                f"{[r.kind for r in rows]}",
            )

    else:  # reply_cancels_chase
        world.at(due + timedelta(hours=1))
        await world.sweep_followups()
        rows = await _followups(world)
        result.check("chase_proposed", len(rows) == 1, f"{len(rows)} follow-ups")
        world.at(world.now + timedelta(hours=1))
        await world.quote(persona, rfq, 0)
        await world.drain()
        rows = await _followups(world)
        result.check(
            "chase_canceled",
            [(r.status, r.canceled_reason) for r in rows] == [("canceled", "supplier_replied")],
            f"{[(r.status, r.canceled_reason) for r in rows]}",
        )
        if rows and rows[0].approval_id is not None:
            async with tenant_session(world.engine, world.tenant_id) as session:
                approval = await session.get(Approval, rows[0].approval_id)
            result.check(
                "approval_expired",
                approval is not None and approval.status == "expired",
                f"{approval.status if approval else None}",
            )
        result.check("nothing_chased", len(world.outbox.to(persona.email)) == 1, "a chase went out")


# --- Negotiation -----------------------------------------------------------------------------

NEGOTIATION_CASES: Final = ("mandate_five", "early_close", "held_out_of_mandate")
MAX_ROUNDS: Final = 3
PER_DAY: Final = 2


Asks = dict[int, tuple[Decimal | None, int | None]]


def golden_asks(quote: Quote, items: Sequence[Item] = ITEMS) -> Asks:
    """What the buyer asks next, from the buyer's rules: per line, the target price when the
    offer is above it, the maximum lead time when the offer is later; nothing on a line
    within target and on time, nothing on a line not offered."""
    asks: Asks = {}
    for item in items:
        price = quote.prices.get(item.position)
        if price is None:
            continue
        late = quote.lead > item.max_lead
        ask_price = item.target if price > item.target else None
        if ask_price is None and not late:
            continue
        asks[item.position] = (ask_price, item.max_lead if late else None)
    return asks


def golden_actions(persona: Persona) -> list[tuple[str, Asks]]:
    """Expected sends, stopping after two unchanged supplier replies.

    The final allowed productive round is best-and-final; stagnant negotiations
    are held for the buyer rather than spending another automatic round.
    """
    actions: list[tuple[str, Asks]] = []
    for round in range(1, MAX_ROUNDS + 1):
        if round >= 3 and persona.quote(round - 1) == persona.quote(round - 2) == persona.quote(
            round - 3
        ):
            break
        asks = golden_asks(persona.quote(round - 1))
        if not asks:
            break
        actions.append(("bafo" if round == MAX_ROUNDS else "counter", asks))
    return actions


def _asked(task: NegotiationEmailInput) -> Asks:
    return {
        line.position: (
            _n(line.target_unit_price) if line.target_unit_price is not None else None,
            line.required_lead_time_days,
        )
        for line in task.lines
    }


def _norm_asks(asks: Asks) -> Asks:
    return {p: (_n(a) if a is not None else None, d) for p, (a, d) in asks.items()}


async def _mandate(world: World, rfq: RfqRef, deadline: datetime) -> UUID:
    proposed = await mandates.propose_mandate(
        world.engine,
        world.principal,
        rfq.id,
        ProposeMandateRequest(
            deadline=deadline,
            max_rounds=MAX_ROUNDS,
            max_messages_per_supplier_per_day=PER_DAY,
            lines=[
                LineLimitsUpdate(
                    rfq_line_id=rfq.lines[i.position],
                    target_unit_price=i.target,
                    max_unit_price=i.max_price,
                    max_lead_time_days=i.max_lead,
                )
                for i in ITEMS
            ],
        ),
        writer=world.email_writer,
        mailboxes=world.mailboxes,
        resolver=world.resolver,
    )
    await world.approve(proposed.approval_id)
    return proposed.approval_id


def _negotiation_emails(world: World, persona: Persona) -> list[Captured]:
    return world.outbox.to(persona.email)[1:]  # the first one is the RFQ email


async def _rfq_row(world: World, rfq: RfqRef) -> Rfq:
    async with tenant_session(world.engine, world.tenant_id) as session:
        row = await session.get(Rfq, rfq.id)
    assert row is not None
    return row


async def _sends(world: World) -> list[MandateSend]:
    async with tenant_session(world.engine, world.tenant_id) as session:
        return list(await session.scalars(select(MandateSend)))


def _check_extraction(
    result: RunResult,
    persona: Persona,
    round: int,
    versions: list[dict[int, tuple[Decimal | None, int | None]]],  # fmt: skip
) -> None:
    quote = persona.quote(round)
    name = f"extraction:{persona.key}:r{round}"
    if len(versions) != round + 1:
        result.check(name, False, f"{len(versions)} proposal versions after reply {round}")
        return
    latest = versions[-1]
    wrong = [
        f"pos {p}: {latest.get(p)} vs ({price}, {quote.lead})"
        for p, price in quote.prices.items()
        if latest.get(p, (None, None))[0] is None
        or _n(latest[p][0] or Decimal(0)) != _n(price)
        or latest[p][1] != quote.lead
    ]
    result.check(name, not wrong, "; ".join(wrong))


def _check_negotiation_emails(
    result: RunResult, world: World, cast: Sequence[Persona], *, rounds: dict[str, int]
) -> None:
    """Every negotiation email against the golden asks and the disclosure rules."""
    drafts = {(task.supplier_name, task.round): task for task, _ in world.negotiation_writer.calls}
    for persona in cast:
        emails = _negotiation_emails(world, persona)
        golden = golden_actions(persona)[: rounds.get(persona.key, MAX_ROUNDS)]
        tasks = sorted(
            (t for (name, _), t in drafts.items() if name == persona.name),
            key=lambda t: t.round,
        )
        got = [(t.action, _asked(t)) for t in tasks]
        want = [(action, _norm_asks(asks)) for action, asks in golden]
        result.check(f"asks:{persona.key}", got == want, f"got {got} want {want}")
        result.check(
            f"sent:{persona.key}",
            len(emails) == len(want),
            f"{len(emails)} negotiation emails, {len(want)} expected",
        )
        result.check(f"round_cap:{persona.key}", len(emails) <= MAX_ROUNDS, f"{len(emails)} rounds")
        per_day = Counter(e.at.date() for e in world.outbox.to(persona.email))
        result.check(
            f"daily_cap:{persona.key}", max(per_day.values()) <= PER_DAY, f"{dict(per_day)}"
        )
        others = [p for p in NEGOTIATION_CAST + (WITHIN, ABOVE) if p.key != persona.key]
        own = {_n(v) for q in persona.quotes for v in q.prices.values()}
        own |= {_n(i.target) for i in ITEMS}
        maxima = {_n(i.max_price) for i in ITEMS} - own
        rivals = {_n(v) for p in others for q in p.quotes for v in q.prices.values()} - own
        for index, email in enumerate(emails, 1):
            body = email.body
            tag = f"{persona.key}:e{index}"
            # Rendered by code: the same email as text and as HTML.
            result.check(f"html_part:{tag}", email.html is not None, "no HTML part")
            numbers = written_numbers(body)
            result.check(f"no_max:{tag}", not numbers & maxima, f"{sorted(numbers & maxima)}")
            leaked = written_numbers(body, _DECIMAL) & rivals
            names = [p.name for p in others if p.name.split()[0].lower() in body.lower()]
            result.check(
                f"no_competitor:{tag}", not leaked and not names, f"{sorted(leaked)} {names}"
            )
            result.check(f"no_commitment:{tag}", not commitment_language(body), body[:200])
            guessed = guess_language(body)
            result.check(f"language:{tag}", guessed == persona.language, f"reads as {guessed}")
            task = tasks[index - 1] if index <= len(tasks) else None
            if task is not None:
                missing = [
                    str(t)
                    for t in (a[0] for a in _asked(task).values())
                    if t is not None and t not in numbers
                ]
                result.check(f"targets_written:{tag}", not missing, f"missing {missing}")


async def _replies(
    world: World,
    rfq: RfqRef,
    cast: Sequence[Persona],
    answered: dict[str, int],
    last: dict[str, Reply],
    result: RunResult,
) -> None:
    """Each persona answers the newest negotiation email it got (and the RFQ on day 0)."""
    due = [
        p for p in cast if p.key not in last or len(_negotiation_emails(world, p)) > answered[p.key]
    ]
    for persona in due:
        round = len(_negotiation_emails(world, persona))
        last[persona.key] = await world.quote(persona, rfq, round, previous=last.get(persona.key))
        answered[persona.key] = round
    await world.drain()
    for persona in due:
        _check_extraction(result, persona, answered[persona.key], await world.offers(persona))


async def scenario_negotiation(world: World, case: str, result: RunResult) -> None:
    need_by = world.today + timedelta(days=60)
    # Held out of the mandate: the buyer's own title reads as a commitment ("contract"), so
    # every reply's subject carries it and the mandate holds the email for a person.
    title = (
        "Contract for MLCC and connectors"
        if case == "held_out_of_mandate"
        else "MLCC and connectors for the pilot run"
    )
    rfq = await world.draft_rfq(title, need_by)
    cast: tuple[Persona, ...] = {
        "mandate_five": NEGOTIATION_CAST,
        "early_close": (WITHIN, ABOVE),
        "held_out_of_mandate": (ABOVE,),
    }[case]
    for persona in cast:
        await world.invite(rfq, persona)
    if case == "held_out_of_mandate":
        inner = world.negotiation_writer.inner

        async def committing(task: NegotiationEmailInput, context: ToolContext) -> EmailNoteOutput:
            await inner(task, context)
            # The writer's note commits: it is left out before anything is proposed.
            return EmailNoteOutput(note="We accept your offer and will place the order.")

        world.negotiation_writer = Recording(committing)

    deadline = world.start + timedelta(days=5)
    await _mandate(world, rfq, deadline)
    result.check(
        "rfq_emails_under_mandate",
        all(len(world.outbox.to(p.email)) == 1 for p in cast),
        f"{[len(world.outbox.to(p.email)) for p in cast]}",
    )
    answered: dict[str, int] = {}
    last: dict[str, Reply] = {}

    if case == "held_out_of_mandate":
        world.at(world.now + timedelta(hours=1))
        await _replies(world, rfq, cast, answered, last, result)
        world.at(world.now + timedelta(hours=1))
        await world.sweep_negotiation()
        held = [s for s in await _sends(world) if s.outcome == "held"]
        result.check(
            "held_commitment",
            [s.reason for s in held] == ["commitment_language"],
            f"{[s.reason for s in held]}",
        )
        result.check("not_sent", not _negotiation_emails(world, ABOVE), "a committing email left")
        if held:
            async with tenant_session(world.engine, world.tenant_id) as session:
                approval = await session.get(Approval, held[0].approval_id)
            result.check(
                "waits_for_a_person",
                approval is not None and approval.status == "pending",
                f"{approval.status if approval else None}",
            )
            result.check(
                "committing_note_left_out",
                approval is not None and "accept your offer" not in str(approval.args["body"]),
                "the writer's committing note is in the email",
            )
        return

    if case == "early_close":
        world.at(world.now + timedelta(hours=1))
        await _replies(world, rfq, cast, answered, last, result)
        world.at(world.now + timedelta(hours=1))
        await world.sweep_negotiation()
        row = await _rfq_row(world, rfq)
        result.check(
            "ready_to_close", row.negotiation_status == "ready_to_close", row.negotiation_status
        )
        result.check(
            "no_counter_to_accepted", not _negotiation_emails(world, WITHIN), "countered WITHIN"
        )
        _check_negotiation_emails(result, world, (ABOVE,), rounds={"above": 1})
        outcome = await negotiation.close(world.engine, world.principal, rfq.id, now=world.now)
        winners = {o.rfq_line_id: o.invite_id for o in outcome if o.tier == 0}
        expected = {rfq.lines[i.position]: world.invites["within"] for i in ITEMS}
        result.check("outcome", winners == expected, f"{winners}")
        return

    # mandate_five: four days of replies and sweeps, then the deadline.
    for day in range(MAX_ROUNDS + 1):
        world.at(world.start + timedelta(days=day, hours=1))
        await _replies(world, rfq, cast, answered, last, result)
        world.at(world.start + timedelta(days=day, hours=2))
        await world.sweep_negotiation()
        if day == 0:
            row = await _rfq_row(world, rfq)
            result.check(
                "not_ready_early",
                row.negotiation_status == "open",
                f"{row.negotiation_status} (no supplier meets the target on line 2)",
            )
    sent_before_close = len(world.outbox.sent)
    world.at(deadline + timedelta(hours=1))
    await world.sweep_negotiation()
    row = await _rfq_row(world, rfq)
    result.check("deadline_close", row.negotiation_status == "closed", row.negotiation_status)
    result.check("silent_after_close", len(world.outbox.sent) == sent_before_close, "sent more")
    async with tenant_session(world.engine, world.tenant_id) as session:
        mandate = await session.scalar(select(NegotiationMandate))
    result.check("mandate_ended", mandate is not None and mandate.status == "ended", f"{mandate}")
    held = [s.reason for s in await _sends(world) if s.outcome == "held"]
    result.check("nothing_held", not held, f"held: {held}")
    _check_negotiation_emails(result, world, cast, rounds={})
    outcome = [o for o in (row.negotiation_outcome or []) if o.get("tier") == 0]
    winners = {str(o.get("rfq_line_id")): o.get("invite_id") for o in outcome}
    golden = {
        str(rfq.lines[1]): (str(world.invites[EAGER.key]), Decimal("0.040")),
        str(rfq.lines[2]): (str(world.invites[EAGER.key]), Decimal("1.05")),
    }
    for line_id, (invite_id, price) in golden.items():
        found = next((o for o in outcome if str(o.get("rfq_line_id")) == line_id), None)
        got_price = found.get("effective_unit_price_base") if found else None
        ok = (
            found is not None
            and found.get("invite_id") == invite_id
            and got_price is not None
            and _n(Decimal(str(got_price))) == _n(price)
        )
        position = 1 if line_id == str(rfq.lines[1]) else 2
        who = {str(v): k for k, v in world.invites.items()}
        result.check(
            f"recommendation:line{position}",
            ok,
            f"{who.get(str(found.get('invite_id')) if found else '', None)} at {got_price}",
        )
    result.check(
        "broker_not_recommended",
        str(world.invites[RESELLER.key]) not in {str(v) for v in winners.values()},
        "the reseller won a line against authorized offers",
    )


# --- Recipient guard -------------------------------------------------------------------------

RECIPIENT_CASES: Final = ("unconfirmed_domain_held", "test_domain_passes", "guarded_sender_net")
# Not a reserved test domain, so the guard treats it as a real company. Nothing is sent.
UNCONFIRMED_DOMAIN: Final = "lieferant-bido-eval.de"


async def scenario_recipients(world: World, case: str, result: RunResult) -> None:
    rfq = await world.draft_rfq("MLCC and connectors", world.today + timedelta(days=60))
    if case == "guarded_sender_net":
        async with tenant_session(world.engine, world.tenant_id) as session:
            sender = await world.mailboxes(session, world.tenant_id, world.principal.user_id)
            try:
                await sender.send(
                    OutboundEmail(
                        from_name="Eval",
                        from_address=world.buyer_email,
                        to=(f"sales@{UNCONFIRMED_DOMAIN}",),
                        subject=f"[{rfq.ref}] test",
                        text_body="test",
                    )
                )
                blocked = False
            except RecipientDomainsNotConfirmed:
                blocked = True
        result.check("blocked_by_guard", blocked, "the guarded sender let it through")
        result.check("nothing_captured", not world.outbox.sent, "an email reached the transport")
        return
    domain = UNCONFIRMED_DOMAIN if case == "unconfirmed_domain_held" else "hansa.example.test"
    persona = Persona("guarded", "Hansa Bauteile GmbH", "de", "DE", "authorized_distributor",
                      (Quote({}, 0),), domain=domain)  # fmt: skip
    await world.invite(rfq, persona)
    prepared = await rfq_service.prepare_send(
        world.engine, world.principal, rfq.id, world.email_writer, world.mailboxes
    )
    approval_id = prepared.approvals[0].approval_id
    if case == "test_domain_passes":
        await world.approve(approval_id)
        result.check("sent", len(world.outbox.to(persona.email)) == 1, "not sent")
        return
    try:
        await world.approve(approval_id)
        refused = False
    except ApprovalRecipientsUnconfirmed:
        refused = True
    result.check("approval_refused_unconfirmed", refused, "approved without confirmation")
    result.check("nothing_sent", not world.outbox.sent, "an email reached the transport")
    async with tenant_session(world.engine, world.tenant_id) as session:
        approval = await session.get(Approval, approval_id)
    result.check(
        "still_pending",
        approval is not None and approval.status == "pending",
        f"{approval.status if approval else None}",
    )
    await world.approve(approval_id, confirmed_domains=[UNCONFIRMED_DOMAIN])
    result.check(
        "sent_once_confirmed", len(world.outbox.to(persona.email)) == 1, "not sent after confirm"
    )


# --- Award -> purchase orders ------------------------------------------------------------------

AWARD_CAST: Final = (
    Persona("alpen", "Alpen Bauteile AG", "de", "AT", "authorized_distributor",
            (Quote({1: Decimal("0.039"), 2: Decimal("0.98")}, 21),)),
    Persona("atlantic", "Atlantic Components Ltd", "en", "IE", "authorized_distributor",
            (Quote({1: Decimal("0.037"), 2: Decimal("1.05")}, 21),)),
    Persona("broker", "Broker Componenti S.r.l.", "it", "IT", "reseller",
            (Quote({1: Decimal("0.030"), 2: Decimal("0.80")}, 20),)),
)  # fmt: skip
# The recommendation per line (the broker is cheapest on both lines but never wins
# against authorized offers), and the purchase orders that follow.
AWARD_GOLDEN: Final = {1: ("atlantic", Decimal("0.037")), 2: ("alpen", Decimal("0.98"))}


async def scenario_award(world: World, result: RunResult) -> None:
    need_by = world.today + timedelta(days=60)
    rfq = await world.draft_rfq("MLCC and connectors", need_by)
    for persona in AWARD_CAST:
        await world.invite(rfq, persona)
    await world.send_rfq(rfq)
    world.at(world.now + timedelta(hours=2))
    for persona in AWARD_CAST:
        await world.quote(persona, rfq, 0)
    await world.drain()
    requested = await awards.request_award(
        world.engine, world.principal, rfq.id, AwardRequest(), world.today
    )
    by_position = {line.position: line for line in requested.lines}
    for position, (key, price) in AWARD_GOLDEN.items():
        line = by_position.get(position)
        ok = (
            line is not None
            and line.invite_id == world.invites[key]
            and _n(Decimal(line.unit_price)) == _n(price)
        )
        result.check(
            f"award:line{position}", ok, f"{line.supplier_name if line else None} "
            f"{line.unit_price if line else None}"
        )  # fmt: skip
    await world.approve(requested.approval_id)
    orders = [
        o
        for o in await awards.list_purchase_orders(world.engine, world.principal)
        if o.rfq_id == rfq.id
    ]
    expected_suppliers = {key for key, _ in AWARD_GOLDEN.values()}
    names = {p.key: p.name for p in AWARD_CAST}
    result.check(
        "po_per_winner",
        sorted(o.supplier_name for o in orders) == sorted(names[k] for k in expected_suppliers),
        f"{[o.supplier_name for o in orders]}",
    )
    items = {i.position: i for i in ITEMS}
    for summary in orders:
        po = await awards.get_purchase_order(world.engine, world.principal, summary.id)
        key = next(k for k, n in names.items() if n == po.supplier_name)
        golden_lines = [(p, items[p].quantity, price) for p, (k, price) in AWARD_GOLDEN.items()
                        if k == key]  # fmt: skip
        got = [(line.position, _n(line.quantity), _n(line.unit_price)) for line in po.lines]
        want = [(p, _n(q), _n(price)) for p, q, price in golden_lines]
        result.check(f"po_lines:{key}", got == want, f"{got} vs {want}")
        total = sum((q * price for _, q, price in golden_lines), Decimal(0))
        result.check(f"po_total:{key}", _n(po.total) == _n(total), f"{po.total} vs {total}")
        result.check(f"po_currency:{key}", po.currency == "EUR", po.currency)
        persona = next(p for p in AWARD_CAST if p.key == key)
        sent_before = len(world.outbox.to(persona.email))
        approval = await awards.request_po_send(
            world.engine, world.principal, po.id, world.mailboxes
        )
        await world.approve(approval.approval_id)
        sent = world.outbox.to(persona.email)
        if len(sent) != sent_before + 1:
            result.check(f"po_sent:{key}", False, f"{len(sent) - sent_before} emails")
            continue
        email = sent[-1]
        thread = sent[0]
        # In the RFQ email's thread, replying to the latest message with the supplier.
        result.check(
            f"po_threaded:{key}",
            bool(email.references)
            and email.references[0] == thread.message_id
            and email.in_reply_to == email.references[-1],
            f"{email.in_reply_to} {email.references}",
        )
        result.check(f"po_attached:{key}", bool(email.attachments), "no PO document attached")
        result.check(f"po_ref:{key}", po.ref in email.subject, email.subject)
        guessed = guess_language(email.body)
        result.check(f"po_language:{key}", guessed == persona.language, f"reads as {guessed}")
    notices = await awards.request_notices(
        world.engine, world.principal, rfq.id, world.notice_writer
    )
    for item in notices.proposed:
        await world.approve(item.approval_id)
    losers = [p for p in AWARD_CAST if p.key not in expected_suppliers]
    for persona in AWARD_CAST:
        received = world.outbox.to(persona.email)
        notice = [e for e in received[1:] if not e.attachments]
        if persona in losers:
            result.check(f"notice:{persona.key}", len(notice) == 1, f"{len(notice)} notices")
            for email in notice:
                body = email.body
                result.check(f"notice_no_price:{persona.key}", not _MONEY.search(body), body[:200])
                prices = written_numbers(body, _DECIMAL) & {
                    _n(v) for p in AWARD_CAST for q in p.quotes for v in q.prices.values()
                }
                result.check(f"notice_no_numbers:{persona.key}", not prices, f"{prices}")
                guessed = guess_language(body)
                result.check(
                    f"notice_language:{persona.key}",
                    guessed == persona.language,
                    f"reads as {guessed}",
                )
        else:
            result.check(f"no_notice_to_winner:{persona.key}", not notice, "winner noticed")


# --- The suite -------------------------------------------------------------------------------

SCENARIOS: Final = ("intake", "rfq_email", "followups", "negotiation", "recipients", "award")
Runner = Callable[[World, RunResult], Awaitable[None]]


def cases(today: date) -> list[tuple[str, str, Runner]]:
    out: list[tuple[str, str, Runner]] = []
    for case in intake_cases(today):
        out.append(("intake", case.name, lambda w, r, c=case: scenario_intake(w, c, r)))
    for language in RFQ_EMAIL_LANGUAGES:
        out.append(("rfq_email", language, lambda w, r, x=language: scenario_rfq_email(w, x, r)))
    for name in FOLLOWUP_CASES:
        out.append(("followups", name, lambda w, r, x=name: scenario_followups(w, x, r)))
    for name in NEGOTIATION_CASES:
        out.append(("negotiation", name, lambda w, r, x=name: scenario_negotiation(w, x, r)))
    for name in RECIPIENT_CASES:
        out.append(("recipients", name, lambda w, r, x=name: scenario_recipients(w, x, r)))
    out.append(("award", "recommend_and_po", lambda w, r: scenario_award(w, r)))
    return out


async def _one(env: Env, scenario: str, case: str, runner: Runner, run: int,
               model: ScriptedModel | None) -> RunResult:  # fmt: skip
    result = RunResult(scenario, case, run)
    worlds: list[World] = []

    async def go() -> None:
        world = await World.create(env, f"flow {scenario} {case}", model)
        worlds.append(world)
        await runner(world, result)

    await run_timed(go, result)
    if worlds:
        world = worlds[0]
        result.notes["emails_captured"] = len(world.outbox.sent)
        if world.extraction_seconds:
            result.notes["extraction_s"] = round(
                sum(world.extraction_seconds) / len(world.extraction_seconds), 1
            )
        writer_s = world.negotiation_writer.seconds + world.followup_writer.seconds
        if writer_s:
            result.notes["writer_s"] = round(sum(writer_s) / len(writer_s), 1)
        # Outbound never leaves the process: every capture went through the guard.
        leaked = [c.to for c in world.outbox.sent if not all(
            is_test_domain(a.rpartition("@")[2]) or a.endswith(UNCONFIRMED_DOMAIN) for a in c.to
        )]  # fmt: skip
        result.check("only_eval_recipients", not leaked, f"{leaked}")
    return result


async def run_flow(
    env: Env,
    *,
    k: int,
    scenarios: Sequence[str] = SCENARIOS,
    only: Sequence[str] = (),
) -> list[RunResult]:
    model = ScriptedModel() if env.mode == "mock" else None
    selected = [c for c in cases(env.today) if c[0] in scenarios and (not only or c[1] in only)]
    results: list[RunResult] = []

    async def suite() -> None:
        for run in range(1, k + 1):
            for scenario, case, runner in selected:
                results.append(await _one(env, scenario, case, runner, run, model))

    if model is not None:
        with model.installed():
            await suite()
    else:
        await suite()
    return results


# --- Report ----------------------------------------------------------------------------------


def top_failures(results: Iterable[RunResult], limit: int = 15) -> list[tuple[str, int, str]]:
    """The checks that failed most often, with one detail each: the refinement backlog."""
    counts: Counter[str] = Counter()
    detail: dict[str, str] = {}
    for r in results:
        if r.error:
            key = f"{r.scenario}/{r.case}: error"
            counts[key] += 1
            detail.setdefault(key, r.error)
        for c in r.checks:
            if not c.ok:
                key = f"{r.scenario}/{r.case}: {c.name}"
                counts[key] += 1
                detail.setdefault(key, c.detail)
    return [(key, n, detail[key]) for key, n in counts.most_common(limit)]


def check_rates(results: Iterable[RunResult]) -> dict[str, tuple[int, int]]:
    """Per scenario: (checks passed, checks run)."""
    rates: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in results:
        if r.skipped:
            continue
        for c in r.checks:
            rates[r.scenario][0] += int(c.ok)
            rates[r.scenario][1] += 1
        if r.error:
            rates[r.scenario][1] += 1
    return {s: (v[0], v[1]) for s, v in rates.items()}


def flow_markdown(results: Sequence[RunResult], mode: Mode, k: int, today: date) -> str:
    rows = summarize(results)
    out = [markdown(rows, mode, k, today).replace("# Eval report", "# Flow eval report", 1)]
    out += ["## Assertion pass rate per scenario", "", "| Scenario | Checks passed | Rate |",
            "|---|---|---|"]  # fmt: skip
    for scenario, (ok, total) in check_rates(results).items():
        rate = f"{ok / total:.0%}" if total else "—"
        out.append(f"| {scenario} | {ok}/{total} | {rate} |")
    out += ["", "## Latency (average per run)", "", "| Scenario | Case | Run s | Extraction s "
            "| Writer s |", "|---|---|---|---|---|"]  # fmt: skip
    seen: dict[tuple[str, str], list[RunResult]] = defaultdict(list)
    for r in results:
        seen[(r.scenario, r.case)].append(r)
    for (scenario, case), runs in seen.items():

        def avg(key: str, runs: list[RunResult] = runs) -> str:
            values = [float(r.notes[key]) for r in runs if key in r.notes]
            return f"{sum(values) / len(values):.1f}" if values else "—"

        seconds = sum(r.seconds for r in runs) / len(runs)
        out.append(
            f"| {scenario} | {case} | {seconds:.1f} | {avg('extraction_s')} | {avg('writer_s')} |"
        )
    out += ["", "## Top failure reasons (refinement backlog)", ""]
    failures = top_failures(results)
    out += [f"{i}. **{key}** ×{n}: {detail}" for i, (key, n, detail) in enumerate(failures, 1)]
    if not failures:
        out.append("None.")
    return "\n".join(out) + "\n"


def write_flow_report(
    results: Sequence[RunResult], env: Env, k: int, folder: Path | None = None
) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = folder or REPORTS / f"{stamp}-flow-{env.mode}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "report.md").write_text(flow_markdown(results, env.mode, k, env.today))
    (folder / "report.json").write_text(
        json.dumps(
            {
                "mode": env.mode,
                "k": k,
                "today": env.today.isoformat(),
                "summary": summarize(results),
                "check_rates": check_rates(results),
                "top_failures": top_failures(results),
                "runs": [asdict(r) for r in results],
            },
            indent=1,
            default=str,
            ensure_ascii=False,
        )
    )
    return folder


async def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the RFQ flow evals (scripted suppliers).")
    parser.add_argument("--mock", action="store_true", help="scripted model outputs")
    parser.add_argument("-k", type=int, default=None, help="runs per case (default 3 live, 1 mock)")
    parser.add_argument("--scenario", action="append", choices=SCENARIOS, help="repeatable")
    parser.add_argument("--case", action="append", default=[], help="case name, repeatable")
    args = parser.parse_args(argv)

    mode: Mode = "mock" if args.mock else "live"
    settings = get_settings()
    if mode == "live" and not (settings.openai_api_key or os.environ.get("OPENAI_API_KEY")):
        print("live evals need OPENAI_API_KEY; use --mock to check the harness", file=sys.stderr)
        return 2
    k = args.k or (1 if args.mock else 3)
    engine = create_engine(settings)
    env = Env(
        engine=engine,
        storage=MemoryStorage(),
        runner_config=RunnerConfig.from_settings(settings),
        mode=mode,
        today=date.today(),
    )
    try:
        results = await run_flow(env, k=k, scenarios=args.scenario or SCENARIOS, only=args.case)
    finally:
        await engine.dispose()
    folder = write_flow_report(results, env, k)
    print((folder / "report.md").read_text())
    print(f"report: {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
