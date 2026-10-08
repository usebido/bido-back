"""Copilot conversation titles, the way chat apps do them.

A conversation starts with its first message as the title, truncated
(`fallback`, set in `service._record_turn`). After each of the first turns,
while the title is not settled, the `conversation_titler` agent reads the
conversation so far and proposes a short title plus whether the
conversation's purpose is now clear (`settled`). A vague opener ("oi",
"vai no que dá", files alone) stays unsettled and is refined on the next
turn; once settled, or after `MAX_TITLED_TURNS` turns, the titler stops.
A proposal only replaces the stored title when it differs meaningfully, so
the list does not flicker.

Milestones are deterministic and need no model: creating an RFQ, running an
analysis or preparing an award writes a title with the reference
("RFQ-3 · Necessidades BR01"), once per kind, and settles the title.

A buyer's rename (`user`) is final: nothing here changes it afterwards.
"""

import difflib
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Final, Literal

from pydantic import BaseModel, Field, JsonValue

from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.agents.definition import AgentDefinition

logger = logging.getLogger(__name__)

TITLER_MODEL: Final = "gpt-6-luna"
# A proposed title is cut to this, at a word boundary.
AI_TITLE_CHARS: Final = 60
# Any stored title (a rename, a milestone) fits this.
TITLE_MAX_CHARS: Final = 120
# The titler runs after turns 1..MAX_TITLED_TURNS at most.
MAX_TITLED_TURNS: Final = 5
# What the titler reads per turn: enough to tell the topic, no more.
MESSAGE_SLICE: Final = 600
ANSWER_SLICE: Final = 400
# Proposals at least this similar to the stored title are not applied.
SAME_TITLE_RATIO: Final = 0.9

TitleSource = Literal["fallback", "ai", "milestone", "user"]
MilestoneKind = Literal["analysis", "rfq", "award"]


# --- The titler agent -------------------------------------------------------------------


class TitleTurn(BaseModel):
    message: str = Field(description="What the buyer wrote (empty when they only sent files)")
    attachments: list[str] = Field(default_factory=list[str], description="Attached file names")
    answer: str = Field(default="", description="The start of the copilot's reply")
    tools: list[str] = Field(default_factory=list[str], description="Tools the copilot used")


class ConversationTitlerInput(BaseModel):
    current_title: str = Field(description="The title the conversation has now")
    turns: list[TitleTurn] = Field(description="The conversation so far, oldest first")


class ConversationTitlerOutput(BaseModel):
    title: str = Field(description="3 to 6 words in the buyer's language; no quotes or period")
    settled: bool = Field(
        description=(
            "True once the conversation's purpose is clear (a concrete purchase, RFQ, "
            "analysis, supplier or quote topic); false for greetings, vague or "
            "files-only openers and clarifying exchanges"
        )
    )
    language: str | None = Field(
        default=None, description="The buyer's language as an ISO 639-1 code, e.g. pt, en, it"
    )


def conversation_titler(model: str = TITLER_MODEL) -> AgentDefinition[ConversationTitlerOutput]:
    return AgentDefinition(
        name="conversation_titler",
        prompt_file="conversation_titler.v1.md",
        prompt_version=1,
        model=model,
        output_model=ConversationTitlerOutput,
        # One request plus a spare if the first answer does not validate.
        max_turns=2,
        max_tokens=400,
        thinking=False,
        effort="low",
    )


CONVERSATION_TITLER: Final = conversation_titler()

Titler = Callable[[ConversationTitlerInput, ToolContext], Awaitable[ConversationTitlerOutput]]


class TitlerFailed(Exception):
    """The titler ended without a usable output. The message is a code."""


def default_titler(config: RunnerConfig) -> Titler:
    async def titler(
        input: ConversationTitlerInput, context: ToolContext
    ) -> ConversationTitlerOutput:
        from bido_back.agents.runner import Completed, Failed, run_to_completion

        result = await run_to_completion(CONVERSATION_TITLER, input, context, config=config)
        if isinstance(result, Completed):
            return result.output
        raise TitlerFailed(
            result.failure.code if isinstance(result, Failed) else "unexpected_approval"
        )

    return titler


def titler_input(current_title: str, turns: Sequence[TitleTurn]) -> ConversationTitlerInput:
    return ConversationTitlerInput(
        current_title=current_title,
        turns=[
            TitleTurn(
                message=t.message[:MESSAGE_SLICE],
                attachments=t.attachments[:10],
                answer=t.answer[:ANSWER_SLICE],
                tools=t.tools[:20],
            )
            for t in turns[-MAX_TITLED_TURNS:]
        ],
    )


