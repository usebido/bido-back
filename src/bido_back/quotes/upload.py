"""A supplier's quote the buyer attached in the Copilot, registered on its RFQ invite.

A quote normally reaches Bido by email: the intake stores the reply as a
`messages` row with its attachments, and the QUOTE_EXTRACT job turns it into a
proposal (`bido_back.quotes.extract_job`). A buyer who got the quote outside
Bido (a PDF forwarded to them, a document handed over) attaches it in the chat
and asks the Copilot to register it. This module runs that same path on the
file, in the two steps of a gated Copilot tool:

1. `extract_uploaded_quote`: read the uploads the buyer may use, run the same
   `quote_extraction` agent on their text (there is no email subject or body),
   validate the output against the RFQ's lines and the document
   (`bido_back.quotes.validation`), and return the validated quote with what
   the preview needs. Nothing is stored.
2. `register_uploaded_quote`, once the buyer confirmed: in one tenant
   transaction, store a `messages` row for the invite (direction 'in', origin
   'copilot_upload'), copy each file as its attachment under the intake's key
   scheme, persist the proposal through `persist_quote` (the extraction job's
   own store: reconciled, versioned, with provenance), mark the invite
   responded as the intake would, and write the `quote.registered` audit event
   with the buyer as actor.

The same files registered twice for one invite are refused: the message gets
a Message-ID derived from the invite and the upload ids, which the unique
`messages_tenant_id_message_id_header_key` catches, as the intake's
hash-derived Message-ID catches a redelivered email. Other files for the same
supplier become the invite's next proposal version, as a second email reply
would; the comparison shows the latest version.

Logs carry ids only, never the document's content.
"""

import hashlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.catalog.contracts import (
    PriorSupplierOffer,
    QuoteExtractionInput,
    QuoteExtractionOutput,
)
from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import (
    Attachment,
    Contact,
    Invite,
    Message,
    Proposal,
    Rfq,
    RfqLine,
    Supplier,
    TemplateUpload,
)
from bido_back.documents import DocumentText, extract_text, render_documents
from bido_back.files.policy import can_read_upload
from bido_back.intake.service import safe_filename
from bido_back.quotes.extract_job import (
    RunMeta,
    email_line,
    message_date,
    offer_events,
    persist_quote,
    requested_line,
)
from bido_back.quotes.parsing import decimal_separator_for
from bido_back.quotes.state import reconcile
from bido_back.quotes.validation import QuoteSources, ValidatedQuote, validate_quote
from bido_back.rfqs import audit
from bido_back.storage import BlobStorage

logger = logging.getLogger(__name__)

type Db = AsyncEngine | async_sessionmaker[AsyncSession]

# The extraction seam: input contract in, output contract out. The default runs
# the catalog's `quote_extraction` definition, the one the email job runs.
QuoteExtractor = Callable[[QuoteExtractionInput, ToolContext], Awaitable[QuoteExtractionOutput]]

ORIGIN: Final = "copilot_upload"
MAX_FILES: Final = 5
AGENT_NAME: Final = "quote_extraction"


class QuoteUploadError(Exception):
    """The quote cannot be extracted or registered; the message says why (no content)."""


class NotAQuote(QuoteUploadError):
    """The document does not read as a priced offer for this RFQ."""


class AlreadyRegistered(QuoteUploadError):
    """These very files are already registered on this invite."""

    def __init__(self, version: int) -> None:
        super().__init__(
            f"these files are already registered as this supplier's quote (version {version})"
        )
        self.version = version


class ExtractionFailed(QuoteUploadError):
    """The extraction agent did not complete."""


def default_quote_extractor(config: RunnerConfig | None = None) -> QuoteExtractor:
    """`QUOTE_EXTRACTION` through the runner, imported on first use (no Agno at import)."""

    async def run_extractor(
        task: QuoteExtractionInput, context: ToolContext
    ) -> QuoteExtractionOutput:
        from bido_back.agents.catalog.definitions import QUOTE_EXTRACTION
        from bido_back.agents.runner import Completed, Failed, run_to_completion

        result = await run_to_completion(QUOTE_EXTRACTION, task, context, config=config)
        if isinstance(result, Completed):
            return result.output
        code = result.failure.code if isinstance(result, Failed) else "unexpected_approval"
        logger.error("agent quote_extraction run %s failed: %s", context.run_id, code)
        raise ExtractionFailed(f"reading the quote failed ({code}); try again")

    return run_extractor


