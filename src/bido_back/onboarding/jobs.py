"""The onboarding worker jobs: TEMPLATE_SCAN and TEMPLATE_BUILD.

TEMPLATE_SCAN (`{"scan_id": ...}`): the user's one-off scan of their sent
mail. The job claims the scan, lists sent messages from the last 12 months
that match RFQ keywords (metadata only, at most `SCAN_LIMIT`), has the
classifier agent sort each page, and after every page writes the progress
(`checked`, `found`) and the kept candidates, so the frontend can poll it.

TEMPLATE_BUILD (`{"template_id": ...}`): learns the RFQ template from the
picked candidates (their full messages are fetched now, only those, only ids
listed in the scan) or from the uploaded files, through the builder agent,
and stores it as `ready`. Nothing from the messages or files is stored: only
the template (greeting, columns, footer, fields).

Both are idempotent by their row: a job runs only while the row is
`running` / `building` and nobody holds a fresh claim (`started_at`). A worker
that dies mid-job leaves a stale claim, which a redelivery takes over after
`CLAIM_TIMEOUT`. Every failure ends the row as `failed` with an error code
and returns normally (the user starts again from the screen); nothing is
retried in a loop.

Provider calls and agent runs happen outside database transactions. Logs
carry ids, counts and codes only.
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import ColumnElement, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.catalog.contracts import (
    RfqTemplateBuilderInput,
    RfqTemplateBuilderOutput,
)
from bido_back.agents.context import ToolContext
from bido_back.db import tenant_session
from bido_back.db.models import RfqTemplate, TemplateScan, TemplateUpload
from bido_back.documents import DocumentText, extract_text
from bido_back.email.inbound import parse_inbound
from bido_back.mailbox.base import (
    SCAN_KEYWORDS,
    ConsentRevoked,
    MailboxError,
    MailboxNotConnected,
    ProviderError,
    SentSummary,
)
from bido_back.onboarding.agents import AgentFailed, OnboardingAgents
from bido_back.onboarding.candidates import (
    MAX_CANDIDATES,
    StoredCandidate,
    classifier_input,
    keep,
    ordered,
    stored,
)
from bido_back.storage import BlobStorage
from bido_back.worker.envelope import Envelope

logger = logging.getLogger(__name__)

type Db = AsyncEngine | async_sessionmaker[AsyncSession]

# How far back and how many sent messages the scan looks at.
SCAN_WINDOW: Final = timedelta(days=365)
SCAN_LIMIT: Final = 1500
# Candidates per classifier run.
CLASSIFY_BATCH: Final = 50
# Messages / files a template is learned from, and the text the model reads.
MAX_EXAMPLES: Final = 10
MAX_EXAMPLE_CHARS: Final = 150_000
MAX_BODY_CHARS: Final = 20_000
CLAIM_TIMEOUT: Final = timedelta(minutes=10)


class ScanMailbox(Protocol):
    """What the jobs need from a mailbox (`MailboxSender` implements it)."""

    def list_sent(
        self, *, keywords: Sequence[str], since: datetime, limit: int
    ) -> AsyncIterator[list[SentSummary]]: ...

    async def get_message(self, provider_message_id: str) -> bytes: ...


# (tenant_id, mailbox_connection_id) -> the mailbox; raises MailboxNotConnected.
MailboxOpener = Callable[[UUID, UUID], Awaitable[ScanMailbox]]


@dataclass(frozen=True, slots=True)
class OnboardingJobDeps:
    db: Db
    storage: BlobStorage
    agents: OnboardingAgents
    open_mailbox: MailboxOpener


def _row_id(envelope: Envelope, key: str) -> UUID | None:
    raw = envelope.payload.get(key)
    if not isinstance(raw, str):
        return None
    try:
        return UUID(raw)
    except ValueError:
        return None


def _claimable(model: type[TemplateScan] | type[RfqTemplate], now: datetime) -> ColumnElement[bool]:
    return or_(model.started_at.is_(None), model.started_at < now - CLAIM_TIMEOUT)


# --- TEMPLATE_SCAN ---------------------------------------------------------------------


async def handle_template_scan(envelope: Envelope, deps: OnboardingJobDeps) -> None:
    tenant_id = envelope.tenant_id
    scan_id = _row_id(envelope, "scan_id")
    if scan_id is None:
        logger.error("template.scan: invalid payload key=%s", envelope.idempotency_key)
        return
    now = datetime.now(UTC)
    async with tenant_session(deps.db, tenant_id) as session:
        connection_id = await session.scalar(
            update(TemplateScan)
            .where(
                TemplateScan.id == scan_id,
                TemplateScan.status == "running",
                _claimable(TemplateScan, now),
            )
            .values(started_at=now, checked=0, found=0, candidates=[], updated_at=func.now())
            .returning(TemplateScan.mailbox_connection_id)
        )
    if connection_id is None:
        logger.info("template.scan: nothing to do scan=%s", scan_id)
        return

    try:
        checked, found = await _scan(deps, tenant_id, scan_id, connection_id, now)
    except Exception as exc:
        code = _failure_code(exc)
        if code == "internal_error":
            logger.exception("template.scan: failed tenant=%s scan=%s", tenant_id, scan_id)
        else:
            logger.warning(
                "template.scan: failed tenant=%s scan=%s code=%s", tenant_id, scan_id, code
            )
        await _finish_scan(deps.db, tenant_id, scan_id, status="failed", error=code)
        return
    await _finish_scan(deps.db, tenant_id, scan_id, status="done")
    logger.info(
        "template.scan: done tenant=%s scan=%s checked=%s found=%s",
        tenant_id,
        scan_id,
        checked,
        found,
    )


async def _scan(
    deps: OnboardingJobDeps, tenant_id: UUID, scan_id: UUID, connection_id: UUID, now: datetime
) -> tuple[int, int]:
    mailbox = await deps.open_mailbox(tenant_id, connection_id)
    context = ToolContext(tenant_id=tenant_id, user_id=None, run_id=uuid4())
    checked = 0
    kept: list[StoredCandidate] = []
    async for page in mailbox.list_sent(
        keywords=SCAN_KEYWORDS, since=now - SCAN_WINDOW, limit=SCAN_LIMIT
    ):
        for start in range(0, len(page), CLASSIFY_BATCH):
            batch = page[start : start + CLASSIFY_BATCH]
            ids = [f"c{checked + start + i + 1}" for i in range(len(batch))]
            output = await deps.agents.classify(classifier_input(ids, batch), context)
            verdicts = {v.id: v for v in output.verdicts}
            for candidate_id, summary in zip(ids, batch, strict=True):
                candidate = keep(candidate_id, summary, verdicts.get(candidate_id))
                if candidate is not None:
                    kept.append(candidate)
        checked += len(page)
        kept = ordered(kept)
        await _progress(deps.db, tenant_id, scan_id, checked, kept)
        if len(kept) >= MAX_CANDIDATES:
            break
    return checked, len(kept)


async def _progress(
    db: Db, tenant_id: UUID, scan_id: UUID, checked: int, candidates: list[StoredCandidate]
) -> None:
    async with tenant_session(db, tenant_id) as session:
        await session.execute(
            update(TemplateScan)
            .where(TemplateScan.id == scan_id, TemplateScan.status == "running")
            .values(
                checked=checked,
                found=len(candidates),
                candidates=candidates,
                updated_at=func.now(),
            )
        )


async def _finish_scan(
    db: Db, tenant_id: UUID, scan_id: UUID, *, status: str, error: str | None = None
) -> None:
    async with tenant_session(db, tenant_id) as session:
        await session.execute(
            update(TemplateScan)
            .where(TemplateScan.id == scan_id, TemplateScan.status == "running")
            .values(status=status, error=error, updated_at=func.now())
        )


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, ConsentRevoked):
        return "mailbox_revoked"
    if isinstance(exc, MailboxNotConnected):
        return "mailbox_not_connected"
    if isinstance(exc, ProviderError | MailboxError):
        return "provider_error"
    if isinstance(exc, AgentFailed):
        return "agent_failed"
    return "internal_error"


# --- TEMPLATE_BUILD ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _BuildWork:
    source: str
    refs: dict[str, object]


async def handle_template_build(envelope: Envelope, deps: OnboardingJobDeps) -> None:
    tenant_id = envelope.tenant_id
    template_id = _row_id(envelope, "template_id")
    if template_id is None:
        logger.error("template.build: invalid payload key=%s", envelope.idempotency_key)
        return
    now = datetime.now(UTC)
    async with tenant_session(deps.db, tenant_id) as session:
        row = (
            await session.execute(
                update(RfqTemplate)
                .where(
                    RfqTemplate.id == template_id,
                    RfqTemplate.status == "building",
                    _claimable(RfqTemplate, now),
                )
                .values(started_at=now, updated_at=func.now())
                .returning(RfqTemplate.source, RfqTemplate.source_refs)
            )
        ).one_or_none()
    if row is None:
        logger.info("template.build: nothing to do template=%s", template_id)
        return
    work = _BuildWork(source=row[0], refs=cast(dict[str, object], row[1] or {}))

    try:
        documents = await _documents(deps, tenant_id, work)
        examples = _render(documents)
        if not examples:
            raise _BuildRefused("nothing_readable")
        context = ToolContext(tenant_id=tenant_id, user_id=None, run_id=uuid4())
        output = await deps.agents.build(
            RfqTemplateBuilderInput(example_count=len(documents), examples=examples), context
        )
    except Exception as exc:
        code = exc.code if isinstance(exc, _BuildRefused) else _failure_code(exc)
        if code == "internal_error":
            logger.exception("template.build: failed tenant=%s template=%s", tenant_id, template_id)
        else:
            logger.warning(
                "template.build: failed tenant=%s template=%s code=%s", tenant_id, template_id, code
            )
        await _finish_template(deps.db, tenant_id, template_id, error=code)
        return
    await _finish_template(deps.db, tenant_id, template_id, output=output)
    logger.info(
        "template.build: ready tenant=%s template=%s examples=%s fields=%s",
        tenant_id,
        template_id,
        len(documents),
        len(output.fields),
    )


class _BuildRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _id_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [v for v in cast(list[object], raw) if isinstance(v, str)][:MAX_EXAMPLES]


async def _documents(
    deps: OnboardingJobDeps, tenant_id: UUID, work: _BuildWork
) -> list[DocumentText]:
    if work.source == "search":
        return await _from_messages(deps, tenant_id, work.refs)
    if work.source == "upload":
        return await _from_uploads(deps, tenant_id, work.refs)
    raise _BuildRefused("unsupported_source")


async def _from_messages(
    deps: OnboardingJobDeps, tenant_id: UUID, refs: dict[str, object]
) -> list[DocumentText]:
    scan_id = refs.get("scan_id")
    picked = set(_id_list(refs.get("candidate_ids")))
    if not isinstance(scan_id, str) or not picked:
        raise _BuildRefused("no_examples")
    async with tenant_session(deps.db, tenant_id) as session:
        scan = await session.scalar(select(TemplateScan).where(TemplateScan.id == UUID(scan_id)))
    if scan is None:
        raise _BuildRefused("scan_not_found")
    # Only messages the scan listed: the client names candidates, never provider ids.
    chosen = [c for c in stored(scan.candidates) if c["id"] in picked]
    if not chosen:
        raise _BuildRefused("no_examples")
    mailbox = await deps.open_mailbox(tenant_id, scan.mailbox_connection_id)
    documents: list[DocumentText] = []
    for number, candidate in enumerate(chosen, start=1):
        try:
            raw = await mailbox.get_message(candidate["provider_message_id"])
        except ProviderError as exc:
            if exc.status == 404:
                # Deleted since the scan: learn from the others.
                continue
            raise
        email = parse_inbound(raw)
        body = email.text_body.strip()[:MAX_BODY_CHARS]
        marker = f"[email {number}] Subject: {email.subject or ''}"
        documents.append(
            DocumentText(filename=f"email {number}", kind="text", text=f"{marker}\n{body}")
        )
        for attachment in email.attachments[:MAX_EXAMPLES]:
            documents.append(
                extract_text(
                    f"email {number} - {attachment.filename}",
                    attachment.content_type,
                    attachment.data,
                )
            )
    return documents


async def _from_uploads(
    deps: OnboardingJobDeps, tenant_id: UUID, refs: dict[str, object]
) -> list[DocumentText]:
    ids = [UUID(v) for v in _id_list(refs.get("upload_ids"))]
    if not ids:
        raise _BuildRefused("no_examples")
    async with tenant_session(deps.db, tenant_id) as session:
        uploads = list(
            await session.scalars(
                select(TemplateUpload)
                .where(TemplateUpload.id.in_(ids))
                .order_by(TemplateUpload.created_at)
            )
        )
    if not uploads:
        raise _BuildRefused("no_examples")
    return [
        extract_text(
            upload.filename, upload.content_type, await deps.storage.get(upload.storage_key)
        )
        for upload in uploads
    ]


def _render(documents: Sequence[DocumentText]) -> str:
    """The readable documents as one text, bounded. Empty when nothing was readable."""
    parts: list[str] = []
    left = MAX_EXAMPLE_CHARS
    for document in documents:
        if not document.readable or left <= 0:
            continue
        text = document.text[:left]
        parts.append(text)
        left -= len(text)
    return "\n\n".join(parts)


def _clean(values: Sequence[str], limit: int) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values:
        label = " ".join(value.split())[:80]
        if label and label.lower() not in seen:
            seen.add(label.lower())
            cleaned.append(label)
    return cleaned[:limit]


async def _finish_template(
    db: Db,
    tenant_id: UUID,
    template_id: UUID,
    *,
    output: RfqTemplateBuilderOutput | None = None,
    error: str | None = None,
) -> None:
    values: dict[str, object]
    if output is not None:
        footer = _clean(output.footer_fields, 8)
        values = {
            "status": "ready",
            "language": output.language.strip().lower()[:8] or None,
            "greeting": " ".join(output.greeting.split())[:600],
            "columns": _clean(output.columns, 8),
            "footer": " · ".join(footer) or None,
            "fields": _clean(output.fields, 30),
            "error": None,
        }
    else:
        values = {"status": "failed", "error": error}
    async with tenant_session(db, tenant_id) as session:
        await session.execute(
            update(RfqTemplate)
            .where(RfqTemplate.id == template_id, RfqTemplate.status == "building")
            .values(**values, updated_at=func.now())
        )


# --- Registry ---------------------------------------------------------------------------


class TemplateScanHandler:
    def __init__(self, deps: OnboardingJobDeps) -> None:
        self._deps = deps

    async def __call__(self, envelope: Envelope) -> None:
        await handle_template_scan(envelope, self._deps)


class TemplateBuildHandler:
    def __init__(self, deps: OnboardingJobDeps) -> None:
        self._deps = deps

    async def __call__(self, envelope: Envelope) -> None:
        await handle_template_build(envelope, self._deps)
