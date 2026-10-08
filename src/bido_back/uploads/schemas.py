"""Response bodies of `/v1/uploads/{id}/download` and `/v1/uploads/{id}/preview`."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class FileLinkView(BaseModel):
    """A short-lived link the browser opens itself; it always downloads the file."""

    url: str
    expires_at: datetime
    filename: str
    content_type: str


class SheetPreview(BaseModel):
    name: str
    # The first rows and columns, as text, every row the same width.
    rows: list[list[str]]
    total_rows: int = Field(description="Rows with at least one value, in the whole sheet")
    total_columns: int = Field(description="The widest row, in the whole sheet")
    truncated: bool = Field(description="Rows, columns or cells were cut for the preview")


PreviewKind = Literal["table", "inline", "none"]
NoPreviewReason = Literal["unsupported", "too_large", "unreadable"]


class UploadPreview(BaseModel):
    """What to show for an uploaded file.

    * `table`: spreadsheets (xlsx, csv) as `sheets`, values only (formulas are
      never evaluated: a formula shows its last saved result, or nothing).
    * `inline`: PDF and images, as a short-lived `url` served inline with the
      file's exact type, for an iframe or an img.
    * `none`: no preview (`reason`); the file can still be downloaded.
    """

    id: UUID
    filename: str
    size_bytes: int
    content_type: str
    kind: PreviewKind
    sheets: list[SheetPreview] = Field(default_factory=list[SheetPreview])
    url: str | None = None
    expires_at: datetime | None = None
    reason: NoPreviewReason | None = None
