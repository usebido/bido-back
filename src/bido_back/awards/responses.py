"""A supplier's reply to a purchase order: read it, then acknowledge the order or flag it.

After the PO email goes out, the supplier answers in the same thread. Intake
routes such a message here (job `po.response`) instead of quote extraction: the
RFQ is awarded, and a confirmation is not a new offer.

1. The `po_response_reader` agent says what the email states: a confirmation,
   a confirmation with changes, a refusal, a question, a delivery update, or
   something else; the supplier's order number; and per ordered line any
   delivery date, unit price or quantity it states, each with the exact words.
2. Code grounds every value: words that are not in the email (subject, body,
   attachments) count as unread, and a date or number counts only when it is in
   those words. Code parses them itself and compares them with the order. The
   decimal separator is the one the supplier's own numbers prove (`2.75` a point,
   `2,75` a comma), never the language they are registered with: an Italian
   supplier may answer in English. A number that reads two ways with nothing to
   prove which (`2,000`: two thousand or two) is never a change; it matches the
   order when one reading does, else it waits for the buyer as written.
3. A plain confirmation of a `sent` order, grounded, with no stated value that
   differs from the order and no other date in the supplier's own words, is
   recorded as the acknowledgement (`purchase_order.acknowledged`, source
   `email`, with the supplier's order number). Everything else is recorded as
   `purchase_order.supplier_response` with what differs, and the order is not
   changed: a new date, price or quantity, a refusal or a question waits for
   the buyer (`needs_review`). A reply the agent could not read is flagged too.

Nothing here writes to the supplier or edits the order's lines; the buyer
decides (acknowledge, cancel, or answer in their mailbox).
"""

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.catalog import PO_RESPONSE_READER
from bido_back.agents.catalog.contracts import (
    PoResponseInput,
    PoResponseOrderedLine,
    PoResponseOutput,
    Quoted,
)
from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.agents.definition import AgentDefinition
from bido_back.agents.runner import Completed, run_to_completion
from bido_back.awards.lifecycle import PO_SUPPLIER_RESPONSE, acknowledge
from bido_back.awards.orders import po_document
from bido_back.awards.schemas import RESPONDING_FULFILMENT_STATES, AcknowledgeRequest
from bido_back.db import tenant_session
from bido_back.db.models import Attachment, Invite, IssuedPurchaseOrder, Message, Supplier
from bido_back.documents import DocumentText, extract_text, render_documents
from bido_back.quotes.parsing import (
    dates_in,
    decimal_separator_in,
    normalize_text,
    stated_values,
)
from bido_back.rfqs.audit import rfq_event
from bido_back.rfqs.language import supplier_email_language
from bido_back.storage import BlobStorage
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import PO_RESPONSE

logger = logging.getLogger(__name__)

type Db = AsyncEngine | async_sessionmaker[AsyncSession]

_READABLE: Final = frozenset({"stored", "failed"})
MAX_BODY_CHARS: Final = 60_000

# Where the supplier's own words end: a reply header or the quoted original.
_QUOTED: Final = re.compile(
    r"^\s*(?:>|-{2,}\s*(?:original|mensagem|ursprüngliche|message d'origine|mensaje)"
    r"|(?:on|em|am|le|el|il)\s.+(?:wrote|escreveu|schrieb|a écrit|escribió|ha scritto)\s*:"
    r"|(?:from|de|von|da):\s)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Change:
    position: int
    field: str
    ordered: str | None
    stated: str | None
    # The stated value could not be read as a date or number, or was not in the email.
    unverified: bool = False
    # The number reads two ways (`2,000`) and neither is the order's: `stated` as written.
    ambiguous: bool = False

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "position": self.position,
            "field": self.field,
            "ordered": self.ordered,
            "stated": self.stated,
            "unverified": self.unverified,
            "ambiguous": self.ambiguous,
        }


@dataclass(frozen=True, slots=True)
class Reading:
    """What the email says once grounded and compared with the order."""

    kind: str
    confirmed: bool
    supplier_order_ref: str | None
    changes: list[Change]
    # Dates in the supplier's own words that match nothing on the order.
    other_dates: list[date]
    summary: str

    @property
    def plain_confirmation(self) -> bool:
        return (
            self.kind == "confirmation"
            and self.confirmed
            and not self.changes
            and not self.other_dates
        )


