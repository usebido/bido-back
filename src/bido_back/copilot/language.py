"""The language the Copilot answers the buyer in, decided by code from the buyer's own words.

Live, 2026-10-10 (api.dev): a buyer wrote every message of a conversation in English,
from the first one, and the Copilot answered every turn in Portuguese, dates included
("07/11/2026"). The prompt already said "answer in the language the buyer started this
conversation in", but only the prompt said it, and the prompt quotes dozens of
Portuguese example phrases. So code reads the language off the buyer's messages and
hands it to the model as `reply_language`, next to the message, every turn.

The conversation keeps the language it started in (the Copilot's rule since prompt v11):
the first of the buyer's own messages that shows one decides it, and a later message in
another language (a pasted supplier email, a part description) does not switch it.
Switching on the buyer's explicit request is left to the model, which reads it in the
conversation. The standing instruction sent for files alone is not the buyer's words and
never decides the language.

Detection counts function words that belong to one language only (shared ones such as
"de", "la", "con", "una" or "no" are left out). It answers None rather than guess: a
message of part numbers, references or one ambiguous word shows no language.
"""

import re
from collections.abc import Iterable
from typing import Final

# Words that mark one language, and none of the others listed here.
MARKERS: Final[dict[str, frozenset[str]]] = {
    "en": frozenset(
        "the and to of for with is are was were be been will would should could can "
        "please you your we our my it this that these those from by on at an have has "
        "does did what which how when where need want create add send show find "
        "them they their yes thanks thank confirm confirmed sure go ahead invite supplier "
        "suppliers price quote order".split()
    ),
    "pt": frozenset(
        "não você vocês uma um com em do ao aos às pelo pela pelos pelas é isso esse essa "
        "esses essas sim também já obrigado obrigada preciso quero cria crie criar manda "
        "mande pra meu minha seu sua eu nós fornecedor fornecedores cotação prévia "
        "confirmo confirma pode até faz fazer faça análise analise preço peças estoque "
        "quantos".split()
    ),
    "es": frozenset(
        "el los las y usted ustedes también gracias hola necesito quiero muy pero cuando "
        "dónde qué cómo hay sí proveedor proveedores cotización nosotros puede".split()
    ),
    "it": frozenset(
        "gli della delle dei degli nel nella sono è che anche grazie ciao vorrei ho "
        "abbiamo questo questa sì fornitore fornitori ordine prezzo di può possiamo "
        "conferma confermo".split()
    ),
    "de": frozenset(
        "der die und ist nicht ich wir mit für bitte ein eine einen den dem zu von auf "
        "danke wie lieferant lieferanten bestellung angebot können".split()
    ),
    "fr": frozenset(
        "les des et est une pour avec je nous vous merci bonjour oui du au aux ce cette "
        "sont pas fournisseur fournisseurs commande prix pouvez".split()
    ),
}

# A message this short may show its language in one word ("yes", "sim", "grazie").
SHORT_WORDS: Final = 4

_WORD: Final = re.compile(r"[^\W\d_]+")
# Addresses and links carry words of no language ("com", "eu", "it").
_ADDRESS: Final = re.compile(r"\S+@\S+|\S+://\S+|www\.\S+")


def language_of(text: str) -> str | None:
    """The ISO 639-1 code of the language `text` is written in, or None when it shows none.

    The winner needs at least twice the marker words of any other language, and at
    least two of them unless the text is a few words long.
    """
    words = _WORD.findall(_ADDRESS.sub(" ", text).casefold())
    scores = sorted(
        ((sum(word in markers for word in words), code) for code, markers in MARKERS.items()),
        reverse=True,
    )
    (best, code), (second, _) = scores[0], scores[1]
    if best == 0 or best < 2 * second:
        return None
    if best < 2 and len(words) > SHORT_WORDS:
        return None
    return code


def reply_language(buyer_messages: Iterable[str]) -> str | None:
    """The conversation's language: that of the first of the buyer's messages that shows one.

    `buyer_messages` are the buyer's own words, oldest first (without the standing
    instruction for files alone). None when no message shows a language yet.
    """
    for message in buyer_messages:
        found = language_of(message)
        if found is not None:
            return found
    return None
