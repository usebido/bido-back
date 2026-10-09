"""Request and response models of `/v1/rfq-templates` and the RFQ email preview."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from bido_back.agents.catalog.contracts import LineCategory

TemplateSource = Literal["search", "upload", "none"]
TemplateStatus = Literal["building", "ready", "failed"]
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class RequiredFieldView(BaseModel):
    key: str
    # item: line data the email always carries; ask: what the supplier must answer.
    kind: Literal["item", "ask"]
    # In the template's language (English when we have no label in it).
    label: str
    # Required only when a line needs it (qualification, e.g. AEC-Q200).
    conditional: bool


class TemplateDetail(BaseModel):
    id: UUID
    source: TemplateSource
    status: TemplateStatus
    # A variant for one item category; null is the general template.
    category: LineCategory | None
    version: int
    language: str | None
    greeting: str | None
    columns: list[str]
    footer_fields: list[str]
    fields: list[str]
    is_default: bool
    # Required sector fields none of the template's labels cover: suggestions
    # to add. The rendered email asks for them anyway.
    missing_required: list[RequiredFieldView]
    created_at: datetime
    updated_at: datetime


class TemplateVersionView(BaseModel):
    version: int
    current: bool
    language: str
    greeting: str
    columns: list[str]
    footer_fields: list[str]
    fields: list[str]
    created_at: datetime


class TemplateEdit(BaseModel):
    """The full content of the new version. Required fields may not be removed."""

    model_config = ConfigDict(extra="forbid")

    language: Annotated[str, StringConstraints(pattern=r"^[a-z]{2}$")]
    greeting: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]
    columns: list[Label] = Field(max_length=12)
    footer_fields: list[Label] = Field(default_factory=list[Label], max_length=20)
    fields: list[Label] = Field(default_factory=list[Label], max_length=40)


class VariantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: LineCategory


class RfqEmailPreview(BaseModel):
    invite_id: UUID
    subject: str
    # The exact body that goes to approval and is sent (plain text).
    text: str
    # The same content as HTML, for display.
    html: str
    language: str
    # The template version the email is rendered from; template_id null and
    # version 0 is Bido's standard template.
    template_id: UUID | None
    template_version: int
    missing_required: list[RequiredFieldView]
    # The RFQ document attached as a PDF (the email is then a short cover note);
    # null when the full email goes without it.
    attachment_name: str | None = None
