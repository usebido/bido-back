"""Carry a template's own text (greeting, headings, footer fields) into the supplier's language.

Same language: the buyer's words are used exactly as written, no model call.
Otherwise the email writer agent translates them; it never writes the email.
If it returns the wrong number of labels, the headings and footer fields stay
in the buyer's language (logged) rather than being guessed or misaligned.
"""

import logging

from bido_back.agents.catalog.contracts import RfqEmailLocalizeInput
from bido_back.agents.context import ToolContext
from bido_back.onboarding.standard import TemplateContent
from bido_back.rfqs.agents import EmailWriterAgent

logger = logging.getLogger(__name__)


def same_language(a: str, b: str) -> bool:
    return a.strip().lower()[:2] == b.strip().lower()[:2]


async def localize(
    content: TemplateContent, language: str, writer: EmailWriterAgent, context: ToolContext
) -> TemplateContent:
    """`content` shown in `language`: same columns and footer fields, same order."""
    if same_language(content.language, language):
        return content
    labels = [*content.columns, *content.footer_fields]
    out = await writer(
        RfqEmailLocalizeInput(
            source_language=content.language,
            target_language=language,
            greeting=content.greeting,
            labels=labels,
        ),
        context,
    )
    greeting = out.greeting.strip() or content.greeting
    translated = [label.strip() for label in out.labels]
    if len(translated) != len(labels) or not all(translated):
        logger.warning(
            "template labels not localized run=%s expected=%d got=%d",
            context.run_id,
            len(labels),
            len(translated),
        )
        translated = labels
    columns = translated[: len(content.columns)]
    footer_fields = translated[len(content.columns) :]
    return TemplateContent(
        language=language,
        greeting=greeting,
        columns=columns,
        footer_fields=footer_fields,
        fields=list(content.fields),
    )
