"""Enqueueing worker jobs: the producer side of the queue the worker consumes.

`JobQueue` is the seam; `SqsJobQueue` is the production implementation and
tests use an in-memory fake. Delivery is at-least-once and a job can be
enqueued more than once, so every handler deduplicates by the envelope's
`idempotency_key` (or by the state it guards).

Not yet transactional: callers enqueue AFTER their database transaction
commits. A crash between the commit and the enqueue loses the job; the
transactional outbox (written in the same transaction, relayed to SQS) closes
that gap and is not built yet.
"""

import asyncio
from typing import Protocol, cast

from bido_back.worker.envelope import Envelope


class JobQueue(Protocol):
    async def enqueue(self, envelope: Envelope) -> None: ...


class _SqsSendApi(Protocol):
    """The slice of the boto3 SQS client used here (boto3 ships no type stubs)."""

    def send_message(self, **kwargs: object) -> object: ...


class SqsJobQueue:
    """`JobQueue` over boto3 `send_message`. boto3 blocks, so it runs in a thread."""

    def __init__(self, queue_url: str, *, region: str, sqs: _SqsSendApi | None = None) -> None:
        self._queue_url = queue_url
        self._sqs = sqs or _boto3_sqs(region)

    async def enqueue(self, envelope: Envelope) -> None:
        await asyncio.to_thread(
            self._sqs.send_message,
            QueueUrl=self._queue_url,
            MessageBody=envelope.model_dump_json(),
            # Ids only, for routing and tracing; never payload content.
            MessageAttributes={
                "type": {"DataType": "String", "StringValue": envelope.type},
                "idempotency_key": {
                    "DataType": "String",
                    "StringValue": envelope.idempotency_key,
                },
            },
        )


def _boto3_sqs(region: str) -> _SqsSendApi:
    import boto3  # pyright: ignore[reportMissingTypeStubs]

    return cast(_SqsSendApi, boto3.client("sqs", region_name=region))  # pyright: ignore[reportUnknownMemberType]