def own_words(body: str) -> str:
    """The supplier's text above the quoted original (the PO email we sent)."""
    kept: list[str] = []
    for line in body.splitlines():
        if _QUOTED.match(line):
            break
        kept.append(line)
    return "\n".join(kept)


def _grounded(quoted: Quoted | None, text: str) -> str | None:
    """The quoted words, when they are really in the email; else None."""
    if quoted is None or not quoted.quote or not quoted.quote.strip():
        return None
    return quoted.quote if normalize_text(quoted.quote) in text else None


_NUMERIC_DATE: Final = re.compile(r"(?<!\d)(\d{1,2})([./-])(\d{1,2})\2(\d{4}|\d{2})(?!\d)")


def _one_date(value: str, language: str | None) -> date | None:
    """The one date written in `value`; 04.12.2026 is day-first (US English: 04/12 is
    month-first only with slashes, and then it stays ambiguous: None)."""
    found = dates_in(value)
    if len(found) == 1:
        return next(iter(found))
    numeric = _NUMERIC_DATE.findall(value)
    if len(found) != 2 or len(numeric) != 1:
        return None
    first, separator, second, year = numeric[0]
    if separator == "/" and (language or "en").lower().startswith("en"):
        return None
    full_year = int(year) + 2000 if len(year) == 2 else int(year)
    try:
        return date(full_year, int(second), int(first))
    except ValueError:
        return None


def _same_or_swapped(day: date, expected: set[date]) -> bool:
    if day in expected:
        return True
    try:
        return date(day.year, day.day, day.month) in expected
    except ValueError:
        return False


