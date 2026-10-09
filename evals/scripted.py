"""Mock mode for the flow evals: every agent answers from a script, nothing calls a model.

`ScriptedModel.installed()` swaps the one function every agent run goes
through (`agno_adapter.stream_run`) for a lookup by agent name, so the real
services, writers, checks and jobs run unchanged around it. The scripts are
what a correct model returns: intake answers the scenario's golden lines,
extraction the supplier's truth (registered when the supplier writes), and the
thread writers no note (code renders their emails). Mock mode checks the
harness and the deterministic code, not a model.
"""

from collections.abc import AsyncIterator, Callable, Generator
from contextlib import contextmanager
from typing import Any, Final

from pydantic import BaseModel

from bido_back.agents.catalog.contracts import (
    AwardNoticeInput,
    EmailNoteOutput,
    FollowupEmailInput,
    NegotiationEmailInput,
    PoResponseInput,
    PoResponseOutput,
    QuoteExtractionInput,
    QuoteExtractionOutput,
    RfqEmailLocalizeInput,
    RfqEmailLocalizeOutput,
    RfqIntakeInput,
    RfqIntakeOutput,
)
from bido_back.agents.context import ToolContext
from bido_back.agents.events import Event, RunCompleted, RunFailed

Script = Callable[[Any], BaseModel | None]


class ScriptedModel:
    def __init__(self) -> None:
        self.intake: dict[str, RfqIntakeOutput] = {}
        self.extraction: dict[str, QuoteExtractionOutput] = {}
        # A supplier's reply to a purchase order (its body) -> what a correct reader says.
        self.po_responses: dict[str, PoResponseOutput] = {}
        self.calls: list[str] = []
        self._scripts: dict[str, Script] = {
            "rfq_intake": self._intake,
            "quote_extraction": self._extract,
            "rfq_email_writer": localize,
            "rfq_followup_writer": followup,
            "rfq_negotiation_writer": negotiation,
            "award_notice_writer": notice,
            "po_response_reader": self._po_response,
        }

    def _intake(self, task: RfqIntakeInput) -> RfqIntakeOutput | None:
        return self.intake.get(task.text.strip())

    def _extract(self, task: QuoteExtractionInput) -> QuoteExtractionOutput | None:
        return self.extraction.get(task.email_body.strip())

    def _po_response(self, task: PoResponseInput) -> PoResponseOutput | None:
        return self.po_responses.get(task.email_body.strip())

    async def stream_run(
        self, definition: Any, input: BaseModel, context: ToolContext, **_: object
    ) -> AsyncIterator[Event]:
        name = str(definition.name)
        self.calls.append(name)
        script = self._scripts.get(name)
        output = script(input) if script is not None else None
        if output is None:
            yield RunFailed(
                run_id=context.run_id, seq=0, code="no_script", message=f"no script for {name}"
            )
            return
        yield RunCompleted(run_id=context.run_id, seq=0, output=output.model_dump(mode="json"))

    @contextmanager
    def installed(self) -> Generator["ScriptedModel"]:
        from bido_back.agents import agno_adapter

        original = agno_adapter.stream_run
        agno_adapter.stream_run = self.stream_run  # pyright: ignore[reportAttributeAccessIssue]
        try:
            yield self
        finally:
            agno_adapter.stream_run = original


# --- Template localization (the evals' Portuguese template) -----------------------------------

GREETING_PT: Final = "Prezados, solicitamos cotação para os itens abaixo."
LABELS_PT: Final = (
    "Item",
    "Descrição",
    "Código do fabricante",
    "Quantidade",
    "Prazo de entrega",
    "Validade da proposta",
    "Condições de pagamento",
)
_LOCALIZED: Final[dict[str, tuple[str, tuple[str, ...]]]] = {
    "de": (
        "Sehr geehrte Damen und Herren, wir bitten um ein Angebot für die folgenden Positionen.",
        ("Pos.", "Bezeichnung", "Herstellerteilenummer", "Menge", "Lieferzeit",
         "Angebotsgültigkeit", "Zahlungsbedingungen"),
    ),
    "it": (
        "Buongiorno, vi chiediamo un'offerta per gli articoli seguenti.",
        ("Pos.", "Descrizione", "Codice produttore", "Quantità", "Tempi di consegna",
         "Validità dell'offerta", "Condizioni di pagamento"),
    ),
    "fr": (
        "Bonjour, nous vous prions de nous faire une offre pour les articles suivants.",
        ("Pos.", "Désignation", "Référence fabricant", "Quantité", "Délai de livraison",
         "Validité de l'offre", "Conditions de paiement"),
    ),
    "en": (
        "Hello, please send us your quotation for the items below.",
        ("Item", "Description", "Manufacturer part number", "Quantity", "Lead time",
         "Offer validity", "Payment terms"),
    ),
}  # fmt: skip
# The standard template (no buyer template yet): its Portuguese labels, translated.
_STANDARD: Final[dict[str, tuple[str, ...]]] = {
    "de": ("Pos.", "Menge", "Einheit", "Lieferzeit", "Angebotsgültigkeit", "Zahlungsbedingung"),
    "it": ("Pos.", "Quantità", "Unità", "Tempi di consegna", "Validità dell'offerta",
           "Condizione di pagamento"),
    "fr": ("Pos.", "Quantité", "Unité", "Délai", "Validité de l'offre",
           "Condition de paiement"),
    "en": ("Item", "Qty", "Unit", "Lead time", "Offer validity", "Payment terms"),
}  # fmt: skip


def localize(task: RfqEmailLocalizeInput) -> RfqEmailLocalizeOutput | None:
    found = _LOCALIZED.get(task.target_language)
    if found is None:
        return None
    greeting, labels = found
    if tuple(task.labels) != LABELS_PT:
        standard = _STANDARD[task.target_language]
        if len(task.labels) != len(standard):
            return None
        labels = standard
    return RfqEmailLocalizeOutput(greeting=greeting, labels=list(labels))


# --- Writers --------------------------------------------------------------------------------
#
# Code renders every follow-up, negotiation and "not selected" email (`bido_back.supplier_email`):
# the writers' v2 prompts only allow an optional note, empty by default. A correct model
# returns none, and so do these scripts.


def followup(task: FollowupEmailInput) -> EmailNoteOutput:
    return EmailNoteOutput(note="")


def negotiation(task: NegotiationEmailInput) -> EmailNoteOutput:
    return EmailNoteOutput(note="")


def notice(task: AwardNoticeInput) -> EmailNoteOutput:
    return EmailNoteOutput(note="")
