"""Blob storage for raw emails and attachments (local folder in dev, S3 in AWS).

Only the interface lives here; implementations and `build_storage(settings)`
are added alongside it.
"""

from typing import Protocol


class BlobStorage(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...
