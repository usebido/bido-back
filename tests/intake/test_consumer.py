"""Intake consumer: notification parsing and queue outcomes, with fakes only."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from bido_back.intake.consumer import (
    IntakeConsumer,
    RawEmailRef,
    UnusableMessage,
    parse_intake_body,
)
from bido_back.intake.service import IngestResult
from bido_back.storage.local import LocalFileStorage
from bido_back.worker.main import QueueMessage


def _s3_event(bucket: str, key: str) -> dict[str, object]:
    return {
        "Records": [
            {"eventSource": "aws:s3", "s3": {"bucket": {"name": bucket}, "object": {"key": key}}}
        ]
    }


def test_parses_s3_event_with_url_encoded_key() -> None:
    body = json.dumps(_s3_event("mail", "inbound/raw+mail%2B1"))
    assert parse_intake_body(body) == [RawEmailRef(key="inbound/raw mail+1", bucket="mail")]


def test_parses_ses_notification_wrapped_in_sns() -> None:
    ses = {
        "notificationType": "Received",
        "receipt": {"action": {"type": "S3", "bucketName": "mail", "objectKey": "ses/abc"}},
    }
    body = json.dumps({"Type": "Notification", "Message": json.dumps(ses)})
    assert parse_intake_body(body) == [RawEmailRef(key="ses/abc", bucket="mail")]


def test_parses_local_raw_key_and_ignores_s3_test_event() -> None:
    assert parse_intake_body('{"raw_key": "inbound/x.eml"}') == [RawEmailRef(key="inbound/x.eml")]
    assert parse_intake_body('{"Event": "s3:TestEvent"}') == []


@pytest.mark.parametrize(
    "body",
    ["not json", "[]", '{"what": 1}', '{"Records": []}', '{"Records": [{"eventSource": "x"}]}'],
)
def test_unusable_bodies(body: str) -> None:
    with pytest.raises(UnusableMessage):
        parse_intake_body(body)


class FakeQueue:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.parked: list[tuple[str, str]] = []

    async def receive(self, *, max_messages: int, wait_seconds: int) -> list[QueueMessage]:
        return []

    async def delete(self, message: QueueMessage) -> None:
        self.deleted.append(message.message_id)

    async def park(self, message: QueueMessage, reason: str) -> None:
        self.parked.append((message.message_id, reason))


class FakeService:
    def __init__(self, fail: bool = False) -> None:
        self.raws: list[bytes] = []
        self.fail = fail

    async def ingest(self, raw: bytes) -> IngestResult:
        if self.fail:
            raise RuntimeError("database down")
        self.raws.append(raw)
        return IngestResult(outcome="unmatched", raw_key="inbound/x.eml")


def _consumer(
    tmp_path: Path, service: FakeService, queue: FakeQueue, bucket: str | None = None
) -> IntakeConsumer:
    return IntakeConsumer(
        queue,
        service,
        LocalFileStorage(tmp_path),
        bucket=bucket,
    )


def _msg(body: object) -> QueueMessage:
    mid = uuid4().hex
    return QueueMessage(message_id=mid, receipt_handle=f"rh-{mid}", body=json.dumps(body))


async def test_ingests_and_deletes(tmp_path: Path) -> None:
    await LocalFileStorage(tmp_path).put("ses/abc", b"raw email", "message/rfc822")
    queue, service = FakeQueue(), FakeService()
    message = _msg(_s3_event("mail", "ses/abc"))
    assert await _consumer(tmp_path, service, queue, "mail").process(message) == "handled"
    assert service.raws == [b"raw email"]
    assert queue.deleted == [message.message_id]


async def test_parks_foreign_bucket_missing_object_and_bad_key(tmp_path: Path) -> None:
    queue, service = FakeQueue(), FakeService()
    consumer = _consumer(tmp_path, service, queue, "mail")
    assert await consumer.process(_msg(_s3_event("other", "ses/abc"))) == "parked"
    assert await consumer.process(_msg(_s3_event("mail", "ses/missing"))) == "parked"
    assert await consumer.process(_msg({"raw_key": "../etc/passwd"})) == "parked"
    assert [reason for _, reason in queue.parked] == [
        "foreign_bucket",
        "raw_not_found",
        "invalid_key",
    ]
    assert service.raws == [] and queue.deleted == []


async def test_failure_leaves_message_for_retry(tmp_path: Path) -> None:
    await LocalFileStorage(tmp_path).put("inbound/x.eml", b"raw", "message/rfc822")
    queue = FakeQueue()
    consumer = _consumer(tmp_path, FakeService(fail=True), queue)
    assert await consumer.process(_msg({"raw_key": "inbound/x.eml"})) == "failed"
    assert queue.deleted == [] and queue.parked == []
