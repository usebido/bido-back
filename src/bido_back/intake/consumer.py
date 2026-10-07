"""The intake queue consumer: raw inbound emails from SQS to `InboundService`.

The intake queue (`SQS_INTAKE_QUEUE_URL`) is separate from the jobs queue
because its messages carry no tenant: the tenant is only known after
`InboundService.ingest` has matched the email to an invite. The worker's
envelope rules (tenant_id required) therefore do not apply here.

Accepted message bodies:

* S3 event notification (SES receipt rule writes the email to S3; the bucket
  notifies SQS): `{"Records": [{"eventSource": "aws:s3", "s3": {"bucket":
  {"name": ...}, "object": {"key": ...}}}]}` (the key is URL-encoded);
* SES receipt notification with an S3 action: `{"notificationType":
  "Received", "receipt": {"action": {"type": "S3", "bucketName": ...,
  "objectKey": ...}}}`;
* either of the above wrapped in an SNS envelope (`{"Type": "Notification",
  "Message": "<json>"}`);
* local development: `{"raw_key": "<storage key>"}`.

An S3 test event (`{"Event": "s3:TestEvent"}`) is acknowledged and dropped.

Per message: "handled" (ingested, or a test event; deleted), "failed" (left on
the queue for retry and, after maxReceiveCount, the DLQ), or "parked" (can
never succeed: unreadable body, unknown shape, a bucket other than ours, a
missing object).

Wiring (in the worker process, next to the jobs `Worker`):

    intake = build_intake_consumer(settings, engine)
    if intake is not None:
        loop.add_signal_handler(sig, intake.request_stop)   # with the worker's
        await asyncio.gather(worker.run_forever(), intake.run_forever())
"""

import asyncio
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, cast
from urllib.parse import unquote_plus

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.intake.service import InboundService, IngestResult
from bido_back.settings import Settings
from bido_back.storage import BlobNotFound, BlobStorage, InvalidBlobKey, build_storage, validate_key

if TYPE_CHECKING:
    # Imported for types only: worker.main will import this module.
    from bido_back.worker.main import QueueClient, QueueMessage

logger = logging.getLogger(__name__)

IntakeOutcome = Literal["handled", "failed", "parked"]


