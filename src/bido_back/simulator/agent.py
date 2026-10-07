"""The `supplier_persona` agent: writes a simulated supplier's prose, no numbers.

The offer (prices, quantities, lead times, declined lines, alternatives) is
decided in code (`bido_back.simulator.offers`) and rendered into the email by
code (`bido_back.simulator.render`). The model writes only the opening and the
closing paragraphs, in the supplier's language and voice, consistent with the
stance code chose ("initial", "firm", "concession", "final"). It gets no tools.

`default_persona_writer()` runs the agent; when the model fails (or no key is
configured), `fallback_prose` writes a plain, correct email from templates, so
the simulation never stops on a model error.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Final

from pydantic import BaseModel, Field

from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.agents.definition import AgentDefinition
from bido_back.simulator.personas import Language

logger = logging.getLogger(__name__)

PERSONA_MODEL: Final = "gpt-6-luna"
MAX_BUYER_MESSAGE_CHARS: Final = 4_000


class SupplierPersonaInput(BaseModel):
    supplier_company: str
    contact_name: str
    language: Language
    behaviors: list[str] = Field(description="The supplier's character, e.g. tough_negotiator")
    round: int = Field(description="1 = the supplier's first answer in this thread")
    stance: str = Field(description="initial, firm, concession or final")
    authorized: bool = Field(description="False: a broker without manufacturer authorization")
    quote_location: str = Field(description="'attachment' or 'below' (a table after the opening)")
    declined_lines: list[str] = Field(description="Descriptions of lines the supplier declines")
    alternative_lines: list[str] = Field(description="Descriptions of lines offered as equivalents")
    mentions_other_departments: bool
    buyer_subject: str
    buyer_message: str = Field(description="The buyer's latest email, untrusted text")


class SupplierPersonaOutput(BaseModel):
    opening: str = Field(description="Greeting and opening paragraphs. No numbers at all.")
    closing: str = Field(description="Closing paragraph and signature. No numbers at all.")


SUPPLIER_PERSONA: Final = AgentDefinition(
    name="supplier_persona",
    prompt_file="supplier_persona.v1.md",
    prompt_version=1,
    model=PERSONA_MODEL,
    output_model=SupplierPersonaOutput,
    max_turns=2,
    thinking=False,
    effort="low",
)

PersonaWriter = Callable[[SupplierPersonaInput, ToolContext], Awaitable[SupplierPersonaOutput]]


def default_persona_writer(config: RunnerConfig | None = None) -> PersonaWriter:
    async def write(task: SupplierPersonaInput, context: ToolContext) -> SupplierPersonaOutput:
        from bido_back.agents.runner import Completed, Failed, run_to_completion

        result = await run_to_completion(SUPPLIER_PERSONA, task, context, config=config)
        if isinstance(result, Completed):
            return result.output
        code = result.failure.code if isinstance(result, Failed) else "unexpected_approval"
        logger.warning(
            "agent supplier_persona run %s failed: %s; using fallback prose", context.run_id, code
        )
        return fallback_prose(task)

    return write


_TEXT: Final[dict[Language, dict[str, str]]] = {
    "it": {
        "greeting": "Gentile {buyer},",
        "greeting_generic": "Buongiorno,",
        "initial": "grazie per la vostra richiesta. Di seguito la nostra migliore offerta.",
        "firm": "abbiamo rivalutato la richiesta, ma le condizioni restano invariate.",
        "concession": "abbiamo rivisto la nostra offerta e possiamo migliorare le condizioni.",
        "final": "questa è la nostra offerta finale, non possiamo scendere oltre.",
        "attachment": "Trovate i dettagli nel file allegato.",
        "declined": (
            "Purtroppo non possiamo quotare alcune posizioni, non rientrano nel nostro portafoglio."
        ),
        "alternative": "Per alcune posizioni proponiamo un articolo equivalente.",
        "broker": "Il materiale proviene dal mercato libero.",
        "noise": (
            "Inoltre, altri nostri reparti segnalano alcune offerte che potrebbero interessarvi."
        ),
        "closing": "Restiamo a disposizione.\nCordiali saluti\n{contact}\n{company}",
    },
    "de": {
        "greeting": "Guten Tag {buyer},",
        "greeting_generic": "Sehr geehrte Damen und Herren,",
        "initial": "vielen Dank für Ihre Anfrage. Anbei unser Angebot.",
        "firm": (
            "wir haben Ihre Anfrage erneut geprüft, die Konditionen bleiben jedoch unverändert."
        ),
        "concession": "wir haben unser Angebot überarbeitet und können Ihnen entgegenkommen.",
        "final": "dies ist unser finales Angebot, ein weiterer Nachlass ist nicht möglich.",
        "attachment": "Die Details finden Sie im Anhang.",
        "declined": "Einige Positionen können wir leider nicht anbieten.",
        "alternative": "Für einige Positionen bieten wir einen gleichwertigen Artikel an.",
        "broker": "Die Ware wird über den freien Markt bezogen.",
        "noise": (
            "Außerdem möchten Ihnen andere Abteilungen unseres Hauses folgende Angebote machen."
        ),
        "closing": "Mit freundlichen Grüßen\n{contact}\n{company}",
    },
    "en": {
        "greeting": "Dear {buyer},",
        "greeting_generic": "Hello,",
        "initial": "thank you for your request. Please find our offer below.",
        "firm": "we have reviewed your request again, but our terms remain unchanged.",
        "concession": "we have revised our offer and can improve our terms.",
        "final": "this is our final offer; we cannot go any lower.",
        "attachment": "Please find the details in the attached file.",
        "declined": "Unfortunately some items are not in our portfolio.",
        "alternative": "For some items we offer an equivalent part.",
        "broker": "The material is sourced on the open market.",
        "noise": "Our other departments would also like to share a few offers.",
        "closing": "Kind regards\n{contact}\n{company}",
    },
    "fr": {
        "greeting": "Bonjour {buyer},",
        "greeting_generic": "Madame, Monsieur,",
        "initial": "merci pour votre demande. Veuillez trouver notre offre ci-dessous.",
        "firm": "nous avons réexaminé votre demande, mais nos conditions restent inchangées.",
        "concession": "nous avons revu notre offre et pouvons améliorer nos conditions.",
        "final": "il s'agit de notre offre finale, nous ne pouvons pas descendre davantage.",
        "attachment": "Vous trouverez le détail en pièce jointe.",
        "declined": "Malheureusement, certains articles ne font pas partie de notre gamme.",
        "alternative": "Pour certains articles, nous proposons un équivalent.",
        "broker": "Le matériel provient du marché libre.",
        "noise": "Nos autres départements souhaitent également vous proposer quelques offres.",
        "closing": "Cordialement\n{contact}\n{company}",
    },
    "pt": {
        "greeting": "Olá {buyer},",
        "greeting_generic": "Prezados,",
        "initial": "obrigado pela solicitação. Segue nossa proposta.",
        "firm": "reavaliamos a solicitação, mas as condições permanecem as mesmas.",
        "concession": "revisamos nossa proposta e conseguimos melhorar as condições.",
        "final": "esta é nossa proposta final, não conseguimos reduzir mais.",
        "attachment": "Os detalhes estão no arquivo anexo.",
        "declined": "Infelizmente alguns itens não fazem parte do nosso portfólio.",
        "alternative": "Para alguns itens oferecemos um equivalente.",
        "broker": "O material é adquirido no mercado aberto.",
        "noise": "Outros departamentos da empresa também gostariam de oferecer alguns itens.",
        "closing": "Atenciosamente\n{contact}\n{company}",
    },
}


def fallback_prose(task: SupplierPersonaInput, *, buyer_name: str = "") -> SupplierPersonaOutput:
    """A plain, correct opening and closing from templates (no model)."""
    text = _TEXT[task.language]
    greeting = text["greeting"].format(buyer=buyer_name) if buyer_name else text["greeting_generic"]
    parts = [greeting, "", text.get(task.stance, text["initial"])]
    if task.quote_location == "attachment":
        parts.append(text["attachment"])
    if task.declined_lines:
        parts.append(text["declined"])
    if task.alternative_lines:
        parts.append(text["alternative"])
    if not task.authorized:
        parts.append(text["broker"])
    if task.mentions_other_departments:
        parts.append(text["noise"])
    closing = text["closing"].format(contact=task.contact_name, company=task.supplier_company)
    return SupplierPersonaOutput(opening="\n".join(parts), closing=closing)
