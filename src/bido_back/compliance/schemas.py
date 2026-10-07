"""Request and response shapes of the supplier compliance checklist (also the API's)."""

from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

RequirementScope = Literal["supplier", "manufacturer", "line"]
ChecklistStatus = Literal["missing", "received", "expired"]

RequirementCode = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[a-z0-9_]{2,40}$")
]
Keyword = Annotated[
    str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=2, max_length=40)
]
HttpsUrl = Annotated[
    str, StringConstraints(strip_whitespace=True, max_length=2000, pattern=r"^https://\S+$")
]


class Requirement(BaseModel):
    """One document the tenant expects from its suppliers."""

    model_config = ConfigDict(extra="forbid")

    code: RequirementCode
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
    scope: RequirementScope = Field(
        description=(
            "supplier: every supplier; manufacturer: suppliers of type manufacturer; "
            "line: each RFQ line whose qualification mentions line_keyword"
        )
    )
    line_keyword: Keyword | None = Field(
        default=None, description="Required for scope=line, e.g. 'aec-q' or 'ppap'"
    )
    keywords: list[Keyword] = Field(
        default_factory=list[str],
        max_length=20,
        description="Filename fragments that mark a received attachment as a candidate",
    )
    validity_months: int | None = Field(
        default=None,
        ge=1,
        le=120,
        description="Default validity from the issue date when no expiry date is given",
    )
    blocking: bool = Field(
        default=False, description="A missing or expired document blocks the supplier"
    )

    @model_validator(mode="after")
    def _line_keyword_matches_scope(self) -> "Requirement":
        if (self.scope == "line") != (self.line_keyword is not None):
            raise ValueError("line_keyword is required for scope 'line' and only for it")
        return self


class RequirementsView(BaseModel):
    customized: bool = Field(description="False while the tenant uses Bido's default set")
    requirements: list[Requirement]


class UpdateRequirementsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirements: list[Requirement] = Field(max_length=50)

    @model_validator(mode="after")
    def _unique_codes(self) -> "UpdateRequirementsRequest":
        codes = [r.code for r in self.requirements]
        if len(codes) != len(set(codes)):
            raise ValueError("requirement codes must be unique")
        return self


class RecordDocumentRequest(BaseModel):
    """Confirm a document: an attachment the supplier sent, or an https link."""

    model_config = ConfigDict(extra="forbid")

    requirement_code: RequirementCode
    rfq_line_id: UUID | None = Field(
        default=None, description="Required for line-scoped requirements"
    )
    attachment_id: UUID | None = None
    url: HttpsUrl | None = None
    issued_on: date | None = None
    expires_on: date | None = None

    @model_validator(mode="after")
    def _evidence_and_dates(self) -> "RecordDocumentRequest":
        if (self.attachment_id is None) == (self.url is None):
            raise ValueError("give exactly one of attachment_id or url")
        if self.issued_on and self.expires_on and self.expires_on < self.issued_on:
            raise ValueError("expires_on is before issued_on")
        return self


class DocumentView(BaseModel):
    id: UUID
    requirement_code: str
    rfq_line_id: UUID | None
    attachment_id: UUID | None
    filename: str | None
    url: str | None
    issued_on: date | None
    expires_on: date | None
    recorded_at: datetime


class CandidateDocument(BaseModel):
    """An attachment the supplier sent whose filename matches the requirement."""

    attachment_id: UUID
    filename: str
    received_at: datetime


class ChecklistItem(BaseModel):
    code: str
    name: str
    scope: RequirementScope
    blocking: bool
    status: ChecklistStatus
    rfq_id: UUID | None = None
    rfq_line_id: UUID | None = None
    line_position: int | None = None
    line_mpn: str | None = None
    document: DocumentView | None = None
    candidates: list[CandidateDocument] = Field(default_factory=list[CandidateDocument])


class ChecklistCounts(BaseModel):
    received: int = 0
    missing: int = 0
    expired: int = 0


class SupplierChecklist(BaseModel):
    supplier_id: UUID
    supplier_name: str
    supplier_type: str
    as_of: date
    counts: ChecklistCounts
    items: list[ChecklistItem]


class BlockingItem(BaseModel):
    code: str
    name: str
    status: ChecklistStatus
    rfq_line_id: UUID | None = None


class SupplierComplianceSummary(BaseModel):
    """Per invited supplier, in the RFQ comparison under `compliance`."""

    supplier_id: UUID
    supplier_name: str
    counts: ChecklistCounts
    blocking: list[BlockingItem]