class UnusableMessage(ValueError):
    """The queue message can never be ingested; `reason` is safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class RawEmailRef:
    key: str
    bucket: str | None = None


def parse_intake_body(body: str) -> list[RawEmailRef]:
    """The raw-email objects a queue message points at (empty for a test event)."""
    try:
        data: object = json.loads(body)
    except json.JSONDecodeError:
        raise UnusableMessage("invalid_json") from None
    return _refs(data, depth=0)


def _refs(data: object, *, depth: int) -> list[RawEmailRef]:
    if not isinstance(data, dict) or depth > 2:
        raise UnusableMessage("unrecognized_body")
    obj = cast(dict[str, object], data)

    if obj.get("Type") == "Notification" and isinstance(obj.get("Message"), str):
        try:
            inner: object = json.loads(cast(str, obj["Message"]))
        except json.JSONDecodeError:
            raise UnusableMessage("invalid_sns_message") from None
        return _refs(inner, depth=depth + 1)

    if obj.get("Event") == "s3:TestEvent":
        return []

    raw_key = obj.get("raw_key")
    if isinstance(raw_key, str):
        return [RawEmailRef(key=raw_key)]

    records = obj.get("Records")
    if isinstance(records, list):
        refs = [_s3_record(r) for r in cast(list[object], records)]
        if not refs:
            raise UnusableMessage("no_records")
        return refs

    if obj.get("notificationType") == "Received":
        action = _get(_get(obj, "receipt"), "action")
        bucket, key = _get(action, "bucketName"), _get(action, "objectKey")
        if _get(action, "type") == "S3" and isinstance(bucket, str) and isinstance(key, str):
            return [RawEmailRef(key=key, bucket=bucket)]
        raise UnusableMessage("ses_notification_without_s3_action")

    raise UnusableMessage("unrecognized_body")


def _s3_record(record: object) -> RawEmailRef:
    if _get(record, "eventSource") != "aws:s3":
        raise UnusableMessage("not_an_s3_record")
    s3 = _get(record, "s3")
    bucket, key = _get(_get(s3, "bucket"), "name"), _get(_get(s3, "object"), "key")
    if not isinstance(bucket, str) or not isinstance(key, str):
        raise UnusableMessage("s3_record_without_object")
    # S3 event keys are URL-encoded (spaces as '+').
    return RawEmailRef(key=unquote_plus(key), bucket=bucket)


def _get(obj: object, key: str) -> object:
    return cast(Mapping[str, object], obj).get(key) if isinstance(obj, dict) else None


class RawEmailIngestor(Protocol):
    """What the consumer needs from `InboundService`."""

    async def ingest(self, raw: bytes) -> IngestResult: ...


class IntakeConsumer:
    """Long-polls the intake queue and feeds each raw email to `InboundService`."""

    def __init__(
        self,
        queue: "QueueClient",
        service: RawEmailIngestor,
        storage: BlobStorage,
        *,
        bucket: str | None = None,
        max_messages: int = 10,
        wait_seconds: int = 20,
    ) -> None:
        self._queue = queue
        self._service = service
        self._storage = storage
        # When set, notifications naming any other bucket are parked: the
        # storage can only read our own bucket.
        self._bucket = bucket
        self._max_messages = max_messages
        self._wait_seconds = wait_seconds
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def process(self, message: "QueueMessage") -> IntakeOutcome:
        try:
            refs = parse_intake_body(message.body)
            for ref in refs:
                if self._bucket is not None and ref.bucket not in (None, self._bucket):
                    raise UnusableMessage("foreign_bucket")
                validate_key(ref.key)
        except (UnusableMessage, InvalidBlobKey) as exc:
            return await self._park(message, getattr(exc, "reason", "invalid_key"))

        try:
            for ref in refs:
                raw = await self._storage.get(ref.key)
                result = await self._service.ingest(raw)
                logger.info(
                    "intake message=%s outcome=%s tenant=%s",
                    message.message_id,
                    result.outcome,
                    result.tenant_id,
                )
        except BlobNotFound:
            return await self._park(message, "raw_not_found")
        except Exception:
            logger.exception("intake failed message=%s", message.message_id)
            return "failed"

        await self._queue.delete(message)
        return "handled"

    async def run_once(self) -> list[IntakeOutcome]:
        messages = await self._queue.receive(
            max_messages=self._max_messages, wait_seconds=self._wait_seconds
        )
        return [await self.process(m) for m in messages]

    async def run_forever(self) -> None:
        logger.info("intake consumer started")
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
                receive.cancel()
                break
            try:
                messages = receive.result()
            except Exception:
                logger.exception("intake receive failed; backing off")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=5)
                except TimeoutError:
                    pass
                continue
            for message in messages:
                await self.process(message)
        logger.info("intake consumer stopped")

    async def _park(self, message: "QueueMessage", reason: str) -> IntakeOutcome:
        logger.error("parking intake message=%s reason=%s", message.message_id, reason)
        await self._queue.park(message, reason)
        return "parked"


# --- construction from settings ---------------------------------------------


class _SqsApi(Protocol):
    """The boto3 SQS calls `SqsQueueClient` and `SqsJobQueue` make."""

    def receive_message(self, **kwargs: object) -> Mapping[str, object]: ...
    def delete_message(self, **kwargs: object) -> object: ...
    def send_message(self, **kwargs: object) -> object: ...
    def change_message_visibility(self, **kwargs: object) -> object: ...


def sqs_client(settings: Settings) -> _SqsApi:
    """A boto3 SQS client honouring `SQS_ENDPOINT_URL` (ElasticMQ locally)."""
    import boto3  # pyright: ignore[reportMissingTypeStubs]

    return cast(
        _SqsApi,
        boto3.client(  # pyright: ignore[reportUnknownMemberType]
            "sqs", region_name=settings.aws_region, endpoint_url=settings.sqs_endpoint_url
        ),
    )


def build_inbound_service(
    settings: Settings,
    db: AsyncEngine | async_sessionmaker[AsyncSession],
    *,
    sqs: _SqsApi | None = None,
) -> InboundService:
    """`InboundService` with storage from settings and QUOTE_EXTRACT jobs to SQS_QUEUE_URL."""
    from bido_back.worker.jobs import SqsJobQueue

    if not settings.sqs_queue_url:
        raise RuntimeError("SQS_QUEUE_URL is not set; inbound replies could not be extracted")
    jobs = SqsJobQueue(
        settings.sqs_queue_url, region=settings.aws_region, sqs=sqs or sqs_client(settings)
    )
    return InboundService(db, build_storage(settings), jobs, settings.email_domain)


def build_intake_consumer(
    settings: Settings, db: AsyncEngine | async_sessionmaker[AsyncSession]
) -> IntakeConsumer | None:
    """The consumer for `SQS_INTAKE_QUEUE_URL`, or None when it is not set."""
    from bido_back.worker.main import SqsQueueClient

    if not settings.sqs_intake_queue_url:
        return None
    sqs = sqs_client(settings)
    storage = build_storage(settings)
    return IntakeConsumer(
        SqsQueueClient(
            settings.sqs_intake_queue_url,
            region=settings.aws_region,
            parking_queue_url=settings.sqs_parking_queue_url,
            sqs=sqs,
        ),
        build_inbound_service(settings, db, sqs=sqs),
        storage,
        bucket=settings.s3_bucket if settings.storage_backend == "s3" else None,
    )
