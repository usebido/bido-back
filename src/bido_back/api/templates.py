"""`/v1/rfq-templates`: the tenant's RFQ templates after onboarding.

* `GET    /v1/rfq-templates`: every template, with what required sector
  fields each one is missing (suggestions; the email asks for them anyway).
* `GET    /v1/rfq-templates/required-fields?language=pt`: the required set.
* `GET    /v1/rfq-templates/{id}` and `/{id}/versions`.
* `PUT    /v1/rfq-templates/{id}`: the full content of a new version. 422 when
  it drops a required field the previous version had.
* `POST   /v1/rfq-templates/{id}/variants`: a copy for one item category.
* `POST   /v1/rfq-templates/{id}/default`: the default for its category.

Onboarding (`/v1/onboarding/templates`) still creates templates and picks the
general default; these routes manage them afterwards.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import get_db, get_principal
from bido_back.api.rfqs import error_responses, rfq_http_error
from bido_back.auth import Principal
from bido_back.rfqs.errors import RfqError
from bido_back.templates import service
from bido_back.templates.required import REQUIRED_FIELDS
from bido_back.templates.schemas import (
    RequiredFieldView,
    TemplateDetail,
    TemplateEdit,
    TemplateVersionView,
    VariantRequest,
)

Db = Annotated[async_sessionmaker[AsyncSession], Depends(get_db)]
CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


def template_http_error(exc: RfqError) -> HTTPException:
    if isinstance(exc, service.RequiredFieldsRemoved):
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"required sector fields cannot be removed: {', '.join(exc.keys)}",
        )
    return rfq_http_error(exc)


def templates_router() -> APIRouter:
    router = APIRouter(prefix="/v1/rfq-templates", tags=["rfq-templates"])

    @router.get("", responses=error_responses())
    async def list_templates(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal, db: Db
    ) -> list[TemplateDetail]:
        return await service.list_templates(db, principal)

    @router.get("/required-fields", responses=error_responses())
    async def required_fields(  # pyright: ignore[reportUnusedFunction]
        principal: CurrentPrincipal,
        language: Annotated[str, Query(pattern=r"^[a-z]{2}$")] = "en",
    ) -> list[RequiredFieldView]:
        del principal  # authenticated, not tenant-specific
        return [
            RequiredFieldView(
                key=f.key, kind=f.kind, label=f.label(language), conditional=f.conditional
            )
            for f in REQUIRED_FIELDS
        ]

    @router.get("/{template_id}", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def get_template(  # pyright: ignore[reportUnusedFunction]
        template_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> TemplateDetail:
        try:
            return await service.get_template(db, principal, template_id)
        except RfqError as exc:
            raise template_http_error(exc) from None

    @router.get("/{template_id}/versions", responses=error_responses(status.HTTP_404_NOT_FOUND))
    async def list_versions(  # pyright: ignore[reportUnusedFunction]
        template_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> list[TemplateVersionView]:
        try:
            return await service.list_versions(db, principal, template_id)
        except RfqError as exc:
            raise template_http_error(exc) from None

    @router.put(
        "/{template_id}",
        responses=error_responses(
            status.HTTP_404_NOT_FOUND,
            status.HTTP_409_CONFLICT,
            status.HTTP_422_UNPROCESSABLE_CONTENT,
        ),
    )
    async def edit_template(  # pyright: ignore[reportUnusedFunction]
        template_id: UUID, body: TemplateEdit, principal: CurrentPrincipal, db: Db
    ) -> TemplateDetail:
        """Save `body` as the template's next version. Earlier versions stay unchanged."""
        try:
            return await service.edit_template(db, principal, template_id, body)
        except RfqError as exc:
            raise template_http_error(exc) from None

    @router.post(
        "/{template_id}/variants",
        status_code=status.HTTP_201_CREATED,
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
    )
    async def create_variant(  # pyright: ignore[reportUnusedFunction]
        template_id: UUID, body: VariantRequest, principal: CurrentPrincipal, db: Db
    ) -> TemplateDetail:
        try:
            return await service.create_variant(db, principal, template_id, body.category)
        except RfqError as exc:
            raise template_http_error(exc) from None

    @router.post(
        "/{template_id}/default",
        responses=error_responses(status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT),
    )
    async def make_default(  # pyright: ignore[reportUnusedFunction]
        template_id: UUID, principal: CurrentPrincipal, db: Db
    ) -> TemplateDetail:
        try:
            return await service.make_default(db, principal, template_id)
        except RfqError as exc:
            raise template_http_error(exc) from None

    return router
