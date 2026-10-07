"""`/v1/onboarding`: the first-run screens (connect e-mail, then the RFQ template).

The mailbox itself is connected through `/v1/mailbox`. Here:

* `GET  /v1/onboarding`: done or not, the connected mailbox, the default template.
* `POST /v1/onboarding/scans`: start the one-off scan of the caller's SENT mail
  for old RFQs (409 without a connected mailbox); poll `GET /scans/{id}` for
  progress and the candidates.
* `POST /v1/onboarding/uploads?filename=<name>`: one file per request, the raw
  bytes as the body (xlsx, xls, csv, pdf, docx; 10 MB).
* `POST /v1/onboarding/templates`: build a template from picked candidates
  (`search`), uploads (`upload`), or take Bido's standard one (`none`, ready at
  once); poll `GET /templates/{id}` until `ready` or `failed`.
* `POST /v1/onboarding/templates/{id}/use`: make it the tenant's default.
* `POST /v1/onboarding/complete`: onboarding done for the caller.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import get_db, get_job_queue, get_principal, get_storage
from bido_back.api.rfqs import error_responses
from bido_back.auth import Principal
from bido_back.onboarding import service
from bido_back.onboarding.schemas import (
    CreateTemplateRequest,
    OnboardingState,
    Provider,
    ScanView,
    TemplateView,
    UploadView,
)
from bido_back.storage import BlobStorage
from bido_back.worker.jobs import JobQueue

Db = Annotated[async_sessionmaker[AsyncSession], Depends(get_db)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
Jobs = Annotated[JobQueue, Depends(get_job_queue)]


class StartScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Which connected mailbox to scan; the most recently connected one if omitted.
    provider: Provider | None = None


def onboarding_http_error(exc: service.OnboardingError) -> HTTPException:
    if isinstance(exc, service.NotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, service.UnsupportedUpload):
        return HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc))
    if isinstance(exc, service.UploadTooLarge):
        return HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(exc))
    if isinstance(exc, service.InvalidRequest):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))
    if isinstance(exc, service.Conflict):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "onboarding operation failed")


async def _read_capped(request: Request, limit: int) -> bytes:
    """The request body, refusing it as soon as it passes `limit` bytes."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise service.UploadTooLarge("files are limited to 10 MB")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise service.UploadTooLarge("files are limited to 10 MB")
        chunks.append(chunk)
    return b"".join(chunks)


def onboarding_router() -> APIRouter:
    router = APIRouter(prefix="/v1/onboarding", tags=["onboarding"])

    @router.get("", responses=error_responses())
    async def state(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, db: Db
    ) -> OnboardingState:
        return await service.get_state(db, principal)

    @router.post("/complete", responses=error_responses())
    async def complete(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, db: Db
    ) -> OnboardingState:
        return await service.complete(db, principal)

    @router.post(
        "/scans",
        status_code=status.HTTP_202_ACCEPTED,
        responses=error_responses(status.HTTP_409_CONFLICT, status.HTTP_503_SERVICE_UNAVAILABLE),
    )
    async def start_scan(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, db: Db, jobs: Jobs, body: StartScanRequest | None = None
    ) -> ScanView:
        """Start (or join, if one is running) the scan of the caller's sent mail."""
        try:
            return await service.start_scan(
                db, principal, jobs, body.provider if body is not None else None
            )
        except service.OnboardingError as exc:
            raise onboarding_http_error(exc) from None

    @router.get("/scans/{scan_id}", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def get_scan(  # pyright: ignore[reportUnusedFunction]
        scan_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> ScanView:
        try:
            return await service.get_scan(db, principal, scan_id)
        except service.OnboardingError as exc:
            raise onboarding_http_error(exc) from None

    @router.post(
        "/uploads",
        status_code=status.HTTP_201_CREATED,
        responses=error_responses(
            status.HTTP_413_CONTENT_TOO_LARGE,
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        ),
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                },
            }
        },
    )
    async def upload(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        principal: CurrentPrincipal,
        db: Db,
        storage: Annotated[BlobStorage, Depends(get_storage)],
        filename: Annotated[str, Query(min_length=1, max_length=255)],
        content_type: Annotated[str, Header(alias="Content-Type")] = "application/octet-stream",
    ) -> UploadView:
        """One file per request: the raw bytes as the body, its name in `filename`."""
        try:
            if service.upload_extension(filename) not in service.UPLOAD_EXTENSIONS:
                raise service.UnsupportedUpload("accepted files: xlsx, xls, csv, pdf, docx")
            data = await _read_capped(request, service.MAX_UPLOAD_BYTES)
            return await service.store_upload(
                db,
                storage,
                principal,
                filename=filename,
                content_type=content_type.split(";", 1)[0].strip()[:120],
                data=data,
            )
        except service.OnboardingError as exc:
            raise onboarding_http_error(exc) from None

    @router.post(
        "/templates",
        status_code=status.HTTP_202_ACCEPTED,
        responses=error_responses(
            status.HTTP_404_NOT_FOUND,
            status.HTTP_409_CONFLICT,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ),
    )
    async def create_template(  # pyright: ignore[reportUnusedFunction]
        body: CreateTemplateRequest, principal: CurrentPrincipal, db: Db, jobs: Jobs
    ) -> TemplateView:
        """Build a template; `none` is ready at once, the others are built by the worker."""
        try:
            return await service.create_template(db, principal, jobs, body)
        except service.OnboardingError as exc:
            raise onboarding_http_error(exc) from None

    @router.get("/templates/{template_id}", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def get_template(  # pyright: ignore[reportUnusedFunction]
        template_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> TemplateView:
        try:
            return await service.get_template(db, principal, template_id)
        except service.OnboardingError as exc:
            raise onboarding_http_error(exc) from None

    @router.post(
        "/templates/{template_id}/use",
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
    )
    async def use_template(  # pyright: ignore[reportUnusedFunction]
        template_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> TemplateView:
        try:
            return await service.use_template(db, principal, template_id)
        except service.OnboardingError as exc:
            raise onboarding_http_error(exc) from None

    return router
