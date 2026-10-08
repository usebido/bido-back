"""`/v1/company`: the buyer company's branding on RFQ documents.

An RFQ goes out in the buyer's name: their logo, legal name, tax id, address
and terms. Bido shows only as a small "via Bido" line.

* `GET` / `PUT /v1/company/branding`: the company details and default footer
  (PUT replaces every field).
* `GET /v1/company/logos`: the logo library, newest first.
* `POST /v1/company/logos`: a form to upload a logo straight to storage (PNG,
  JPEG, WebP or SVG, 2 MB), then `POST /v1/company/logos/{id}/complete` checks
  and records it. The first logo becomes the default.
* `POST /v1/company/logos/{id}/default`, `PATCH /v1/company/logos/{id}` (rename),
  `DELETE /v1/company/logos/{id}`.
* `GET /v1/company/logos/{id}/file`: a short-lived inline link for an `<img>`.
* `POST /v1/company/logos/import-clerk`: copy the Clerk organization's image.

Any member of the company can edit these for now. An RFQ picks its own logo and
footer with `PATCH /v1/rfqs/{id}` (`logo_asset_id`, `footer_override`).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import (
    get_db,
    get_direct_uploads,
    get_download_links,
    get_principal,
    get_storage,
)
from bido_back.api.mailbox import get_mailbox_settings
from bido_back.api.rfqs import error_responses
from bido_back.auth import Principal
from bido_back.branding import service
from bido_back.branding.schemas import (
    BrandingView,
    CompleteLogoUploadRequest,
    LogoFileLink,
    LogoList,
    LogoUploadForm,
    LogoView,
    RenameLogoRequest,
    StartLogoUploadRequest,
    UpdateBrandingRequest,
)
from bido_back.settings import Settings
from bido_back.storage import BlobStorage
from bido_back.storage.direct import DirectUploads
from bido_back.storage.links import DownloadLinks

Db = Annotated[async_sessionmaker[AsyncSession], Depends(get_db)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
Storage = Annotated[BlobStorage, Depends(get_storage)]


def branding_http_error(exc: service.BrandingError) -> HTTPException:
    if isinstance(exc, service.NotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, service.ClerkImageUnavailable):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


def branding_router() -> APIRouter:
    router = APIRouter(prefix="/v1/company", tags=["company"])

    @router.get("/branding", responses=error_responses())
    async def get_branding(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, db: Db
    ) -> BrandingView:
        """The company's details on RFQ documents. Every field may be empty."""
        return await service.get_branding(db, principal)

    @router.put(
        "/branding",
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_422_UNPROCESSABLE_CONTENT),
    )
    async def put_branding(  # pyright: ignore[reportUnusedFunction]
        body: UpdateBrandingRequest, principal: CurrentPrincipal, db: Db
    ) -> BrandingView:
        """Replace the company's details; a missing or null field is cleared."""
        try:
            return await service.update_branding(db, principal, body)
        except service.BrandingError as exc:
            raise branding_http_error(exc) from None

    @router.get("/logos", responses=error_responses())
    async def list_logos(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, db: Db
    ) -> LogoList:
        return LogoList(logos=await service.list_logos(db, principal))

    @router.post(
        "/logos",
        status_code=status.HTTP_201_CREATED,
        responses=error_responses(status.HTTP_422_UNPROCESSABLE_CONTENT),
    )
    async def start_logo_upload(  # pyright: ignore[reportUnusedFunction]
        body: StartLogoUploadRequest,
        principal: CurrentPrincipal,
        direct: Annotated[DirectUploads, Depends(get_direct_uploads)],
    ) -> LogoUploadForm:
        """A form to upload one logo straight to storage; then call `complete`."""
        try:
            return await service.start_logo_upload(
                direct, principal, filename=body.filename, size_bytes=body.size_bytes
            )
        except service.BrandingError as exc:
            raise branding_http_error(exc) from None

    @router.post(
        "/logos/import-clerk",
        status_code=status.HTTP_201_CREATED,
        responses=error_responses(status.HTTP_409_CONFLICT, status.HTTP_422_UNPROCESSABLE_CONTENT),
    )
    async def import_clerk_logo(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal,
        db: Db,
        storage: Storage,
        settings: Annotated[Settings, Depends(get_mailbox_settings)],
    ) -> LogoView:
        """Copy the Clerk organization's image into the library and make it the default."""
        try:
            return await service.import_clerk_logo(
                db, storage, principal, clerk_secret_key=settings.clerk_secret_key
            )
        except service.BrandingError as exc:
            raise branding_http_error(exc) from None

    @router.post(
        "/logos/{logo_id}/complete",
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_422_UNPROCESSABLE_CONTENT),
    )
    async def complete_logo_upload(  # pyright: ignore[reportUnusedFunction]
        logo_id: UUID,
        body: CompleteLogoUploadRequest,
        principal: CurrentPrincipal,
        db: Db,
        storage: Storage,
    ) -> LogoView:
        """Check the uploaded file and add it to the library (404 if it is not stored)."""
        try:
            return await service.complete_logo_upload(db, storage, principal, logo_id, body)
        except service.BrandingError as exc:
            raise branding_http_error(exc) from None

    @router.patch("/logos/{logo_id}", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def rename_logo(  # pyright: ignore[reportUnusedFunction]
        logo_id: UUID, body: RenameLogoRequest, principal: CurrentPrincipal, db: Db
    ) -> LogoView:
        try:
            return await service.rename_logo(db, principal, logo_id, body.name)
        except service.BrandingError as exc:
            raise branding_http_error(exc) from None

    @router.post("/logos/{logo_id}/default", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def make_default_logo(  # pyright: ignore[reportUnusedFunction]
        logo_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> BrandingView:
        try:
            return await service.make_default_logo(db, principal, logo_id)
        except service.BrandingError as exc:
            raise branding_http_error(exc) from None

    @router.delete(
        "/logos/{logo_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_class=Response,
        responses=error_responses(status.HTTP_404_NOT_FOUND),
    )
    async def delete_logo(  # pyright: ignore[reportUnusedFunction]
        logo_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> None:
        """Remove it from the library; the default and RFQs using it fall back."""
        try:
            await service.delete_logo(db, principal, logo_id)
        except service.BrandingError as exc:
            raise branding_http_error(exc) from None

    @router.get("/logos/{logo_id}/file", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def logo_file(  # pyright: ignore[reportUnusedFunction]
        logo_id: UUID,
        principal: CurrentPrincipal,
        db: Db,
        links: Annotated[DownloadLinks, Depends(get_download_links)],
    ) -> LogoFileLink:
        """A short-lived link serving the image inline, for an `<img>` tag."""
        try:
            return await service.logo_link(db, links, principal, logo_id)
        except service.BrandingError as exc:
            raise branding_http_error(exc) from None

    return router
