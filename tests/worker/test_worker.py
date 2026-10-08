"""Worker behavior against an in-memory queue. No AWS."""

import asyncio
import json
from uuid import uuid4

from bido_back.worker.main import Envelope, HandlerRegistry, QueueMessage, Worker


class FakeQueue:
    def __init__(self, messages: list[QueueMessage]) -> None:
        self.pending = list(messages)
        self.deleted: list[str] = []
        self.parked: list[tuple[str, str]] = []
        self.any_deleted = asyncio.Event()

    async def receive(self, *, max_messages: int, wait_seconds: int) -> list[QueueMessage]:
        batch, self.pending = self.pending[:max_messages], self.pending[max_messages:]
        return batch

    async def delete(self, message: QueueMessage) -> None:
        self.deleted.append(message.message_id)
        self.any_deleted.set()

    async def park(self, message: QueueMessage, reason: str) -> None:
        self.parked.append((message.message_id, reason))


def message(message_id: str, body: object) -> QueueMessage:
    text = body if isinstance(body, str) else json.dumps(body)
    return QueueMessage(message_id=message_id, receipt_handle=f"rh-{message_id}", body=text)


def envelope(message_type: str = "ping", **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "type": message_type,
        "tenant_id": str(uuid4()),
        "idempotency_key": "key-1",
        "payload": {"n": 1},
    }
    return {**base, **overrides}


def registry(seen: list[Envelope]) -> HandlerRegistry:
    reg = HandlerRegistry()

    @reg.handler("ping")
    async def ping(env: Envelope) -> None:  # pyright: ignore[reportUnusedFunction]
        seen.append(env)

    @reg.handler("explode")
    async def explode(env: Envelope) -> None:  # pyright: ignore[reportUnusedFunction]
        raise RuntimeError("downstream unavailable")

    return reg


async def test_handled_message_is_deleted() -> None:
    seen: list[Envelope] = []
    body = envelope()
    queue = FakeQueue([message("m1", body)])

    outcomes = await Worker(queue, registry(seen)).run_once()

    assert outcomes == ["handled"]
    assert queue.deleted == ["m1"]
    assert queue.parked == []
    assert [str(e.tenant_id) for e in seen] == [body["tenant_id"]]
    assert seen[0].payload == {"n": 1}


async def test_failed_message_is_left_for_retry() -> None:
    queue = FakeQueue([message("m1", envelope("explode"))])

    outcomes = await Worker(queue, registry([])).run_once()

    assert outcomes == ["failed"]
    assert queue.deleted == []
    assert queue.parked == []


async def test_missing_tenant_id_is_parked() -> None:
    seen: list[Envelope] = []
    body = envelope()
    del body["tenant_id"]
    queue = FakeQueue([message("m1", body), message("m2", envelope(tenant_id=""))])

    outcomes = await Worker(queue, registry(seen)).run_once()

    assert outcomes == ["parked", "parked"]
    assert queue.parked == [("m1", "missing_tenant_id"), ("m2", "missing_tenant_id")]
    assert queue.deleted == []
    assert seen == []


async def test_unknown_type_is_parked() -> None:
    queue = FakeQueue([message("m1", envelope("nobody_handles_this"))])

    outcomes = await Worker(queue, registry([])).run_once()

    assert outcomes == ["parked"]
    assert queue.parked == [("m1", "unknown_type")]


async def test_malformed_messages_are_parked() -> None:
    queue = FakeQueue(
        [
            message("m1", "not json"),
            message("m2", ["a", "list"]),
            message("m3", envelope(tenant_id="not-a-uuid")),
            message("m4", envelope(idempotency_key="")),
        ]
    )

    outcomes = await Worker(queue, registry([])).run_once()

    assert outcomes == ["parked"] * 4
    assert [reason for _, reason in queue.parked] == [
        "invalid_json",
        "invalid_envelope",
        "invalid_envelope",
        "invalid_envelope",
    ]


async def test_run_forever_stops_on_request() -> None:
    seen: list[Envelope] = []
    queue = FakeQueue([message("m1", envelope())])
    worker = Worker(queue, registry(seen), wait_seconds=0)

    task = asyncio.create_task(worker.run_forever())
    await asyncio.wait_for(queue.any_deleted.wait(), timeout=1)
    worker.request_stop()
    await asyncio.wait_for(task, timeout=1)

    assert queue.deleted == ["m1"]
    assert len(seen) == 1


async def test_each_job_is_logged_with_its_type_tenant_outcome_and_attempt() -> None:
    import logging

    from bido_back.logs import JsonFormatter

    class Lines(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.setFormatter(JsonFormatter(service="worker", release=None))
            self.lines: list[dict[str, object]] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.lines.append(json.loads(self.format(record)))

    ok, failing = envelope(), envelope("explode")
    queue = FakeQueue(
        [
            QueueMessage("m1", "rh-1", json.dumps(ok), receive_count=1),
            QueueMessage("m2", "rh-2", json.dumps(failing), receive_count=3),
        ]
    )
    handler = Lines()
    logger = logging.getLogger("bido_back.worker")
    logger.addHandler(handler)
    previous = logger.level
    logger.setLevel(logging.INFO)
    try:
        await Worker(queue, registry([])).run_once()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    jobs = [line for line in handler.lines if line["msg"] == "job"]
    assert [(j["job"], j["outcome"], j["attempt"]) for j in jobs] == [
        ("ping", "handled", 1),
        ("explode", "failed", 3),
    ]
    assert [j["tenant_id"] for j in jobs] == [ok["tenant_id"], failing["tenant_id"]]
    failure = next(line for line in handler.lines if str(line["msg"]).startswith("handler failed"))
    assert failure["job"] == "explode" and failure["exc_type"] == "RuntimeError"
