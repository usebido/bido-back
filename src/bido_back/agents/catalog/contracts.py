"""Input/output contracts of the RFQ-flow agents.

These are the stable interface between the agent definitions (prompts, tools,
model) and the code that calls them through `run()`. Change them deliberately:
the API, the extraction job and the evals depend on them.
"""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field, WithJsonSchema
from pydantic.json_schema import SkipJsonSchema

LineCategory = Literal["catalog", "drawing", "cell", "material"]


# --- rfq_intake: free text / spreadsheet -> structured RFQ draft --------------


class RfqIntakeInput(BaseModel):
    text: str = Field(
        description="Buyer's request: free text, a forwarded email, or a sheet as text"
    )
    # The RFQ's currency, resolved by the caller (`bido_back.rfqs.currency`); no default.
    base_currency: str | None = None
    buyer_messages: list[str] = Field(
        default_factory=list[str],
        description=(
            "When the request comes from a Copilot conversation: the buyer's own messages, "
            "oldest first. `text` is then the Copilot's summary of what to quote."
        ),
    )


# Pydantic's schema for Decimal carries a regex with lookarounds, which OpenAI's
# strict structured output rejects. The model answers a plain number instead.
ModelDecimal = Annotated[Decimal, WithJsonSchema({"type": "number"})]


class RfqLineDraft(BaseModel):
    description: str
    part_number: str | None = Field(
        default=None, description="The buyer's own code for the part, copied verbatim"
    )
    mpn: str | None = Field(
        default=None, description="A manufacturer part number the buyer wrote as such"
    )
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
    # The buyer's code: the line's main reference for the supplier.
    part_number: str | None = None
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
    # The buyer's payment terms; when set, the email must state them as given.
    payment_terms: str | None = None


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


def _absent_value() -> ExtractedValue:
    return ExtractedValue(value=None, source=None, confidence=0)


# How a supplier states a lead time; code turns it into a delivery date
# (`bido_back.quotes.lead_time`). Business days are never calendar days.
LeadTimeUnit = Literal["calendar_days", "business_days", "weeks", "months", "date"]
# When the lead time starts (or, for `ex_works`, that it ends when the goods are
# ready at the supplier, transport not included).
LeadTimeBasis = Literal["order", "payment", "approval", "ex_works", "unspecified"]
ValidityUnit = Literal["calendar_days", "business_days", "weeks", "months"]


class LeadTimeValue(ExtractedValue):
    """A lead time as written: `value` is the number of `unit`s (or, for unit `date`,
    the ISO delivery date). The evidence rules of `ExtractedValue` apply."""

    unit: LeadTimeUnit | None = Field(
        default=None,
        description="calendar_days, business_days, weeks, months, or date; null when no value",
    )
    basis: LeadTimeBasis | None = Field(
        default=None,
        description=(
            "order: from the order / PO confirmation; payment: from receipt of payment; "
            "approval: from drawing or sample approval; ex_works: until ready at the "
            "supplier, transport not included; unspecified: not stated"
        ),
    )
    approximate: bool = Field(
        default=False,
        description="True for 'approximately', 'about', 'ca.', 'cerca de', or a range",
    )
    risk_note: str | None = Field(
        default=None,
        description=(
            "The supplier's exact words when it says this lead time may slip or need extra "
            "days, copied character for character; else null"
        ),
    )


class ValidityValue(ExtractedValue):
    """An offer validity given as a duration: `value` is the number of `unit`s."""

    unit: ValidityUnit | None = Field(
        default=None, description="calendar_days, business_days, weeks or months; null if none"
    )


def _absent_lead_time() -> LeadTimeValue:
    return LeadTimeValue(value=None, source=None, confidence=0)


def _absent_validity() -> ValidityValue:
    return ValidityValue(value=None, source=None, confidence=0)


def _lead_time_input(value: object) -> object:
    # Reconciliation copies plain `ExtractedValue`s back into drafts; read them as a
    # lead time without unit rather than rejecting the whole draft.
    if isinstance(value, ExtractedValue) and not isinstance(value, LeadTimeValue):
        return value.model_dump()
    return value


def _validity_input(value: object) -> object:
    if isinstance(value, ExtractedValue) and not isinstance(value, ValidityValue):
        return value.model_dump()
    return value


