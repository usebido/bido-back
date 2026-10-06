"""The QUOTE_EXTRACT job: a supplier's reply -> a validated, versioned proposal.

Payload: `{"message_id": "<uuid>"}`; the tenant comes from the envelope.

1. In a tenant-scoped transaction, load the message, its invite, RFQ, RFQ
   lines, supplier and attachment rows. Skip (and change nothing) when a
   proposal already references the message, the message is already
   `extracted`, or it is not a matched, extractable reply.
2. Outside any transaction: fetch the attachments from blob storage and turn
   them into text (`bido_back.documents`), then run the `quote_extraction`
   agent. The agent has no tools at all: supplier content is untrusted.
3. Validate the output deterministically (`bido_back.quotes.validation`).
4. In a second transaction, lock the message and the invite, re-check that no
   proposal references the message (a concurrent delivery may have won), and
   insert the next proposal version with its lines and per-field provenance.
   The message becomes `extracted`. A reply that is not a quote (out-of-office,
   question, refusal) creates no proposal; a refusal marks the invite
   `declined`.

An agent-run failure marks the message `failed` and returns normally: the job
is done, a retry is a deliberate re-enqueue (a `failed` message is extracted
again). Infrastructure errors (database, storage) raise, so the queue retries.

Logs carry ids and codes only, never message or document content.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.approvals import ApprovalSink
from bido_back.agents.catalog import QUOTE_EXTRACTION
from bido_back.agents.catalog.contracts import (
    QuoteExtractionInput,
    QuoteExtractionOutput,
    RfqEmailLine,
)
from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.agents.definition import AgentDefinition
from bido_back.agents.runner import Completed, Failed, run_to_completion
from bido_back.db import tenant_session
from bido_back.db.models import (
    Attachment,
    Invite,
    Message,
    Proposal,
    ProposalLine,
    Rfq,
    RfqLine,
    Supplier,
)
from bido_back.documents import DocumentText, extract_text, render_documents
from bido_back.quotes.parsing import decimal_separator_for
from bido_back.quotes.validation import (
    LineCheck,
    QuoteSources,
    RequestedLine,
    ValidatedQuote,
    validate_quote,
)
from bido_back.storage import BlobStorage
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import QUOTE_EXTRACT

logger = logging.getLogger(__name__)

JOB_TYPE: Final = QUOTE_EXTRACT
# Statuses a message can be extracted from. `failed` allows a deliberate retry.
_EXTRACTABLE: Final = frozenset({"stored", "failed"})
# The email body is supplier text like any attachment: bound it.
MAX_BODY_CHARS: Final = 60_000

type Db = AsyncEngine | async_sessionmaker[AsyncSession]


@dataclass(frozen=True, slots=True)
class _AttachmentRef:
    filename: str
    content_type: str
    storage_key: str


@dataclass(frozen=True, slots=True)
class _Loaded:
    message_id: UUID
    invite_id: UUID
    rfq_ref: str
    subject: str
    body: str
    supplier_language: str | None
    requested: list[RfqEmailLine]
    line_ids: dict[int, UUID]
    line_mpns: dict[int, str | None]
    attachments: list[_AttachmentRef]


async def handle_quote_extract(
    envelope: Envelope,
    *,
    db: Db,
    storage: BlobStorage,
    runner_config: RunnerConfig,
    approvals: ApprovalSink | None = None,
    agent: AgentDefinition[QuoteExtractionOutput] = QUOTE_EXTRACTION,
    today: date | None = None,
) -> None:
    """Handle one QUOTE_EXTRACT envelope. Idempotent per message."""
    tenant_id = envelope.tenant_id
    message_id = _message_id(envelope)
    if message_id is None:
        logger.error(
            "quote.extract: invalid payload tenant=%s key=%s", tenant_id, envelope.idempotency_key
        )
        return

    loaded = await _load(db, tenant_id, message_id)
    if loaded is None:
        return

    documents = await _documents(storage, loaded.attachments)
    body = loaded.body
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n[email body truncated]"
    task = QuoteExtractionInput(
        rfq_ref=loaded.rfq_ref,
        requested_lines=loaded.requested,
        email_subject=loaded.subject,
        email_body=body,
        attachments_text=render_documents(documents),
    )

    context = ToolContext(tenant_id=tenant_id, user_id=None, run_id=uuid4())
    result = await run_to_completion(
        agent, task, context, approvals=approvals, config=runner_config
    )
    if not isinstance(result, Completed):
        code = result.failure.code if isinstance(result, Failed) else "unexpected_approval"
        logger.warning(
            "quote.extract: agent run failed tenant=%s message=%s run=%s code=%s",
            tenant_id,
            message_id,
            context.run_id,
            code,
        )
        await _mark_failed(db, tenant_id, message_id)
        return

    validated = validate_quote(
        result.output,
        requested=[
            RequestedLine(id=line_id, position=position, mpn=loaded.line_mpns.get(position))
            for position, line_id in loaded.line_ids.items()
        ],
        sources=QuoteSources(
            email_subject=loaded.subject,
            email_body=body,
            documents={doc.filename: doc for doc in documents},
        ),
        today=today or datetime.now(UTC).date(),
        decimal=decimal_separator_for(loaded.supplier_language),
    )
    meta: _RunMeta = {
        "agent": agent.name,
        "prompt_version": agent.prompt_version,
        "model": agent.model,
        "run_id": str(context.run_id),
    }
    await _persist(db, tenant_id, loaded, validated, meta)


def quote_extract_handler(
    *,
    db: Db,
    storage: BlobStorage,
    runner_config: RunnerConfig,
    approvals: ApprovalSink | None = None,
) -> Callable[[Envelope], Awaitable[None]]:
    """`handle_quote_extract` bound to its dependencies: a worker `Handler`.

    Register with `registry.register(JOB_TYPE, quote_extract_handler(...))`.
    """

    async def handle(envelope: Envelope) -> None:
        await handle_quote_extract(
            envelope, db=db, storage=storage, runner_config=runner_config, approvals=approvals
        )

    return handle


def _message_id(envelope: Envelope) -> UUID | None:
    raw = envelope.payload.get("message_id")
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


# --- Load --------------------------------------------------------------------------


async def _load(db: Db, tenant_id: UUID, message_id: UUID) -> _Loaded | None:
    async with tenant_session(db, tenant_id) as session:
        message = await session.scalar(select(Message).where(Message.id == message_id))
        if message is None:
            logger.warning(
                "quote.extract: message not found tenant=%s message=%s", tenant_id, message_id
            )
            return None
        if await _has_proposal(session, message_id):
            logger.info("quote.extract: already extracted message=%s", message_id)
            return None
        if message.status not in _EXTRACTABLE or message.direction != "in":
            logger.info("quote.extract: skipped message=%s status=%s", message_id, message.status)
            return None
        if message.invite_id is None:
            logger.warning("quote.extract: message without invite message=%s", message_id)
            return None

        row = (
            await session.execute(
                select(Invite.id, Rfq.id, Rfq.ref, Supplier.language)
                .join(Rfq, Rfq.id == Invite.rfq_id)
                .join(Supplier, Supplier.id == Invite.supplier_id)
                .where(Invite.id == message.invite_id)
            )
        ).one_or_none()
        if row is None:
            logger.warning("quote.extract: invite not found message=%s", message_id)
            return None
        invite_id, rfq_id, rfq_ref, language = row

        lines = (
            await session.scalars(
                select(RfqLine).where(RfqLine.rfq_id == rfq_id).order_by(RfqLine.position)
            )
        ).all()
        attachments = (
            await session.scalars(
                select(Attachment)
                .where(Attachment.message_id == message_id)
                .order_by(Attachment.created_at, Attachment.id)
            )
        ).all()

        return _Loaded(
            message_id=message_id,
            invite_id=invite_id,
            rfq_ref=rfq_ref,
            subject=message.subject or "",
            body=message.body_text or "",
            supplier_language=language,
            requested=[_email_line(line) for line in lines],
            line_ids={line.position: line.id for line in lines},
            line_mpns={line.position: line.mpn for line in lines},
            attachments=[
                _AttachmentRef(a.filename, a.content_type, a.storage_key) for a in attachments
            ],
        )


def _email_line(line: RfqLine) -> RfqEmailLine:
    return RfqEmailLine(
        position=line.position,
        description=line.description,
        mpn=line.mpn,
        manufacturer=line.manufacturer,
        quantities=[Decimal(str(q)) for q in line.quantities],
        unit=line.unit,
        need_by=line.need_by,
        incoterm=line.incoterm,
        qualification=line.qualification,
    )


async def _has_proposal(session: AsyncSession, message_id: UUID) -> bool:
    found = await session.scalar(
        select(Proposal.id).where(Proposal.message_id == message_id).limit(1)
    )
    return found is not None


async def _documents(storage: BlobStorage, refs: list[_AttachmentRef]) -> list[DocumentText]:
    documents: list[DocumentText] = []
    seen: set[str] = set()
    for ref in refs:
        data = await storage.get(ref.storage_key)
        document = extract_text(ref.filename, ref.content_type, data)
        name = _unique(document.filename, seen)
        if name != document.filename:
            # Two attachments with one name: rename before the text is built, so
            # the markers (and the model's citations) tell them apart.
            document = extract_text(name, ref.content_type, data)
        seen.add(document.filename)
        documents.append(document)
    return documents


def _unique(name: str, seen: set[str]) -> str:
    if name not in seen:
        return name
    stem, dot, suffix = name.rpartition(".")
    if not dot:
        stem, suffix = name, ""
    n = 2
    while True:
        candidate = f"{stem} ({n}).{suffix}" if suffix else f"{stem} ({n})"
        if candidate not in seen:
            return candidate
        n += 1


# --- Persist -----------------------------------------------------------------------

type _RunMeta = dict[str, str | int]


async def _mark_failed(db: Db, tenant_id: UUID, message_id: UUID) -> None:
    async with tenant_session(db, tenant_id) as session:
        message = await session.scalar(
            select(Message).where(Message.id == message_id).with_for_update()
        )
        if message is not None and message.status in _EXTRACTABLE:
            message.status = "failed"


async def _persist(
    db: Db,
    tenant_id: UUID,
    loaded: _Loaded,
    validated: ValidatedQuote,
    meta: _RunMeta,
) -> None:
    async with tenant_session(db, tenant_id) as session:
        message = await session.scalar(
            select(Message).where(Message.id == loaded.message_id).with_for_update()
        )
        invite = await session.scalar(
            select(Invite).where(Invite.id == loaded.invite_id).with_for_update()
        )
        if message is None or invite is None:
            logger.warning(
                "quote.extract: message or invite vanished message=%s", loaded.message_id
            )
            return
        if await _has_proposal(session, loaded.message_id) or message.status == "extracted":
            logger.info("quote.extract: concurrent extraction won message=%s", loaded.message_id)
            return

        if not validated.is_quote:
            message.status = "extracted"
            if validated.output.declined:
                invite.status = "declined"
            logger.info(
                "quote.extract: not a quote message=%s declined=%s run=%s",
                loaded.message_id,
                validated.output.declined,
                meta["run_id"],
            )
            return

        # The invite row lock serializes versions across replies to one invite.
        latest = await session.scalar(
            select(func.max(Proposal.version)).where(Proposal.invite_id == invite.id)
        )
        fields = validated.provenance()
        fields["_extraction"] = {key: value for key, value in meta.items()}
        proposal = Proposal(
            tenant_id=tenant_id,
            invite_id=invite.id,
            message_id=loaded.message_id,
            version=(latest or 0) + 1,
            currency=_str(validated.parsed("currency")),
            valid_until=_date(validated.parsed("valid_until")),
            incoterm=_str(validated.parsed("incoterm")),
            payment_terms=_str(validated.parsed("payment_terms")),
            fields=fields,
        )
        session.add(proposal)
        await session.flush()
        session.add_all(_proposal_line(tenant_id, proposal.id, line) for line in validated.lines)
        message.status = "extracted"
        await session.flush()
        logger.info(
            "quote.extract: proposal stored message=%s proposal=%s version=%s lines=%s "
            "needs_review=%s run=%s",
            loaded.message_id,
            proposal.id,
            proposal.version,
            len(validated.lines),
            validated.needs_review,
            meta["run_id"],
        )


def _proposal_line(tenant_id: UUID, proposal_id: UUID, line: LineCheck) -> ProposalLine:
    lead_time = line.parsed("lead_time_days")
    return ProposalLine(
        tenant_id=tenant_id,
        proposal_id=proposal_id,
        rfq_line_id=line.rfq_line_id,
        offered_mpn=_str(line.parsed("offered_mpn")),
        is_alternative=line.is_alternative,
        min_quantity=_decimal(line.parsed("min_quantity")),
        unit_price=_decimal(line.parsed("unit_price")),
        unit=_str(line.parsed("unit")),
        tooling_cost=_decimal(line.parsed("tooling_cost")),
        moq=_decimal(line.parsed("moq")),
        lead_time_days=lead_time if isinstance(lead_time, int) else None,
        origin_country=_str(line.parsed("origin_country")),
        fields=line.provenance(),
        needs_review=line.needs_review,
    )


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _date(value: object) -> date | None:
    return value if isinstance(value, date) else None


def _decimal(value: object) -> Decimal | None:
    return value if isinstance(value, Decimal) else None
