"""A PO payment's milestones (Tool start, T1, PPAP): the buyer's side.

  GET    /v1/purchase-orders/{po_id}/payment/milestones                    each milestone
  POST   /v1/purchase-orders/{po_id}/payment/factory-link                  the factory's link
  DELETE /v1/purchase-orders/{po_id}/payment/factory-link                  stop it
  POST   /v1/purchase-orders/{po_id}/payment/milestones/{index}/documents  add one file
  DELETE /v1/purchase-orders/{po_id}/payment/milestones/{index}/documents/{document_id}
  GET    /v1/purchase-orders/{po_id}/payment/milestones/{index}/documents/{document_id}/link
  POST   /v1/purchase-orders/{po_id}/payment/milestones/{index}/submit     send for approval
  POST   /v1/purchase-orders/{po_id}/payment/milestones/{index}/request-changes
  POST   /v1/purchase-orders/{po_id}/payment/milestones/{index}/retry-release

Once the order's payment is funded, the factory sends each milestone's documents through
its link (`/v1/factory/...`), or a person of the company does it on its behalf here. Sending
a milestone creates a `release_po_milestone` approval that a person decides at
POST /v1/approvals/{approval_id}/decision; nothing here releases money. Any member of the
company may use these routes; another company's order answers 404.

Uploads take the file's raw bytes as the body (one file per request), with `doc_type` and
`filename` in the query; its first bytes decide what kind of file it is.
"""

from typing import Annotated, Final
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import get_db, get_download_links, get_job_queue, get_principal, get_storage
from bido_back.api.payments import PaymentSettings, payments_http_error
from bido_back.api.rfqs import error_responses
from bido_back.auth import Principal
from bido_back.payments.errors import FactoryLinkInvalid, PaymentError
from bido_back.payments.milestones import documents, execute, links, service
from bido_back.payments.milestones.documents import DocumentTooLarge, DocumentTypeRefused
from bido_back.payments.milestones.schemas import (
    RequestMilestoneChangesRequest,
    SubmitMilestoneRequest,
)
from bido_back.payments.milestones.views import (
    FactoryLinkView,
    MilestoneDocumentView,
    MilestoneSubmitted,
    PaymentMilestoneView,
)
from bido_back.rfqs.errors import RfqError
from bido_back.settings import Settings
from bido_back.storage import BlobStorage
from bido_back.storage.links import Disposition, DownloadLinks
from bido_back.uploads.schemas import FileLinkView
from bido_back.worker.jobs import JobQueue

Db = Annotated[async_sessionmaker[AsyncSession], Depends(get_db)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
Storage = Annotated[BlobStorage, Depends(get_storage)]
Links = Annotated[DownloadLinks, Depends(get_download_links)]
Jobs = Annotated[JobQueue, Depends(get_job_queue)]

MilestoneIndex = Annotated[
    int, Path(ge=0, le=3, description="The milestone's position in the escrow: 0 is the first")
]
DocType = Annotated[
    str,
    Query(
        min_length=1,
        max_length=48,
        pattern=r"^[a-z][a-z0-9_]{0,47}$",
        description="What the file is: one of the milestone's `requirements[].doc_type`",
    ),
]
Filename = Annotated[
    str,
    Query(min_length=1, max_length=255, description="The file's name, with its extension"),
]
DeclaredType = Annotated[
    str,
    Header(
        alias="Content-Type",
        description="`application/octet-stream` or the file's own type; never trusted: the "
        "file's first bytes decide its kind",
    ),
]
LinkDisposition = Annotated[
    Disposition,
    Query(description="`inline` shows a PDF or an image in the browser; anything else downloads"),
]

# The raw file as the request body (python-multipart is not a dependency).
DOCUMENT_BODY: Final[dict[str, object]] = {
    "requestBody": {
        "required": True,
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
    }
}
FACTORY_LINK_INVALID: Final = "this link is not valid: ask the buyer for a new one"


def milestones_http_error(exc: PaymentError | RfqError) -> HTTPException:
    """`payments_http_error`, plus 401 for a factory link that stopped, 413 for a file too
    big and 415 for a kind of file the document does not take."""
    if isinstance(exc, FactoryLinkInvalid):
        return HTTPException(status.HTTP_401_UNAUTHORIZED, FACTORY_LINK_INVALID)
    if isinstance(exc, DocumentTooLarge):
        return HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, str(exc))
    if isinstance(exc, DocumentTypeRefused):
        return HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(exc))
    return payments_http_error(exc)