def extraction_meta() -> RunMeta:
    """What the proposal records about its extraction (`fields["_extraction"]`)."""
    from bido_back.agents.catalog.definitions import QUOTE_EXTRACTION

    return {
        "agent": QUOTE_EXTRACTION.name,
        "prompt_version": QUOTE_EXTRACTION.prompt_version,
        "model": QUOTE_EXTRACTION.model,
        "origin": ORIGIN,
    }


# --- What the buyer is shown, and what the confirmation needs -----------------------------


@dataclass(frozen=True, slots=True)
class UploadedFile:
    upload_id: UUID
    filename: str
    content_type: str
    storage_key: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class QuoteTarget:
    """The RFQ and the invite a quote is registered on, with what the preview names."""

    rfq_id: UUID
    rfq_ref: str
    rfq_title: str
    rfq_status: str
    rfq_currency: str | None
    invite_id: UUID
    supplier_id: UUID
    supplier_name: str
    supplier_language: str | None
    # Its holiday calendar counts business-day lead times and validity periods.
    supplier_country: str | None
    contact_email: str | None
    # Position -> description, for the preview's lines.
    line_descriptions: dict[int, str]


@dataclass(frozen=True, slots=True)
class ExtractedUpload:
    target: QuoteTarget
    files: tuple[UploadedFile, ...]
    validated: ValidatedQuote
    # The extraction agent's run (for the proposal's provenance).
    run_id: UUID


@dataclass(frozen=True, slots=True)
class Registered:
    rfq_id: UUID
    rfq_ref: str
    invite_id: UUID
    supplier_name: str
    message_id: UUID
    proposal_id: UUID
    version: int
    needs_review: bool


# --- Step 1: read and validate, store nothing ---------------------------------------------


async def quote_target(session: AsyncSession, rfq: Rfq, invite_id: UUID) -> QuoteTarget | None:
    """The invite of `rfq` to register on, with its supplier, or None if it is not its."""
    row = (
        await session.execute(
            select(Invite, Supplier, Contact.email)
            .join(Supplier, Supplier.id == Invite.supplier_id)
            .join(Contact, Contact.id == Invite.contact_id, isouter=True)
            .where(Invite.id == invite_id, Invite.rfq_id == rfq.id)
        )
    ).one_or_none()
    if row is None:
        return None
    invite, supplier, email = row
    descriptions = {
        position: description
        for position, description in await session.execute(
            select(RfqLine.position, RfqLine.description).where(RfqLine.rfq_id == rfq.id)
        )
    }
    return QuoteTarget(
        rfq_id=rfq.id,
        rfq_ref=rfq.ref,
        rfq_title=rfq.title,
        rfq_status=rfq.status,
        rfq_currency=rfq.base_currency,
        invite_id=invite.id,
        supplier_id=supplier.id,
        supplier_name=supplier.name,
        supplier_language=supplier.language,
        supplier_country=supplier.country,
        contact_email=email,
        line_descriptions=descriptions,
    )


