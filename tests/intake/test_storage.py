"""LocalFileStorage: round trip, atomic writes, and refusing keys that escape the root."""

import asyncio
from pathlib import Path

import pytest

from bido_back.settings import Settings
from bido_back.storage import BlobNotFound, InvalidBlobKey, build_storage
from bido_back.storage.local import LocalFileStorage
from bido_back.storage.s3 import S3Storage


async def test_put_get_round_trip(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    await storage.put("attachments/t/m/1-quote.pdf", b"%PDF", "application/pdf")
    assert await storage.get("attachments/t/m/1-quote.pdf") == b"%PDF"
    await storage.put("attachments/t/m/1-quote.pdf", b"v2", "application/pdf")
    assert await storage.get("attachments/t/m/1-quote.pdf") == b"v2"
    assert not list(await asyncio.to_thread(lambda: list(tmp_path.rglob(".tmp-*"))))


async def test_missing_key_raises_blob_not_found(tmp_path: Path) -> None:
    with pytest.raises(BlobNotFound):
        await LocalFileStorage(tmp_path).get("inbound/nope.eml")


@pytest.mark.parametrize(
    "key",
    [
        "",
        "/etc/passwd",
        "../outside",
        "a/../../outside",
        "a/./b",
        "a//b",
        "a/b/",
        "..",
        "a\\..\\b",
        "a\x00b",
        "x" * 2000,
    ],
)
async def test_traversal_and_malformed_keys_are_refused(tmp_path: Path, key: str) -> None:
    storage = LocalFileStorage(tmp_path / "root")
    with pytest.raises(InvalidBlobKey):
        await storage.put(key, b"x", "text/plain")
    with pytest.raises(InvalidBlobKey):
        await storage.get(key)
    assert not (tmp_path / "outside").exists()


async def test_symlink_out_of_the_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "elsewhere").mkdir()
    (root / "link").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(InvalidBlobKey):
        await LocalFileStorage(root).put("link/file", b"x", "text/plain")


class NoSuchKeyError(Exception):
    def __init__(self) -> None:
        super().__init__("NoSuchKey")
        self.response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, object]]] = {}

    def put_object(self, **kwargs: object) -> object:
        body = kwargs["Body"]
        assert isinstance(body, bytes)
        self.objects[str(kwargs["Key"])] = (body, kwargs)
        return {}

    def get_object(self, **kwargs: object) -> dict[str, object]:
        key = str(kwargs["Key"])
        if key not in self.objects:
            raise NoSuchKeyError
        data = self.objects[key][0]

        class Body:
            def read(self) -> bytes:
                return data

        return {"Body": Body()}

    def head_object(self, **kwargs: object) -> dict[str, object]:
        raise NoSuchKeyError

    def generate_presigned_url(self, *args: object, **kwargs: object) -> str:
        raise AssertionError("not used here")

    def generate_presigned_post(self, **kwargs: object) -> dict[str, object]:
        raise NotImplementedError


async def test_s3_storage_uses_sse_kms_and_maps_missing_keys() -> None:
    s3 = FakeS3()
    storage = S3Storage("bucket", region="eu-central-1", kms_key_id="alias/bido", s3=s3)
    await storage.put("inbound/a.eml", b"raw", "message/rfc822")
    assert await storage.get("inbound/a.eml") == b"raw"
    _, kwargs = s3.objects["inbound/a.eml"]
    assert kwargs["ServerSideEncryption"] == "aws:kms"
    assert kwargs["SSEKMSKeyId"] == "alias/bido"
    assert kwargs["ContentType"] == "message/rfc822"
    with pytest.raises(BlobNotFound):
        await storage.get("inbound/missing.eml")
    with pytest.raises(InvalidBlobKey):
        await storage.put("../x", b"", "text/plain")


def test_build_storage_selects_the_backend(tmp_path: Path) -> None:
    base = {"database_url": "postgresql://u@localhost:5432/db"}
    local = build_storage(Settings.model_validate({**base, "storage_local_dir": str(tmp_path)}))
    assert isinstance(local, LocalFileStorage)
    assert local.root == tmp_path.resolve()
    with pytest.raises(RuntimeError):
        build_storage(Settings.model_validate({**base, "storage_backend": "s3"}))
