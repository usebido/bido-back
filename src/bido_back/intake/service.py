"""Inbound email intake: store a supplier's reply against its invite.

Two entry points share one storage path:

* `ingest_for_invite(raw, tenant_id=..., invite_id=...)`: the primary path.
  Bido reads the buyer's own mailbox (Gmail / Microsoft 365) and only in RFQ
  threads it started, so the mailbox sync already knows the tenant and the
  invite. No lookup is needed.
* `ingest(raw)`: plan B, for mail sent to the Bido-domain reply address
  (SES -> S3 -> SQS intake queue in AWS, `POST /dev/inbound` locally). The
  email has no tenant until it is matched, and RLS hides every invite without
  one, so matching goes through the SECURITY DEFINER functions of migration
  0004, which return only `(tenant_id, invite_id)`. In order:
    1. the reply token in `rfq-<token>@<email_domain>` among Delivered-To /
       X-Original-To / To / Cc (unguessable: the strong match);
    2. In-Reply-To, then References (newest first), against the Message-IDs
       we sent;
    3. an `[RFQ-<n>]` marker in the subject, then the body, but only if the
       sender is a known contact of that invite's supplier (a ref alone is
       guessable and unique only per tenant).
  A lookup that returns more than one invite counts as no match. No match:
  outcome "unmatched", and nothing is written to the database, because every
  table that could hold the message is tenant-owned and there is no tenant to
  write it under. The raw email stays in storage under its `inbound/` key for
  manual triage; only ids are logged.

Both paths then:

1. store the raw bytes, content-addressed, under `inbound/<sha256>.eml` (a
   redelivery rewrites the same object);
2. in one tenant-scoped transaction:
     * dedupe by Message-ID (`messages_tenant_id_message_id_header_key`); a
       missing Message-ID is replaced by one derived from the raw bytes' hash,
       so a redelivery of the same bytes still deduplicates;
     * find or create the sender's `contacts` row (kind supplier; not for
       bounces, whose sender is a mail system, not a person);
     * insert the `messages` row (direction 'in'). Status 'stored', or
       'quarantined' when it is an auto-reply or a bounce, or when the
       invite's supplier has `email_domain` set and the sender's domain is
       neither that domain nor a subdomain of it;
     * store each attachment under
       `attachments/<tenant_id>/<message row id>/<n>-<filename>` and insert
       its `attachments` row (`n` keeps two files with one name apart);
     * for a 'stored' reply, mark the invite responded (`responded_at` keeps
       the first response; a declined invite stays declined) and cancel any
       proposed follow-up; for an auto-reply, pause chases until the stated
       return date (`bido_back.followups.hooks`);
3. after the commit, for a 'stored' reply, enqueue QUOTE_EXTRACT with
   idempotency key `extract:<message row id>`.

Delivery is at-least-once. A crash after the commit but before the enqueue is
healed by the redelivery: the duplicate path enqueues the extraction again
while the message is still 'stored' (same idempotency key, so the handler
runs it once). Blobs written in a transaction that then rolls back are left
under a message id that never committed; nothing references them.
"""

import hashlib
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import TextClause, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import Attachment, Contact, Invite, Message, Supplier
from bido_back.email.addresses import address_domain, extract_reply_tokens, extract_rfq_refs
from bido_back.email.inbound import InboundEmail, parse_inbound
from bido_back.followups.hooks import cancel_on_reply, pause_on_auto_reply
from bido_back.storage import BlobStorage
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import QUOTE_EXTRACT
from bido_back.worker.jobs import JobQueue

logger = logging.getLogger(__name__)

IngestOutcome = Literal["stored", "quarantined", "duplicate", "unmatched"]
QuarantineReason = Literal["auto_reply", "bounce", "sender_domain_mismatch"]
# How the invite was found: given by the caller (mailbox sync), or a 0004 lookup.
MatchedBy = Literal["known_invite", "reply_token", "thread", "rfq_ref"]

_BY_TOKEN = text("SELECT tenant_id, invite_id FROM resolve_invite_by_token(:value)")
_BY_MESSAGE_ID = text("SELECT tenant_id, invite_id FROM resolve_invite_by_message_id(:value)")
_BY_RFQ_REF = text("SELECT tenant_id, invite_id FROM resolve_invite_by_rfq_ref(:value, :sender)")
_DEDUPE_CONSTRAINT = "messages_tenant_id_message_id_header_key"
_MAX_FILENAME = 150


@dataclass(frozen=True, slots=True)
class IngestResult:
    outcome: IngestOutcome
    raw_key: str
    tenant_id: UUID | None = None
    invite_id: UUID | None = None
    # Our `messages.id` (not the Message-ID header).
    message_id: UUID | None = None
    reason: QuarantineReason | None = None
    matched_by: MatchedBy | None = None
    extraction_enqueued: bool = False


@dataclass(frozen=True, slots=True)
class InviteMatch:
    tenant_id: UUID
    invite_id: UUID
    matched_by: MatchedBy


