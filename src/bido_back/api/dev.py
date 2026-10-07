"""Local-only dev endpoints: `POST /dev/inbound` and `POST /dev/followups/sweep`.

`POST /dev/inbound` simulates an inbound email locally.

Stands in for SES inbound -> S3 -> SQS: the body is a raw .eml, either as the
request body itself or as the first file of a multipart/form-data upload
(`curl -F file=@reply.eml`), and it goes through the same
`InboundService.ingest` the intake consumer calls.

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

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import get_db, get_job_queue
from bido_back.intake.service import InboundService
from bido_back.settings import Settings, get_settings
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import FOLLOWUPS_SWEEP
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
