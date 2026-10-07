"""Input/output contracts of the RFQ-flow agents.

These are the stable interface between the agent definitions (prompts, tools,
model) and the code that calls them through `run()`. Change them deliberately:
the API, the extraction job and the evals depend on them.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, WithJsonSchema

LineCategory = Literal["catalog", "drawing", "cell", "material"]


# --- rfq_intake: free text / spreadsheet -> structured RFQ draft --------------


class RfqIntakeInput(BaseModel):
    text: str = Field(
        description="Buyer's request: free text, a forwarded email, or a sheet as text"
    )
    base_currency: str = "EUR"


# Pydantic's schema for Decimal carries a regex with lookarounds, which OpenAI's
# strict structured output rejects. The model answers a plain number instead.
ModelDecimal = Annotated[Decimal, WithJsonSchema({"type": "number"})]


class RfqLineDraft(BaseModel):
    description: str
    mpn: str | None = None
    manufacturer: str | None = None
    category: LineCategory = "catalog"
    quantities: list[ModelDecimal] = Field(min_length=1, description="Requested quantity tiers")
    unit: str = "pcs"
    need_by: date | None = None
    incoterm: str | None = None
    delivery_location: str | None = None
    qualification: str | None = Field(default=None, description="e.g. AEC-Q100, AEC-Q200")


class RfqIntakeOutput(BaseModel):
    title: str
    response_due: date | None = None
    lines: list[RfqLineDraft] = Field(min_length=1)
    questions: list[str] = Field(
        default_factory=list[str], description="What the buyer must clarify"
    )


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


class RfqEmailTemplate(BaseModel):
    """The buyer's usual RFQ style (their default template), when they have one."""

    language: str = Field(description="ISO 639-1 of the template, e.g. pt, en")
    greeting: str = Field(description="The buyer's usual opening, to adapt, not to copy")
    fields: list[str] = Field(description="What the buyer usually asks suppliers for")


class RfqEmailInput(BaseModel):
    rfq_ref: str
    rfq_title: str
    buyer_company: str
    buyer_name: str
    supplier_name: str
    supplier_language: str = Field(description="ISO 639-1, e.g. de, en, fr, zh")
    response_due: date | None = None
    lines: list[RfqEmailLine]
    template: RfqEmailTemplate | None = None


class RfqEmailOutput(BaseModel):
    subject: str
    body: str = Field(description="Plain text. Must ask for every field listed in the prompt.")


# --- rfq_email_writer (v2): the buyer's template text in the supplier's language --------
#
# The RFQ email itself is rendered by code (`bido_back.templates.render`). The
# writer only carries the buyer's own words (greeting, column headings, footer
# fields) into the supplier's language. Same-language sends never call it.


class RfqEmailLocalizeInput(BaseModel):
    source_language: str = Field(description="ISO 639-1 of the buyer's template, e.g. pt")
    target_language: str = Field(description="ISO 639-1 of the supplier, e.g. de")
    greeting: str = Field(description="The buyer's opening paragraph")
    labels: list[str] = Field(description="Column headings and footer fields, in order")


class RfqEmailLocalizeOutput(BaseModel):
    greeting: str = Field(description="The opening paragraph in the target language")
    labels: list[str] = Field(
        description="The same labels in the target language: same count, same order"
    )


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
    # The automotive qualification the supplier states for this offer, e.g. AEC-Q200.
    # No Field(description=...): next to a $ref, OpenAI's strict schema rejects it.
    qualification: ExtractedValue


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
    lines: list[ProposalLineDraft] = Field(default_factory=list[ProposalLineDraft])
    missing_fields: list[str] = Field(default_factory=list[str])


# --- rfq_candidate_classifier: sent-mail metadata -> is it an RFQ the buyer sent?


class RfqCandidate(BaseModel):
    """One sent email, as metadata only: no body, no addresses (domains only)."""

    id: str
    subject: str
    recipient_count: int
    recipient_domains: list[str]
    attachments: list[str] = Field(description="Attachment file names")
    sent_on: date | None = None


class RfqCandidateClassifierInput(BaseModel):
    candidates: list[RfqCandidate]


class RfqCandidateVerdict(BaseModel):
    id: str
    is_rfq: bool = Field(
        description="True if this looks like a request for quotation the buyer sent"
    )
    confident: bool
    note: str | None = Field(
        default=None,
        description="When in doubt: a short reason in Brazilian Portuguese, else null",
    )


class RfqCandidateClassifierOutput(BaseModel):
    verdicts: list[RfqCandidateVerdict]


# --- rfq_template_builder: the buyer's own RFQs -> their RFQ template ----------


class RfqTemplateBuilderInput(BaseModel):
    example_count: int
    examples: str = Field(description="The example RFQs as text, each starting with a [marker]")


class RfqTemplateBuilderOutput(BaseModel):
    language: str = Field(description="ISO 639-1 of the examples, e.g. pt, en")
    greeting: str = Field(description="The opening the buyer uses, generalized")
    # No length constraints here (strict structured output): the build job caps them.
    columns: list[str] = Field(description="Item table headings, at most 8")
    footer_fields: list[str] = Field(description="Commercial terms asked for after the items")
    fields: list[str] = Field(description="Every field the buyer asks for")


# --- rfq_followup_writer: a chase or a clarification question, in the thread ----

FollowupKind = Literal["chase", "clarification"]
FollowupField = Literal["currency", "valid_until", "unit_price", "quantity", "lead_time"]


class FollowupItem(BaseModel):
    """One thing the clarification email asks the supplier for."""

    position: int | None = Field(description="RFQ line position; null for the whole offer")
    line: str | None = Field(description="The line's description and MPN, as requested")
    field: FollowupField
    problem: Literal["missing", "unclear"]


class FollowupEmailInput(BaseModel):
    kind: FollowupKind
    rfq_ref: str
    rfq_title: str
    buyer_company: str
    buyer_name: str
    supplier_name: str
    supplier_language: str = Field(description="ISO 639-1, e.g. de, en, fr, pt")
    chase_number: int = Field(description="1 for the first reminder; 0 for a clarification")
    original_sent_on: date | None = None
    response_due: date | None = None
    open_items: list[FollowupItem] = Field(default_factory=list[FollowupItem])


class FollowupEmailOutput(BaseModel):
    body: str = Field(description="Plain text. The subject is set by code (Re: the RFQ email).")
