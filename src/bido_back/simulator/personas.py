"""Supplier personas: how a simulated supplier answers (data, not prompts).

A persona is a row in `supplier_personas`. Everything that decides numbers
(prices, concessions, lead times, which lines are declined) reads these fields
in code (`bido_back.simulator.offers`); the model only writes the prose.
"""

from decimal import Decimal
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from bido_back.db.models import SupplierPersona

Language = Literal["it", "de", "en", "fr", "pt"]
QuoteFormat = Literal["xlsx", "pdf", "body"]
Behavior = Literal[
    # Concedes nothing in round 2, then a little from round 3 on.
    "tough_negotiator",
    # Cheapest anchor, but the lead time misses the need-by date.
    "cheap_but_late",
    # A broker: sources on the open market, no manufacturer authorization.
    "reseller",
    # Ignores the first RFQ email; answers only once chased.
    "slow_responder",
    # Declines every third line ("not in our portfolio").
    "declines_some_lines",
    # Offers an equivalent part instead of the requested MPN on some lines.
    "offers_alternatives",
    # Mixes offers from other departments of the same company into the email.
    "noisy",
]

BEHAVIORS: Final[tuple[Behavior, ...]] = (
    "tough_negotiator",
    "cheap_but_late",
    "reseller",
    "slow_responder",
    "declines_some_lines",
    "offers_alternatives",
    "noisy",
)


class Persona(BaseModel):
    """A persona as the simulator and the api see it."""

    model_config = ConfigDict(frozen=True)

    language: Language = "en"
    behaviors: tuple[Behavior, ...] = ()
    # Multiplies the reference price: 0.85 is a cheap supplier, 1.2 an expensive one.
    price_anchor: Decimal = Field(default=Decimal("1.0"), gt=0, le=10)
    # Reference unit price in the RFQ's base currency, by lower-case keyword
    # found in a line's description or MPN (e.g. {"bearing": 4.2}).
    reference_prices: dict[str, Decimal] = Field(default_factory=dict[str, Decimal])
    # Lower-case keywords of what the supplier sells. Lines matching none are
    # declined ("not in our portfolio"); empty = quotes everything (a broker).
    portfolio: tuple[str, ...] = ()
    lead_time_days: int = Field(default=28, ge=1, le=730)
    moq: Decimal | None = Field(default=None, gt=0)
    response_delay_minutes: int = Field(default=0, ge=0, le=1440)
    quote_format: QuoteFormat = "xlsx"

    def has(self, behavior: Behavior) -> bool:
        return behavior in self.behaviors


DEFAULT_PERSONA: Final = Persona()


def persona_from_row(row: SupplierPersona | None) -> Persona:
    if row is None:
        return DEFAULT_PERSONA
    return Persona.model_validate(
        {
            "language": row.language,
            "behaviors": tuple(row.behaviors),
            "price_anchor": row.price_anchor,
            "reference_prices": {str(k): Decimal(str(v)) for k, v in row.reference_prices.items()},
            "portfolio": tuple(str(k) for k in row.portfolio),
            "lead_time_days": row.lead_time_days,
            "moq": row.moq,
            "response_delay_minutes": row.response_delay_minutes,
            "quote_format": row.quote_format,
        }
    )


def apply_persona(row: SupplierPersona, persona: Persona) -> None:
    """Copy a persona onto its row (the caller flushes)."""
    row.language = persona.language
    row.behaviors = list(persona.behaviors)
    row.price_anchor = persona.price_anchor
    row.reference_prices = {k: str(v) for k, v in persona.reference_prices.items()}
    row.portfolio = list(persona.portfolio)
    row.lead_time_days = persona.lead_time_days
    row.moq = persona.moq
    row.response_delay_minutes = persona.response_delay_minutes
    row.quote_format = persona.quote_format