def _num(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if text != "-0" else "0"


def read_response(
    output: PoResponseOutput,
    *,
    lines: Sequence[PoResponseOrderedLine],
    subject: str,
    body: str,
    attachments_text: str,
    language: str | None,
    known_dates: set[date],
) -> Reading:
    """Ground the agent's reading in the email and compare it with the order. Pure."""
    text = normalize_text("\n".join((subject, body, attachments_text)))
    # Proven by the supplier's numbers, not by the quoted order below their words.
    decimal = decimal_separator_in("\n".join((subject, own_words(body), attachments_text)))
    ordered = {line.position: line for line in lines}
    changes: list[Change] = []
    for stated in output.lines:
        line = ordered.get(stated.position)
        if line is None:
            continue
        need_by = date.fromisoformat(line.delivery_date) if line.delivery_date else None
        if stated.delivery_date is not None and stated.delivery_date.value:
            words = _grounded(stated.delivery_date, text)
            # The date is read from the supplier's own words, never from the model's value.
            day = _one_date(words, language) if words is not None else None
            if day is None:
                changes.append(
                    Change(
                        line.position,
                        "delivery_date",
                        line.delivery_date,
                        stated.delivery_date.value,
                        unverified=True,
                    )
                )
            elif need_by is None or not _same_or_swapped(day, {need_by}):
                changes.append(
                    Change(line.position, "delivery_date", line.delivery_date, day.isoformat())
                )
        for field, current in (("unit_price", line.unit_price), ("quantity", line.quantity)):
            quoted = getattr(stated, field)
            if not isinstance(quoted, Quoted) or not quoted.value:
                continue
            words = _grounded(quoted, text)
            # Empty when the number is not in the words it claims to come from.
            values: set[Decimal] = (
                stated_values(quoted.value, words, decimal=decimal) if words else set()
            )
            if not values:
                changes.append(Change(line.position, field, current, quoted.value, unverified=True))
            elif len(values) > 1:
                if Decimal(current) not in values:
                    changes.append(
                        Change(
                            line.position,
                            field,
                            current,
                            quoted.value,
                            unverified=True,
                            ambiguous=True,
                        )
                    )
            elif (value := next(iter(values))) != Decimal(current):
                changes.append(Change(line.position, field, current, _num(value)))
    expected = known_dates | {
        date.fromisoformat(line.delivery_date) for line in lines if line.delivery_date
    }
    other = sorted(
        day
        for day in dates_in(own_words(body))
        if not _same_or_swapped(day, expected)
        and not _same_or_swapped(day, {c for c in _changed_dates(changes)})
    )
    ref = _grounded(output.supplier_order_ref, text)
    order_ref = output.supplier_order_ref.value if output.supplier_order_ref and ref else None
    return Reading(
        kind=output.kind,
        confirmed=_grounded(output.confirmation, text) is not None,
        supplier_order_ref=order_ref.strip()[:120] if order_ref else None,
        changes=changes,
        other_dates=_dedupe_swapped(other),
        summary=output.summary.strip(),
    )


def _changed_dates(changes: Sequence[Change]) -> list[date]:
    found: list[date] = []
    for change in changes:
        if change.field == "delivery_date" and change.stated and not change.unverified:
            found.append(date.fromisoformat(change.stated))
    return found


def _dedupe_swapped(days: list[date]) -> list[date]:
    """A day-first/month-first pair from one written date counts once (the earlier kept)."""
    kept: list[date] = []
    for day in days:
        if not _same_or_swapped(day, set(kept)):
            kept.append(day)
    return kept


# --- The job ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Loaded:
    message_id: UUID
    po_id: UUID
    occurred_on: date
    task: PoResponseInput
    language: str
    known_dates: set[date]
    attachments: list[tuple[str, str, str]]


async def responding_po(session: AsyncSession, invite_id: UUID) -> IssuedPurchaseOrder | None:
    """The invite's sent purchase order a supplier reply is about, if any."""
    return await session.scalar(
        select(IssuedPurchaseOrder)
        .where(
            IssuedPurchaseOrder.invite_id == invite_id,
            IssuedPurchaseOrder.status == "sent",
            IssuedPurchaseOrder.fulfilment_state.in_(list(RESPONDING_FULFILMENT_STATES)),
        )
        .order_by(IssuedPurchaseOrder.sent_at.desc())
        .limit(1)
    )


async def _load(db: Db, tenant_id: UUID, message_id: UUID) -> _Loaded | None:
    async with tenant_session(db, tenant_id) as session:
        message = await session.get(Message, message_id)
        if message is None or message.direction != "in" or message.status not in _READABLE:
            return None
        if message.invite_id is None:
            return None
        po = await responding_po(session, message.invite_id)
        if po is None:
            logger.info("po.response: no open purchase order message=%s", message_id)
            return None
        invite = await session.get(Invite, message.invite_id)
        supplier = await session.get(Supplier, invite.supplier_id) if invite else None
        if supplier is None:
            return None
        doc = await po_document(session, po, branded=False)
        attachments = [
            (a.filename, a.content_type, a.storage_key)
            for a in await session.scalars(
                select(Attachment)
                .where(Attachment.message_id == message_id)
                .order_by(Attachment.created_at, Attachment.id)
            )
        ]
        body = message.body_text or ""
        language = supplier_email_language(supplier)
        known = {doc.issued_on, message.occurred_at.date()}
        if po.sent_at is not None:
            known.add(po.sent_at.date())
        if doc.quote_date is not None:
            known.add(doc.quote_date)
        return _Loaded(
            message_id=message_id,
            po_id=po.id,
            occurred_on=message.occurred_at.date(),
            language=language,
            known_dates=known,
            attachments=attachments,
            task=PoResponseInput(
                po_ref=po.ref,
                rfq_ref=doc.rfq_ref,
                supplier_name=supplier.name,
                supplier_language=language,
                lines=[
                    PoResponseOrderedLine(
                        position=line.position,
                        description=line.description,
                        part_number=line.part_number,
                        mpn=line.mpn,
                        quantity=_num(line.quantity),
                        unit=line.unit,
                        unit_price=_num(line.unit_price),
                        currency=doc.currency,
                        delivery_date=line.need_by.isoformat() if line.need_by else None,
                    )
                    for line in doc.lines
                ],
                email_subject=message.subject or "",
                email_body=body[:MAX_BODY_CHARS],
            ),
        )


async def _documents(storage: BlobStorage, refs: list[tuple[str, str, str]]) -> list[DocumentText]:
    return [
        extract_text(filename, content_type, await storage.get(key))
        for filename, content_type, key in refs
    ]


async def _record(
    db: Db,
    tenant_id: UUID,
    loaded: _Loaded,
    reading: Reading | None,
) -> str:
    """Acknowledge or flag the order, and mark the message read. Returns the outcome."""
    async with tenant_session(db, tenant_id) as session:
        message = await session.scalar(
            select(Message).where(Message.id == loaded.message_id).with_for_update()
        )
        if message is None or message.status not in _READABLE:
            return "already_read"
        po = await session.scalar(
            select(IssuedPurchaseOrder)
            .where(IssuedPurchaseOrder.id == loaded.po_id)
            .with_for_update()
        )
        if po is None:
            return "no_order"
        message.status = "extracted" if reading is not None else "failed"
        if reading is not None and reading.plain_confirmation and po.fulfilment_state == "sent":
            await acknowledge(
                session,
                po,
                None,
                AcknowledgeRequest(
                    acknowledged_on=loaded.occurred_on,
                    supplier_order_ref=reading.supplier_order_ref,
                    note=reading.summary[:2000] or None,
                ),
                source="email",
                message_id=loaded.message_id,
            )
            return "acknowledged"
        needs_review = reading is None or reading.kind != "other" or bool(reading.changes)
        if reading is not None and reading.plain_confirmation:
            # Confirmed again after the order was acknowledged: nothing new.
            needs_review = False
        session.add(
            rfq_event(
                po.tenant_id,
                None,
                PO_SUPPLIER_RESPONSE,
                po.rfq_id,
                purchase_order_id=po.id,
                purchase_order_ref=po.ref,
                invite_id=po.invite_id,
                message_id=loaded.message_id,
                kind=reading.kind if reading else "unread",
                needs_review=needs_review,
                confirmed=reading.confirmed if reading else False,
                supplier_order_ref=reading.supplier_order_ref if reading else None,
                changes=[c.as_json() for c in reading.changes] if reading else [],
                other_dates=[d.isoformat() for d in reading.other_dates] if reading else [],
                summary=reading.summary if reading else None,
            )
        )
        await session.flush()
        return "flagged" if needs_review else "noted"


type Reader = Callable[[PoResponseInput, ToolContext], Awaitable[PoResponseOutput | None]]


def agent_reader(
    runner_config: RunnerConfig, agent: AgentDefinition[PoResponseOutput] = PO_RESPONSE_READER
) -> Reader:
    """The `po_response_reader` agent through the runner; None when the run fails."""

    async def read(task: PoResponseInput, context: ToolContext) -> PoResponseOutput | None:
        result = await run_to_completion(agent, task, context, config=runner_config)
        return result.output if isinstance(result, Completed) else None

    return read


async def handle_po_response(
    envelope: Envelope,
    *,
    db: Db,
    storage: BlobStorage,
    runner_config: RunnerConfig | None = None,
    reader: Reader | None = None,
) -> None:
    """Handle one `po.response` job. Idempotent per message (its status moves on).

    `reader` defaults to the agent (`agent_reader(runner_config)`); tests pass their own.
    """
    try:
        message_id = UUID(str(envelope.payload.get("message_id")))
    except ValueError:
        logger.error("po.response: invalid payload key=%s", envelope.idempotency_key)
        return
    tenant_id = envelope.tenant_id
    loaded = await _load(db, tenant_id, message_id)
    if loaded is None:
        return
    documents = await _documents(storage, loaded.attachments)
    attachments_text = render_documents(documents)
    task = loaded.task.model_copy(update={"attachments_text": attachments_text})
    context = ToolContext(tenant_id=tenant_id, user_id=None, run_id=uuid4())
    read = reader or agent_reader(runner_config or RunnerConfig())
    output = await read(task, context)
    reading: Reading | None = None
    if output is not None:
        reading = read_response(
            output,
            lines=task.lines,
            subject=task.email_subject,
            body=task.email_body,
            attachments_text=attachments_text,
            language=loaded.language,
            known_dates=loaded.known_dates,
        )
    else:
        logger.warning(
            "po.response: the reader failed message=%s run=%s", message_id, context.run_id
        )
    outcome = await _record(db, tenant_id, loaded, reading)
    logger.info(
        "po.response message=%s po=%s outcome=%s kind=%s changes=%d",
        message_id,
        loaded.po_id,
        outcome,
        reading.kind if reading else "unread",
        len(reading.changes) if reading else 0,
    )


def po_response_handler(
    *, db: Db, storage: BlobStorage, runner_config: RunnerConfig
) -> Callable[[Envelope], Awaitable[None]]:
    """`handle_po_response` bound to its dependencies: a worker `Handler`."""

    async def handle(envelope: Envelope) -> None:
        await handle_po_response(envelope, db=db, storage=storage, runner_config=runner_config)

    return handle


JOB_TYPE: Final = PO_RESPONSE
