"""Local-only dev endpoints: `POST /dev/inbound`, `POST /dev/followups/sweep`,
`POST /dev/negotiation/sweep`, `POST /dev/uploads` and `GET /dev/files`.

`POST /dev/inbound` simulates an inbound email locally.

Stands in for SES inbound -> S3 -> SQS: the body is a raw .eml, either as the
request body itself or as the first file of a multipart/form-data upload
(`curl -F file=@reply.eml`), and it goes through the same
`InboundService.ingest` the intake consumer calls.

`POST /dev/uploads` stands in for S3 for direct uploads: it takes the form
signed by `LocalDirectUploads` (multipart/form-data, the fields then `file`)
and answers 204 like S3, or 403 when the form does not match its signature.

`GET /dev/files` stands in for S3 presigned GETs: it serves the file a link
signed by `LocalDownloadLinks` points at, with the content type and
disposition the link fixed, or 403 when the link does not verify.

Local only, twice over: mount the router only when
`settings.environment == "local"`, and every request is refused with 404
unless the settings say "local" (in case it is mounted by mistake). It is left
out of the OpenAPI schema, so `scripts/export_openapi.py` (which builds the app
with environment "local") does not put it in the public contract.

Dependencies, overridable with `app.dependency_overrides`:
  * `get_dev_settings`: the settings (default: `get_settings()`);
  * `get_inbound_service`: the service (default: the app-role sessionmaker
    from `api.deps.get_db`, storage from settings, and QUOTE_EXTRACT jobs to
    `SQS_QUEUE_URL`, honouring `SQS_ENDPOINT_URL` for ElasticMQ).
"""

from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import get_db, get_job_queue, get_storage
from bido_back.intake.service import InboundService
from bido_back.onboarding.service import MAX_DIRECT_UPLOAD_BYTES
from bido_back.settings import Settings, get_settings
from bido_back.storage import BlobNotFound, BlobStorage
from bido_back.storage.direct import InvalidUploadForm, LocalDirectUploads
from bido_back.storage.links import InvalidFileLink, LocalDownloadLinks
from bido_back.storage.local import LocalFileStorage
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import FOLLOWUPS_SWEEP, NEGOTIATION_SWEEP
from bido_back.worker.jobs import JobQueue

MAX_EML_BYTES = 25 * 1024 * 1024


class InboundResponse(BaseModel):
    outcome: Literal["stored", "quarantined", "duplicate", "unmatched"]
    tenant_id: UUID | None
    invite_id: UUID | None
    message_id: UUID | None
    reason: str | None
    matched_by: str | None
    extraction_enqueued: bool


def get_dev_settings() -> Settings:
    return get_settings()


def require_local(settings: Annotated[Settings, Depends(get_dev_settings)]) -> Settings:
    if settings.environment != "local":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    return settings


def get_inbound_service(
    settings: Annotated[Settings, Depends(require_local)],
    db: Annotated[async_sessionmaker[AsyncSession], Depends(get_db)],
) -> InboundService:
    from bido_back.intake.consumer import build_inbound_service

    try:
        return build_inbound_service(settings, db)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None


dev_router = APIRouter(prefix="/dev", tags=["dev"], include_in_schema=False)


@dev_router.post("/inbound", dependencies=[Depends(require_local)])
async def post_inbound(
    request: Request,
    service: Annotated[InboundService, Depends(get_inbound_service)],
) -> InboundResponse:
    raw = await _read_eml(request)
    result = await service.ingest(raw)
    return InboundResponse(
        outcome=result.outcome,
        tenant_id=result.tenant_id,
        invite_id=result.invite_id,
        message_id=result.message_id,
        reason=result.reason,
        matched_by=result.matched_by,
        extraction_enqueued=result.extraction_enqueued,
    )


@dev_router.post("/followups/sweep", dependencies=[Depends(require_local)])
async def post_followups_sweep(
    jobs: Annotated[JobQueue, Depends(get_job_queue)],
) -> dict[str, str]:
    """Run the follow-up sweep now for every tenant (in AWS it runs on a schedule)."""
    key = f"followups.sweep:dev:{uuid4()}"
    await jobs.enqueue(Envelope(type=FOLLOWUPS_SWEEP, tenant_id=UUID(int=0), idempotency_key=key))
    return {"enqueued": key}


@dev_router.post("/negotiation/sweep", dependencies=[Depends(require_local)])
async def post_negotiation_sweep(
    jobs: Annotated[JobQueue, Depends(get_job_queue)],
) -> dict[str, str]:
    """Run the negotiation sweep now for every tenant (in AWS it runs on a schedule)."""
    key = f"negotiation.sweep:dev:{uuid4()}"
    await jobs.enqueue(Envelope(type=NEGOTIATION_SWEEP, tenant_id=UUID(int=0), idempotency_key=key))
    return {"enqueued": key}


@dev_router.post(
    "/uploads", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_local)]
)
async def post_upload(
    request: Request, storage: Annotated[BlobStorage, Depends(get_storage)]
) -> None:
    """Receive a direct upload signed by `LocalDirectUploads` (the S3 stand-in)."""
    if not isinstance(storage, LocalFileStorage):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "expected multipart/form-data")
    body = await _read_capped(request, MAX_DIRECT_UPLOAD_BYTES + 64 * 1024)
    fields, data = _form_fields_and_file(content_type, body)
    try:
        await LocalDirectUploads(storage, str(request.base_url)).receive(fields, data)
    except InvalidUploadForm as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None


@dev_router.get("/files", dependencies=[Depends(require_local)])
async def get_file(
    storage: Annotated[BlobStorage, Depends(get_storage)],
    token: Annotated[str, Query()] = "",
    signature: Annotated[str, Query()] = "",
) -> Response:
    """Serve a file through a link signed by `LocalDownloadLinks` (the S3 stand-in)."""
    if not isinstance(storage, LocalFileStorage):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")
    try:
        key, content_type, disposition = LocalDownloadLinks.verify(token, signature)
        data = await storage.get(key)
    except InvalidFileLink as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None
    except BlobNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found") from None
    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": disposition, "X-Content-Type-Options": "nosniff"},
    )


async def _read_capped(request: Request, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "upload too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _form_fields_and_file(content_type: str, body: bytes) -> tuple[dict[str, str], bytes]:
    """The text fields and the `file` part of a multipart/form-data body (stdlib parser)."""
    envelope = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    form = BytesParser(policy=policy.default).parsebytes(envelope)
    fields: dict[str, str] = {}
    data: bytes | None = None
    for part in form.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not isinstance(name, str):
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        if name == "file":
            data = payload
        else:
            fields[name] = payload.decode()
    if data is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "no file in the form")
    return fields, data


async def _read_eml(request: Request) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_EML_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "email too large")
        chunks.append(chunk)
    body = b"".join(chunks)
    content_type = request.headers.get("content-type", "")
    if content_type.lower().startswith("multipart/form-data"):
        body = _first_form_file(content_type, body)
    if not body.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "empty email")
    return body


def _first_form_file(content_type: str, body: bytes) -> bytes:
    """The first file in a multipart/form-data body, parsed with the stdlib.

    (FastAPI's own form parsing needs the python-multipart package, which this
    dev-only endpoint does not justify adding.)
    """
    envelope = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
    form = BytesParser(policy=policy.default).parsebytes(envelope)
    for part in form.iter_parts():
        if part.get_filename() is not None or part.get_param("name", header="content-disposition"):
            if part.get_content_type() == "message/rfc822":
                inner = part.get_payload(0)
                return inner.as_bytes() if isinstance(inner, EmailMessage) else b""
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes) and payload:
                return payload
    raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "no file in the form")
