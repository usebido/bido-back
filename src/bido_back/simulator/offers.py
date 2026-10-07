"""What a simulated supplier offers: every number is decided here, in code.

Pure functions, no I/O: the reply job loads the RFQ lines, the persona and the
supplier's earlier offer, and `decide_offer` returns the offer the email will
carry. The model only writes the prose around it (`bido_back.simulator.agent`).

Rounds: round 1 is the supplier's first answer; every later answer in the same
thread (to a chase, a clarification question, a counteroffer or a request for
a best and final offer) is the next round. How much the supplier concedes per
round depends on its behaviors, not on the buyer's prose, so the simulation
is reproducible. One exception, detected by keywords (`is_final_request`): a
request for a best and final offer (BAFO) gets the supplier's last concession
at once, as stance "final".
"""

import hashlib
import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from bido_back.simulator.personas import Language, Persona

Stance = Literal["initial", "firm", "concession", "final"]

_TIER_STEP: Final = Decimal("0.03")  # each larger quantity tier is 3% cheaper
_ALTERNATIVE_DISCOUNT: Final = Decimal("0.92")
_VALIDITY_DAYS: Final = 30
_TOUGH_VALIDITY_DAYS: Final = 21

_ORIGIN_BY_LANGUAGE: Final[dict[Language, str]] = {
    "it": "IT",
    "de": "DE",
    "en": "GB",
    "fr": "FR",
    "pt": "PT",
}


class LineSpec(BaseModel):
    """One requested RFQ line, as the simulator reads it."""

    model_config = ConfigDict(frozen=True)

    position: int
    description: str
    mpn: str | None = None
    unit: str = "pcs"
    quantities: tuple[Decimal, ...] = ()
    need_by: date | None = None
    incoterm: str | None = None


class OfferTier(BaseModel):
    quantity: Decimal
    unit_price: Decimal


class OfferLine(BaseModel):
    position: int
    description: str
    requested_mpn: str | None = None
    offered_mpn: str | None = None
    is_alternative: bool = False
    declined: bool = False
    unit: str = "pcs"
    moq: Decimal | None = None
    lead_time_days: int | None = None
    tiers: list[OfferTier] = Field(default_factory=list[OfferTier])


class NoiseItem(BaseModel):
    """An offer from another department of the same supplier: not for this RFQ."""

    department: str
    product: str
    unit_price: Decimal
    unit: str


class Offer(BaseModel):
    round: int
    stance: Stance
    concession: Decimal  # fraction off the first offer, e.g. 0.03
    currency: str
    valid_until: date
    incoterm: str
    payment_terms: str
    origin_country: str
    authorized: bool
    lines: list[OfferLine]
    noise: list[NoiseItem] = Field(default_factory=list[NoiseItem])


_FINAL_REQUEST = re.compile(
    r"\bbafo\b|best and final|final offer|offerta finale|migliore offerta finale"
    r"|letztes angebot|finales angebot|offre finale|meilleure offre|oferta final",
    re.IGNORECASE,
)


def is_final_request(text: str) -> bool:
    """Whether the buyer asks for a best and final offer (any of our languages)."""
    return _FINAL_REQUEST.search(text) is not None


def should_answer(persona: Persona, *, is_first_email: bool) -> bool:
    """A slow responder ignores the first RFQ email and answers only once chased."""
    return not (persona.has("slow_responder") and is_first_email)


def response_delay_minutes(persona: Persona) -> int:
    delay = persona.response_delay_minutes
    return min(delay * 3, 1440) if persona.has("slow_responder") else delay


def concession_for(persona: Persona, round_number: int) -> Decimal:
    """Fraction off the first offer at `round_number` (1 = first answer)."""
    if round_number <= 1 or persona.has("reseller"):
        return Decimal(0)
    if persona.has("tough_negotiator"):
        schedule = {2: "0", 3: "0.03", 4: "0.05"}
        return Decimal(schedule.get(round_number, "0.08"))
    if persona.has("cheap_but_late"):
        return min(Decimal("0.01") * (round_number - 1), Decimal("0.02"))
    return min(Decimal("0.02") * (round_number - 1), Decimal("0.06"))


def _max_concession(persona: Persona) -> Decimal:
    return concession_for(persona, 99)


def reference_price(
    line: LineSpec, persona: Persona, history: dict[str, Decimal] | None = None
) -> Decimal:
    """The supplier's list price for a line, before its anchor and concessions.

    The persona's own reference prices win (longest keyword found in the
    description or MPN); then the tenant's last quoted price for the MPN;
    else a stable made-up price derived from the description.
    """
    haystack = f"{line.description} {line.mpn or ''}".lower()
    matches = [k for k in persona.reference_prices if k.lower() in haystack]
    if matches:
        return persona.reference_prices[max(matches, key=len)]
    if history and line.mpn and line.mpn.lower() in history:
        return history[line.mpn.lower()]
    digest = int(hashlib.sha256(line.description.lower().encode()).hexdigest()[:8], 16)
    return Decimal(digest % 9000) / 100 + Decimal("0.50")


def _money(value: Decimal) -> Decimal:
    exponent = Decimal("0.01") if value >= 10 else Decimal("0.0001")
    return value.quantize(exponent, rounding=ROUND_HALF_UP)


_PART_CODE = re.compile(r"\b[A-Z0-9]+(?:[-./][A-Z0-9]+)+\b")


def article_code(line: LineSpec, prefix: str) -> str:
    """The part number a supplier quotes: the requested MPN, a code in the
    description (e.g. `M270-35A`), or its own article number."""
    if line.mpn:
        return line.mpn
    found = _PART_CODE.search(line.description)
    return found.group(0) if found else f"{prefix}-{line.position:03d}"