LeadTimeField = Annotated[LeadTimeValue, BeforeValidator(_lead_time_input)]
ValidityField = Annotated[ValidityValue, BeforeValidator(_validity_input)]


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
    # Calendar days. Since prompt v5 the model writes `lead_time` and code computes
    # this value from it, so it is not part of the model's output schema.
    lead_time_days: SkipJsonSchema[ExtractedValue] = Field(default_factory=_absent_value)
    origin_country: ExtractedValue
    # The automotive qualification the supplier states for this offer, e.g. AEC-Q200.
    # No Field(description=...): next to a $ref, OpenAI's strict schema rejects it.
    qualification: ExtractedValue
    # The written price and its denominator remain separate; code normalizes them.
    price_basis_quantity: ExtractedValue = Field(default_factory=_absent_value)
    freight_cost: ExtractedValue = Field(default_factory=_absent_value)
    tax_rate_percent: ExtractedValue = Field(default_factory=_absent_value)
    conditional_terms: ExtractedValue = Field(default_factory=_absent_value)
    # This line's own lead time; absent when only the offer-level one applies.
    lead_time: LeadTimeField = Field(default_factory=_absent_lead_time)
    # Set by code, never by the model: the lead time that applies to this line (its
    # own or the offer's), validated, as a plain value. Reconciliation copies field
    # values back into drafts, so this is not `lead_time` itself.
    lead_time_terms: SkipJsonSchema[ExtractedValue] = Field(default_factory=_absent_value)


class PriorSupplierOffer(BaseModel):
    """Supplier-only context, with no buyer limits or competitor information."""

    message_id: str
    currency: str | None
    lines: list[ProposalLineDraft]


class OfferConflict(BaseModel):
    """Two incompatible claims in the current message or its attachments."""

    field: str
    rfq_line_position: int | None = None
    first: SourceRef
    second: SourceRef


class QuoteExtractionInput(BaseModel):
    rfq_ref: str
    requested_lines: list[RfqEmailLine]
    email_subject: str
    email_body: str
    attachments_text: str = Field(description="Extracted text with [file p.N] / [sheet!A1] markers")
    prior_supplier_offer: PriorSupplierOffer | None = None
    # When the supplier sent the message (prompt v5): only to complete a date written
    # without a year. Code, not the model, counts durations from it.
    message_date: date | None = None


class ExtraOfferDraft(BaseModel):
    """Something the supplier offers that the RFQ did not ask for: another item,
    a volume discount, a bundle, another department's offer in the same email.
    Kept as written; never priced into the comparison."""

    description: str = Field(description="What is offered, in the supplier's words, short")
    mpn: str | None = Field(description="Part number if given, else null")
    quantity: str | None = Field(description="Quantity or volume condition as written, or null")
    unit_price: str | None = Field(description="Price as written (number only), or null")
    currency: str | None = Field(description="ISO 4217 code if stated, or null")
    conditions: str | None = Field(
        description="Validity, minimums or other conditions as written, or null"
    )
    quote: str = Field(description="The exact text the offer was read from")


QuestionTopic = Literal["technical", "commercial"]


class DeclinedLine(BaseModel):
    """A requested line the supplier explicitly says it will not or cannot quote."""

    rfq_line_position: int
    quote: str = Field(description="The exact text of the refusal, copied character for character")


class SupplierQuestion(BaseModel):
    """A question the supplier asks the buyer, copied exactly."""

    quote: str = Field(description="The question exactly as written, character for character")
    topic: QuestionTopic = Field(
        description="technical: specification, drawing, material, part; commercial: price, "
        "quantity, delivery, payment, terms"
    )
    rfq_line_position: int | None = Field(
        description="The requested line the question concerns; null for the whole request"
    )


class QuoteExtractionOutput(BaseModel):
    is_quote: bool = Field(
        description="True for a priced offer or an amendment/withdrawal of an existing offer"
    )
    declined: bool = False
    currency: ExtractedValue
    valid_until: ExtractedValue
    incoterm: ExtractedValue
    payment_terms: ExtractedValue
    lines: list[ProposalLineDraft] = Field(default_factory=list[ProposalLineDraft])
    missing_fields: list[str] = Field(default_factory=list[str])
    # Offers for things not requested; empty when there are none.
    extra_offers: list[ExtraOfferDraft] = Field(default_factory=list[ExtraOfferDraft])
    reply_kind: Literal["replacement", "amendment", "withdrawal", "clarification"] = "replacement"
    # Only an explicit, grounded preservation statement permits field inheritance.
    unchanged_terms: ExtractedValue = Field(default_factory=_absent_value)
    update_evidence: ExtractedValue = Field(default_factory=_absent_value)
    withdrawn_line_positions: list[int] = Field(default_factory=list[int])
    conflicts: list[OfferConflict] = Field(default_factory=list[OfferConflict])
    # Code adds the text of every `supplier_questions` entry here: a question holds the offer.
    technical_questions: list[str] = Field(default_factory=list[str])
    # Since prompt v5. A validity given as a duration; code computes `valid_until`.
    validity: ValidityField = Field(default_factory=_absent_validity)
    # The lead time stated for the whole offer: it applies to every line without its own.
    lead_time: LeadTimeField = Field(default_factory=_absent_lead_time)
    # The supplier says this is its final or best offer; `value` is that statement.
    final_offer: ExtractedValue = Field(default_factory=_absent_value)
    declined_lines: list[DeclinedLine] = Field(default_factory=list[DeclinedLine])
    supplier_questions: list[SupplierQuestion] = Field(default_factory=list[SupplierQuestion])
    offer_notes: list[str] = Field(
        default_factory=list[str],
        description=(
            "Exact sentences with general conditions of the whole offer that have no field "
            "of their own (packaging, warranty, taxes...); never prices"
        ),
    )


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


