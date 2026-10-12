"""The factory's page (no login): its order, and what to send for each milestone.

  GET    /v1/factory/order                                           the order, by milestone
  POST   /v1/factory/order/accept                                    confirm the order
  POST   /v1/factory/order/milestones/{index}/documents              add one file (raw body)
  DELETE /v1/factory/order/milestones/{index}/documents/{document_id}
  GET    /v1/factory/order/milestones/{index}/documents/{document_id}/link
  POST   /v1/factory/order/milestones/{index}/submit                 send for approval

Every route takes the link's token in the `X-Factory-Token` header, and only there: never
in a path or a query string, which logs and proxies keep. A missing, unknown, revoked or
expired token answers 401, the same way every time. The factory confirms the order once
before its first upload; it sees its order, the amounts in the order's currency and what to
send, never the buyer's other data. Answers are not cached (`Cache-Control: no-store`).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Security, status
from fastapi.security import APIKeyHeader

from bido_back.api.milestones import (
    DOCUMENT_BODY,
    FACTORY_LINK_INVALID,
    Db,
    DeclaredType,
    DocType,
    Filename,
    LinkDisposition,
    Links,
    MilestoneIndex,
    Storage,
    declared_type,
    milestones_http_error,
    read_document_upload,
)
from bido_back.api.payments import PaymentSettings
from bido_back.api.rfqs import error_responses
from bido_back.logs import annotate
from bido_back.payments.errors import FactoryLinkInvalid, PaymentError
from bido_back.payments.milestones import documents, links, service
from bido_back.payments.milestones.links import FactoryAccess
from bido_back.payments.milestones.schemas import SubmitMilestoneRequest
from bido_back.payments.milestones.views import (
    FactoryAcceptedView,
    FactoryDocumentView,
    FactoryMilestoneView,
    FactoryOrderView,
)
from bido_back.rfqs.errors import RfqError
from bido_back.uploads.schemas import FileLinkView

FACTORY_LINK_HEADER = "X-Factory-Token"

_factory_token = APIKeyHeader(
    name=FACTORY_LINK_HEADER,
    scheme_name="FactoryToken",
    description="The factory link's token: the part of the link after `#`",
    auto_error=False,
)


async def get_factory_access(
    db: Db, token: Annotated[str | None, Security(_factory_token)]
) -> FactoryAccess:
    """The factory's access from its token; 401 for any token that does not open a link."""
    try:
        access = await links.resolve_factory_link(db, token)
    except FactoryLinkInvalid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, FACTORY_LINK_INVALID) from None
    # Every later log line of this request names the tenant and the link (ids only).
    annotate(tenant_id=access.tenant_id, factory_link_id=access.link_id)
    return access


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


CurrentFactory = Annotated[FactoryAccess, Depends(get_factory_access)]


