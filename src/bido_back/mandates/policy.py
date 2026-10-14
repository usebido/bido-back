"""Is this email inside the mandate? Pure checks, no I/O.

Every automatic send passes here first. A failed check never sends: the
approval stays pending for a person and the reason is recorded.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Final

from pydantic import JsonValue

from bido_back.mandates.schemas import ApproveMandateArgs

# Language that would commit the buyer (accept, order, award). Automatic emails
# ask, chase and counter; accepting and ordering stay with the buyer (award/PO).
_COMMITMENT: Final = re.compile(
    "|".join(
        (
            r"\bwe (?:hereby )?accept\b",
            r"\baccept(?:ed|ing)? (?:your|the) (?:offer|quote|quotation|proposal)\b",
            r"\bpurchase order\b",
            r"\bP\.?O\.? ?(?:no\.?|number|#)",
            r"\bwe (?:will|shall) (?:order|place (?:an|the) order)\b",
            r"\bawarded?\b",
            r"\bcontract\b",
            r"\baccettiamo\b",
            r"\bordine (?:d'acquisto|di acquisto)\b",
            r"\bconfermiamo l'ordine\b",
            r"\bcontratto\b",
            r"\bbestellung\b",
            r"\bnehmen (?:ihr|das|ihres) angebot an\b",
            r"\bzuschlag\b",
            r"\bvertrag\b",
            r"\bnous acceptons\b",
            r"\bbon de commande\b",
            r"\bcontrat\b",
            r"\baceitamos\b",
            r"\bordem de compra\b",
            r"\bpedido de compra\b",
            r"\bcontrato\b",
            r"\baceptamos\b",
            r"\borden de compra\b",
        )
    ),
    re.IGNORECASE,
)


def commitment_language(*texts: str) -> bool:
    return any(_COMMITMENT.search(text) for text in texts)


def recipients_problem(to: Sequence[str], cc: Sequence[str], allowed: Iterable[str]) -> str | None:
    """Only the mandated supplier addresses, and no copies."""
    if cc:
        return "cc_not_in_mandate"
    allowed_lower = {a.strip().lower() for a in allowed}
    if not to or any(a.strip().lower() not in allowed_lower for a in to):
        return "recipient_not_in_mandate"
    return None


def _decimal(value: JsonValue) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def negotiation_terms_problem(
    terms: Mapping[str, JsonValue], mandate: ApproveMandateArgs
) -> str | None:
    """Every price asked is within the mandate's maximum; same currency; known lines.

    Asks and the maximum are both unit prices (one-off charges such as tooling are never
    in either), so they compare directly."""
    if terms.get("currency") != mandate.currency:
        return "currency_not_in_mandate"
    limits = {str(line.rfq_line_id): line for line in mandate.lines}
    raw_lines = terms.get("lines")
    if not isinstance(raw_lines, list):
        return "terms_unreadable"
    for raw in raw_lines:
        if not isinstance(raw, dict):
            return "terms_unreadable"
        ask = raw.get("ask")
        if ask is None:
            continue
        line = limits.get(str(raw.get("rfq_line_id")))
        if line is None:
            return "line_not_in_mandate"
        price = _decimal(ask)
        if price is None or price <= 0 or price > line.max_unit_price:
            return "price_outside_mandate"
    return None
