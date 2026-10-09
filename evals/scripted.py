"""Mock mode for the flow evals: every agent answers from a script, nothing calls a model.

`ScriptedModel.installed()` swaps the one function every agent run goes
through (`agno_adapter.stream_run`) for a lookup by agent name, so the real
services, writers, checks and jobs run unchanged around it. The scripts are
what a correct model returns: intake answers the scenario's golden lines,
extraction the supplier's truth (registered when the supplier writes), and the
writers fixed sentences in the supplier's language with every number exactly
as given. Mock mode checks the harness and the deterministic code, not a model.
"""

from collections.abc import AsyncIterator, Callable, Generator
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Final

from pydantic import BaseModel

from bido_back.agents.catalog.contracts import (
    AwardNoticeInput,
    AwardNoticeOutput,
    FollowupEmailInput,
    FollowupEmailOutput,
    NegotiationEmailInput,
    NegotiationEmailOutput,
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
from evals.personas import number, written_date

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

_W: Final[dict[str, dict[str, str]]] = {
    "de": {
        "hello": "Sehr geehrte Damen und Herren,",
        "chase": "wir möchten freundlich an unsere Anfrage {ref} ({title}) erinnern und "
        "freuen uns auf Ihr Angebot.",
        "clarify": "vielen Dank für Ihr Angebot zu {ref}. Bitte ergänzen Sie noch:",
        "counter": "vielen Dank für Ihr Angebot zu {ref}. Wir bitten um folgende Anpassung:",
        "bafo": "vielen Dank für Ihr Angebot zu {ref}. Bitte senden Sie uns Ihr bestes und "
        "letztes Angebot:",
        "competition": "Uns liegt ein wettbewerbsfähigeres Angebot vor.",
        "price": "Pos. {p} {line}: Zielpreis {price} {cur} pro Stück",
        "lead": "Pos. {p} {line}: Lieferzeit höchstens {days} Tage",
        "by": "Bitte antworten Sie bis {date}.",
        "notice": "vielen Dank für Ihr Angebot zu {ref} ({title}). Wir haben uns diesmal für "
        "ein anderes Angebot entschieden und freuen uns auf künftige Anfragen.",
        "bye": "Mit freundlichen Grüßen",
    },
    "it": {
        "hello": "Buongiorno,",
        "chase": "vi ricordiamo la nostra richiesta {ref} ({title}) e restiamo in attesa "
        "della vostra offerta.",
        "clarify": "grazie per la vostra offerta per {ref}. Vi chiediamo di completare:",
        "counter": "grazie per la vostra offerta per {ref}. Vi chiediamo di rivedere:",
        "bafo": "grazie per la vostra offerta per {ref}. Vi chiediamo la vostra migliore "
        "offerta finale:",
        "competition": "Abbiamo ricevuto un'offerta più competitiva.",
        "price": "Pos. {p} {line}: prezzo obiettivo {price} {cur} cad.",
        "lead": "Pos. {p} {line}: consegna entro {days} giorni",
        "by": "Vi preghiamo di rispondere entro il {date}.",
        "notice": "grazie per la vostra offerta per {ref} ({title}). Questa volta abbiamo "
        "scelto un'altra offerta e restiamo a disposizione per le prossime richieste.",
        "bye": "Cordiali saluti",
    },
    "fr": {
        "hello": "Bonjour,",
        "chase": "nous nous permettons de vous rappeler notre demande {ref} ({title}) et "
        "attendons votre offre.",
        "clarify": "merci pour votre offre concernant {ref}. Merci de nous préciser :",
        "counter": "merci pour votre offre concernant {ref}. Nous vous demandons de revoir :",
        "bafo": "merci pour votre offre concernant {ref}. Merci de nous envoyer votre "
        "meilleure offre finale :",
        "competition": "Nous avons reçu une offre plus compétitive.",
        "price": "Pos. {p} {line} : prix cible {price} {cur} l'unité",
        "lead": "Pos. {p} {line} : délai de livraison de {days} jours maximum",
        "by": "Merci de nous répondre avant le {date}.",
        "notice": "merci pour votre offre concernant {ref} ({title}). Nous avons retenu une "
        "autre offre cette fois et restons à votre disposition pour nos prochaines demandes.",
        "bye": "Cordialement",
    },
    "en": {
        "hello": "Hello,",
        "chase": "we would like to remind you of our request {ref} ({title}) and look "
        "forward to your quotation.",
        "clarify": "thank you for your quotation for {ref}. Please complete the following:",
        "counter": "thank you for your quotation for {ref}. We ask you to review:",
        "bafo": "thank you for your quotation for {ref}. Please send us your best and final offer:",
        "competition": "We have received a more competitive offer.",
        "price": "Item {p} {line}: target price {price} {cur} each",
        "lead": "Item {p} {line}: lead time of at most {days} days",
        "by": "Please answer by {date}.",
        "notice": "thank you for your quotation for {ref} ({title}). This time we chose "
        "another offer and look forward to working with you on future requests.",
        "bye": "Best regards",
    },
}
_FIELD: Final[dict[str, dict[str, str]]] = {
    "de": {"currency": "Währung", "valid_until": "Gültigkeit des Angebots",
           "unit_price": "Stückpreis", "quantity": "Menge", "lead_time": "Lieferzeit"},
    "it": {"currency": "valuta", "valid_until": "validità dell'offerta",
           "unit_price": "prezzo unitario", "quantity": "quantità",
           "lead_time": "tempi di consegna"},
    "fr": {"currency": "devise", "valid_until": "validité de l'offre",
           "unit_price": "prix unitaire", "quantity": "quantité",
           "lead_time": "délai de livraison"},
    "en": {"currency": "currency", "valid_until": "offer validity",
           "unit_price": "unit price", "quantity": "quantity", "lead_time": "lead time"},
}  # fmt: skip


def _lang(language: str) -> str:
    return language if language in _W else "en"


def _t(language: str, key: str, **values: object) -> str:
    return _W[_lang(language)][key].format(**values)


def followup(task: FollowupEmailInput) -> FollowupEmailOutput:
    lang = _lang(task.supplier_language)
    if task.kind == "chase":
        middle = _t(lang, "chase", ref=task.rfq_ref, title=task.rfq_title)
    else:
        fields = _FIELD[lang]
        items = [
            f"- {item.line + ': ' if item.line else ''}{fields[item.field]}"
            for item in task.open_items
        ]
        middle = "\n".join([_t(lang, "clarify", ref=task.rfq_ref), *items])
    return FollowupEmailOutput(
        body=f"{_t(lang, 'hello')}\n\n{middle}\n\n{_t(lang, 'bye')}\n{task.buyer_name}"
    )


def negotiation(task: NegotiationEmailInput) -> NegotiationEmailOutput:
    lang = _lang(task.supplier_language)
    lines: list[str] = [_t(lang, task.action, ref=task.rfq_ref)]
    if task.mention_competition:
        lines.insert(0, _t(lang, "competition"))
    for ask in task.lines:
        if ask.target_unit_price is not None:
            price = number(Decimal(ask.target_unit_price), lang)
            lines.append(_t(lang, "price", p=ask.position, line=ask.line, price=price,
                            cur=ask.currency))  # fmt: skip
        if ask.required_lead_time_days is not None:
            lines.append(_t(lang, "lead", p=ask.position, line=ask.line,
                            days=ask.required_lead_time_days))  # fmt: skip
    if task.reply_by is not None:
        lines.append(_t(lang, "by", date=written_date(task.reply_by, lang)))
    body = "\n".join(lines)
    return NegotiationEmailOutput(
        body=f"{_t(lang, 'hello')}\n\n{body}\n\n{_t(lang, 'bye')}\n{task.buyer_name}"
    )


def notice(task: AwardNoticeInput) -> AwardNoticeOutput:
    lang = _lang(task.supplier_language)
    middle = _t(lang, "notice", ref=task.rfq_ref, title=task.rfq_title)
    return AwardNoticeOutput(
        body=f"{_t(lang, 'hello')}\n\n{middle}\n\n{_t(lang, 'bye')}\n{task.buyer_name}"
    )