def factory_router() -> APIRouter:
    router = APIRouter(prefix="/v1/factory", tags=["factory"], dependencies=[Depends(_no_store)])

    @router.get("/order", responses=error_responses())
    async def factory_order(  # pyright: ignore[reportUnusedFunction]
        access: CurrentFactory, db: Db, settings: PaymentSettings
    ) -> FactoryOrderView:
        """The factory's order: the amount reserved for it, each milestone's share and
        status, and the documents each milestone asks for with those already sent. Reading
        it changes nothing. 401: the link is not valid (ask the buyer for a new one)."""
        try:
            return await service.factory_order(db, access, settings=settings)
        except (PaymentError, RfqError) as exc:
            raise milestones_http_error(exc) from None

    @router.post("/order/accept", responses=error_responses())
    async def accept_factory_order(  # pyright: ignore[reportUnusedFunction]
        access: CurrentFactory, db: Db
    ) -> FactoryAcceptedView:
        """Confirm the order and its milestones; documents are sent only after that. Doing
        it again answers the first confirmation. 401: the link is not valid."""
        try:
            return await links.accept_order(db, access)
        except (PaymentError, RfqError) as exc:
            raise milestones_http_error(exc) from None

    @router.post(
        "/order/milestones/{index}/documents",
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
    async def add_factory_document(  # pyright: ignore[reportUnusedFunction]
        request: Request,
        access: CurrentFactory,
        index: MilestoneIndex,
        doc_type: DocType,
        db: Db,
        storage: Storage,
        settings: PaymentSettings,
        content_type: DeclaredType = "application/octet-stream",
        filename: Filename = None,
    ) -> FactoryDocumentView:
        """Send one file for a milestone (the raw bytes as the body). Sending the same file
        again answers the file already there. 401: the link is not valid; 404: no such
        milestone; 409: the order is not confirmed yet, the milestone is not open (under
        review or released), or the file was already sent as another type; 413: too big;
        415: not a kind this document takes, or not what its name says; 422: the milestone
        does not ask for that document, the file is empty, or it already has 30 files."""
        try:
            upload = await read_document_upload(
                request, settings, filename, fallback_filename=doc_type
            )
            added = await documents.add_document(
                db,
                storage,
                access,
                access.payment_id,
                index,
                doc_type=doc_type,
                filename=upload.filename,
                content_type=declared_type(content_type),
                data=upload.data,
                settings=settings,
            )
        except (PaymentError, RfqError) as exc:
            raise milestones_http_error(exc) from None
        return FactoryDocumentView(
            id=added.id,
            doc_type=added.doc_type,
            filename=added.filename,
            size_bytes=added.size_bytes,
            uploaded_at=added.uploaded_at,
        )

    @router.delete(
        "/order/milestones/{index}/documents/{document_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
    )
    async def remove_factory_document(  # pyright: ignore[reportUnusedFunction]
        access: CurrentFactory, index: MilestoneIndex, document_id: UUID, db: Db
    ) -> None:
        """Take back a file not yet sent for approval. 401: the link is not valid; 404: no
        such file; 409: the order is not confirmed, or the file was already sent."""
        try:
            await documents.remove_document(db, access, access.payment_id, index, document_id)
        except (PaymentError, RfqError) as exc:
            raise milestones_http_error(exc) from None

    @router.get(
        "/order/milestones/{index}/documents/{document_id}/link",
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_503_SERVICE_UNAVAILABLE),
    )
    async def factory_document_link(  # pyright: ignore[reportUnusedFunction]
        access: CurrentFactory,
        index: MilestoneIndex,
        document_id: UUID,
        db: Db,
        file_links: Links,
        disposition: LinkDisposition = "attachment",
    ) -> FileLinkView:
        """A 5-minute link to one of the order's files, under its name; photos and PDFs may
        be shown `inline`. 401: the link is not valid; 404: no such file."""
        try:
            return await documents.document_link(
                db,
                file_links,
                access.tenant_id,
                access.payment_id,
                index,
                document_id,
                disposition=disposition,
            )
        except (PaymentError, RfqError) as exc:
            raise milestones_http_error(exc) from None

    @router.post(
        "/order/milestones/{index}/submit",
        status_code=status.HTTP_202_ACCEPTED,
        responses=error_responses(
            status.HTTP_404_NOT_FOUND,
            status.HTTP_409_CONFLICT,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        ),
    )
    async def submit_factory_milestone(  # pyright: ignore[reportUnusedFunction]
        access: CurrentFactory,
        index: MilestoneIndex,
        body: SubmitMilestoneRequest,
        db: Db,
        settings: PaymentSettings,
    ) -> FactoryMilestoneView:
        """Send the milestone's files to the buyer for approval; the milestone is then
        `in_review`. 401: the link is not valid; 404: no such milestone; 409: the order is
        not confirmed, an earlier milestone is not released yet, or this one is already
        under review; 422: a required document is missing (the message names them); 503:
        not available on this server yet."""
        try:
            await service.submit_milestone(
                db, access, access.payment_id, index, body, settings=settings
            )
            return await service.factory_milestone(db, access, index, settings=settings)
        except (PaymentError, RfqError) as exc:
            raise milestones_http_error(exc) from None

    return router
