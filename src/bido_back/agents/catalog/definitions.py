"""The RFQ-flow agents: intake, RFQ email writer, quote extraction, and the two
onboarding agents (sent-mail candidate classifier, RFQ template builder).

All of them are single-shot structured output: no tools at all. In particular
the extraction agent reads supplier content, which is untrusted (prompt
injection), so it gets nothing it could act with: no `act` tools, no tools
that send, no tools at all. Its output is data that `bido_back.quotes`
validates before anything is stored.

Each agent has a factory taking the model id, so the caller can choose it
from configuration; the module constants use `DEFAULT_MODEL`, except quote
extraction (`EXTRACTION_MODEL`), the template builder (`TEMPLATE_MODEL`) and the
sheet mapper (`MAPPER_MODEL`).
"""

import re
from decimal import Decimal
from typing import Final

from bido_back.agents.catalog.contracts import (
    AwardNoticeInput,
    AwardNoticeOutput,
    FollowupEmailInput,
    FollowupEmailOutput,
    NegotiationEmailInput,
    NegotiationEmailOutput,
    PoResponseOutput,
    QuoteExtractionOutput,
    RfqCandidateClassifierOutput,
    RfqEmailInput,
    RfqEmailLocalizeOutput,
    RfqEmailOutput,
    RfqIntakeOutput,
    RfqTemplateBuilderOutput,
    SheetMappingOutput,
)
from bido_back.agents.definition import AgentDefinition

DEFAULT_MODEL: Final = "gpt-6-luna"
# Quote extraction turns supplier documents into the prices the buyer compares,
# so it runs on the stronger model.
EXTRACTION_MODEL: Final = "gpt-6.1-sol"
# The template is learned once per tenant from a handful of documents.
TEMPLATE_MODEL: Final = "gpt-6.1-sol"
# Reading a spreadsheet's columns is the way into every analysis, runs once per
# workbook (about $0.02 a read) and was at least as accurate as the small model
# on the mapper evals and a real AI-generated ERP export (2026-10-08).
MAPPER_MODEL: Final = "gpt-6.1-sol"

# One request for the answer plus one spare, in case the model's first final
# answer does not validate and Agno asks again.
_MAX_TURNS: Final = 2


def rfq_intake(model: str = DEFAULT_MODEL) -> AgentDefinition[RfqIntakeOutput]:
    """v2: the buyer's own code goes to `part_number` verbatim, never into `mpn`; the
    buyer's own messages win over the Copilot's summary; RFQ-wide Incoterm and delivery
    location reach every line. v3: the unit is the requested quantity's (the need's), never
    a stock table's or a price unit; conflicting units become a question. v4: the title
    and the questions are in the buyer's language, never English by default."""
    return AgentDefinition(
        name="rfq_intake",
        prompt_file="rfq_intake.v4.md",
        prompt_version=4,
        model=model,
        output_model=RfqIntakeOutput,
        max_turns=_MAX_TURNS,
        effort="medium",
    )


def rfq_email_writer(model: str = DEFAULT_MODEL) -> AgentDefinition[RfqEmailLocalizeOutput]:
    """v3: carries the buyer's template text into the supplier's language.

    The RFQ is rendered by code: the PDF document (`bido_back.rfqs.document`)
    carries the buyer's greeting and labels, and the email is a short cover
    note (`bido_back.templates.render`). v3 tells the model where its text now
    goes; v2 (text in the email body) and v1 (wrote the whole email) are kept
    only as the released prompts they were.
    """
    return AgentDefinition(
        name="rfq_email_writer",
        prompt_file="rfq_email_writer.v3.md",
        prompt_version=3,
        model=model,
        output_model=RfqEmailLocalizeOutput,
        max_turns=_MAX_TURNS,
        effort="low",
    )


def quote_extraction(model: str = EXTRACTION_MODEL) -> AgentDefinition[QuoteExtractionOutput]:
    return AgentDefinition(
        name="quote_extraction",
        prompt_file="quote_extraction.v5.md",
        prompt_version=5,
        model=model,
        output_model=QuoteExtractionOutput,
        max_turns=_MAX_TURNS,
        effort="high",
    )


def rfq_candidate_classifier(
    model: str = DEFAULT_MODEL,
) -> AgentDefinition[RfqCandidateClassifierOutput]:
    return AgentDefinition(
        name="rfq_candidate_classifier",
        prompt_file="rfq_candidate_classifier.v1.md",
        prompt_version=1,
        model=model,
        output_model=RfqCandidateClassifierOutput,
        max_turns=_MAX_TURNS,
        thinking=False,
        effort="low",
    )


def rfq_template_builder(model: str = TEMPLATE_MODEL) -> AgentDefinition[RfqTemplateBuilderOutput]:
    return AgentDefinition(
        name="rfq_template_builder",
        prompt_file="rfq_template_builder.v1.md",
        prompt_version=1,
        model=model,
        output_model=RfqTemplateBuilderOutput,
        max_turns=_MAX_TURNS,
        effort="medium",
    )


