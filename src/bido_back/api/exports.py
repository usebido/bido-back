"""Excel / CSV downloads: `/v1/rfqs.{xlsx,csv}` and `/v1/rfqs/{id}/comparison.{xlsx,csv}`.

The file is built in memory (at most a few hundred RFQs, or one comparison)
and sent with a download filename.
"""

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import get_db, get_principal
from bido_back.api.rfqs import error_responses, rfq_http_error
from bido_back.auth import Principal
from bido_back.exports import service
from bido_back.exports.render import MEDIA_TYPES, ExportFormat, render
from bido_back.rfqs.errors import RfqError

Db = Annotated[async_sessionmaker[AsyncSession], Depends(get_db)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]

_BINARY: dict[str, Any] = {"schema": {"type": "string", "format": "binary"}}
_FILE_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_200_OK: {
        "content": {media: _BINARY for media in MEDIA_TYPES.values()},
        "description": "The file, as an attachment",
    }
}


def _download(data: bytes, fmt: ExportFormat, filename: str) -> Response:
    return Response(
        content=data,
        media_type=MEDIA_TYPES[fmt],
        headers={"Content-Disposition": f'attachment; filename="{filename}.{fmt}"'},
    )


def exports_router() -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["exports"])

    @router.get(
        "/rfqs.{fmt}",
        response_class=Response,
        responses={**_FILE_RESPONSE, **error_responses()},
    )
    async def export_rfqs(  # pyright: ignore[reportUnusedFunction]
        fmt: ExportFormat, principal: CurrentPrincipal, db: Db
    ) -> Response:
        """The RFQ list (newest 500) with suppliers, responses and the best landed cost."""
        table = await service.export_rfq_list(db, principal, datetime.now(UTC).date())
        return _download(render(table, fmt, "RFQs"), fmt, "rfqs")

    @router.get(
        "/rfqs/{rfq_id}/comparison.{fmt}",
        response_class=Response,
        responses={**_FILE_RESPONSE, **error_responses(status.HTTP_404_NOT_FOUND)},
    )
    async def export_comparison(  # pyright: ignore[reportUnusedFunction]
        rfq_id: UUID, fmt: ExportFormat, principal: CurrentPrincipal, db: Db
    ) -> Response:
        """One row per supplier, line and quantity tier, as the comparison shows it."""
        try:
            ref, table = await service.export_comparison(
                db, principal, rfq_id, datetime.now(UTC).date()
            )
        except RfqError as exc:
            raise rfq_http_error(exc) from None
        return _download(render(table, fmt, f"{ref} comparison"), fmt, f"{ref}-comparison")

    return router
