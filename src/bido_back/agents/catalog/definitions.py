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
    """v2: carries the buyer's template text into the supplier's language.

    The RFQ email is rendered by code (`bido_back.templates.render`); v1 wrote
    the whole email and is kept only as the released prompt it was.
    """
    return AgentDefinition(
        name="rfq_email_writer",
        prompt_file="rfq_email_writer.v2.md",
        prompt_version=2,
        model=model,
        output_model=RfqEmailLocalizeOutput,
        max_turns=_MAX_TURNS,
        effort="low",
    )


def quote_extraction(model: str = EXTRACTION_MODEL) -> AgentDefinition[QuoteExtractionOutput]:
    return AgentDefinition(
        name="quote_extraction",
        prompt_file="quote_extraction.v4.md",
        prompt_version=4,
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
    """v2: does not sign (no buyer name or company in its input); code adds the signature."""
    return AgentDefinition(
        name="rfq_followup_writer",
        prompt_file="rfq_followup_writer.v2.md",
        prompt_version=2,
        model=model,
        output_model=FollowupEmailOutput,
        max_turns=_MAX_TURNS,
        thinking=False,
        effort="low",
    )


def rfq_negotiation_writer(
    model: str = DEFAULT_MODEL,
) -> AgentDefinition[NegotiationEmailOutput]:
    """v2: does not sign (code does) and writes every number, lead times too, in digits."""
    return AgentDefinition(
        name="rfq_negotiation_writer",
        prompt_file="rfq_negotiation_writer.v2.md",
        prompt_version=2,
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
    """v2: does not sign (no buyer name or company in its input); code adds the signature."""
    return AgentDefinition(
        name="award_notice_writer",
        prompt_file="award_notice_writer.v2.md",
        prompt_version=2,
        model=model,
        output_model=AwardNoticeOutput,
        max_turns=_MAX_TURNS,
        thinking=False,
        effort="low",
    )


AWARD_NOTICE_WRITER: Final = award_notice_writer()


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

_CURRENCY: Final = r"(?:R\$|US\$|[€$£¥]|\b(?:EUR|USD|GBP|CHF|CNY|JPY|RMB|BRL)\b)"
# What may sit between a currency and its number: a space, a no-break space or a
# narrow no-break space. Never a line break: "BRL\n2." is a code and a list number.
_GAP: Final = r"[ \u00a0\u202f]"

# A number next to a currency symbol or code: `12.50 EUR`, `€ 0,45`, `USD 3`, `18,00 R$`.
_PRICE = re.compile(
    rf"{_CURRENCY}{_GAP}?\d|\d(?:[\d.,]*\d)?{_GAP}?{_CURRENCY}",
    re.IGNORECASE,
)


def rfq_email_problems(task: RfqEmailInput, draft: RfqEmailOutput) -> list[str]:
    """What is wrong with an RFQ email draft, as codes. Empty means it may go to approval.

    The model writes the email; code checks the parts that must hold: the RFQ
    reference in subject and body, every requested MPN listed, the buyer's payment
    terms when set, no prices.
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
# One written number, on one line: digits, thousands grouped by `.`, `,`, `'` or a
# (no-break) space before three digits, and decimals. A separator always sits between
# two digits, so "18," ends the number, and "BRL 18\n2. Eixo" reads 18, never 182.
_DIGITS: Final = r"\d+(?:[.,']\d+|[ \u00a0\u202f]\d{3}(?!\d))*"
# Not part of a code: the 100 in "MTR-100" or the 6 in "RFQ-6" is no amount.
_STANDALONE: Final = r"(?<![\w-])"
_NUMBER = re.compile(_STANDALONE + _DIGITS)
_PRICED_NUMBER = re.compile(
    rf"{_CURRENCY}{_GAP}?({_DIGITS})|{_STANDALONE}({_DIGITS}){_GAP}?{_CURRENCY}",
    re.IGNORECASE,
)
_GROUPING: Final = re.compile(r"[ \u00a0\u202f']")


def _readings(text: str) -> set[Decimal]:
    """Every value a written number may stand for (either decimal separator)."""
    raw = _GROUPING.sub("", text.strip())
    values: set[Decimal] = set()
    for decimal_sep, group_sep in ((".", ","), (",", ".")):
        candidate = raw.replace(group_sep, "").replace(decimal_sep, ".")
        if candidate.count(".") > 1:
            continue
        try:
            values.add(Decimal(candidate))
        except ArithmeticError:
            continue
    return values


def negotiation_email_problems(
    task: NegotiationEmailInput, draft: NegotiationEmailOutput
) -> list[str]:
    """What is wrong with a counter-offer or BAFO draft, as codes. Empty means it may go
    to approval.

    Code decided the numbers; this checks the model kept them, against the
    task's asks as numbers (never as text): the RFQ reference is in the body,
    every target price and every required lead time appears, and every amount
    next to a currency is one of the prices in the task (ours or the
    supplier's own). Numbers are read one line at a time and never inside a
    code ("MTR-100"). It stays short.
    """
    problems: list[str] = []
    body = draft.body.strip()
    if not body:
        return ["empty"]
    if task.rfq_ref not in body:
        problems.append("rfq_ref_missing_in_body")
    priced = [m.group(1) or m.group(2) for m in _PRICED_NUMBER.finditer(body)]
    written: set[Decimal] = set()
    for text in (*(m.group(0) for m in _NUMBER.finditer(body)), *priced):
        written |= _readings(text)
    allowed: set[Decimal] = set()
    for line in task.lines:
        allowed |= {v for v in (line.offered_unit_price, line.target_unit_price) if v is not None}
        if line.target_unit_price is not None and line.target_unit_price not in written:
            problems.append(f"target_missing:{line.position}")
        lead = line.required_lead_time_days
        if lead is not None and Decimal(lead) not in written:
            problems.append(f"lead_time_missing:{line.position}")
    if any(not (_readings(amount) & allowed) for amount in priced):
        problems.append("unknown_amount")
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