async def extract_uploaded_quote(
    db: Db,
    storage: BlobStorage,
    principal: Principal,
    *,
    rfq_id: UUID,
    invite_id: UUID,
    upload_ids: Sequence[UUID],
    extractor: QuoteExtractor,
    context: ToolContext,
    today: date,
) -> ExtractedUpload:
    """Run the email path's extraction and validation on the buyer's uploads; store nothing.

    Raises `QuoteUploadError` when the RFQ, the invite or a file is not usable,
    `NotAQuote` when the document holds no priced offer (the Copilot should
    then summarize it to the buyer instead), `ExtractionFailed` when the agent
    did not complete.
    """
    async with tenant_session(db, principal.tenant_id) as session:
        rfq = await session.scalar(select(Rfq).where(Rfq.id == rfq_id))
        if rfq is None:
            raise QuoteUploadError("the RFQ was not found")
        target = await quote_target(session, rfq, invite_id)
        if target is None:
            raise QuoteUploadError("that supplier is not invited to this RFQ")
        _check_status(target)
        lines = (
            await session.scalars(
                select(RfqLine).where(RfqLine.rfq_id == rfq.id).order_by(RfqLine.position)
            )
        ).all()
        files = await owned_uploads(session, principal, upload_ids)
        prior = await offer_events(session, target.invite_id)
        prior_state = reconcile(prior) if prior else None
    # The file carries no message date: durations in it count from the day it is
    # registered, as an emailed quote's count from its Date header.
    original_offer_date = min(
        (message_date(event.occurred_at, event.received_at) for event in prior), default=None
    )

    documents = await _documents(storage, files)
    task = QuoteExtractionInput(
        rfq_ref=target.rfq_ref,
        requested_lines=[email_line(line) for line in lines],
        email_subject="",
        email_body="",
        attachments_text=render_documents(documents),
        prior_supplier_offer=(
            PriorSupplierOffer(
                message_id=str(prior_state.active_message_id),
                currency=_str(prior_state.quote.parsed("currency")),
                lines=[line.draft for line in prior_state.quote.lines],
            )
            if prior_state is not None
            else None
        ),
        message_date=today,
    )
    run_id = uuid4()
    output = await extractor(
        task, ToolContext(tenant_id=principal.tenant_id, user_id=None, run_id=run_id)
    )
    validated = validate_quote(
        output,
        requested=[requested_line(line) for line in lines],
        sources=QuoteSources(
            email_subject="",
            email_body="",
            documents={doc.filename: doc for doc in documents},
        ),
        today=today,
        decimal=decimal_separator_for(target.supplier_language),
        offer_date=today,
        original_offer_date=original_offer_date,
        country=target.supplier_country,
        rfq_currency=target.rfq_currency,
    )
    if not validated.is_quote:
        raise NotAQuote(
            "the file does not read as a priced supplier quote for this RFQ"
            + (" (the supplier declines)" if validated.output.declined else "")
            + "; summarize what it says to the buyer instead"
        )
    logger.info(
        "quote.upload: extracted tenant=%s rfq=%s invite=%s files=%s lines=%s "
        "needs_review=%s run=%s",
        principal.tenant_id,
        target.rfq_id,
        target.invite_id,
        len(files),
        len(validated.lines),
        validated.needs_review,
        run_id,
    )
    return ExtractedUpload(target=target, files=tuple(files), validated=validated, run_id=run_id)


def _check_status(target: QuoteTarget) -> None:
    if target.rfq_status == "cancelled":
        raise QuoteUploadError(f"{target.rfq_ref} is cancelled; a quote cannot be registered on it")


async def owned_uploads(
    session: AsyncSession, principal: Principal, upload_ids: Sequence[UUID]
) -> list[UploadedFile]:
    """The uploads, in the order given, each one the caller may read; else `QuoteUploadError`."""
    wanted = list(dict.fromkeys(upload_ids))
    if not wanted:
        raise QuoteUploadError("attach the quote file to register")
    if len(wanted) > MAX_FILES:
        raise QuoteUploadError(f"at most {MAX_FILES} files make up one quote")
    rows = {
        row.id: row
        for row in await session.scalars(
            select(TemplateUpload).where(TemplateUpload.id.in_(wanted))
        )
    }
    files: list[UploadedFile] = []
    for upload_id in wanted:
        row = rows.get(upload_id)
        if row is None or not can_read_upload(row.user_id, principal):
            raise QuoteUploadError("that id is not one of the files attached in this conversation")
        files.append(
            UploadedFile(
                upload_id=row.id,
                filename=row.filename,
                content_type=row.content_type,
                storage_key=row.storage_key,
                size_bytes=row.size_bytes,
            )
        )
    return files


async def _documents(storage: BlobStorage, files: Sequence[UploadedFile]) -> list[DocumentText]:
    documents: list[DocumentText] = []
    seen: set[str] = set()
    for file in files:
        data = await storage.get(file.storage_key)
        document = extract_text(file.filename, file.content_type, data)
        name = _unique(document.filename, seen)
        if name != document.filename:
            # Two files with one name: rename before the text is built, so the
            # markers (and the model's citations) tell them apart.
            document = extract_text(name, file.content_type, data)
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


# --- Step 2: register, after the buyer confirmed ------------------------------------------


def upload_message_header(invite_id: UUID, upload_ids: Sequence[UUID]) -> str:
    """A Message-ID for the stored upload: the same files on the same invite collide."""
    digest = hashlib.sha256(
        ":".join([str(invite_id), *sorted(str(u) for u in upload_ids)]).encode()
    ).hexdigest()[:32]
    return f"<upload-{digest}@copilot.bido>"


