"""SQS long-poll worker.

Every message body is a JSON envelope:

    {"type": "...", "tenant_id": "<uuid>", "idempotency_key": "...", "payload": {...}}

Outcomes per message:

- handled - the handler for `type` returned; the message is deleted.
- failed  - the handler raised; the message is NOT deleted, so SQS makes it
  visible again after the visibility timeout and it is retried (and, after the
  queue's maxReceiveCount, moved to the dead-letter queue by the redrive policy).
- parked  - the message can never succeed (bad JSON, no tenant_id, bad
  envelope, unknown type); it is moved out of the way without running anything.

SQS is at-least-once: the same message can be delivered twice, concurrently.
Handlers must be idempotent. Any external side effect (email, webhook, payment)
goes through the outbox, keyed by the envelope's `idempotency_key`, never
straight from the handler.

The queue sits behind `QueueClient` so tests use an in-memory fake.
"""

import asyncio
import json
import logging
import signal
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.approvals.executor import APPROVAL_EXECUTE_JOB, ActToolRegistry, ApprovalExecutor
from bido_back.db import create_engine
from bido_back.observability import init_observability
from bido_back.settings import Settings, get_settings
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import FX_REFRESH, MAILBOX_SYNC, QUOTE_EXTRACT

logger = logging.getLogger(__name__)

Outcome = Literal["handled", "failed", "parked"]


# --- Envelope and handlers -------------------------------------------------


Handler = Callable[[Envelope], Awaitable[None]]


class HandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, message_type: str, handler: Handler) -> None:
        if message_type in self._handlers:
            raise ValueError(f"handler already registered for {message_type!r}")
        self._handlers[message_type] = handler

    def handler(self, message_type: str) -> Callable[[Handler], Handler]:
        """Decorator form of `register`."""

        def decorate(fn: Handler) -> Handler:
            self.register(message_type, fn)
            return fn

        return decorate

    def get(self, message_type: str) -> Handler | None:
        return self._handlers.get(message_type)


def build_registry(
    db: AsyncEngine | async_sessionmaker[AsyncSession],
    act_tools: ActToolRegistry | None = None,
) -> HandlerRegistry:
    # Job handlers are registered here as features land. Each handler opens its
    # own tenant-scoped session from `envelope.tenant_id`.
    registry = HandlerRegistry()
    # The only place an approved `act` tool runs (see bido_back.approvals).
    registry.register(APPROVAL_EXECUTE_JOB, ApprovalExecutor(db, act_tools or ActToolRegistry()))
    return registry


def build_worker_registry(
    settings: Settings, engine: AsyncEngine, *, sqs: "_SqsApi | None" = None
) -> HandlerRegistry:
    """Every job handler of the RFQ flow, wired from settings (the production registry)."""
    from bido_back.agents.config import RunnerConfig
    from bido_back.fx.ecb import FxRefreshHandler
    from bido_back.intake.consumer import build_inbound_service
    from bido_back.mailbox import MailboxSyncDeps, MailboxSyncHandler
    from bido_back.quotes.extract_job import quote_extract_handler
    from bido_back.rfqs.mailbox import mailbox_senders_for
    from bido_back.rfqs.send import RfqActDeps, register_act_tools
    from bido_back.storage import build_storage

    act_tools = ActToolRegistry()
    register_act_tools(
        act_tools,
        RfqActDeps(
            db=engine,
            email_domain=settings.email_domain,
            mailbox_senders=mailbox_senders_for(settings),
        ),
    )
    registry = build_registry(engine, act_tools)
    registry.register(
        QUOTE_EXTRACT,
        quote_extract_handler(
            db=engine,
            storage=build_storage(settings),
            runner_config=RunnerConfig.from_settings(settings),
        ),
    )
    registry.register(FX_REFRESH, FxRefreshHandler(engine))
    inbound = build_inbound_service(settings, engine, sqs=sqs)
    registry.register(
        MAILBOX_SYNC,
        MailboxSyncHandler(
            MailboxSyncDeps(db=engine, ingest=inbound.ingest_for_invite, settings=settings)
        ),
    )
    return registry


# --- Queue abstraction -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueueMessage:
    message_id: str
    receipt_handle: str
    body: str


class QueueClient(Protocol):
    async def receive(self, *, max_messages: int, wait_seconds: int) -> list[QueueMessage]: ...

    async def delete(self, message: QueueMessage) -> None: ...

    async def park(self, message: QueueMessage, reason: str) -> None:
        """Take a message that can never succeed out of circulation."""
        ...


class _SqsApi(Protocol):
    """The slice of the boto3 SQS client we use (boto3 ships no type stubs)."""

    def receive_message(self, **kwargs: object) -> Mapping[str, object]: ...
    def delete_message(self, **kwargs: object) -> object: ...
    def send_message(self, **kwargs: object) -> object: ...
    def change_message_visibility(self, **kwargs: object) -> object: ...


_MAX_VISIBILITY_SECONDS = 12 * 60 * 60