# --- Cleaning and comparing titles -----------------------------------------------------

_EMAIL = re.compile(r"\S+@\S+")
_QUOTES = "\"'“”‘’«»`"


def clean_title(raw: str, limit: int = AI_TITLE_CHARS) -> str | None:
    """A model's title made safe to show: no emails or quotes, one line, cut at a word."""
    text = _EMAIL.sub("", raw)
    text = " ".join(text.split()).strip(_QUOTES + " ")
    text = text.rstrip(".;:,!").strip()
    if not text:
        return None
    if len(text) > limit:
        cut = text[:limit].rsplit(" ", 1)[0].rstrip(" -·,;:")
        text = cut or text[:limit]
    return text


def stored_title(raw: str) -> str:
    """A title the buyer or a milestone sets: one line, at most `TITLE_MAX_CHARS`."""
    text = " ".join(raw.split())
    if len(text) <= TITLE_MAX_CHARS:
        return text
    return text[: TITLE_MAX_CHARS - 1].rstrip() + "…"


def _normal(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in folded if c.isalnum())


def differs(old: str, new: str) -> bool:
    """Whether `new` is worth replacing `old` with (not just case, accents or a word)."""
    a, b = _normal(old), _normal(new)
    if a == b:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() < SAME_TITLE_RATIO


# --- Milestones -------------------------------------------------------------------------

_ANALYSIS_TOOLS: Final = frozenset({"run_purchase_analysis", "analyze_requirements"})
_RFQ_TOOLS: Final = frozenset(
    {"create_rfq", "create_rfq_from_analysis", "create_rfq_from_assessment"}
)
_AWARD_TOOLS: Final = frozenset({"prepare_award"})

_ANALYSIS_LABEL: Final = {
    "pt": "Análise de compra",
    "en": "Purchase analysis",
    "it": "Analisi acquisti",
    "es": "Análisis de compra",
    "de": "Einkaufsanalyse",
    "fr": "Analyse d'achat",
}
_AWARD_LABEL: Final = {
    "pt": "Vencedor escolhido",
    "en": "Award",
    "it": "Aggiudicazione",
    "es": "Adjudicación",
    "de": "Vergabe",
    "fr": "Attribution",
}


class Milestone(BaseModel):
    kind: MilestoneKind
    title: str


def _label(labels: dict[str, str], language: str | None) -> str:
    code = (language or "").split("-")[0].lower()
    return labels.get(code, labels["en"])


def _text(output: Mapping[str, JsonValue], key: str) -> str | None:
    value = output.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def milestone(
    actions: Sequence[tuple[str, bool, Mapping[str, JsonValue] | None]],
    *,
    language: str | None,
    base_title: str,
) -> Milestone | None:
    """The most advanced milestone a turn reached, from its draft actions.

    `actions` are (tool, ok, output) in order. An award beats an RFQ, which
    beats an analysis; within a kind the last one wins.
    """
    found: dict[MilestoneKind, Mapping[str, JsonValue]] = {}
    for tool, ok, output in actions:
        if not ok or output is None:
            continue
        if tool in _AWARD_TOOLS:
            found["award"] = output
        elif tool in _RFQ_TOOLS:
            found["rfq"] = output
        elif tool in _ANALYSIS_TOOLS:
            found["analysis"] = output
    if (output := found.get("award")) is not None:
        ref = _text(output, "ref")
        label = _label(_AWARD_LABEL, language)
        return Milestone(kind="award", title=stored_title(f"{ref} · {label}" if ref else label))
    if (output := found.get("rfq")) is not None:
        ref = _text(output, "ref")
        subject = _text(output, "title") or base_title
        return Milestone(kind="rfq", title=stored_title(f"{ref} · {subject}" if ref else subject))
    if (output := found.get("analysis")) is not None:
        subject = _text(output, "product") or base_title
        label = _label(_ANALYSIS_LABEL, language)
        return Milestone(kind="analysis", title=stored_title(f"{label} · {subject}"))
    return None


def log_titler_failure(context: ToolContext, code: str) -> None:
    logger.warning(
        "conversation titler failed",
        extra={"run_id": str(context.run_id), "code": code},
    )
