"""InboundService end to end against Postgres, with LocalFileStorage and a fake job queue."""

from email.message import EmailMessage
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.db import tenant_session
from bido_back.db.models import Attachment, Contact, Invite, Message
from bido_back.email.addresses import reply_address, rfq_marker
from bido_back.intake.service import InboundService
from bido_back.storage.local import LocalFileStorage
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import QUOTE_EXTRACT

from .conftest import EMAIL_DOMAIN, TenantWorld, WorldFactory

pytestmark = pytest.mark.db


class FakeJobs:
    def __init__(self) -> None:
        self.enqueued: list[Envelope] = []

    async def enqueue(self, envelope: Envelope) -> None:
        self.enqueued.append(envelope)


@pytest.fixture
def jobs() -> FakeJobs:
    return FakeJobs()


@pytest.fixture
def storage(tmp_path: Path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "blobs")


@pytest.fixture
def service(app_engine: AsyncEngine, storage: LocalFileStorage, jobs: FakeJobs) -> InboundService:
    return InboundService(app_engine, storage, jobs, EMAIL_DOMAIN)


def reply(
    w: TenantWorld,
    *,
    to: str | None = None,
    sender: str | None = None,
    subject: str = "Re: RFQ",
    in_reply_to: str | None = None,
    body: str = "Unit price 1.20 EUR, lead time 3 weeks.",
    **headers: str,
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"Jane Supplier <{sender or w.contact_email}>"
    msg["To"] = to if to is not None else reply_address(w.reply_token, EMAIL_DOMAIN)
    msg["Cc"] = "buyer@acme.test"
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{uuid4()}@supplier.test>"
    msg["Date"] = "Tue, 06 Oct 2026 10:00:00 +0200"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    msg.set_content(body)
    return msg


async def _message(engine: AsyncEngine, tenant_id: UUID, message_id: UUID) -> Message:
    async with tenant_session(engine, tenant_id) as session:
        return (await session.execute(select(Message).where(Message.id == message_id))).scalar_one()


async def _invite(engine: AsyncEngine, w: TenantWorld) -> Invite:
    async with tenant_session(engine, w.tenant_id) as session:
        return (await session.execute(select(Invite).where(Invite.id == w.invite_id))).scalar_one()


async def _inbound_count(engine: AsyncEngine, w: TenantWorld) -> int:
    async with tenant_session(engine, w.tenant_id) as session:
        rows = await session.execute(
            select(Message.id).where(Message.invite_id == w.invite_id, Message.direction == "in")
        )
        return len(rows.all())


async def test_token_match_stores_and_enqueues_extraction(
    app_engine: AsyncEngine,
    make_world: WorldFactory,
    service: InboundService,
    storage: LocalFileStorage,
    jobs: FakeJobs,
) -> None:
    w = await make_world()
    raw = reply(w, in_reply_to=w.thread_message_id).as_bytes()

    result = await service.ingest(raw)

    assert result.outcome == "stored"
    assert result.matched_by == "reply_token"
    assert (result.tenant_id, result.invite_id) == (w.tenant_id, w.invite_id)
    assert result.message_id is not None and result.extraction_enqueued
    assert await storage.get(result.raw_key) == raw

    msg = await _message(app_engine, w.tenant_id, result.message_id)
    assert msg.direction == "in" and msg.status == "stored"
    assert msg.invite_id == w.invite_id
    assert msg.in_reply_to == w.thread_message_id
    assert msg.subject == "Re: RFQ"
    assert msg.body_text is not None and "1.20 EUR" in msg.body_text
    assert msg.raw_key == result.raw_key
    assert msg.occurred_at.isoformat() == "2026-10-06T08:00:00+00:00"
    assert msg.from_contact_id == w.contact_id  # the existing contact, reused

    invite = await _invite(app_engine, w)
    assert invite.status == "responded" and invite.responded_at is not None

    assert jobs.enqueued == [
        Envelope(
            type=QUOTE_EXTRACT,
            tenant_id=w.tenant_id,
            idempotency_key=f"extract:{result.message_id}",
            payload={"message_id": str(result.message_id)},
        )
    ]


async def test_in_reply_to_match_without_token(
    make_world: WorldFactory, service: InboundService, jobs: FakeJobs
) -> None:
    w = await make_world()
    result = await service.ingest(
        reply(w, to="buyer@acme.test", in_reply_to=w.followup_message_id).as_bytes()
    )
    assert result.outcome == "stored"
    assert result.matched_by == "thread"
    assert result.invite_id == w.invite_id
    assert len(jobs.enqueued) == 1


async def test_references_match_newest_first(
    make_world: WorldFactory, service: InboundService
) -> None:
    w = await make_world()
    msg = reply(w, to="buyer@acme.test")
    msg["References"] = f"<{uuid4()}@x.test> {w.thread_message_id} <{uuid4()}@y.test>"
    result = await service.ingest(msg.as_bytes())
    assert (result.outcome, result.matched_by) == ("stored", "thread")


async def test_rfq_marker_match_needs_known_sender(
    make_world: WorldFactory, service: InboundService
) -> None:
    w = await make_world()
    subject = f"AW: {rfq_marker(w.rfq_ref)} M8 bolts"

    known = await service.ingest(reply(w, to="buyer@acme.test", subject=subject).as_bytes())
    assert (known.outcome, known.matched_by, known.invite_id) == ("stored", "rfq_ref", w.invite_id)

    stranger = await service.ingest(
        reply(w, to="buyer@acme.test", subject=subject, sender="x@supplier.test").as_bytes()
    )
    assert stranger.outcome == "unmatched"


async def test_unmatched_keeps_raw_and_writes_nothing(
    app_engine: AsyncEngine,
    make_world: WorldFactory,
    service: InboundService,
    storage: LocalFileStorage,
    jobs: FakeJobs,
) -> None:
    w = await make_world()
    raw = reply(w, to=f"rfq-zzzzzzzzzzzz@{EMAIL_DOMAIN}").as_bytes()
    result = await service.ingest(raw)
    assert result.outcome == "unmatched"
    assert result.tenant_id is None and result.message_id is None
    assert await storage.get(result.raw_key) == raw
    assert await _inbound_count(app_engine, w) == 0
    assert jobs.enqueued == []


async def test_duplicate_message_id_is_ignored(
    app_engine: AsyncEngine, make_world: WorldFactory, service: InboundService, jobs: FakeJobs
) -> None:
    w = await make_world()
    raw = reply(w).as_bytes()
    first = await service.ingest(raw)
    second = await service.ingest(raw)

    assert first.outcome == "stored"
    assert second.outcome == "duplicate"
    assert second.message_id == first.message_id
    assert await _inbound_count(app_engine, w) == 1
    # Still 'stored' (not yet extracted): enqueued again under the same key,
    # which heals a crash between commit and enqueue.
    assert [e.idempotency_key for e in jobs.enqueued] == [f"extract:{first.message_id}"] * 2

    async with tenant_session(app_engine, w.tenant_id) as session:
        await session.execute(
            update(Message).where(Message.id == first.message_id).values(status="extracted")
        )
    third = await service.ingest(raw)
    assert third.outcome == "duplicate" and not third.extraction_enqueued
    assert len(jobs.enqueued) == 2


async def test_auto_reply_is_quarantined_and_not_extracted(
    app_engine: AsyncEngine, make_world: WorldFactory, service: InboundService, jobs: FakeJobs
) -> None:
    w = await make_world()
    result = await service.ingest(
        reply(w, subject="Out of Office: back Monday", Auto_Submitted="auto-replied").as_bytes()
    )
    assert (result.outcome, result.reason) == ("quarantined", "auto_reply")
    assert result.message_id is not None and not result.extraction_enqueued
    assert (await _message(app_engine, w.tenant_id, result.message_id)).status == "quarantined"
    invite = await _invite(app_engine, w)
    assert invite.status == "sent" and invite.responded_at is None
    assert jobs.enqueued == []


async def test_bounce_is_quarantined_without_a_contact(
    app_engine: AsyncEngine, make_world: WorldFactory, service: InboundService, jobs: FakeJobs
) -> None:
    w = await make_world()
    result = await service.ingest(
        reply(w, sender="MAILER-DAEMON@mx.supplier.test", subject="Undeliverable").as_bytes()
    )
    assert (result.outcome, result.reason) == ("quarantined", "bounce")
    assert result.message_id is not None
    assert (await _message(app_engine, w.tenant_id, result.message_id)).from_contact_id is None
    assert jobs.enqueued == []


async def test_wrong_sender_domain_is_quarantined(
    app_engine: AsyncEngine, make_world: WorldFactory, service: InboundService, jobs: FakeJobs
) -> None:
    w = await make_world()
    result = await service.ingest(reply(w, sender="attacker@evil.test").as_bytes())
    assert (result.outcome, result.reason) == ("quarantined", "sender_domain_mismatch")
    assert jobs.enqueued == []
    assert (await _invite(app_engine, w)).status == "sent"

    # A subdomain of the supplier's domain is the supplier.
    ok = await service.ingest(reply(w, sender="jane@eu.supplier.test").as_bytes())
    assert ok.outcome == "stored"


async def test_new_sender_gets_a_supplier_contact(
    app_engine: AsyncEngine, make_world: WorldFactory, service: InboundService
) -> None:
    w = await make_world()
    result = await service.ingest(reply(w, sender="New.Person@Supplier.test").as_bytes())
    assert result.outcome == "stored" and result.message_id is not None
    msg = await _message(app_engine, w.tenant_id, result.message_id)
    async with tenant_session(app_engine, w.tenant_id) as session:
        contact = (
            await session.execute(select(Contact).where(Contact.id == msg.from_contact_id))
        ).scalar_one()
    assert (contact.kind, contact.email, contact.name) == (
        "supplier",
        "new.person@supplier.test",
        "Jane Supplier",
    )


async def test_attachments_are_stored(
    app_engine: AsyncEngine,
    make_world: WorldFactory,
    service: InboundService,
    storage: LocalFileStorage,
) -> None:
    w = await make_world()
    msg = reply(w)
    msg.add_attachment(b"%PDF quote", maintype="application", subtype="pdf", filename="quote.pdf")
    msg.add_attachment(b"PK sheet", maintype="application", subtype="vnd.ms-excel",
                       filename="../../prices.xls")  # fmt: skip
    msg.add_attachment(b"%PDF again", maintype="application", subtype="pdf", filename="quote.pdf")

    result = await service.ingest(msg.as_bytes())
    assert result.outcome == "stored" and result.message_id is not None

    async with tenant_session(app_engine, w.tenant_id) as session:
        rows = (
            (
                await session.execute(
                    select(Attachment)
                    .where(Attachment.message_id == result.message_id)
                    .order_by(Attachment.storage_key)
                )
            )
            .scalars()
            .all()
        )
    prefix = f"attachments/{w.tenant_id}/{result.message_id}/"
    assert [(r.filename, r.storage_key, r.size_bytes) for r in rows] == [
        ("quote.pdf", f"{prefix}1-quote.pdf", 10),
        ("../../prices.xls", f"{prefix}2-prices.xls", 8),
        ("quote.pdf", f"{prefix}3-quote.pdf", 10),
    ]
    assert await storage.get(f"{prefix}1-quote.pdf") == b"%PDF quote"
    assert await storage.get(f"{prefix}3-quote.pdf") == b"%PDF again"
    assert all(p.is_relative_to(storage.root) for p in storage.root.rglob("*"))


async def test_ingest_for_invite_needs_no_lookup(
    app_engine: AsyncEngine, make_world: WorldFactory, service: InboundService, jobs: FakeJobs
) -> None:
    w = await make_world()
    # A reply as synced from the buyer's mailbox: no reply token, no Bido headers.
    raw = reply(w, to="buyer@acme.test", subject="Re: bolts").as_bytes()

    result = await service.ingest_for_invite(raw, tenant_id=w.tenant_id, invite_id=w.invite_id)

    assert (result.outcome, result.matched_by) == ("stored", "known_invite")
    assert result.invite_id == w.invite_id
    assert [e.idempotency_key for e in jobs.enqueued] == [f"extract:{result.message_id}"]
    again = await service.ingest_for_invite(raw, tenant_id=w.tenant_id, invite_id=w.invite_id)
    assert again.outcome == "duplicate"
    assert await _inbound_count(app_engine, w) == 1


async def test_ingest_for_invite_rejects_another_tenants_invite(
    app_engine: AsyncEngine, make_world: WorldFactory, service: InboundService, jobs: FakeJobs
) -> None:
    a, b = await make_world(), await make_world()
    result = await service.ingest_for_invite(
        reply(a).as_bytes(), tenant_id=b.tenant_id, invite_id=a.invite_id
    )
    assert result.outcome == "unmatched"
    assert await _inbound_count(app_engine, a) == 0
    assert jobs.enqueued == []


async def test_ingest_for_invite_applies_quarantine_rules(
    make_world: WorldFactory, service: InboundService, jobs: FakeJobs
) -> None:
    w = await make_world()
    result = await service.ingest_for_invite(
        reply(w, sender="someone@elsewhere.test").as_bytes(),
        tenant_id=w.tenant_id,
        invite_id=w.invite_id,
    )
    assert (result.outcome, result.reason) == ("quarantined", "sender_domain_mismatch")
    assert jobs.enqueued == []
