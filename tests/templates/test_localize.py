"""Localizing the buyer's template text: no model call for the same language, safe fallback."""

from uuid import uuid4

from bido_back.agents.catalog.contracts import RfqEmailLocalizeInput, RfqEmailLocalizeOutput
from bido_back.agents.context import ToolContext
from bido_back.onboarding.standard import STANDARD_TEMPLATE
from bido_back.templates.localize import localize

CONTEXT = ToolContext(tenant_id=uuid4(), user_id=uuid4(), run_id=uuid4())


class Writer:
    def __init__(self, drop_one: bool = False) -> None:
        self.calls: list[RfqEmailLocalizeInput] = []
        self.drop_one = drop_one

    async def __call__(
        self, input: RfqEmailLocalizeInput, context: ToolContext
    ) -> RfqEmailLocalizeOutput:
        self.calls.append(input)
        labels = [f"de:{label}" for label in input.labels]
        return RfqEmailLocalizeOutput(
            greeting=f"de:{input.greeting}", labels=labels[:-1] if self.drop_one else labels
        )


async def test_same_language_uses_the_buyers_words_without_a_model_call() -> None:
    writer = Writer()

    shown = await localize(STANDARD_TEMPLATE, "pt-BR", writer, CONTEXT)

    assert shown is STANDARD_TEMPLATE
    assert writer.calls == []


async def test_other_language_translates_greeting_and_labels_in_order() -> None:
    writer = Writer()

    shown = await localize(STANDARD_TEMPLATE, "de", writer, CONTEXT)

    [task] = writer.calls
    assert task.labels == [*STANDARD_TEMPLATE.columns, *STANDARD_TEMPLATE.footer_fields]
    assert shown.language == "de"
    assert shown.greeting == f"de:{STANDARD_TEMPLATE.greeting}"
    assert shown.columns == [f"de:{c}" for c in STANDARD_TEMPLATE.columns]
    assert shown.footer_fields == [f"de:{f}" for f in STANDARD_TEMPLATE.footer_fields]


async def test_a_wrong_label_count_keeps_the_labels_untranslated() -> None:
    shown = await localize(STANDARD_TEMPLATE, "de", Writer(drop_one=True), CONTEXT)

    assert shown.greeting == f"de:{STANDARD_TEMPLATE.greeting}"
    assert shown.columns == STANDARD_TEMPLATE.columns
    assert shown.footer_fields == STANDARD_TEMPLATE.footer_fields
