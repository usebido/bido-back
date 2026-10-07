"""The RFQ-flow agents: intake, RFQ email writer, quote extraction.

All three are single-shot structured output: no tools at all. In particular
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
    QuoteExtractionOutput,
    RfqEmailInput,
    RfqEmailOutput,
    RfqIntakeOutput,
)
from bido_back.agents.definition import AgentDefinition

DEFAULT_MODEL: Final = "gpt-6-luna"
# Quote extraction turns supplier documents into the prices the buyer compares,
# so it runs on the stronger model.
EXTRACTION_MODEL: Final = "gpt-6.1-sol"

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


def rfq_email_writer(model: str = DEFAULT_MODEL) -> AgentDefinition[RfqEmailOutput]:
    return AgentDefinition(
        name="rfq_email_writer",
        prompt_file="rfq_email_writer.v1.md",
        prompt_version=1,
        model=model,
        output_model=RfqEmailOutput,
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


RFQ_INTAKE: Final = rfq_intake()
RFQ_EMAIL_WRITER: Final = rfq_email_writer()
QUOTE_EXTRACTION: Final = quote_extraction()


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
