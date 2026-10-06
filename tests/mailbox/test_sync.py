"""Thread sync without a database: one thread, the buyer's own mail, state, the job."""

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from bido_back.mailbox import service
from bido_back.mailbox.base import b64url_encode
from bido_back.mailbox.google import GmailMailbox
from bido_back.mailbox.service import ALL_TENANTS, MailboxSyncDeps, handle_mailbox_sync, sync_thread
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import MAILBOX_SYNC

from .support import BUYER, SUPPLIER, Recorder, authorized, json_response, raw_email


class FakeIngest:
    def __init__(self, fail_for: bytes | None = None) -> None:
        self.calls: list[tuple[bytes, UUID, UUID]] = []
        self.fail_for = fail_for

    async def __call__(self, raw: bytes, *, tenant_id: UUID, invite_id: UUID) -> object:
        if raw == self.fail_for:
            raise RuntimeError("intake unavailable")
        self.calls.append((raw, tenant_id, invite_id))
        return None


def _thread(raws: dict[str, bytes]) -> Recorder:
    """A Gmail thread whose metadata does not show From (forces the raw check)."""

    def route(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/gmail/v1/users/me/threads/thread-42":
            return json_response(
                200, {"messages": [{"id": i, "labelIds": ["INBOX"]} for i in raws]}
            )
        message_id = path.rsplit("/", 1)[1]
        return json_response(200, {"raw": b64url_encode(raws[message_id])})

    return Recorder(route)


async def test_sync_thread_skips_own_ingests_rest_and_advances_state() -> None:
    supplier_reply = raw_email(from_address=SUPPLIER, to=BUYER, message_id="<r1@s.example>")
    own = raw_email(from_address=BUYER.upper(), to=SUPPLIER, message_id="<own@acme.example>")
    recorder = _thread({"m-own": own, "m-reply": supplier_reply})
    http, _, _ = authorized("google", recorder)
    ingest = FakeIngest()
    tenant_id, invite_id = uuid4(), uuid4()

    result = await sync_thread(
        GmailMailbox(http, address=BUYER),
        "thread-42",
        {"seen": ["m-old"]},
        tenant_id=tenant_id,
        invite_id=invite_id,
        ingest=ingest,
    )

    assert ingest.calls == [(supplier_reply, tenant_id, invite_id)]
    assert result.ingested == 1
    assert result.skipped_own == 1
    assert result.state == {"seen": ["m-old", "m-own", "m-reply"]}
    assert all("/threads/thread-42" in p or "/messages/m-" in p for p in recorder.paths())


async def test_sync_thread_second_run_ingests_nothing() -> None:
    reply = raw_email(from_address=SUPPLIER, to=BUYER)
    http, _, _ = authorized("google", _thread({"m-reply": reply}))
    ingest = FakeIngest()
    mailbox = GmailMailbox(http, address=BUYER)
    tenant_id, invite_id = uuid4(), uuid4()

    first = await sync_thread(
        mailbox, "thread-42", None, tenant_id=tenant_id, invite_id=invite_id, ingest=ingest
    )
    second = await sync_thread(
        mailbox, "thread-42", first.state, tenant_id=tenant_id, invite_id=invite_id, ingest=ingest
    )

    assert len(ingest.calls) == 1
    assert second.ingested == 0
    assert second.state == first.state


async def test_failed_ingest_is_not_marked_seen_and_others_go_on() -> None:
    bad = raw_email(from_address=SUPPLIER, to=BUYER, message_id="<bad@s.example>")
    good = raw_email(from_address=SUPPLIER, to=BUYER, message_id="<good@s.example>")
    http, _, _ = authorized("google", _thread({"m-bad": bad, "m-good": good}))
    ingest = FakeIngest(fail_for=bad)

    result = await sync_thread(
        GmailMailbox(http, address=BUYER),
        "thread-42",
        None,
        tenant_id=uuid4(),
        invite_id=uuid4(),
        ingest=ingest,
    )

    assert result.failed == 1
    assert result.ingested == 1
    assert result.state == {"seen": ["m-good"]}


# --- The job --------------------------------------------------------------------------------


async def _no_ingest(raw: bytes, *, tenant_id: UUID, invite_id: UUID) -> object:
    return None


def _deps() -> MailboxSyncDeps:
    from .support import make_settings

    return MailboxSyncDeps(
        # Never connected: the tests replace everything that would query it.
        db=create_async_engine("postgresql+asyncpg://unused@localhost:1/unused"),
        ingest=_no_ingest,
        settings=make_settings(),
    )


def _envelope(tenant_id: UUID) -> Envelope:
    return Envelope(type=MAILBOX_SYNC, tenant_id=tenant_id, idempotency_key="mailbox.sync:t")


async def test_all_tenants_sentinel_syncs_every_active_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = uuid4(), uuid4()
    synced: list[UUID] = []

    async def tenants(db: object) -> list[UUID]:
        return [a, b]

    async def sync(db: object, tenant_id: UUID, **kwargs: object) -> None:
        synced.append(tenant_id)
        if tenant_id == a:
            raise RuntimeError("one tenant failing must not stop the others")

    monkeypatch.setattr(service, "active_mailbox_tenants", tenants)
    monkeypatch.setattr(service, "sync_rfq_threads", sync)
    await handle_mailbox_sync(_envelope(ALL_TENANTS), _deps())

    assert synced == [a, b]
    assert ALL_TENANTS == UUID("00000000-0000-0000-0000-000000000000")


async def test_real_tenant_syncs_only_that_tenant_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = uuid4()
    synced: list[UUID] = []

    async def tenants(db: object) -> list[UUID]:
        raise AssertionError("the lookup is only for the all-tenants run")

    async def sync(db: object, tenant_id: UUID, **kwargs: object) -> None:
        synced.append(tenant_id)
        raise RuntimeError("retry me")

    monkeypatch.setattr(service, "active_mailbox_tenants", tenants)
    monkeypatch.setattr(service, "sync_rfq_threads", sync)
    with pytest.raises(RuntimeError):
        await handle_mailbox_sync(_envelope(tenant), _deps())

    assert synced == [tenant]


def test_job_type_name() -> None:
    assert MAILBOX_SYNC == "mailbox.sync"


async def test_worker_accepts_the_scheduled_all_tenants_envelope() -> None:
    """The EventBridge Scheduler message (all-zeros tenant) is handled, not parked."""
    from bido_back.worker.main import HandlerRegistry, QueueMessage, Worker

    seen: list[Envelope] = []

    async def handler(envelope: Envelope) -> None:
        seen.append(envelope)

    class Queue:
        async def receive(self, *, max_messages: int, wait_seconds: int) -> list[QueueMessage]:
            return []

        async def delete(self, message: QueueMessage) -> None:
            return None

        async def park(self, message: QueueMessage, reason: str) -> None:
            raise AssertionError(f"parked: {reason}")

    registry = HandlerRegistry()
    registry.register(MAILBOX_SYNC, handler)
    body = (
        '{"type": "mailbox.sync", "tenant_id": "00000000-0000-0000-0000-000000000000",'
        ' "idempotency_key": "mailbox.sync:2026-10-06T12:00:00Z", "payload": {}}'
    )
    outcome = await Worker(Queue(), registry).process(
        QueueMessage(message_id="m1", receipt_handle="rh", body=body)
    )

    assert outcome == "handled"
    assert [e.tenant_id for e in seen] == [ALL_TENANTS]
