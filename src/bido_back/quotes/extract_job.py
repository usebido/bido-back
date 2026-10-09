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
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.approvals import ApprovalSink
from bido_back.agents.catalog import QUOTE_EXTRACTION
from bido_back.agents.catalog.contracts import (
    ExtractedValue,
    PriorSupplierOffer,
    ProposalLineDraft,
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
from bido_back.negotiation.hooks import cancel_on_new_offer
from bido_back.quotes.parsing import decimal_separator_for
from bido_back.quotes.state import (
    OfferEvent,
    StoredField,
    reconcile,
    restore_field,
    restore_quote,
    revision_marker,
    stamp_quote,
    store_quote,
)
from bido_back.quotes.validation import (
    HEADER_FIELDS,
    LINE_FIELDS,
    FieldCheck,
    FieldKind,
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
    prior_supplier_offer: PriorSupplierOffer | None


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
        prior_supplier_offer=loaded.prior_supplier_offer,
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
    meta: RunMeta = {
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
        prior_events = [
            event
            for event in await offer_events(session, invite_id)
            if event.occurred_at < message.occurred_at and event.received_at <= message.created_at
        ]
        prior_state = reconcile(prior_events) if prior_events else None

        return _Loaded(
            message_id=message_id,
            invite_id=invite_id,
            rfq_ref=rfq_ref,
            subject=message.subject or "",
            body=message.body_text or "",
            supplier_language=language,
            requested=[email_line(line) for line in lines],
            line_ids={line.position: line.id for line in lines},
            line_mpns={line.position: line.mpn for line in lines},
            attachments=[
                _AttachmentRef(a.filename, a.content_type, a.storage_key) for a in attachments
            ],
            prior_supplier_offer=(
                PriorSupplierOffer(
                    message_id=str(prior_state.active_message_id),
                    currency=_str(prior_state.quote.parsed("currency")),
                    lines=[line.draft for line in prior_state.quote.lines],
                )
                if prior_state is not None
                else None
            ),
        )


def email_line(line: RfqLine) -> RfqEmailLine:
    """An RFQ line as the extraction agent sees it (the request it answers)."""
    return RfqEmailLine(
        position=line.position,
        description=line.description,
        part_number=line.part_number,
        mpn=line.mpn,
        manufacturer=line.manufacturer,
        quantities=[Decimal(str(q)) for q in line.quantities],
        unit=line.unit,
        need_by=line.need_by,
        incoterm=line.incoterm,
        qualification=line.qualification,
    )


def _field_from_row(
    name: str, kind: FieldKind, raw: object, value: object, message_id: UUID
) -> FieldCheck:
    entry = cast(dict[str, object], raw) if isinstance(raw, dict) else {}
    extracted = ExtractedValue.model_validate(
        {
            "value": entry.get("value", str(value) if value is not None else None),
            "source": entry.get("source"),
            "confidence": entry.get("confidence", 0),
        }
    )
    parsed = entry.get("parsed", value)
    serialized = (
        parsed.isoformat()
        if isinstance(parsed, date)
        else str(parsed)
        if parsed is not None
        else None
    )
    return restore_field(
        name,
        kind,
        StoredField.model_validate(
            {
                "extracted": extracted.model_dump(mode="json"),
                "parsed": serialized,
                "issues": entry.get("issues", []),
                "message_id": entry.get("message_id", str(message_id)),
                "retained_by": entry.get("retained_by", []),
                "corrected": entry.get("corrected", False),
                "correction_id": entry.get("correction_id"),
            }
        ),
    )


def _stored_issues(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, dict):
        return ()
    review = cast(dict[str, object], raw).get("_review")
    if not isinstance(review, dict):
        return ()
    issues = cast(dict[str, object], review).get("issues")
    return (
        tuple(str(issue) for issue in cast(list[object], issues))
        if isinstance(issues, list)
        else ()
    )


def _legacy_quote(
    proposal: Proposal, rows: list[ProposalLine], positions: dict[UUID, int]
) -> ValidatedQuote:
    header = {
        name: _field_from_row(
            name,
            kind,
            proposal.fields.get(name),
            getattr(proposal, name, None),
            proposal.message_id,
        )
        for name, kind in HEADER_FIELDS.items()
    }
    lines: list[LineCheck] = []
    for row in rows:
        fields = {
            name: _field_from_row(
                name, kind, row.fields.get(name), getattr(row, name, None), proposal.message_id
            )
            for name, kind in LINE_FIELDS.items()
        }
        draft = ProposalLineDraft.model_validate(
            {
                "rfq_line_position": positions.get(row.rfq_line_id) if row.rfq_line_id else None,
                "is_alternative": row.is_alternative,
                **{name: check.extracted.model_dump(mode="json") for name, check in fields.items()},
            }
        )
        issues = _stored_issues(row.fields)
        if (
            row.needs_review
            and not issues
            and not any(check.needs_review for check in fields.values())
        ):
            issues = ("legacy_review_required",)
        lines.append(
            LineCheck(
                draft=draft,
                rfq_line_id=row.rfq_line_id,
                is_alternative=row.is_alternative,
                fields=fields,
                issues=issues,
            )
        )
    output = QuoteExtractionOutput.model_validate(
        {
            "is_quote": True,
            **{name: check.extracted.model_dump(mode="json") for name, check in header.items()},
            "lines": [line.draft.model_dump(mode="json") for line in lines],
        }
    )
    return ValidatedQuote(
        output=output, header=header, lines=tuple(lines), issues=_stored_issues(proposal.fields)
    )


async def offer_events(session: AsyncSession, invite_id: UUID) -> list[OfferEvent]:
    """Every proposal of the invite as an immutable offer event, in version order."""
    rows = (
        await session.execute(
            select(Proposal, Message)
            .join(Message, Message.id == Proposal.message_id)
            .where(Proposal.invite_id == invite_id)
            .order_by(Proposal.version)
        )
    ).all()
    if not rows:
        return []
    proposal_ids = [proposal.id for proposal, _ in rows]
    stored_lines: dict[UUID, list[ProposalLine]] = {proposal_id: [] for proposal_id in proposal_ids}
    for line in await session.scalars(
        select(ProposalLine).where(ProposalLine.proposal_id.in_(proposal_ids))
    ):
        stored_lines[line.proposal_id].append(line)
    positions = dict(
        (
            await session.execute(
                select(RfqLine.id, RfqLine.position)
                .join(Invite, Invite.rfq_id == RfqLine.rfq_id)
                .where(Invite.id == invite_id)
            )
        ).all()
    )
    events: list[OfferEvent] = []
    for proposal, message in rows:
        event_record: object = proposal.fields.get("_quote_event")
        quote = (
            restore_quote(event_record)
            if event_record is not None
            else _legacy_quote(proposal, stored_lines[proposal.id], positions)
        )
        events.append(
            OfferEvent(
                message_id=message.id,
                message_header=message.message_id_header,
                in_reply_to=message.in_reply_to,
                occurred_at=message.occurred_at,
                received_at=message.created_at,
                quote=quote,
                revision=revision_marker(message.subject, message.body_text),
            )
        )
    # A human correction remains authoritative for its original field evidence.
    # Later supplier statements have a different message origin and supersede it.
    for proposal, _ in rows:
        for name, kind in HEADER_FIELDS.items():
            entry: object = proposal.fields.get(name)
            if isinstance(entry, dict) and cast(dict[str, object], entry).get("corrected") is True:
                check = _field_from_row(
                    name,
                    kind,
                    cast(dict[str, object], entry),
                    getattr(proposal, name),
                    proposal.message_id,
                )
                for index, event in enumerate(events):
                    if event.message_id == check.message_id:
                        events[index] = replace(
                            event,
                            quote=replace(event.quote, header={**event.quote.header, name: check}),
                        )
        for row in stored_lines[proposal.id]:
            for name, kind in LINE_FIELDS.items():
                entry = row.fields.get(name)
                if (
                    not isinstance(entry, dict)
                    or cast(dict[str, object], entry).get("corrected") is not True
                ):
                    continue
                check = _field_from_row(
                    name,
                    kind,
                    cast(dict[str, object], entry),
                    getattr(row, name, None),
                    proposal.message_id,
                )
                for index, event in enumerate(events):
                    if event.message_id != check.message_id:
                        continue
                    matches = [
                        line
                        for line in event.quote.lines
                        if line.rfq_line_id == row.rfq_line_id
                        and (
                            line.fields["min_quantity"].parsed == row.min_quantity
                            or name == "min_quantity"
                        )
                    ]
                    if len(matches) != 1:
                        continue
                    target = matches[0]
                    corrected = replace(
                        target,
                        fields={**target.fields, name: check},
                        draft=target.draft.model_copy(update={name: check.extracted}),
                    )
                    events[index] = replace(
                        event,
                        quote=replace(
                            event.quote,
                            lines=tuple(
                                corrected if line is target else line for line in event.quote.lines
                            ),
                        ),
                    )
    return events


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

type RunMeta = dict[str, str | int]


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
    meta: RunMeta,
) -> None:
    async with tenant_session(db, tenant_id) as session:
        # Corrections and negotiation preparation lock the RFQ first. Replay
        # must observe a committed correction consistently across its fields.
        rfq_id = await session.scalar(select(Invite.rfq_id).where(Invite.id == loaded.invite_id))
        if rfq_id is not None:
            await session.scalar(select(Rfq.id).where(Rfq.id == rfq_id).with_for_update())
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
        await persist_quote(
            session,
            tenant_id=tenant_id,
            message=message,
            invite=invite,
            validated=validated,
            meta=meta,
        )


async def persist_quote(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    message: Message,
    invite: Invite,
    validated: ValidatedQuote,
    meta: RunMeta,
) -> Proposal | None:
    """Store `validated` as the invite's next proposal version; the message becomes `extracted`.

    The caller holds the row locks on the RFQ, the message and the invite (they
    serialize versions across replies to one invite) and has checked that no
    proposal references the message yet. A reply that is not a quote creates
    no proposal and returns None (a refusal marks the invite `declined`).

    The email extraction job and the Copilot's `register_quote` (a quote the
    buyer uploaded, stored as a message of origin `copilot_upload`) both end here,
    so an uploaded quote is reconciled, versioned and shown exactly like a reply.
    """
    previous_events = await offer_events(session, invite.id)
    if previous_events and not validated.is_quote and validated.output.declined:
        # A refusal must not leave a previously usable offer live. Without
        # explicit withdrawal evidence keep its facts, but hold all decisions.
        validated = replace(
            validated, output=validated.output.model_copy(update={"reply_kind": "withdrawal"})
        )
        invite.status = "declined"
    updates_state = validated.output.reply_kind in {"amendment", "withdrawal"} or (
        validated.output.reply_kind == "clarification" and bool(previous_events)
    )
    if not validated.is_quote and not updates_state:
        message.status = "extracted"
        if validated.output.declined:
            invite.status = "declined"
        logger.info(
            "quote.extract: not a quote message=%s declined=%s run=%s",
            message.id,
            validated.output.declined,
            meta["run_id"],
        )
        return None

    # The invite row lock serializes versions across replies to one invite.
    latest = await session.scalar(
        select(func.max(Proposal.version)).where(Proposal.invite_id == invite.id)
    )
    event_quote = stamp_quote(validated, message.id)
    state = reconcile(
        [
            *previous_events,
            OfferEvent(
                message_id=message.id,
                message_header=message.message_id_header,
                in_reply_to=message.in_reply_to,
                occurred_at=message.occurred_at,
                received_at=message.created_at,
                quote=event_quote,
                revision=revision_marker(message.subject, message.body_text),
            ),
        ]
    )
    effective = state.quote
    fields = effective.provenance()
    fields["_quote_event"] = store_quote(event_quote)
    fields["_offer_snapshot"] = store_quote(effective)
    fields["_offer_state"] = state.provenance()
    fields["_extraction"] = {key: value for key, value in meta.items()}
    # Offers for things not requested: kept as written, never compared or priced.
    fields["extra_offers"] = [
        offer.model_dump(mode="json") for offer in effective.output.extra_offers[:20]
    ]
    proposal = Proposal(
        tenant_id=tenant_id,
        invite_id=invite.id,
        message_id=message.id,
        version=(latest or 0) + 1,
        currency=_str(effective.parsed("currency")),
        valid_until=_date(effective.parsed("valid_until")),
        incoterm=_str(effective.parsed("incoterm")),
        payment_terms=_str(effective.parsed("payment_terms")),
        fields=fields,
    )
    session.add(proposal)
    await session.flush()
    session.add_all(_proposal_line(tenant_id, proposal.id, line) for line in effective.lines)
    message.status = "extracted"
    # A counter-offer awaiting approval answered an older offer: cancel it.
    if state.active_message_id == message.id or state.issues:
        await cancel_on_new_offer(session, invite.id)
    await session.flush()
    logger.info(
        "quote.extract: proposal stored message=%s proposal=%s version=%s lines=%s "
        "needs_review=%s run=%s",
        message.id,
        proposal.id,
        proposal.version,
        len(effective.lines),
        effective.needs_review,
        meta["run_id"],
    )
    return proposal


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