class InboundService:
    def __init__(
        self,
        db: AsyncEngine | async_sessionmaker[AsyncSession],
        storage: BlobStorage,
        jobs: JobQueue,
        email_domain: str,
    ) -> None:
        self._db = db
        self._sessionmaker = db if isinstance(db, async_sessionmaker) else create_sessionmaker(db)
        self._storage = storage
        self._jobs = jobs
        self._email_domain = email_domain.lower()

    async def ingest_for_invite(
        self, raw: bytes, *, tenant_id: UUID, invite_id: UUID
    ) -> IngestResult:
        """Store a reply whose tenant and invite are already known (mailbox sync).

        The invite is read under `tenant_id`'s RLS, so an invite of another
        tenant is simply not found (outcome "unmatched"), never written to.
        """
        raw_key = await self._store_raw(raw)
        match = InviteMatch(tenant_id=tenant_id, invite_id=invite_id, matched_by="known_invite")
        return await self._ingest_matched(parse_inbound(raw), match, raw, raw_key)

    async def ingest(self, raw: bytes) -> IngestResult:
        """Match a reply sent to the Bido reply address, then store it (plan B)."""
        raw_key = await self._store_raw(raw)
        email = parse_inbound(raw)
        match = await self.resolve(email)
        if match is None:
            logger.warning("inbound email unmatched raw_key=%s", raw_key)
            return IngestResult(outcome="unmatched", raw_key=raw_key)
        return await self._ingest_matched(email, match, raw, raw_key)

    async def resolve(self, email: InboundEmail) -> InviteMatch | None:
        """The invite this email answers, via the 0004 lookup functions."""
        async with self._sessionmaker() as session, session.begin():
            for token in extract_reply_tokens(email.recipients, self._email_domain):
                match = await _lookup(session, _BY_TOKEN, "reply_token", value=token)
                if match is not None:
                    return match

            thread_ids: list[str] = []
            for candidate in (email.in_reply_to, *reversed(email.references)):
                if candidate and candidate not in thread_ids:
                    thread_ids.append(candidate)
            for message_id in thread_ids:
                match = await _lookup(session, _BY_MESSAGE_ID, "thread", value=message_id)
                if match is not None:
                    return match

            if email.from_address and not email.is_bounce:
                for ref in extract_rfq_refs(email.subject, email.text_body):
                    match = await _lookup(
                        session, _BY_RFQ_REF, "rfq_ref", value=ref, sender=email.from_address
                    )
                    if match is not None:
                        return match
        return None

    async def _store_raw(self, raw: bytes) -> str:
        raw_key = f"inbound/{hashlib.sha256(raw).hexdigest()}.eml"
        await self._storage.put(raw_key, raw, "message/rfc822")
        return raw_key

    async def _ingest_matched(
        self, email: InboundEmail, match: InviteMatch, raw: bytes, raw_key: str
    ) -> IngestResult:
        result = await self._store(email, match, raw, raw_key)
        # _store decides whether extraction is due; the job goes out after the commit.
        if result.extraction_enqueued and result.message_id is not None:
            await self._jobs.enqueue(
                Envelope(
                    type=QUOTE_EXTRACT,
                    tenant_id=match.tenant_id,
                    idempotency_key=f"extract:{result.message_id}",
                    payload={"message_id": str(result.message_id)},
                )
            )
        logger.info(
            "inbound email %s matched_by=%s tenant=%s invite=%s message=%s reason=%s",
            result.outcome,
            result.matched_by,
            result.tenant_id,
            result.invite_id,
            result.message_id,
            result.reason,
        )
        return result

    async def _store(
        self, email: InboundEmail, match: InviteMatch, raw: bytes, raw_key: str
    ) -> IngestResult:
        header_id = email.message_id or (
            f"<sha256.{hashlib.sha256(raw).hexdigest()}@inbound.invalid>"
        )
        matched = IngestResult(
            outcome="duplicate",
            raw_key=raw_key,
            tenant_id=match.tenant_id,
            invite_id=match.invite_id,
            matched_by=match.matched_by,
        )

        async with tenant_session(self._db, match.tenant_id) as session:
            invite = (
                await session.execute(
                    select(Invite.id, Supplier.email_domain)
                    .join(Supplier, Supplier.id == Invite.supplier_id)
                    .where(Invite.id == match.invite_id, Invite.tenant_id == match.tenant_id)
                )
            ).one_or_none()
            if invite is None:
                # Not visible under this tenant: deleted since the match, or (for
                # ingest_for_invite) not this tenant's invite. Nothing to attach to.
                return IngestResult(outcome="unmatched", raw_key=raw_key)
            supplier_domain: str | None = invite.email_domain

            existing = (
                await session.execute(
                    select(Message.id, Message.status).where(
                        Message.tenant_id == match.tenant_id,
                        Message.message_id_header == header_id,
                    )
                )
            ).one_or_none()
            if existing is not None:
                # Still 'stored' means the extraction may never have been
                # enqueued (crash between commit and enqueue): enqueue again.
                return replace(
                    matched,
                    message_id=existing.id,
                    extraction_enqueued=existing.status == "stored",
                )

            reason = _quarantine_reason(email, supplier_domain)
            status = "quarantined" if reason else "stored"
            contact_id = (
                None if email.is_bounce else await _sender_contact(session, match.tenant_id, email)
            )
            occurred_at = email.date or datetime.now(UTC)

            message_row_id = uuid4()
            inserted = (
                await session.execute(
                    insert(Message)
                    .values(
                        id=message_row_id,
                        tenant_id=match.tenant_id,
                        invite_id=match.invite_id,
                        direction="in",
                        message_id_header=header_id,
                        in_reply_to=email.in_reply_to,
                        from_contact_id=contact_id,
                        subject=email.subject,
                        body_text=email.text_body,
                        raw_key=raw_key,
                        status=status,
                        occurred_at=occurred_at,
                    )
                    .on_conflict_do_nothing(constraint=_DEDUPE_CONSTRAINT)
                    .returning(Message.id)
                )
            ).scalar_one_or_none()
            if inserted is None:
                # A concurrent delivery of the same email won the insert; that
                # delivery enqueues the extraction.
                return matched

            for index, attachment in enumerate(email.attachments, start=1):
                key = (
                    f"attachments/{match.tenant_id}/{message_row_id}/"
                    f"{index}-{safe_filename(attachment.filename)}"
                )
                await self._storage.put(key, attachment.data, attachment.content_type)
                session.add(
                    Attachment(
                        tenant_id=match.tenant_id,
                        message_id=message_row_id,
                        filename=attachment.filename,
                        content_type=attachment.content_type,
                        size_bytes=len(attachment.data),
                        storage_key=key,
                    )
                )

            if status == "stored":
                await session.execute(
                    update(Invite)
                    .where(Invite.id == match.invite_id, Invite.status != "declined")
                    .values(
                        status="responded",
                        responded_at=func.coalesce(Invite.responded_at, occurred_at),
                    )
                )
                # An answer makes any proposed chase or clarification moot.
                await cancel_on_reply(session, match.invite_id)
            elif reason == "auto_reply":
                # Out of office: no chase before the stated return date.
                await pause_on_auto_reply(
                    session,
                    match.invite_id,
                    f"{email.subject or ''}\n{email.text_body}",
                    occurred_at,
                )
            await session.flush()

        return replace(
            matched,
            outcome="quarantined" if reason else "stored",
            message_id=message_row_id,
            reason=reason,
            extraction_enqueued=reason is None,
        )


