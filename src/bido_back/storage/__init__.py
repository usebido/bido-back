"""Blob storage for raw emails and attachments (local folder in dev, S3 in AWS).

Keys are relative, slash-separated paths such as `inbound/<sha256>.eml` or
`attachments/<tenant>/<message>/<file>`. Both implementations refuse keys that
are absolute or climb out of their root (`..`), so a filename taken from an
email can never write outside the storage area.

`get` raises `BlobNotFound` when the key does not exist.
"""

from typing import Protocol

from bido_back.settings import Settings
from bido_back.storage.keys import BlobNotFound, InvalidBlobKey, validate_key


class BlobStorage(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...


def build_storage(settings: Settings) -> BlobStorage:
    """The storage backend selected by `settings.storage_backend`."""
    if settings.storage_backend == "s3":
        if not settings.s3_bucket:
            raise RuntimeError("STORAGE_BACKEND=s3 requires S3_BUCKET")
        from bido_back.storage.s3 import S3Storage

        return S3Storage(settings.s3_bucket, region=settings.aws_region)

    from bido_back.storage.local import LocalFileStorage

    return LocalFileStorage(settings.storage_local_dir)


__all__ = ["BlobNotFound", "BlobStorage", "InvalidBlobKey", "build_storage", "validate_key"]