def rfq_followup_writer(model: str = DEFAULT_MODEL) -> AgentDefinition[FollowupEmailOutput]:
    return AgentDefinition(
        name="rfq_followup_writer",
        prompt_file="rfq_followup_writer.v1.md",
        prompt_version=1,
        model=model,
        output_model=FollowupEmailOutput,
        max_turns=_MAX_TURNS,
        thinking=False,
        effort="low",
    )


def rfq_negotiation_writer(
    model: str = DEFAULT_MODEL,
) -> AgentDefinition[NegotiationEmailOutput]:
    return AgentDefinition(
        name="rfq_negotiation_writer",
        prompt_file="rfq_negotiation_writer.v1.md",
        prompt_version=1,
        model=model,
        output_model=NegotiationEmailOutput,
        max_turns=_MAX_TURNS,
        thinking=False,
        effort="low",
    )


RFQ_INTAKE: Final = rfq_intake()
RFQ_EMAIL_WRITER: Final = rfq_email_writer()
QUOTE_EXTRACTION: Final = quote_extraction()
RFQ_CANDIDATE_CLASSIFIER: Final = rfq_candidate_classifier()
RFQ_TEMPLATE_BUILDER: Final = rfq_template_builder()
RFQ_FOLLOWUP_WRITER: Final = rfq_followup_writer()
RFQ_NEGOTIATION_WRITER: Final = rfq_negotiation_writer()


def award_notice_writer(model: str = DEFAULT_MODEL) -> AgentDefinition[AwardNoticeOutput]:
    return AgentDefinition(
        name="award_notice_writer",
        prompt_file="award_notice_writer.v1.md",
        prompt_version=1,
        model=model,
        output_model=AwardNoticeOutput,
        max_turns=_MAX_TURNS,
        thinking=False,
        effort="low",
    )


AWARD_NOTICE_WRITER: Final = award_notice_writer()


def po_response_reader(model: str = EXTRACTION_MODEL) -> AgentDefinition[PoResponseOutput]:
    """A supplier's reply to a purchase order: a confirmation, a change, a refusal, a question.

    The extraction model: what it reads becomes an acknowledgement on the order, or a
    change the buyer must review. Code grounds every value in the email (`awards.responses`).
    """
    return AgentDefinition(
        name="po_response_reader",
        prompt_file="po_response_reader.v1.md",
        prompt_version=1,
        model=model,
        output_model=PoResponseOutput,
        max_turns=_MAX_TURNS,
        thinking=False,
        effort="medium",
    )


PO_RESPONSE_READER: Final = po_response_reader()


def sheet_mapper(model: str = MAPPER_MODEL) -> AgentDefinition[SheetMappingOutput]:
    return AgentDefinition(
        name="sheet_mapper",
        prompt_file="sheet_mapper.v2.md",
        prompt_version=2,
        model=model,
        output_model=SheetMappingOutput,
        max_turns=_MAX_TURNS,
        thinking=False,
        effort="medium",
    )


SHEET_MAPPER: Final = sheet_mapper()


# --- Deterministic checks on the RFQ email draft ---------------------------------

# A number next to a currency symbol or code: `12.50 EUR`, `€ 0,45`, `USD 3`.
_PRICE = re.compile(
    r"(?:[€$£¥]|\b(?:EUR|USD|GBP|CHF|CNY|JPY|RMB|BRL)\b)\s?\d"
    r"|\d(?:[\d.,]*\d)?\s?(?:[€$£¥]|\b(?:EUR|USD|GBP|CHF|CNY|JPY|RMB|BRL)\b)",
    re.IGNORECASE,
)


def rfq_email_problems(task: RfqEmailInput, draft: RfqEmailOutput) -> list[str]:
    """What is wrong with an RFQ email draft, as codes. Empty means it may go to approval.

    Code checks the parts that must hold: the RFQ reference in subject and body,
    every requested MPN listed, the buyer's payment terms when set, no prices.
    `body` is what carries the RFQ: the RFQ document's text when the email goes
    as a cover note with the PDF, the email body itself when it goes without one.
    """
    problems: list[str] = []
    if task.rfq_ref not in draft.subject:
        problems.append("rfq_ref_missing_in_subject")
    if task.rfq_ref not in draft.body:
        problems.append("rfq_ref_missing_in_body")
    for line in task.lines:
        if line.mpn and line.mpn not in draft.body:
            problems.append(f"mpn_missing:{line.position}")
    if task.payment_terms and task.payment_terms not in draft.body:
        problems.append("payment_terms_missing")
    if _PRICE.search(draft.subject) or _PRICE.search(draft.body):
        problems.append("price_mentioned")
    return problems


_MAX_COVER_CHARS: Final = 2000