async def read_document_body(request: Request, settings: Settings) -> bytes:
    """The request body, refused (413) as soon as it passes the document size limit."""
    limit = settings.milestone_document_max_bytes
    too_large = DocumentTooLarge(f"files are limited to {limit // (1024 * 1024)} MB")
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise too_large
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise too_large
        chunks.append(chunk)
    return b"".join(chunks)


def declared_type(value: str) -> str:
    return value.split(";", 1)[0].strip()[:120] or "application/octet-stream"


def milestones_router() -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["payments"])

    @router.get(
        "/purchase-orders/{po_id}/payment/milestones",
        responses=error_responses(status.HTTP_404_NOT_FOUND),
    )
    async def list_payment_milestones(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, po_id: UUID, db: Db, settings: PaymentSettings
    ) -> list[PaymentMilestoneView]:
        """The milestones of the order's payment, in order: each one's share of the order,
        of the card charge and of the escrow, the documents it asks for, its submissions
        (newest first) and where its release stands. A milestone is `locked` until the
        payment is funded and every earlier one is released. 404: no such order, or it has
        no payment."""
        try:
            return await service.list_milestones(db, principal, po_id, settings=settings)
        except (PaymentError, RfqError) as exc:
            raise milestones_http_error(exc) from None

    # --- The factory's link ---------------------------------------------------------------

    @router.post(
        "/purchase-orders/{po_id}/payment/factory-link",
        status_code=status.HTTP_201_CREATED,
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
    )
    async def issue_factory_link(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, po_id: UUID, db: Db, settings: PaymentSettings
    ) -> FactoryLinkView:
        """A link for the factory to follow the order and send each milestone's documents,
        with no login. The token is in this answer only (Bido keeps its hash): share `url`,
        which carries it after `#`. Issuing a new link stops the previous one; the factory
        confirms the order again on the new one. 404: no such order, or no payment; 409:
        the payment is not funded yet."""
        try:
            return await links.issue_factory_link(db, principal, po_id, settings=settings)
        except (PaymentError, RfqError) as exc:
            raise milestones_http_error(exc) from None

    @router.delete(
        "/purchase-orders/{po_id}/payment/factory-link",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        responses=error_responses(status.HTTP_404_NOT_FOUND),
    )
    async def revoke_factory_link(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, po_id: UUID, db: Db
    ) -> None:
        """Stop the factory's link now: it answers 401 from then on. Nothing to stop is
        fine. 404: no such order, or no payment."""
        try:
            await links.revoke_factory_link(db, principal, po_id)
        except (PaymentError, RfqError) as exc:
            raise milestones_http_error(exc) from None

    # --- Documents ----------------------------------------------------------------------------

    @router.post(
        "/purchase-orders/{po_id}/payment/milestones/{index}/documents",
        status_code=status.HTTP_201_CREATED,
        responses=error_responses(
            status.HTTP_404_NOT_FOUND,
            status.HTTP_409_CONFLICT,
            status.HTTP_413_CONTENT_TOO_LARGE,
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        ),
        openapi_extra=DOCUMENT_BODY,
    )
    async def add_milestone_document(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        principal: CurrentPrincipal,
        po_id: UUID,
        index: MilestoneIndex,
        doc_type: DocType,
        filename: Filename,
        db: Db,
        storage: Storage,
        settings: PaymentSettings,
        content_type: DeclaredType = "application/octet-stream",
    ) -> MilestoneDocumentView:
        """Add one file to the milestone's documents on the factory's behalf (the raw bytes
        as the body). The same file again answers the document already there. 404: no such
        order, payment or milestone; 409: the payment is not funded, the milestone is not
        open (under review or released), or the file is already there as another type;
        413: too big; 415: not a kind this document type takes (PDF, images, xlsx, video),
        or not what its name says; 422: the milestone does not ask for that document
        type, the file is empty, or the milestone already has 30 files."""
        try:
            payment_id = await service.payment_id_for(db, principal, po_id)
            data = await read_document_body(request, settings)
            return await documents.add_document(
                db,
                storage,
                principal,
                payment_id,
                index,
                doc_type=doc_type,
                filename=filename,
                content_type=declared_type(content_type),
                data=data,
                settings=settings,
            )
        except (PaymentError, RfqError) as exc:
            raise milestones_http_error(exc) from None

    @router.delete(
        "/purchase-orders/{po_id}/payment/milestones/{index}/documents/{document_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
    )
    async def remove_milestone_document(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal,
        po_id: UUID,
        index: MilestoneIndex,
        document_id: UUID,
        db: Db,
    ) -> None:
        """Take a file out of the milestone's documents before they are sent. 404: no such
        file; 409: it was already sent for approval, or the milestone is not open."""
        try:
            payment_id = await service.payment_id_for(db, principal, po_id)
            await documents.remove_document(db, principal, payment_id, index, document_id)
        except (PaymentError, RfqError) as exc:
            raise milestones_http_error(exc) from None

    @router.get(
        "/purchase-orders/{po_id}/payment/milestones/{index}/documents/{document_id}/link",
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_503_SERVICE_UNAVAILABLE),
    )
    async def milestone_document_link(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal,
        po_id: UUID,
        index: MilestoneIndex,
        document_id: UUID,
        db: Db,
        file_links: Links,
        disposition: LinkDisposition = "attachment",
    ) -> FileLinkView:
        """A 5-minute link to one of the milestone's files, under its name. PDFs and images
        may be shown `inline`; anything else always downloads. 404: no such file; 503: this
        server cannot sign file links."""
        try:
            payment_id = await service.payment_id_for(db, principal, po_id)
            return await documents.document_link(
                db,
                file_links,
                principal.tenant_id,
                payment_id,
                index,
                document_id,
                disposition=disposition,
            )
        except (PaymentError, RfqError) as exc:
            raise milestones_http_error(exc) from None

    # --- Review ---------------------------------------------------------------------------------

    @router.post(
        "/purchase-orders/{po_id}/payment/milestones/{index}/submit",
        status_code=status.HTTP_202_ACCEPTED,
        responses=error_responses(
            status.HTTP_404_NOT_FOUND,
            status.HTTP_409_CONFLICT,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ),
    )
    async def submit_milestone(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal,
        po_id: UUID,
        index: MilestoneIndex,
        body: SubmitMilestoneRequest,
        db: Db,
        settings: PaymentSettings,
    ) -> MilestoneSubmitted:
        """Send the milestone's documents for approval on the factory's behalf: a
        `release_po_milestone` approval, requested in the name of the person who asked for
        the payment, that a person decides at POST /v1/approvals/{approval_id}/decision.
        Approved, the worker releases the milestone's share of the escrow. 404: no such
        order, payment or milestone; 409: the payment is not funded, an earlier milestone
        is not released yet, or this one is already under review or released; 422: a
        required document type is missing (the message names them); 503: releases are not
        set up on this server."""
        try:
            payment_id = await service.payment_id_for(db, principal, po_id)
            return await service.submit_milestone(
                db, principal, payment_id, index, body, settings=settings
            )
        except (PaymentError, RfqError) as exc:
            raise milestones_http_error(exc) from None

    @router.post(
        "/purchase-orders/{po_id}/payment/milestones/{index}/request-changes",
        responses=error_responses(
            status.HTTP_404_NOT_FOUND,
            status.HTTP_409_CONFLICT,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ),
    )
    async def request_milestone_changes(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal,
        po_id: UUID,
        index: MilestoneIndex,
        body: RequestMilestoneChangesRequest,
        db: Db,
        jobs: Jobs,
        settings: PaymentSettings,
    ) -> PaymentMilestoneView:
        """Refuse the milestone under review and tell the factory what to change: its
        approval is rejected and the milestone goes back to the factory, which sees
        `reason_code` and `reason` and sends again (its files carry over). Decided like any
        approval: by the person who asked for the payment when the company allows
        self-approval, else by an approver or owner who did not. 403: you may not decide
        it; 404: no such order, payment or milestone; 409: the milestone is not under
        review (or was just decided)."""
        try:
            return await service.request_milestone_changes(
                db,
                principal,
                po_id,
                index,
                body.reason,
                reason_code=body.reason_code,
                jobs=jobs,
                settings=settings,
            )
        except (PaymentError, RfqError) as exc:
            raise milestones_http_error(exc) from None

    @router.post(
        "/purchase-orders/{po_id}/payment/milestones/{index}/retry-release",
        status_code=status.HTTP_202_ACCEPTED,
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
    )
    async def retry_milestone_release(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal,
        po_id: UUID,
        index: MilestoneIndex,
        db: Db,
        jobs: Jobs,
        settings: PaymentSettings,
    ) -> PaymentMilestoneView:
        """Release again a milestone whose release failed (`status` `failed`), once what
        failed is fixed, e.g. the gate attester's key or its authorization. No new decision
        is needed: only while its approval still vouches for the same files and terms, the
        payment is funded and every earlier milestone is released. The worker asks the chain
        first, so a milestone released meanwhile is only recorded. 404: no such order,
        payment or milestone; 409: its release did not fail, or its approval no longer
        vouches for it (send the milestone again)."""
        try:
            await execute.retry_release(db, principal, po_id, index, jobs=jobs)
            return (await service.list_milestones(db, principal, po_id, settings=settings))[index]
        except (PaymentError, RfqError) as exc:
            raise milestones_http_error(exc) from None

    return router
