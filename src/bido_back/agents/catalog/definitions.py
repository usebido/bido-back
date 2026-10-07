"""The RFQ-flow agents: intake, RFQ email writer, quote extraction, and the two
onboarding agents (sent-mail candidate classifier, RFQ template builder).

All of them are single-shot structured output: no tools at all. In particular
the extraction agent reads supplier content, which is untrusted (prompt
injection), so it gets nothing it could act with: no `act` tools, no tools
that send, no tools at all. Its output is data that `bido_back.quotes`
validates before anything is stored.

Each agent has a factory taking the model id, so the caller can choose it
from configuration; the module constants use `DEFAULT_MODEL`, except quote
extraction, which uses `EXTRACTION_MODEL`.
"""

import re
from typing import Final

from bido_back.agents.catalog.contracts import (
    FollowupEmailInput,
    FollowupEmailOutput,
    QuoteExtractionOutput,
    RfqCandidateClassifierOutput,
    RfqEmailInput,
    RfqEmailLocalizeOutput,
    RfqEmailOutput,
    RfqIntakeOutput,
    RfqTemplateBuilderOutput,
)
from bido_back.agents.definition import AgentDefinition

DEFAULT_MODEL: Final = "gpt-6-luna"
# Quote extraction turns supplier documents into the prices the buyer compares,
# so it runs on the stronger model.
EXTRACTION_MODEL: Final = "gpt-6.1-sol"
# The template is learned once per tenant from a handful of documents.
TEMPLATE_MODEL: Final = "gpt-6.1-sol"

# One request for the answer plus one spare, in case the model's first final
# answer does not validate and Agno asks again.
_MAX_TURNS: Final = 2


def rfq_intake(model: str = DEFAULT_MODEL) -> AgentDefinition[RfqIntakeOutput]:
    return AgentDefinition(
        name="rfq_intake",
        prompt_file="rfq_intake.v1.md",
        prompt_version=1,
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
        prompt_file="quote_extraction.v1.md",
        prompt_version=1,
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


RFQ_INTAKE: Final = rfq_intake()
RFQ_EMAIL_WRITER: Final = rfq_email_writer()
QUOTE_EXTRACTION: Final = quote_extraction()
RFQ_CANDIDATE_CLASSIFIER: Final = rfq_candidate_classifier()
RFQ_TEMPLATE_BUILDER: Final = rfq_template_builder()
RFQ_FOLLOWUP_WRITER: Final = rfq_followup_writer()


# --- Deterministic checks on the RFQ email draft ---------------------------------

# A number next to a currency symbol or code: `12.50 EUR`, `€ 0,45`, `USD 3`.
_PRICE = re.compile(
    r"(?:[€$£¥]|\b(?:EUR|USD|GBP|CHF|CNY|JPY|RMB)\b)\s?\d"
    r"|\d(?:[\d.,]*\d)?\s?(?:[€$£¥]|\b(?:EUR|USD|GBP|CHF|CNY|JPY|RMB)\b)",
    re.IGNORECASE,
)


def rfq_email_problems(task: RfqEmailInput, draft: RfqEmailOutput) -> list[str]:
    """What is wrong with an RFQ email draft, as codes. Empty means it may go to approval.

    The model writes the email; code checks the parts that must hold: the RFQ
    reference in subject and body, every requested MPN listed, no prices.
    """
    problems: list[str] = []
    if task.rfq_ref not in draft.subject:
        problems.append("rfq_ref_missing_in_subject")
    if task.rfq_ref not in draft.body:
        problems.append("rfq_ref_missing_in_body")
    for line in task.lines:
        if line.mpn and line.mpn not in draft.body:
            problems.append(f"mpn_missing:{line.position}")
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
