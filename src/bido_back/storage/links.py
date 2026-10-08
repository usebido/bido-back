"""Short-lived links to read one stored file (download or inline preview).

The browser opens the link itself: the bytes never pass through the api. The
link fixes the `Content-Disposition` and `Content-Type` the file is served
with, so a stored file can never render as a page of ours (no HTML or SVG
from the bucket): downloads are always `attachment`, and only types from an
allowlist are ever served `inline` (see `bido_back.uploads.files`).

In AWS a link is an S3 presigned GET (`S3Storage.presign_get`). Locally there
is no S3, so `LocalDownloadLinks` signs a link to `GET /dev/files` instead.
"""

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, cast
from urllib.parse import quote, urlencode

from bido_back.storage.local import LocalFileStorage

Disposition = Literal["attachment", "inline"]


@dataclass(frozen=True)
class FileLink:
    url: str
    expires_at: datetime


class DownloadLinks(Protocol):
    async def presign_get(
        self,
        key: str,
        *,
        filename: str,
        content_type: str,
        disposition: Disposition,
        expires_in: timedelta,
    ) -> FileLink: ...


class InvalidFileLink(Exception):
    """A local file link that was not signed here, or has expired."""


def content_disposition(disposition: Disposition, filename: str) -> str:
    """`attachment; filename="..."; filename*=UTF-8''...` (RFC 6266 / 5987).

    The plain `filename` is an ASCII stand-in with no quotes or backslashes;
    browsers that know `filename*` use the exact UTF-8 name.
    """
    ascii_name = (
        "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in filename) or "file"
    )
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


# Signs local file links. Per process on purpose, like the local upload forms.
_LOCAL_SECRET = secrets.token_bytes(32)


class LocalDownloadLinks:
    """`DownloadLinks` for local development: signed links to `GET /dev/files`."""

    def __init__(self, storage: LocalFileStorage, base_url: str) -> None:
        self._storage = storage
        self._url = base_url.rstrip("/") + "/dev/files"

    async def presign_get(
        self,
        key: str,
        *,
        filename: str,
        content_type: str,
        disposition: Disposition,
        expires_in: timedelta,
    ) -> FileLink:
        self._storage.path_for(key)  # refuses a bad key now
        expires_at = datetime.now(UTC) + expires_in
        token = _encode(
            {
                "key": key,
                "content_type": content_type,
                "disposition": content_disposition(disposition, filename),
                "expires_at": int(expires_at.timestamp()),
            }
        )
        query = urlencode({"token": token, "signature": _sign(token)})
        return FileLink(url=f"{self._url}?{query}", expires_at=expires_at)

    @staticmethod
    def verify(token: str, signature: str) -> tuple[str, str, str]:
        """The (key, content type, Content-Disposition) a link was signed for."""
        if not token or not hmac.compare_digest(_sign(token), signature):
            raise InvalidFileLink("file link signature does not match")
        terms = _decode(token)
        expires_at = terms.get("expires_at")
        if not isinstance(expires_at, int) or expires_at < int(datetime.now(UTC).timestamp()):
            raise InvalidFileLink("file link expired")
        key = terms.get("key")
        content_type = terms.get("content_type")
        disposition = terms.get("disposition")
        if not (
            isinstance(key, str) and isinstance(content_type, str) and isinstance(disposition, str)
        ):
            raise InvalidFileLink("file link is not readable")
        return key, content_type, disposition


def _sign(token: str) -> str:
    return hmac.new(_LOCAL_SECRET, token.encode(), hashlib.sha256).hexdigest()


def _encode(terms: dict[str, str | int]) -> str:
    return base64.urlsafe_b64encode(json.dumps(terms, sort_keys=True).encode()).decode()


def _decode(token: str) -> dict[str, object]:
    try:
        terms: object = json.loads(base64.urlsafe_b64decode(token.encode()))
    except ValueError:
        raise InvalidFileLink("file link is not readable") from None
    if not isinstance(terms, dict):
        raise InvalidFileLink("file link is not readable")
    return {str(k): v for k, v in cast(dict[object, object], terms).items()}
