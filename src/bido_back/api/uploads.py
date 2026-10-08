"""`/v1/uploads/{id}`: read back a file the caller uploaded (a chat attachment, say).

* `GET /v1/uploads/{id}/download`: a link (5 minutes) that downloads the file
  with its original name. The browser opens the link itself; it never renders
  the file (always `Content-Disposition: attachment`).
* `GET /v1/uploads/{id}/preview`: spreadsheets (xlsx, csv) one page at a time,
  every column: the first rows of every sheet, or `limit` rows of `sheet` from
  `offset` (404 for a sheet the file does not have); PDF and images as a link
  (5 minutes) served inline with their exact type, for an iframe or an img;
  anything else (docx, old .xls) has no preview and is only downloaded.

Both answer 404 for another user's upload, another tenant's, or an unknown id.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import get_db, get_download_links, get_principal, get_storage
from bido_back.api.rfqs import error_responses
from bido_back.auth import Principal
from bido_back.storage import BlobStorage
from bido_back.storage.links import DownloadLinks
from bido_back.uploads import files
from bido_back.uploads.schemas import FileLinkView, UploadPreview

Db = Annotated[async_sessionmaker[AsyncSession], Depends(get_db)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
Links = Annotated[DownloadLinks, Depends(get_download_links)]


def uploads_router() -> APIRouter:
    router = APIRouter(prefix="/v1/uploads", tags=["uploads"])

    @router.get(
        "/{upload_id}/download",
        responses=error_responses(status.HTTP_404_NOT_FOUND),
    )
    async def download_upload(  # pyright: ignore[reportUnusedFunction]
        upload_id: UUID, principal: CurrentPrincipal, db: Db, links: Links
    ) -> FileLinkView:
        """A short-lived link that downloads the file under its original name."""
        try:
            return await files.download_link(db, links, principal, upload_id)
        except files.UploadNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "upload not found") from None

    @router.get(
        "/{upload_id}/preview",
        responses=error_responses(status.HTTP_404_NOT_FOUND),
    )
    async def preview_upload(  # pyright: ignore[reportUnusedFunction]
        upload_id: UUID,
        principal: CurrentPrincipal,
        db: Db,
        links: Links,
        storage: Annotated[BlobStorage, Depends(get_storage)],
        sheet: Annotated[
            str | None, Query(max_length=255, description="Page this sheet only")
        ] = None,
        offset: Annotated[
            int, Query(ge=0, le=files.COUNT_ROWS_LIMIT, description="First row of the page")
        ] = 0,
        limit: Annotated[
            int, Query(ge=1, le=files.PREVIEW_ROWS_MAX, description="Rows in the page")
        ] = files.PREVIEW_ROWS,
    ) -> UploadPreview:
        """What to show for the file: a table, an inline link, or nothing (download only)."""
        page = files.PageRequest(sheet=sheet, offset=offset, limit=limit)
        try:
            return await files.preview(db, storage, links, principal, upload_id, page)
        except files.UploadNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "upload not found") from None
        except files.SheetNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "sheet not found") from None

    return router
