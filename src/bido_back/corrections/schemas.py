"""Request and response models for correcting extracted quote values.

A request names only the fields being corrected: a field left out is not
touched, a field sent with the value it already has is recorded as a
confirmation, and `null` clears an optional value. Values are the canonical
forms the comparison uses (decimal strings or numbers, ISO dates, ISO codes),
not the supplier's spelling.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Final, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator

from bido_back.quotes.parsing import INCOTERMS

# Bounds follow the proposal columns: Numeric(18, 6) prices, Numeric(18, 4)
# quantities, Numeric(18, 2) tooling.
_MAX: Final = Decimal(10) ** 12

Price = Annotated[Decimal, Field(ge=0, lt=_MAX, decimal_places=6)]
Quantity = Annotated[Decimal, Field(gt=0, lt=_MAX, decimal_places=4)]
Money = Annotated[Decimal, Field(ge=0, lt=_MAX, decimal_places=2)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)]

CorrectionAction = Literal["corrected", "confirmed"]


class _Correction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Reason | None = Field(default=None, description="Why, for the review history")

    def changes(self) -> dict[str, object]:
        """The fields the request names, with their new values."""
        return {name: getattr(self, name) for name in self.model_fields_set if name != "reason"}

    @model_validator(mode="after")
    def _names_a_field(self) -> Self:
        if not self.changes():
            raise ValueError("name at least one field to correct")
        return self


class LineCorrectionRequest(_Correction):
    offered_mpn: ShortText | None = None
    is_alternative: bool | None = None
    rfq_line_id: UUID | None = Field(default=None, description="The requested line it answers")
    min_quantity: Quantity | None = None
    unit_price: Price | None = None
    unit: ShortText | None = None
    tooling_cost: Money | None = None
    moq: Quantity | None = None
    lead_time_days: Annotated[int, Field(ge=0, le=3650)] | None = None
    origin_country: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")] | None = None


class HeaderCorrectionRequest(_Correction):
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None
    valid_until: date | None = None
    incoterm: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None
    payment_terms: ShortText | None = None

    @model_validator(mode="after")
    def _known_incoterm(self) -> Self:
        if self.incoterm is not None and self.incoterm not in INCOTERMS:
            raise ValueError(f"unknown Incoterm {self.incoterm!r}")
        return self


class ProposalLineView(BaseModel):
    id: UUID
    proposal_id: UUID
    rfq_line_id: UUID | None
    offered_mpn: str | None
    is_alternative: bool
    min_quantity: Decimal | None
    unit_price: Decimal | None
    unit: str | None
    tooling_cost: Decimal | None
    moq: Decimal | None
    lead_time_days: int | None
    origin_country: str | None
    needs_review: bool
    review_fields: list[str] = Field(description="Fields still flagged for review")
    corrected_fields: list[str] = Field(description="Fields a person corrected or confirmed")


class ProposalView(BaseModel):
    id: UUID
    invite_id: UUID
    rfq_id: UUID
    version: int
    currency: str | None
    valid_until: date | None
    incoterm: str | None
    payment_terms: str | None
    needs_review: bool
    review_fields: list[str]
    corrected_fields: list[str]
    lines: list[ProposalLineView]


class FieldCorrectionView(BaseModel):
    id: UUID
    proposal_id: UUID
    proposal_line_id: UUID | None
    field: str
    action: CorrectionAction
    old_value: JsonValue
    new_value: JsonValue
    old_provenance: dict[str, JsonValue] = Field(
        description="What the extraction recorded for the old value (source, quote, confidence)"
    )
    reason: str | None
    corrected_by: UUID
    created_at: datetime
