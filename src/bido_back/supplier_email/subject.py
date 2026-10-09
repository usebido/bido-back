"""Subjects of an RFQ thread: the first email names the type once, every later one is a reply.

- First email: `[RFQ-6] <type label>: <title>` (the RFQ invitation, `templates.render`).
  A title that repeats the type ("Cotação de componentes …") is shortened.
- Later emails: `Re: ` plus that first subject, byte for byte. Never a localized prefix
  (AW:, RES:), never "Re:" twice. The email's type goes in its body (the purpose line):
  Gmail threads only messages whose subjects match.
"""

import re
import unicodedata
from typing import Final

SUBJECT_LIMIT: Final = 300
REPLY_PREFIX: Final = "Re: "
# Reply prefixes a subject may already start with (any language): no second one is added.
_REPLY_PREFIXES: Final = ("re:", "aw:", "sv:", "réf:", "rif:", "res:", "r:", "wg:", "tr:")

_TYPE_WORDS: Final = (
    r"(?:solicitacao de cotacao|pedido de cotacao|cotacao|request for quotation"
    r"|quotation request|quote request|quotation|quote|rfq|angebotsanfrage|anfrage"
    r"|demande de prix|demande de devis|richiesta di offerta|solicitud de cotizacion"
    r"|cotizacion)"
)
_CONNECTOR: Final = (
    r"(?:\s*[:\-–]\s*|\s+(?:de|da|do|dos|das|para|for|of|fur|zu|pour|per|di|del|des)\s+)"
)
_TYPE_PREFIX: Final = re.compile(rf"^\s*{_TYPE_WORDS}{_CONNECTOR}")


def marker(rfq_ref: str) -> str:
    """The marker every subject of the thread carries, e.g. `[RFQ-6]`."""
    return f"[{rfq_ref}]"


def _fold(text: str) -> str:
    """Lower case without accents, one character for one (so positions still match)."""
    return "".join(unicodedata.normalize("NFKD", c)[:1].lower() or c for c in text)


def shorten_title(title: str) -> str:
    """The title without a leading type word: "Cotação de componentes" -> "Componentes"."""
    match = _TYPE_PREFIX.match(_fold(title))
    if match is None:
        return title
    rest = title[match.end() :].strip()
    if not rest:
        return title
    return rest[0].upper() + rest[1:] if rest[0].islower() else rest


def first_subject(rfq_ref: str, label: str, title: str) -> str:
    """`[RFQ-6] Solicitação de cotação: Componentes para motor MTR-100`."""
    return f"{marker(rfq_ref)} {label}: {shorten_title(title)}"


def reply_subject(first: str | None, rfq_ref: str, title: str) -> str:
    """`Re: ` plus the thread's first subject, byte for byte, always with the `[RFQ-n]` marker.

    Without a stored first subject (an RFQ email never sent through Bido): the marker and
    the title.
    """
    base = first if first else f"{marker(rfq_ref)} {title}"
    # A header is one line: our stored subjects never have breaks (they are kept byte for
    # byte); a fallback title might, and is then written on one line.
    if "\r" in base or "\n" in base:
        base = " ".join(base.split())
    if marker(rfq_ref) not in base:
        base = f"{marker(rfq_ref)} {base}"
    subject = base if base.lower().startswith(_REPLY_PREFIXES) else f"{REPLY_PREFIX}{base}"
    return subject[:SUBJECT_LIMIT]


def subject_problems(subject: str, first: str | None, rfq_ref: str, title: str) -> list[str]:
    """Codes: not `Re: ` plus the first subject, no `[RFQ-n]` marker, or a second Re."""
    problems: list[str] = []
    if subject != reply_subject(first, rfq_ref, title):
        problems.append("subject_not_frozen")
    if marker(rfq_ref) not in subject:
        problems.append("subject_marker_missing")
    rest = subject[len(REPLY_PREFIX) :] if subject.startswith(REPLY_PREFIX) else subject
    if rest.lower().startswith(_REPLY_PREFIXES):
        problems.append("subject_reply_prefix_twice")
    return problems