# --- rfq_negotiation_writer: a counter-offer or a best-and-final request ---------

NegotiationAction = Literal["counter", "bafo"]


class NegotiationLineAsk(BaseModel):
    """One line of the counter-offer. Every number is decided by code; the model
    writes them as given (it may use the language's decimal separator)."""

    position: int
    line: str = Field(description="The line's description and MPN, as requested")
    quantity: Decimal
    unit: str
    currency: str
    offered_unit_price: Decimal | None = Field(
        description="The supplier's current unit price; null if the line is not repriced"
    )
    target_unit_price: Decimal | None = Field(
        description="The unit price we ask for; null when only the lead time is asked"
    )
    offered_lead_time_days: int | None = None
    required_lead_time_days: int | None = Field(
        default=None, description="The lead time we need, when the offer's is too long"
    )


class NegotiationEmailInput(BaseModel):
    action: NegotiationAction
    rfq_ref: str
    rfq_title: str
    buyer_company: str
    buyer_name: str
    supplier_name: str
    supplier_language: str = Field(description="ISO 639-1, e.g. de, en, fr, it, pt")
    round: int = Field(description="1 for the first counter-offer")
    reply_by: date | None = Field(description="Answer by this date; null if none")
    mention_competition: bool = Field(
        description="True: we may say a more competitive offer exists (no names, no numbers)"
    )
    lines: list[NegotiationLineAsk]


class NegotiationEmailOutput(BaseModel):
    body: str = Field(description="Plain text. The subject is set by code (Re: the RFQ email).")


# --- award_notice_writer: the polite "not selected" email after an award ------------


class AwardNoticeInput(BaseModel):
    rfq_ref: str
    rfq_title: str
    buyer_company: str
    buyer_name: str
    supplier_name: str
    supplier_language: str = Field(description="ISO 639-1, e.g. de, en, fr, it, pt")


class AwardNoticeOutput(BaseModel):
    body: str = Field(description="Plain text. The subject is set by code (Re: the RFQ email).")


# --- The thread writers since their v2 prompts: an optional note, nothing else ------------
#
# Code renders every follow-up, negotiation and notice email (`bido_back.supplier_email`):
# salutation, positions, questions, numbers, dates, reply-by, legal line, closing and
# signature. The writers keep their input contracts above and return only this note;
# the v1 output contracts stay for the released v1 prompts.


class EmailNoteOutput(BaseModel):
    note: str = Field(
        description=(
            "Empty by default. At most two short sentences in the supplier's language: no "
            "digits, numbers, amounts, dates, names, links, greeting, list or signature."
        )
    )


# --- sheet_mapper: an arbitrary workbook -> import sections ------------------------------

SheetImportType = Literal[
    "items",
    "bom",
    "inventory",
    "purchase_history",
    "production_plan",
    "supplier_contacts",
    "ignore",
]


class SheetPreview(BaseModel):
    sheet: str
    rows: int
    columns: int
    preview: str = Field(
        description="The first rows, one per line: 'row N: [0] value | [1] value | ...'"
    )
    profile: str = Field(
        default="",
        description=(
            "Every column, one line each, computed over the rows under the likely header "
            "row: '[N] title: kind, distinct count, examples or min..max, % empty'"
        ),
    )


class ImportFieldCatalog(BaseModel):
    import_type: str
    fields: list[str]
    required: list[str]
    # Field -> its value type ("text", "number", "date", "bool", "int").
    types: dict[str, str] = {}
    # Field -> what it means, for fields that are easy to confuse.
    hints: dict[str, str] = {}


class SheetMappingInput(BaseModel):
    filename: str
    sheets: list[SheetPreview]
    catalog: list[ImportFieldCatalog]
    # On a second attempt: what was wrong with the first mapping of these sheets.
    feedback: list[str] = []


class ColumnAssignment(BaseModel):
    field: str = Field(description="A field name of the section's import type")
    column: int = Field(description="0-based column index, as in the preview's [N]")


class SheetSection(BaseModel):
    sheet: str
    import_type: SheetImportType
    header_row: int = Field(description="1-based row of the column headers")
    first_data_row: int = Field(description="1-based first data row")
    last_data_row: int | None = Field(description="1-based last data row, or null for the end")
    columns: list[ColumnAssignment]
    confidence: float = Field(ge=0, le=1)
    question: str | None = Field(
        description="When unsure: one short question for the buyer, in their language; else null"
    )


class SheetMappingOutput(BaseModel):
    sections: list[SheetSection]
