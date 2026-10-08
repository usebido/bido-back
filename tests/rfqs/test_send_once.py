"""An approved RFQ email reaches the supplier once, even when the worker crashes mid-send."""

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.approvals import ActToolRegistry, ApprovalExecutor, decide_approval
from bido_back.db import tenant_session
from bido_back.db.models import Approval, Invite, Message
from bido_back.email.types import OutboundEmail, SentEmail
from bido_back.rfqs.send import RfqActDeps, register_act_tools
from tests.approvals.conftest import FakeJobQueue, People

from .conftest import FakeMailboxes, FakeSender
from .test_service import EMAIL_DOMAIN, approver, prepared

pytestmark = pytest.mark.db


class Crash(Exception):
    """The worker died after the provider accepted the email, before our commit."""


class SentFolderSender(FakeSender):
    """Behaves like `MailboxSender`: a `possibly_sent` email already in Sent is not resent."""

    def __init__(self, crash_after_send: int = 0) -> None:
        super().__init__()
        self.crash_after_send = crash_after_send
        self.lookups: list[str] = []

    async def send(self, email: OutboundEmail) -> SentEmail:
        if email.possibly_sent and email.message_id:
            self.lookups.append(email.message_id)
            if any(e.message_id == email.message_id for e in self.sent):
                return SentEmail(message_id=email.message_id, raw=b"")
        self.sent.append(email)
        if self.crash_after_send:
            self.crash_after_send -= 1
            raise Crash
        return SentEmail(message_id=email.message_id or "<missing@test>", raw=b"")


async def _executor(engine: AsyncEngine, mailboxes: FakeMailboxes) -> ApprovalExecutor:
    registry = ActToolRegistry()
    register_act_tools(
        registry, RfqActDeps(db=engine, email_domain=EMAIL_DOMAIN, mailbox_senders=mailboxes)
    )
    return ApprovalExecutor(engine, registry)


async def test_crash_after_send_then_retry_sends_once(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    sender = SentFolderSender(crash_after_send=1)
    mailboxes = FakeMailboxes(sender=sender)
    _, invite_id, approval_id, _ = await prepared(app_engine, rfq_rows, mailboxes)
    jobs = FakeJobQueue()
    await decide_approval(app_engine, approver(rfq_rows), approval_id, "approve", jobs=jobs)
    executor = await _executor(app_engine, mailboxes)

    with pytest.raises(Crash):
        await executor(jobs.jobs[0])
    async with tenant_session(app_engine, rfq_rows.tenant) as session:
        invite = await session.get(Invite, invite_id)
        approval = await session.get(Approval, approval_id)
    assert invite is not None and approval is not None
    # Rolled back to before the send, but the claimed Message-ID was committed.
    assert invite.status == "pending_approval" and invite.thread_message_id is not None
    assert approval.status == "approved"

    await executor(jobs.jobs[0])  # the queue redelivers

    assert len(sender.sent) == 1
    assert sender.lookups == [invite.thread_message_id]
    async with tenant_session(app_engine, rfq_rows.tenant) as session:
        invite = await session.get(Invite, invite_id)
        approval = await session.get(Approval, approval_id)
        messages = list(
            await session.scalars(select(Message).where(Message.invite_id == invite_id))
        )
    assert invite is not None and approval is not None
    assert invite.status == "sent"
    assert approval.status == "executed"
    assert [m.message_id_header for m in messages] == [sender.sent[0].message_id]


async def test_concurrent_duplicate_jobs_send_once(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    sender = SentFolderSender()
    mailboxes = FakeMailboxes(sender=sender)
    _, _, approval_id, _ = await prepared(app_engine, rfq_rows, mailboxes)
    jobs = FakeJobQueue()
    await decide_approval(app_engine, approver(rfq_rows), approval_id, "approve", jobs=jobs)
    first, second = await _executor(app_engine, mailboxes), await _executor(app_engine, mailboxes)

    await asyncio.gather(first(jobs.jobs[0]), second(jobs.jobs[0]))

    assert len(sender.sent) == 1
    assert sender.lookups == []  # a first attempt never looks up


async def test_first_attempt_is_not_marked_possibly_sent(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    sender = SentFolderSender()
    mailboxes = FakeMailboxes(sender=sender)
    _, _, approval_id, _ = await prepared(app_engine, rfq_rows, mailboxes)
    jobs = FakeJobQueue()
    await decide_approval(app_engine, approver(rfq_rows), approval_id, "approve", jobs=jobs)
    await (await _executor(app_engine, mailboxes))(jobs.jobs[0])

    [email] = sender.sent
    assert email.possibly_sent is False
