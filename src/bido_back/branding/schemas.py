"""API shapes for `/v1/company`: branding details and the logo library."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from bido_back.branding.images import LogoType

Line = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
LongText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)]
AccentColor = Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$")]


class LogoView(BaseModel):
    id: UUID
    name: str
    content_type: LogoType
    size_bytes: int
    width: int | None = Field(description="Pixels; for an SVG, from its width or viewBox")
    height: int | None
    created_at: datetime
    is_default: bool = Field(description="The company's default logo for RFQ documents")


class BrandingView(BaseModel):
    """How the company presents itself on RFQ documents. Every field may be empty."""

    legal_name: str | None = None
    trade_name: str | None = None
    tax_id: str | None = Field(default=None, description="CNPJ, VAT number or similar")
    address: str | None = Field(default=None, description="Multi-line postal address")
    phone: str | None = None
    website: str | None = None
    default_footer: str | None = Field(
        default=None, description="Terms and confidentiality note printed at the bottom"
    )
    accent_color: str | None = Field(default=None, description="#RRGGBB")
    default_logo: LogoView | None = None
    updated_at: datetime | None = None


class UpdateBrandingRequest(BaseModel):
    """Replaces every field: send the whole form; `null` or a missing field clears it."""

    model_config = ConfigDict(extra="forbid")

    legal_name: Line | None = None
    trade_name: Line | None = None
    tax_id: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40)] | None = None
    address: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] | None = None
    phone: Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)] | None = None
    website: Line | None = None
    default_footer: LongText | None = None
    accent_color: AccentColor | None = None
    default_logo_id: UUID | None = None


class LogoList(BaseModel):
    logos: list[LogoView]


class StartLogoUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)


class LogoUploadForm(BaseModel):
    """POST `url` as multipart/form-data with every entry of `fields`, the file last as
    `file`; then `POST /v1/company/logos/{logo_id}/complete`."""

    logo_id: UUID
    url: str
    fields: dict[str, str]
    expires_at: datetime
    max_bytes: int


class CompleteLogoUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The same filename sent to POST /v1/company/logos.
    filename: str = Field(min_length=1, max_length=255)
    name: Line | None = Field(
        default=None, description="Shown in the library; the filename if empty"
    )
    make_default: bool = Field(
        default=False, description="Also make it the default (always, for the first logo)"
    )


class RenameLogoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class LogoFileLink(BaseModel):
    """A short-lived link serving the image inline, for an `<img>` tag."""

    url: str
    expires_at: datetime
    content_type: LogoType
