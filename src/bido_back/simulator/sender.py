"""The sandbox email transport: a sandbox tenant's email never leaves Bido.

`sandbox_aware(inner, schedulers)` wraps the RFQ flow's sender factory
(`bido_back.rfqs.mailbox.MailboxSenderFactory`). For a tenant whose
`tenants.sandbox` is true it returns a `SandboxEmailSender`; for every other
tenant it calls `inner` unchanged (the buyer's mailbox, or Mailpit locally).

`SandboxEmailSender.send` contacts no SMTP server, Gmail or Graph. It records
the email in `sandbox_outbound` (in the caller's tenant-scoped session, so it
commits with the send's own transaction), bound to the invite whose thread it
belongs to, and schedules a `simulator.reply` job for when the simulated
supplier should answer. A sandbox tenant therefore needs no connected mailbox.

The job is enqueued before the caller commits (there is no outbox yet); it is
always delayed by a few seconds, and the handler retries if the row is not
visible yet.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.db.models import Invite, SandboxOutbound, SupplierPersona, Tenant
from bido_back.email.addresses import make_message_id
from bido_back.email.mime import build_mime
from bido_back.email.types import EmailSender, OutboundEmail, SentEmail
from bido_back.rfqs.mailbox import MailboxSenderFactory
from bido_back.settings import Settings
from bido_back.simulator.offers import response_delay_minutes
from bido_back.simulator.personas import persona_from_row
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import SIMULATOR_REPLY

logger = logging.getLogger(__name__)

SANDBOX_DOMAIN: Final = "sandbox.usebido.invalid"
MIN_DELAY_SECONDS: Final = 5
# SQS caps a message's delay at 15 minutes; longer waits are re-scheduled by the job.
MAX_DELAY_SECONDS: Final = 900


class ReplyScheduler(Protocol):
    async def schedule(self, tenant_id: UUID, outbound_id: UUID, delay_seconds: int) -> None: ...


def reply_envelope(tenant_id: UUID, outbound_id: UUID, due_at: datetime) -> Envelope:
    return Envelope(
        type=SIMULATOR_REPLY,
        tenant_id=tenant_id,
        idempotency_key=f"{SIMULATOR_REPLY}:{outbound_id}:{int(due_at.timestamp())}",
        payload={"outbound_id": str(outbound_id)},
    )


class _SqsDelayApi(Protocol):
    def send_message(self, **kwargs: object) -> object: ...


class SqsReplyScheduler:
    """Schedules `simulator.reply` on the jobs queue with an SQS message delay."""

    def __init__(self, queue_url: str, sqs: _SqsDelayApi) -> None:
        self._queue_url = queue_url
        self._sqs = sqs

    async def schedule(self, tenant_id: UUID, outbound_id: UUID, delay_seconds: int) -> None:
        delay = max(MIN_DELAY_SECONDS, min(delay_seconds, MAX_DELAY_SECONDS))
        envelope = reply_envelope(
            tenant_id, outbound_id, datetime.now(UTC) + timedelta(seconds=delay)
        )
        await asyncio.to_thread(
            self._sqs.send_message,
            QueueUrl=self._queue_url,
            MessageBody=envelope.model_dump_json(),
            DelaySeconds=delay,
            MessageAttributes={
                "type": {"DataType": "String", "StringValue": envelope.type},
                "idempotency_key": {"DataType": "String", "StringValue": envelope.idempotency_key},
            },
        )


def sqs_reply_schedulers(settings: Settings) -> Callable[[], ReplyScheduler]:
    """A lazily built scheduler over SQS_QUEUE_URL (built at the first sandbox send)."""
    built: list[ReplyScheduler] = []

    def get() -> ReplyScheduler:
        if not built:
            if not settings.sqs_queue_url:
                raise RuntimeError("SQS_QUEUE_URL is not set; the simulator cannot answer")
            from bido_back.intake.consumer import sqs_client

            built.append(
                SqsReplyScheduler(settings.sqs_queue_url, cast(_SqsDelayApi, sqs_client(settings)))
            )
        return built[0]

    return get


async def is_sandbox(session: AsyncSession, tenant_id: UUID) -> bool:
    return bool(await session.scalar(select(Tenant.sandbox).where(Tenant.id == tenant_id)))


@dataclass(slots=True)
class SandboxEmailSender:
    """`EmailSender` for a sandbox tenant (see the module docstring)."""

    session: AsyncSession
    tenant_id: UUID
    schedulers: Callable[[], ReplyScheduler]

    async def send(self, email: OutboundEmail) -> SentEmail:
        if not await is_sandbox(self.session, self.tenant_id):
            # Never: the factory only builds this sender for sandbox tenants.
            raise RuntimeError(f"tenant {self.tenant_id} is not a sandbox tenant")
        message_id = email.message_id or make_message_id(SANDBOX_DOMAIN)
        thread_ids = [message_id, *(i for i in (email.in_reply_to,) if i), *email.references]
        invite = await self.session.scalar(
            select(Invite).where(Invite.thread_message_id.in_(thread_ids)).limit(1)
        )
        persona_row = (
            await self.session.scalar(
                select(SupplierPersona).where(SupplierPersona.supplier_id == invite.supplier_id)
            )
            if invite is not None
            else None
        )
        delay_minutes = response_delay_minutes(persona_from_row(persona_row))
        due_at = datetime.now(UTC) + timedelta(minutes=delay_minutes)
        outbound_id = await self.session.scalar(
            insert(SandboxOutbound)
            .values(
                tenant_id=self.tenant_id,
                invite_id=invite.id if invite is not None else None,
                message_id=message_id,
                in_reply_to=email.in_reply_to,
                references=list(email.references),
                from_name=email.from_name,
                from_address=email.from_address,
                to_addresses=list(email.to),
                subject=email.subject,
                body_text=email.text_body,
                due_at=due_at,
            )
            .on_conflict_do_nothing(constraint="sandbox_outbound_message_key")
            .returning(SandboxOutbound.id)
        )
        if outbound_id is None:  # a retried send of the same email: already scheduled
            logger.info("sandbox send already recorded tenant=%s", self.tenant_id)
        else:
            await self.session.flush()
            await self.schedulers().schedule(self.tenant_id, outbound_id, delay_minutes * 60)
            logger.info(
                "sandbox send recorded tenant=%s outbound=%s invite=%s",
                self.tenant_id,
                outbound_id,
                invite.id if invite is not None else None,
            )
        raw = build_mime(
            OutboundEmail(
                from_name=email.from_name,
                from_address=email.from_address,
                to=email.to,
                subject=email.subject,
                text_body=email.text_body,
                cc=email.cc,
                message_id=message_id,
                in_reply_to=email.in_reply_to,
                references=email.references,
                attachments=email.attachments,
            )
        ).as_bytes()
        return SentEmail(message_id=message_id, raw=raw)


def sandbox_aware(
    inner: MailboxSenderFactory, schedulers: Callable[[], ReplyScheduler]
) -> MailboxSenderFactory:
    """`inner` for real tenants; the sandbox transport for sandbox tenants."""

    async def factory(session: AsyncSession, tenant_id: UUID, user_id: UUID) -> EmailSender:
        if await is_sandbox(session, tenant_id):
            return SandboxEmailSender(session, tenant_id, schedulers)
        return await inner(session, tenant_id, user_id)

    return factory