async def register_uploaded_quote(
    db: Db,
    storage: BlobStorage,
    principal: Principal,
    *,
    rfq_id: UUID,
    invite_id: UUID,
    upload_ids: Sequence[UUID],
    validated: ValidatedQuote,
    run_id: UUID,
    now: datetime | None = None,
) -> Registered:
    """Store the quote the buyer saw in the preview, exactly as an email reply is stored.

    Re-reads the uploads (the buyer must still be able to read them), so a file
    removed since the preview is refused rather than registered blind.
    """
    when = now or datetime.now(UTC)
    async with tenant_session(db, principal.tenant_id) as session:
        # Same lock order as the extraction job: RFQ, then the invite.
        rfq = await session.scalar(select(Rfq).where(Rfq.id == rfq_id).with_for_update())
        if rfq is None:
            raise QuoteUploadError("the RFQ was not found")
        invite = await session.scalar(
            select(Invite).where(Invite.id == invite_id, Invite.rfq_id == rfq.id).with_for_update()
        )
        if invite is None:
            raise QuoteUploadError("that supplier is not invited to this RFQ")
        target = await quote_target(session, rfq, invite.id)
        assert target is not None
        _check_status(target)
        files = await owned_uploads(session, principal, upload_ids)

        header = upload_message_header(invite.id, [f.upload_id for f in files])
        existing = await session.scalar(
            select(Message.id).where(
                Message.invite_id == invite.id, Message.message_id_header == header
            )
        )
        if existing is not None:
            version = await session.scalar(
                select(func.max(Proposal.version)).where(Proposal.message_id == existing)
            )
            raise AlreadyRegistered(version or 0)

        message = Message(
            tenant_id=principal.tenant_id,
            invite_id=invite.id,
            direction="in",
            origin=ORIGIN,
            message_id_header=header,
            # Named like an email subject so revision markers ("Rev. 2") in a
            # file name count as the supplier's own; no content from the file.
            subject="Quote uploaded by the buyer: " + ", ".join(f.filename for f in files),
            status="stored",
            occurred_at=when,
        )
        session.add(message)
        await session.flush()
        for index, file in enumerate(files, start=1):
            key = (
                f"attachments/{principal.tenant_id}/{message.id}/"
                f"{index}-{safe_filename(file.filename)}"
            )
            await storage.put(key, await storage.get(file.storage_key), file.content_type)
            session.add(
                Attachment(
                    tenant_id=principal.tenant_id,
                    message_id=message.id,
                    filename=file.filename,
                    content_type=file.content_type,
                    size_bytes=file.size_bytes,
                    storage_key=key,
                )
            )

        meta: RunMeta = {**extraction_meta(), "run_id": str(run_id)}
        proposal = await persist_quote(
            session,
            tenant_id=principal.tenant_id,
            message=message,
            invite=invite,
            validated=validated,
            meta=meta,
        )
        if proposal is None:
            raise NotAQuote("the file does not read as a priced supplier quote for this RFQ")
        if invite.status != "declined":
            invite.status = "responded"
            invite.responded_at = invite.responded_at or when
        review = proposal.fields.get("_review")
        needs_review = isinstance(review, dict) and bool(
            cast(dict[str, object], review).get("needs_review")
        )
        session.add(
            audit.rfq_event(
                principal.tenant_id,
                principal.user_id,
                audit.QUOTE_REGISTERED,
                rfq.id,
                invite_id=invite.id,
                supplier_id=invite.supplier_id,
                supplier_name=target.supplier_name,
                proposal_id=proposal.id,
                message_id=message.id,
                version=proposal.version,
                origin=ORIGIN,
                upload_ids=[f.upload_id for f in files],
                files=len(files),
                lines=len(validated.lines),
                needs_review=needs_review,
            )
        )
        await session.flush()
        logger.info(
            "quote.upload: registered tenant=%s rfq=%s invite=%s message=%s proposal=%s version=%s",
            principal.tenant_id,
            rfq.id,
            invite.id,
            message.id,
            proposal.id,
            proposal.version,
        )
        return Registered(
            rfq_id=rfq.id,
            rfq_ref=rfq.ref,
            invite_id=invite.id,
            supplier_name=target.supplier_name,
            message_id=message.id,
            proposal_id=proposal.id,
            version=proposal.version,
            needs_review=needs_review,
        )


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None