async def _lookup(
    session: AsyncSession, query: TextClause, matched_by: MatchedBy, **params: str
) -> InviteMatch | None:
    rows = (await session.execute(query, params)).all()
    matches: set[tuple[UUID, UUID]] = {(r.tenant_id, r.invite_id) for r in rows}
    if len(matches) != 1:
        return None
    tenant_id, invite_id = matches.pop()
    return InviteMatch(tenant_id=tenant_id, invite_id=invite_id, matched_by=matched_by)


def _quarantine_reason(email: InboundEmail, supplier_domain: str | None) -> QuarantineReason | None:
    if email.is_bounce:
        return "bounce"
    if email.is_auto_reply:
        return "auto_reply"
    if supplier_domain and not sender_domain_matches(email.from_address, supplier_domain):
        return "sender_domain_mismatch"
    return None


def sender_domain_matches(from_address: str | None, supplier_domain: str) -> bool:
    """True when the sender's domain is the supplier's domain or a subdomain of it."""
    expected = supplier_domain.strip().lower().lstrip("@")
    actual = address_domain(from_address) if from_address else None
    if not actual or not expected:
        return False
    return actual == expected or actual.endswith(f".{expected}")


async def _sender_contact(
    session: AsyncSession, tenant_id: UUID, email: InboundEmail
) -> UUID | None:
    """The sender's supplier contact in this tenant, created on first contact."""
    if not email.from_address:
        return None
    address = email.from_address.lower()
    existing = (
        await session.execute(
            select(Contact.id)
            .where(
                Contact.tenant_id == tenant_id,
                Contact.kind == "supplier",
                func.lower(Contact.email) == address,
                Contact.pseudonymized_at.is_(None),
            )
            .order_by(Contact.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    contact = Contact(
        id=uuid4(), tenant_id=tenant_id, kind="supplier", name=email.from_name, email=address
    )
    session.add(contact)
    await session.flush()
    return contact.id


def safe_filename(filename: str) -> str:
    """A filename usable as one storage-key segment (no separators, no dot-only names)."""
    base = filename.replace("\\", "/").split("/")[-1]
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in base).strip("._")
    return (cleaned or "attachment")[:_MAX_FILENAME]
