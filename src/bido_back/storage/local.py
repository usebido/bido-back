"""Blob storage in a local folder (development and tests)."""

import asyncio
import os
import tempfile
from pathlib import Path

from bido_back.storage.keys import BlobNotFound, InvalidBlobKey, validate_key


class LocalFileStorage:
    """Stores each blob as a file under `root`; the content type is not kept.

    Writes are atomic (temp file + rename), so a reader never sees half a blob.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, key: str) -> Path:
        validate_key(key)
        path = (self._root / key).resolve()
        # Belt and braces after validate_key: symlinks inside the root must not
        # lead outside it either.
        if not path.is_relative_to(self._root):
            raise InvalidBlobKey("blob key escapes the storage root")
        return path

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self.path_for(key)
        await asyncio.to_thread(_write_atomic, path, data)

    async def get(self, key: str) -> bytes:
        path = self.path_for(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            raise BlobNotFound("no blob stored under the key") from None


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
