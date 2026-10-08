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
    # One page of rows (`offset` on), as text, every row the same width: every
    # column up to 200 (`truncated_columns` when the sheet is wider).
    rows: list[list[str]]
    total_rows: int = Field(
        description=(
            "Rows in the whole sheet: every row an .xlsx has (an empty one shows as an"
            " empty row), or a csv's lines that have a value"
        )
    )
    total_columns: int = Field(description="The widest row, in the whole sheet")
    truncated: bool = Field(
        description="Rows outside this page, columns past the cap, or cut cells"
    )
    offset: int = Field(default=0, description="Index of the first row of this page")
    has_more: bool = Field(default=False, description="Rows follow this page")
    truncated_columns: bool = Field(
        default=False, description="The sheet is wider than the columns returned"
    )


PreviewKind = Literal["table", "inline", "none"]
NoPreviewReason = Literal["unsupported", "too_large", "unreadable"]


class UploadPreview(BaseModel):
    """What to show for an uploaded file.

    * `table`: spreadsheets (xlsx, csv) as `sheets`, values only (formulas are
      never evaluated: a formula shows its last saved result, or nothing). One
      page of rows each: the first rows of every sheet, or, with `sheet`, the
      `limit` rows of that sheet from `offset`.
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
    sheet_names: list[str] = Field(
        default_factory=list[str],
        description="Every sheet of a table, in order; `sheets` may hold only the requested one",
    )
    url: str | None = None
    expires_at: datetime | None = None
    reason: NoPreviewReason | None = None
