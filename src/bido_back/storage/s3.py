"""Blob storage in S3 (AWS). Objects are encrypted with SSE-KMS by default."""

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from bido_back.storage.direct import PresignedPost, StoredObject
from bido_back.storage.keys import BlobNotFound, validate_key


class _Body(Protocol):
    def read(self) -> bytes: ...


class _S3Api(Protocol):
    """The slice of the boto3 S3 client used here (boto3 ships no type stubs)."""

    def put_object(self, **kwargs: object) -> object: ...
    def get_object(self, **kwargs: object) -> Mapping[str, object]: ...
    def head_object(self, **kwargs: object) -> Mapping[str, object]: ...
    def generate_presigned_post(self, **kwargs: object) -> Mapping[str, object]: ...


class S3Storage:
    """`BlobStorage` over boto3. boto3 blocks, so calls run in a thread.

    Without `kms_key_id` S3 uses the bucket's default KMS key (or the
    AWS-managed `aws/s3` key); the request still asks for `aws:kms`, so an
    object is never stored with only SSE-S3.
    """

    def __init__(
        self,
        bucket: str,
        *,
        region: str,
        kms_key_id: str | None = None,
        endpoint_url: str | None = None,
        s3: _S3Api | None = None,
    ) -> None:
        self._bucket = bucket
        self._kms_key_id = kms_key_id
        self._s3 = s3 or _boto3_s3(region, endpoint_url)

    @property
    def bucket(self) -> str:
        return self._bucket

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        validate_key(key)
        kwargs: dict[str, object] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": data,
            "ContentType": content_type,
            "ServerSideEncryption": "aws:kms",
        }
        if self._kms_key_id:
            kwargs["SSEKMSKeyId"] = self._kms_key_id
        await asyncio.to_thread(self._s3.put_object, **kwargs)

    async def get(self, key: str) -> bytes:
        validate_key(key)
        return await asyncio.to_thread(self._get_sync, key)

    def _get_sync(self, key: str) -> bytes:
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            if _is_missing(exc):
                raise BlobNotFound("no blob stored under the key") from None
            raise
        return cast(_Body, response["Body"]).read()

    async def presign_post(
        self, key: str, *, content_type: str, max_bytes: int, expires_in: timedelta
    ) -> PresignedPost:
        """A presigned POST for `key`: this content type, 1 to `max_bytes` bytes.

        Signing is local (no request to S3). The upload is made with the
        signer's credentials, so the task role's PutObject and KMS grants are
        what allow it; the bucket needs CORS for the frontend's origin.
        """
        validate_key(key)
        fields: dict[str, str] = {
            "Content-Type": content_type,
            "x-amz-server-side-encryption": "aws:kms",
        }
        if self._kms_key_id:
            fields["x-amz-server-side-encryption-aws-kms-key-id"] = self._kms_key_id
        conditions: list[object] = [{name: value} for name, value in fields.items()]
        conditions.append(["content-length-range", 1, max_bytes])
        expires_at = datetime.now(UTC) + expires_in
        signed = self._s3.generate_presigned_post(
            Bucket=self._bucket,
            Key=key,
            Fields=fields,
            Conditions=conditions,
            ExpiresIn=int(expires_in.total_seconds()),
        )
        form = cast(Mapping[str, str], signed["fields"])
        return PresignedPost(url=str(signed["url"]), fields=dict(form), expires_at=expires_at)

    async def stat(self, key: str) -> StoredObject | None:
        validate_key(key)
        return await asyncio.to_thread(self._stat_sync, key)

    def _stat_sync(self, key: str) -> StoredObject | None:
        try:
            response = self._s3.head_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            if _is_missing(exc):
                return None
            raise
        size = response.get("ContentLength")
        content_type = response.get("ContentType")
        return StoredObject(
            size_bytes=size if isinstance(size, int) else 0,
            content_type=content_type if isinstance(content_type, str) else None,
        )


def _is_missing(exc: Exception) -> bool:
    response: object = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error: object = cast(dict[str, object], response).get("Error")
    if not isinstance(error, dict):
        return False
    code: object = cast(dict[str, object], error).get("Code")
    return code in ("NoSuchKey", "404", "NotFound")


def _boto3_s3(region: str, endpoint_url: str | None) -> _S3Api:
    import boto3  # pyright: ignore[reportMissingTypeStubs]
    from botocore.config import Config  # pyright: ignore[reportMissingTypeStubs]

    # The regional endpoint, virtual-hosted: presigned POST URLs then point at
    # `<bucket>.s3.<region>.amazonaws.com`, not the global endpoint, which can
    # answer a browser's upload with a redirect.
    return cast(
        _S3Api,
        boto3.client(  # pyright: ignore[reportUnknownMemberType]
            "s3",
            region_name=region,
            endpoint_url=endpoint_url or f"https://s3.{region}.amazonaws.com",
            config=Config(s3={"addressing_style": "virtual"}),
        ),
    )