class SqsQueueClient:
    """`QueueClient` over boto3. boto3 is blocking, so calls run in a thread."""

    def __init__(
        self,
        queue_url: str,
        *,
        region: str,
        parking_queue_url: str | None = None,
        sqs: _SqsApi | None = None,
    ) -> None:
        self._queue_url = queue_url
        self._parking_queue_url = parking_queue_url
        self._sqs = sqs or _boto3_sqs(region)

    async def receive(self, *, max_messages: int, wait_seconds: int) -> list[QueueMessage]:
        response = await asyncio.to_thread(
            self._sqs.receive_message,
            QueueUrl=self._queue_url,
            MaxNumberOfMessages=max_messages,
            WaitTimeSeconds=wait_seconds,
        )
        raw = cast(list[dict[str, str]], response.get("Messages", []))
        return [
            QueueMessage(
                message_id=m["MessageId"], receipt_handle=m["ReceiptHandle"], body=m["Body"]
            )
            for m in raw
        ]

    async def delete(self, message: QueueMessage) -> None:
        await asyncio.to_thread(
            self._sqs.delete_message,
            QueueUrl=self._queue_url,
            ReceiptHandle=message.receipt_handle,
        )

    async def park(self, message: QueueMessage, reason: str) -> None:
        if self._parking_queue_url is not None:
            await asyncio.to_thread(
                self._sqs.send_message,
                QueueUrl=self._parking_queue_url,
                MessageBody=message.body,
                MessageAttributes={
                    "park_reason": {"DataType": "String", "StringValue": reason},
                    "source_message_id": {"DataType": "String", "StringValue": message.message_id},
                },
            )
            await self.delete(message)
            return
        # No parking queue configured: hide it for the maximum time and let the
        # queue's redrive policy move it to the DLQ after maxReceiveCount.
        await asyncio.to_thread(
            self._sqs.change_message_visibility,
            QueueUrl=self._queue_url,
            ReceiptHandle=message.receipt_handle,
            VisibilityTimeout=_MAX_VISIBILITY_SECONDS,
        )


def _boto3_sqs(region: str) -> _SqsApi:
    import boto3  # pyright: ignore[reportMissingTypeStubs]

    return cast(_SqsApi, boto3.client("sqs", region_name=region))  # pyright: ignore[reportUnknownMemberType]


# --- Worker ----------------------------------------------------------------


class Worker:
    def __init__(
        self,
        queue: QueueClient,
        registry: HandlerRegistry,
        *,
        max_messages: int = 10,
        wait_seconds: int = 20,
    ) -> None:
        self._queue = queue
        self._registry = registry
        self._max_messages = max_messages
        self._wait_seconds = wait_seconds
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def process(self, message: QueueMessage) -> Outcome:
        try:
            raw: object = json.loads(message.body)
        except json.JSONDecodeError:
            return await self._park(message, "invalid_json")
        if not isinstance(raw, dict):
            return await self._park(message, "invalid_envelope")
        if not cast(dict[str, object], raw).get("tenant_id"):
            return await self._park(message, "missing_tenant_id")
        try:
            envelope = Envelope.model_validate(raw)
        except ValidationError:
            return await self._park(message, "invalid_envelope")

        handler = self._registry.get(envelope.type)
        if handler is None:
            return await self._park(message, "unknown_type", envelope)

        try:
            await handler(envelope)
        except Exception:
            # Left on the queue: it becomes visible again after the visibility
            # timeout and is retried.
            logger.exception(
                "handler failed type=%s tenant=%s key=%s message=%s",
                envelope.type,
                envelope.tenant_id,
                envelope.idempotency_key,
                message.message_id,
            )
            return "failed"

        await self._queue.delete(message)
        return "handled"

    async def run_once(self) -> list[Outcome]:
        messages = await self._queue.receive(
            max_messages=self._max_messages, wait_seconds=self._wait_seconds
        )
        return [await self.process(m) for m in messages]

    async def run_forever(self) -> None:
        logger.info("worker started")
        while not self._stop.is_set():
            receive = asyncio.create_task(
                self._queue.receive(
                    max_messages=self._max_messages, wait_seconds=self._wait_seconds
                )
            )
            stop = asyncio.create_task(self._stop.wait())
            await asyncio.wait({receive, stop}, return_when=asyncio.FIRST_COMPLETED)
            stop.cancel()
            if not receive.done():
                # Stopping mid-poll. Anything the poll still returns stays
                # invisible until its timeout, then is redelivered.
                receive.cancel()
                break
            try:
                messages = receive.result()
            except Exception:
                logger.exception("receive failed; backing off")
                await self._sleep_unless_stopped(5)
                continue
            # A batch in progress is finished even if a stop arrives meanwhile.
            for message in messages:
                await self.process(message)
        logger.info("worker stopped")

    async def _sleep_unless_stopped(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _park(
        self, message: QueueMessage, reason: str, envelope: Envelope | None = None
    ) -> Outcome:
        logger.error(
            "parking message=%s reason=%s type=%s tenant=%s",
            message.message_id,
            reason,
            envelope.type if envelope else None,
            envelope.tenant_id if envelope else None,
        )
        await self._queue.park(message, reason)
        return "parked"


async def main() -> None:
    from bido_back.intake.consumer import build_intake_consumer, sqs_client

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    init_observability(settings, service="worker")
    if not settings.sqs_queue_url:
        raise SystemExit("SQS_QUEUE_URL is not set")

    engine = create_engine(settings)
    # One client for the jobs queue, honouring SQS_ENDPOINT_URL (ElasticMQ locally).
    sqs = sqs_client(settings)
    worker = Worker(
        SqsQueueClient(
            settings.sqs_queue_url,
            region=settings.aws_region,
            parking_queue_url=settings.sqs_parking_queue_url,
            sqs=sqs,
        ),
        build_worker_registry(settings, engine, sqs=sqs),
    )
    # Raw inbound emails (SES -> S3 -> SQS); None when SQS_INTAKE_QUEUE_URL is unset.
    intake = build_intake_consumer(settings, engine)

    loop = asyncio.get_running_loop()

    def stop() -> None:
        worker.request_stop()
        if intake is not None:
            intake.request_stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop)
    try:
        await asyncio.gather(
            worker.run_forever(), *([intake.run_forever()] if intake is not None else [])
        )
    finally:
        await engine.dispose()