def rfq_cover_problems(task: RfqEmailInput, draft: RfqEmailOutput) -> list[str]:
    """What is wrong with the cover note that carries the RFQ document, as codes.

    The PDF carries the items and questions (checked on the document with
    `rfq_email_problems` and `bido_back.rfqs.document.document_problems`); the
    cover must carry the reference in the subject and the `[RFQ-<n>]` marker the
    supplier keeps when replying, list no items, state no prices, and stay short.
    """
    problems: list[str] = []
    if task.rfq_ref not in draft.subject:
        problems.append("rfq_ref_missing_in_subject")
    if f"[{task.rfq_ref}]" not in draft.body:
        problems.append("reply_marker_missing")
    if any(line.mpn and line.mpn in draft.body for line in task.lines):
        problems.append("items_listed")
    if _PRICE.search(draft.subject) or _PRICE.search(draft.body):
        problems.append("price_mentioned")
    if len(draft.body) > _MAX_COVER_CHARS:
        problems.append("too_long")
    return problems


_MAX_FOLLOWUP_CHARS: Final = 4000


def followup_email_problems(task: FollowupEmailInput, draft: FollowupEmailOutput) -> list[str]:
    """What is wrong with a chase or clarification draft, as codes. Empty means it may go
    to approval: the RFQ reference is in the body, no prices, and it stays short."""
    problems: list[str] = []
    body = draft.body.strip()
    if not body:
        problems.append("empty")
    if task.rfq_ref not in body:
        problems.append("rfq_ref_missing_in_body")
    if _PRICE.search(body):
        problems.append("price_mentioned")
    if len(body) > _MAX_FOLLOWUP_CHARS:
        problems.append("too_long")
    return problems


# --- Deterministic checks on a negotiation draft ---------------------------------------

_MAX_NEGOTIATION_CHARS: Final = 5000
_NUMBER = re.compile(r"\d+(?:[.,\s']\d{3}(?!\d))*(?:[.,]\d+)?")
_PRICED_NUMBER = re.compile(
    r"(?:[€$£¥]|\b(?:EUR|USD|GBP|CHF|CNY|JPY|RMB|BRL)\b)\s?(\d[\d.,\s']*\d|\d)"
    r"|(\d[\d.,\s']*\d|\d)\s?(?:[€$£¥]|\b(?:EUR|USD|GBP|CHF|CNY|JPY|RMB|BRL)\b)",
    re.IGNORECASE,
)


def _readings(text: str) -> set[Decimal]:
    """Every value a written number may stand for (either decimal separator)."""
    raw = text.strip().replace(" ", "").replace("'", "")
    values: set[Decimal] = set()
    for decimal_sep, group_sep in ((".", ","), (",", ".")):
        candidate = raw.replace(group_sep, "").replace(decimal_sep, ".")
        if candidate.count(".") > 1:
            continue
        try:
            values.add(Decimal(candidate).normalize())
        except ArithmeticError:
            continue
    return values


def negotiation_email_problems(
    task: NegotiationEmailInput, draft: NegotiationEmailOutput
) -> list[str]:
    """What is wrong with a counter-offer or BAFO draft, as codes. Empty means it may go
    to approval.

    Code decided the numbers; this checks the model kept them: the RFQ
    reference is in the body, every target price appears, and every amount
    next to a currency is one of the prices in the task (ours or the
    supplier's own). It stays short.
    """
    problems: list[str] = []
    body = draft.body.strip()
    if not body:
        return ["empty"]
    if task.rfq_ref not in body:
        problems.append("rfq_ref_missing_in_body")
    written: set[Decimal] = set()
    for match in _NUMBER.finditer(body):
        written |= _readings(match.group(0))
    allowed: set[Decimal] = set()
    for line in task.lines:
        for value in (line.offered_unit_price, line.target_unit_price):
            if value is not None:
                allowed.add(value.normalize())
        if line.target_unit_price is not None and line.target_unit_price.normalize() not in written:
            problems.append(f"target_missing:{line.position}")
    for match in _PRICED_NUMBER.finditer(body):
        amount = match.group(1) or match.group(2)
        if amount is not None and not (_readings(amount) & allowed):
            problems.append("unknown_amount")
            break
    if len(body) > _MAX_NEGOTIATION_CHARS:
        problems.append("too_long")
    return problems


_MAX_NOTICE_CHARS: Final = 2000


def award_notice_problems(task: AwardNoticeInput, draft: AwardNoticeOutput) -> list[str]:
    """What is wrong with a "not selected" draft, as codes. Empty means it may go to
    approval: the RFQ reference is in the body, no prices, and it stays short."""
    problems: list[str] = []
    body = draft.body.strip()
    if not body:
        problems.append("empty")
    if task.rfq_ref not in body:
        problems.append("rfq_ref_missing_in_body")
    if _PRICE.search(body):
        problems.append("price_mentioned")
    if len(body) > _MAX_NOTICE_CHARS:
        problems.append("too_long")
    return problems