def _in_portfolio(persona: Persona, line: LineSpec) -> bool:
    if not persona.portfolio:
        return True
    haystack = f"{line.description} {line.mpn or ''}".lower()
    return any(keyword.lower() in haystack for keyword in persona.portfolio)


def _lead_time(persona: Persona, line: LineSpec, today: date) -> int:
    lead = persona.lead_time_days
    if not persona.has("cheap_but_late"):
        return lead
    if line.need_by is None:
        return lead * 2
    # Always three weeks too late for the date the buyer needs.
    return max(lead, (line.need_by - today).days + 21)


def decide_offer(
    persona: Persona,
    lines: list[LineSpec],
    *,
    round_number: int,
    currency: str,
    today: date,
    history: dict[str, Decimal] | None = None,
    previous: Offer | None = None,
    final_request: bool = False,
    article_prefix: str = "ART",
) -> Offer:
    """The offer for this round. Deterministic for the same inputs."""
    concession = concession_for(persona, round_number)
    if final_request and round_number > 1:
        concession = _max_concession(persona)
    previous_concession = previous.concession if previous else Decimal(0)
    if round_number <= 1:
        stance: Stance = "initial"
    elif final_request:
        stance = "final"
    elif concession >= _max_concession(persona) and concession > 0:
        stance = "final"
    elif concession > previous_concession:
        stance = "concession"
    else:
        stance = "firm"

    declined_positions = {line.position for line in lines if not _in_portfolio(persona, line)}
    if persona.has("declines_some_lines"):
        quoted = [line.position for line in lines if line.position not in declined_positions]
        dropped = {position for index, position in enumerate(quoted, 1) if index % 3 == 0}
        if len(dropped) < len(quoted):  # always quote at least one line
            declined_positions |= dropped

    offer_lines: list[OfferLine] = []
    for line in sorted(lines, key=lambda item: item.position):
        if line.position in declined_positions:
            offer_lines.append(
                OfferLine(
                    position=line.position,
                    description=line.description,
                    requested_mpn=line.mpn,
                    declined=True,
                    unit=line.unit,
                )
            )
            continue
        alternative = (
            persona.has("offers_alternatives") and line.position % 2 == 0 and bool(line.mpn)
        )
        base = reference_price(line, persona, history) * persona.price_anchor
        if alternative:
            base *= _ALTERNATIVE_DISCOUNT
        quantities = line.quantities or (Decimal(1),)
        tiers = [
            OfferTier(
                quantity=quantity,
                unit_price=_money(base * (1 - _TIER_STEP * index) * (1 - concession)),
            )
            for index, quantity in enumerate(sorted(quantities))
        ]
        offer_lines.append(
            OfferLine(
                position=line.position,
                description=line.description,
                requested_mpn=line.mpn,
                offered_mpn=f"{line.mpn}-EQ" if alternative else article_code(line, article_prefix),
                is_alternative=alternative,
                unit=line.unit,
                # Stated on every line: the smallest tier when the persona has none.
                moq=persona.moq if persona.moq is not None else min(quantities),
                lead_time_days=_lead_time(persona, line, today),
                tiers=tiers,
            )
        )

    reseller = persona.has("reseller")
    validity = _TOUGH_VALIDITY_DAYS if persona.has("tough_negotiator") else _VALIDITY_DAYS
    incoterm = next((line.incoterm for line in lines if line.incoterm), None) or "FCA"
    return Offer(
        round=round_number,
        stance=stance,
        concession=concession,
        currency=currency,
        valid_until=today + timedelta(days=validity),
        incoterm=incoterm,
        payment_terms="100% prepayment" if reseller else "30 days net",
        origin_country="various" if reseller else _ORIGIN_BY_LANGUAGE[persona.language],
        authorized=not reseller,
        lines=offer_lines,
        noise=noise_items(persona) if persona.has("noisy") else [],
    )


_NOISE: Final[dict[Language, list[tuple[str, str, str, str]]]] = {
    "it": [
        ("Reparto utensili", "Frese in metallo duro Ø10 mm", "38.50", "pz"),
        ("Reparto manutenzione", "Contratto di manutenzione presse (annuale)", "4200.00", "anno"),
        ("Reparto logistica", "Pallet EPAL nuovi", "14.90", "pz"),
    ],
    "de": [
        ("Werkzeugabteilung", "VHM-Fräser Ø10 mm", "38.50", "Stk"),
        ("Instandhaltung", "Wartungsvertrag Pressen (jährlich)", "4200.00", "Jahr"),
        ("Logistik", "EPAL-Paletten neu", "14.90", "Stk"),
    ],
    "en": [
        ("Tooling division", "Carbide end mills 10 mm", "38.50", "pcs"),
        ("Maintenance services", "Press maintenance contract (annual)", "4200.00", "year"),
        ("Logistics", "New EPAL pallets", "14.90", "pcs"),
    ],
    "fr": [
        ("Département outillage", "Fraises carbure Ø10 mm", "38.50", "pce"),
        ("Maintenance", "Contrat de maintenance presses (annuel)", "4200.00", "an"),
        ("Logistique", "Palettes EPAL neuves", "14.90", "pce"),
    ],
    "pt": [
        ("Departamento de ferramentas", "Fresas de metal duro Ø10 mm", "38.50", "pç"),
        ("Manutenção", "Contrato de manutenção de prensas (anual)", "4200.00", "ano"),
        ("Logística", "Paletes EPAL novos", "14.90", "pç"),
    ],
}


def noise_items(persona: Persona) -> list[NoiseItem]:
    return [
        NoiseItem(department=dept, product=product, unit_price=Decimal(price), unit=unit)
        for dept, product, price, unit in _NOISE[persona.language]
    ]
