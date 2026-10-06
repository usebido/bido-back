"""Input/output contracts of the RFQ-flow agents.

These are the stable interface between the agent definitions (prompts, tools,
model) and the code that calls them through `run()`. Change them deliberately:
the API, the extraction job and the evals depend on them.
"""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

LineCategory = Literal["catalog", "drawing", "cell", "material"]


# --- rfq_intake: free text / spreadsheet -> structured RFQ draft --------------


class RfqIntakeInput(BaseModel):
    text: str = Field(
        description="Buyer's request: free text, a forwarded email, or a sheet as text"
    )
    base_currency: str = "EUR"


class RfqLineDraft(BaseModel):
    description: str
    mpn: str | None = None
    manufacturer: str | None = None
    category: LineCategory = "catalog"
    quantities: list[Decimal] = Field(min_length=1, description="Requested quantity tiers")
    unit: str = "pcs"
    need_by: date | None = None
    incoterm: str | None = None
    delivery_location: str | None = None
    qualification: str | None = Field(default=None, description="e.g. AEC-Q100, AEC-Q200")


class RfqIntakeOutput(BaseModel):
    title: str
    response_due: date | None = None
    lines: list[RfqLineDraft] = Field(min_length=1)
    questions: list[str] = Field(default_factory=list, description="What the buyer must clarify")


# --- rfq_email_writer: RFQ + supplier -> email draft in the supplier's language


class RfqEmailLine(BaseModel):
    position: int
    description: str
    mpn: str | None = None
    manufacturer: str | None = None
    quantities: list[Decimal]
    unit: str
    need_by: date | None = None
    incoterm: str | None = None
    qualification: str | None = None


class RfqEmailInput(BaseModel):
    rfq_ref: str
    rfq_title: str
    buyer_company: str
    buyer_name: str
    supplier_name: str
    supplier_language: str = Field(description="ISO 639-1, e.g. de, en, fr, zh")
    response_due: date | None = None
    lines: list[RfqEmailLine]


class RfqEmailOutput(BaseModel):
    subject: str
    body: str = Field(description="Plain text. Must ask for every field listed in the prompt.")


# --- quote_extraction: supplier reply (+ attachments) -> proposal -------------


class SourceRef(BaseModel):
    """Where a value came from. `kind` decides which locator fields are set."""

    kind: Literal["email_body", "pdf", "sheet", "image"]
    attachment: str | None = Field(default=None, description="Attachment filename, if any")
    page: int | None = None
    sheet: str | None = None
    cell: str | None = Field(default=None, description="e.g. C14")
    quote: str = Field(description="The exact text the value was read from")


class ExtractedValue(BaseModel):
    value: str | None
    source: SourceRef | None = None
    confidence: float = Field(ge=0, le=1)


class ProposalLineDraft(BaseModel):
    rfq_line_position: int | None = Field(
        description="Which requested line this answers; None if unclear"
    )
    offered_mpn: ExtractedValue
    is_alternative: bool = False
    min_quantity: ExtractedValue
    unit_price: ExtractedValue
    unit: ExtractedValue
    tooling_cost: ExtractedValue
    moq: ExtractedValue
    lead_time_days: ExtractedValue
    origin_country: ExtractedValue


class QuoteExtractionInput(BaseModel):
    rfq_ref: str
    requested_lines: list[RfqEmailLine]
    email_subject: str
    email_body: str
    attachments_text: str = Field(description="Extracted text with [file p.N] / [sheet!A1] markers")


class QuoteExtractionOutput(BaseModel):
    is_quote: bool = Field(
        description="False for out-of-office, refusals, questions without prices"
    )
    declined: bool = False
    currency: ExtractedValue
    valid_until: ExtractedValue
    incoterm: ExtractedValue
    payment_terms: ExtractedValue
    lines: list[ProposalLineDraft] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
