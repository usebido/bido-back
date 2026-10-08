"""`/v1/files`: the company file library (`bido_back.files`).

* `GET /v1/files`: buyers' uploads and suppliers' reply attachments, newest
  first; `q` matches the name or the content (with a snippet), plus filters.
* `GET /v1/files/{id}/download` and `/preview`: as `/v1/uploads/{id}/...`, for
  any library file the caller may read.
* `POST /v1/files/{id}/attach`: an upload id the caller owns for this file,
  for a Copilot message's `attachment_ids` (no copy of the bytes).

Every member of a company reads every file of it (`bido_back.files.policy`);
anything else answers 404.
"""

from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import (
    get_db,
    get_download_links,
    get_optional_job_queue,
    get_principal,
    get_storage,
)
from bido_back.api.rfqs import error_responses
from bido_back.auth import Principal
from bido_back.files import index, service
from bido_back.storage import BlobStorage
from bido_back.storage.links import DownloadLinks
from bido_back.uploads import files as stored
from bido_back.uploads.schemas import FileLinkView, UploadPreview
from bido_back.worker.jobs import JobQueue

Db = Annotated[async_sessionmaker[AsyncSession], Depends(get_db)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]
Links = Annotated[DownloadLinks, Depends(get_download_links)]
Jobs = Annotated[JobQueue | None, Depends(get_optional_job_queue)]


def files_router() -> APIRouter:
    router = APIRouter(prefix="/v1/files", tags=["files"])

    @router.get("", responses=error_responses(status.HTTP_422_UNPROCESSABLE_CONTENT))
    async def list_files(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal,
        db: Db,
        jobs: Jobs,
        q: Annotated[
            str | None,
            Query(max_length=service.MAX_QUERY, description="Matches the file name or its content"),
        ] = None,
        kind: Annotated[service.FileKind | None, Query()] = None,
        type: Annotated[  # noqa: A002 - the query parameter's public name
            service.FileType | None, Query(description="spreadsheet, pdf, doc, image, other")
        ] = None,
        created_from: Annotated[date | None, Query(alias="from")] = None,
        created_to: Annotated[date | None, Query(alias="to")] = None,
        rfq_id: Annotated[UUID | None, Query()] = None,
        supplier_id: Annotated[UUID | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=service.MAX_LIMIT)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> service.FileList:
        """The company's files, newest first."""
        filters = service.FileFilters(
            q=q,
            kind=kind,
            file_type=type,
            created_from=created_from,
            created_to=created_to,
            rfq_id=rfq_id,
            supplier_id=supplier_id,
        )
        result = await service.list_files(db, principal, filters, limit=limit, offset=offset)
        # Files read before this job existed, or a lost request: read them now.
        if jobs is not None and any(not item.indexed for item in result.items):
            await index.request_index(jobs, principal.tenant_id, "listing")
        return result

    @router.get("/{file_id}/download", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def download_file(  # pyright: ignore[reportUnusedFunction]
        file_id: UUID, principal: CurrentPrincipal, db: Db, links: Links
    ) -> FileLinkView:
        """A short-lived link that downloads the file under its original name."""
        try:
            found = await service.get_file(db, principal, file_id)
        except service.FileNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found") from None
        return await stored.download_link_for(links, found)

    @router.get("/{file_id}/preview", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def preview_file(  # pyright: ignore[reportUnusedFunction]
        file_id: UUID,
        principal: CurrentPrincipal,
        db: Db,
        links: Links,
        storage: Annotated[BlobStorage, Depends(get_storage)],
        sheet: Annotated[
            str | None, Query(max_length=255, description="Page this sheet only")
        ] = None,
        offset: Annotated[
            int, Query(ge=0, le=stored.COUNT_ROWS_LIMIT, description="First row of the page")
        ] = 0,
        limit: Annotated[
            int, Query(ge=1, le=stored.PREVIEW_ROWS_MAX, description="Rows in the page")
        ] = stored.PREVIEW_ROWS,
    ) -> UploadPreview:
        """What to show for the file: a table, an inline link, or nothing (download only)."""
        try:
            found = await service.get_file(db, principal, file_id)
            page = stored.PageRequest(sheet=sheet, offset=offset, limit=limit)
            return await stored.preview_for(storage, links, found, page)
        except service.FileNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found") from None
        except stored.SheetNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "sheet not found") from None

    @router.post(
        "/{file_id}/attach",
        responses=error_responses(status.HTTP_404_NOT_FOUND),
    )
    async def attach_file(  # pyright: ignore[reportUnusedFunction]
        file_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> service.AttachedFile:
        """An upload id the caller owns for this file, for a Copilot message's `attachment_ids`."""
        try:
            return await service.attach(db, principal, file_id)
        except service.FileNotFound:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "file not found") from None

    return router
