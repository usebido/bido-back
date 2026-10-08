"""Direct uploads: the browser sends a file straight to blob storage.

The api signs an upload form for one key, the browser posts the file to the
form's `url` with the form's `fields` (the file goes last, as `file`), and the
api is then told the upload is done and checks the stored object. The bytes
never pass through the api or the WAF in front of it, so a file is limited by
the size the form allows and not by a request body limit.

In AWS the form is an S3 presigned POST (`S3Storage`). Locally there is no S3,
so `LocalDirectUploads` signs a form for `POST /dev/uploads` instead, which
the browser fills in exactly the same way.
"""

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from bido_back.storage.keys import InvalidBlobKey
from bido_back.storage.local import LocalFileStorage


@dataclass(frozen=True)
class PresignedPost:
    """A form the browser posts the file to: `fields` first, then `file`."""

    url: str
    fields: dict[str, str]
    expires_at: datetime


@dataclass(frozen=True)
class StoredObject:
    size_bytes: int
    content_type: str | None


class DirectUploads(Protocol):
    async def presign_post(
        self, key: str, *, content_type: str, max_bytes: int, expires_in: timedelta
    ) -> PresignedPost: ...

    async def stat(self, key: str) -> StoredObject | None:
        """The stored object under `key`, or None if nothing was uploaded there."""
        ...


class InvalidUploadForm(Exception):
    """A local upload form that was not signed here, expired, or does not match."""


# Signs local upload forms. Per process on purpose: a form outlives a restart
# by at most its expiry, and local development does not need more than that.
_LOCAL_SECRET = secrets.token_bytes(32)


class LocalDirectUploads:
    """`DirectUploads` for local development, over `LocalFileStorage`.

    The form posts to `POST /dev/uploads` on the api itself (`base_url`), with
    a `policy` (key, content type, size limit, expiry) and its HMAC
    `signature`, the same shape as an S3 presigned POST.
    """

    def __init__(self, storage: LocalFileStorage, base_url: str) -> None:
        self._storage = storage
        self._url = base_url.rstrip("/") + "/dev/uploads"

    async def presign_post(
        self, key: str, *, content_type: str, max_bytes: int, expires_in: timedelta
    ) -> PresignedPost:
        self._storage.path_for(key)  # refuses a bad key now rather than at upload
        expires_at = datetime.now(UTC) + expires_in
        policy = _encode(
            {
                "key": key,
                "content_type": content_type,
                "max_bytes": max_bytes,
                "expires_at": int(expires_at.timestamp()),
            }
        )
        return PresignedPost(
            url=self._url,
            fields={
                "key": key,
                "Content-Type": content_type,
                "policy": policy,
                "signature": _sign(policy),
            },
            expires_at=expires_at,
        )

    async def stat(self, key: str) -> StoredObject | None:
        path = self._storage.path_for(key)
        if not path.is_file():
            return None
        # LocalFileStorage does not keep the content type.
        return StoredObject(size_bytes=path.stat().st_size, content_type=None)

    async def receive(self, fields: dict[str, str], data: bytes) -> None:
        """Store `data` from a posted form, after checking the form was signed here."""
        policy = fields.get("policy", "")
        signature = fields.get("signature", "")
        if not policy or not hmac.compare_digest(_sign(policy), signature):
            raise InvalidUploadForm("upload form signature does not match")
        terms = _decode(policy)
        expires_at = terms.get("expires_at")
        if not isinstance(expires_at, int) or expires_at < int(datetime.now(UTC).timestamp()):
            raise InvalidUploadForm("upload form expired")
        key = terms.get("key")
        content_type = terms.get("content_type")
        max_bytes = terms.get("max_bytes")
        if not isinstance(key, str) or fields.get("key") != key:
            raise InvalidUploadForm("upload form key does not match")
        if not isinstance(content_type, str) or fields.get("Content-Type") != content_type:
            raise InvalidUploadForm("upload form content type does not match")
        if not isinstance(max_bytes, int) or not 0 < len(data) <= max_bytes:
            raise InvalidUploadForm("file size is outside the allowed range")
        try:
            await self._storage.put(key, data, content_type)
        except InvalidBlobKey:
            raise InvalidUploadForm("upload form key is not valid") from None


def _sign(policy: str) -> str:
    return hmac.new(_LOCAL_SECRET, policy.encode(), hashlib.sha256).hexdigest()


def _encode(terms: dict[str, str | int]) -> str:
    return base64.urlsafe_b64encode(json.dumps(terms, sort_keys=True).encode()).decode()


def _decode(policy: str) -> dict[str, object]:
    try:
        terms: object = json.loads(base64.urlsafe_b64decode(policy.encode()))
    except ValueError:
        raise InvalidUploadForm("upload form policy is not readable") from None
    if not isinstance(terms, dict):
        raise InvalidUploadForm("upload form policy is not readable")
    return {str(k): v for k, v in cast(dict[object, object], terms).items()}
