"""The three suppliers of the buyer journey: what they answer, when, and in which words.

Every number is decided here, by code; the text around it is a fixed Portuguese
template, the way Brazilian suppliers write ("R$ 46,90 por pcs", "Moeda: BRL").
`truth()` is what a correct quote extraction returns for each e-mail (mock mode
scripts it; live mode reads the same e-mail with the real model).

The script covers the buyer's flow after the RFQ e-mails go out:

* MetalNova is the cheapest but one item (000104) is late for the need-by date;
  it later changes ONLY that item's lead time ("as demais condições permanecem
  inalteradas": the prices must stay as they were) and ends with a final offer.
* PrecisaoSul is on time and slightly pricier; it lowers ONE price keeping the
  rest (an amendment), then improves another line over two rounds.
* MultiPecas cannot supply 000104, adds an offer from its maintenance department
  for an item nobody asked for (000302: an extra, never part of the comparison),
  then repeats the same offer without improvement (the negotiation must stop
  for it). At the end its "Revisão 2" arrives before its older "Revisão 1",
  delayed in transit: the newer terms must stay.

`expected_states()` is the state each e-mail must leave, from the business
rules (an amendment changes only what it states; the newest written revision
wins), not from the code under test.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final, Literal

from bido_back.agents.catalog.contracts import (
    ExtractedValue,
    ExtraOfferDraft,
    ProposalLineDraft,
    QuoteExtractionOutput,
    SourceRef,
)
from evals.journey_workbook import METALNOVA, MULTIPECAS, PRECISAOSUL, PartGolden, SupplierRef


@dataclass(frozen=True, slots=True)
class Offer:
    price: Decimal
    lead: int


State = Mapping[str, Offer]  # part number -> the supplier's offer for it
ReplyKind = Literal["offer", "revised", "repeat", "final", "lead_change", "price_change"]


def _o(price: str, lead: int) -> Offer:
    return Offer(Decimal(price), lead)


@dataclass(frozen=True, slots=True)
class SupplierEmail:
    key: str
    supplier: SupplierRef
    day: int  # simulated days after the RFQ e-mails went out
    hour: float  # delivery time on that day
    kind: ReplyKind
    # A whole offer (offer/revised/repeat/final) or only what changes (amendments).
    lines: Mapping[str, Offer]
    declined: tuple[str, ...] = ()
    extra: bool = False
    revision: str | None = None
    # The Date header, this many hours before delivery (an older revision delayed).
    written_earlier: float = 0.0
    tests: str = ""

    @property
    def amendment(self) -> bool:
        return self.kind in ("lead_change", "price_change")

    @property
    def written_hour(self) -> float:
        return self.hour - self.written_earlier


MN_R0: Final = {
    "000101": _o("46.90", 5),
    "000102": _o("239.00", 5),
    "000104": _o("112.00", 20),
    "000105": _o("21.40", 5),
    "000106": _o("2.45", 4),
}
MN_R1: Final = {
    **MN_R0,
    "000101": _o("45.60", 5),
    "000102": _o("236.80", 5),
    "000105": _o("21.10", 5),
}
MN_R3: Final = {
    **MN_R1,
    "000102": _o("234.00", 5),
    "000104": _o("112.00", 15),
    "000105": _o("20.95", 5),
}
PS_R0: Final = {
    "000101": _o("48.50", 3),
    "000102": _o("245.00", 4),
    "000104": _o("118.50", 5),
    "000105": _o("21.90", 4),
    "000106": _o("2.55", 3),
}
PS_R2: Final = {**PS_R0, "000104": _o("116.90", 5), "000105": _o("20.70", 4)}
PS_R3: Final = {**PS_R2, "000104": _o("116.20", 5)}
MP_R0: Final = {"000101": _o("48.20", 5), "000102": _o("241.00", 5),
                "000105": _o("21.60", 5), "000106": _o("2.62", 5)}  # fmt: skip

SCRIPT: Final = (
    SupplierEmail("metalnova.r0", METALNOVA, 0, 2.0, "offer", MN_R0,
                  tests="cheapest, but 000104 is late for the need-by date"),
    SupplierEmail("precisaosul.r0", PRECISAOSUL, 0, 2.0, "offer", PS_R0,
                  tests="on time on every line, slightly pricier"),
    SupplierEmail("multipecas.r0", MULTIPECAS, 0, 2.0, "offer", MP_R0, declined=("000104",),
                  extra=True, tests="cannot supply 000104; offers an unrequested maintenance item"),
    SupplierEmail("metalnova.r1", METALNOVA, 1, 1.0, "revised", MN_R1,
                  tests="answers the counter-offer with lower prices"),
    SupplierEmail("precisaosul.r1", PRECISAOSUL, 1, 1.0, "price_change",
                  {"000105": _o("20.70", 4)},
                  tests="lowers one price, keeping the rest (an amendment)"),
    SupplierEmail("multipecas.r1", MULTIPECAS, 1, 1.0, "repeat", MP_R0, declined=("000104",),
                  tests="repeats the same offer without improvement"),
    SupplierEmail("metalnova.r2", METALNOVA, 2, 1.0, "lead_change", {"000104": _o("112.00", 15)},
                  tests="changes ONLY the lead time of 000104; other terms unchanged"),
    SupplierEmail("precisaosul.r2", PRECISAOSUL, 2, 1.0, "revised", PS_R2,
                  tests="improves 000104"),
    SupplierEmail("multipecas.r2", MULTIPECAS, 2, 1.0, "repeat", MP_R0, declined=("000104",),
                  tests="repeats again: no progress, the negotiation stops for it"),
    SupplierEmail("metalnova.r3", METALNOVA, 3, 1.0, "final", MN_R3,
                  tests="best and final offer"),
    SupplierEmail("precisaosul.r3", PRECISAOSUL, 3, 1.0, "final", PS_R3,
                  tests="best and final offer"),
    SupplierEmail("multipecas.rev2", MULTIPECAS, 3, 1.5, "price_change",
                  {"000102": _o("238.00", 5)}, revision="Revisão 2",
                  tests="a newer revision (written 01:30)"),
    SupplierEmail("multipecas.rev1", MULTIPECAS, 3, 2.5, "price_change",
                  {"000102": _o("239.50", 5)}, revision="Revisão 1", written_earlier=1.5,
                  tests="an OLDER revision (written 01:00) delivered after the newer one"),
)  # fmt: skip
DAYS: Final = 1 + max(e.day for e in SCRIPT)
# What the extra offer is: another department's item, never an RFQ line.
EXTRA_PART: Final = "000302"


def expected_states(script: Sequence[SupplierEmail] = SCRIPT) -> dict[str, dict[str, Offer]]:
    """E-mail key -> the supplier's effective offer once that e-mail is in.

    A whole offer replaces the previous one; an amendment changes only what it
    states. Revisions apply in the order they were WRITTEN: an older revision
    delivered late changes nothing that a newer one already set.
    """
    states: dict[str, dict[str, Offer]] = {}
    seen: dict[str, list[SupplierEmail]] = {}
    for email in script:
        mine = [*seen.get(email.supplier.key, []), email]
        seen[email.supplier.key] = mine
        state: dict[str, Offer] = {}
        for item in sorted(mine, key=lambda e: (e.day, e.written_hour)):
            if item.amendment:
                state = {**state, **item.lines}
            else:
                state = dict(item.lines)
        states[email.key] = state
    return states


def final_states(script: Sequence[SupplierEmail] = SCRIPT) -> dict[str, dict[str, Offer]]:
    """Supplier key -> its effective offer at the end."""
    states = expected_states(script)
    return {e.supplier.key: states[e.key] for e in script}


# --- Writing ----------------------------------------------------------------------------------


def money(value: Decimal) -> str:
    """Brazilian notation, two decimals: 1234.5 -> 1.234,50."""
    text = f"{value:,.2f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def quantity(value: Decimal) -> str:
    return format(value.normalize(), "f").replace(".", ",")


def day_text(value: date) -> str:
    return value.strftime("%d/%m/%Y")


_OPENING: Final[dict[ReplyKind, str]] = {
    "offer": "Obrigado pela solicitação {ref}. Segue nossa proposta:",
    "revised": "Sobre a {ref}, segue nossa proposta revisada:",
    "repeat": "Sobre a {ref}: não conseguimos reduzir os preços. Mantemos a mesma proposta:",
    "final": "Sobre a {ref}, segue nossa melhor e última oferta:",
}
_CONTACT: Final = {
    METALNOVA.key: "Equipe comercial",
    PRECISAOSUL.key: "Atendimento B2B",
    MULTIPECAS.key: "Vendas internas",
}
PRESERVE_LEAD: Final = "As demais condições permanecem inalteradas"
PRESERVE_PRICE: Final = "mantendo as demais condições"
_TERMS: Final = "Moeda: BRL. Frete CIF Planta A. Pagamento em 28 dias após a NF."
_EXTRA_TEXT: Final = (
    "Aproveitando o contato, nosso departamento de manutenção também oferece a correia de "
    "equipamento cód. 000302: 15 pcs a R$ 77,00 por pcs, entrega em 7 dias."
)


@dataclass(frozen=True, slots=True)
class Written:
    subject: str
    body: str
    truth: QuoteExtractionOutput


@dataclass(frozen=True, slots=True)
class Requested:
    """An RFQ line as the supplier sees it."""

    position: int
    part: PartGolden

    @property
    def label(self) -> str:
        return f"Item {self.position} - {self.part.part} {self.part.description}"


PriceStyle = Literal["symbol", "code"]


def price_text(value: Decimal, style: PriceStyle) -> str:
    """ "R$ 46,90" as Brazilian suppliers write it, or "46,90 BRL"."""
    return f"R$ {money(value)}" if style == "symbol" else f"{money(value)} BRL"


def _line_text(item: Requested, offer: Offer, style: PriceStyle) -> str:
    unit = item.part.unit
    return (
        f"{item.label}: {quantity(item.part.to_buy)} {unit} a {price_text(offer.price, style)} "
        f"por {unit}, prazo de entrega de {offer.lead} dias."
    )


def _ev(value: str, quote: str) -> ExtractedValue:
    return ExtractedValue(
        value=value, source=SourceRef(kind="email_body", quote=quote), confidence=0.95
    )


_NONE: Final = ExtractedValue(value=None, source=None, confidence=0)


def _draft(
    position: int,
    *,
    min_quantity: ExtractedValue = _NONE,
    unit_price: ExtractedValue = _NONE,
    unit: ExtractedValue = _NONE,
    lead_time_days: ExtractedValue = _NONE,
) -> ProposalLineDraft:
    """A line as a correct extraction reads it: the fields the e-mail states, nothing else."""
    return ProposalLineDraft(
        rfq_line_position=position,
        offered_mpn=_NONE,
        min_quantity=min_quantity,
        unit_price=unit_price,
        unit=unit,
        tooling_cost=_NONE,
        moq=_NONE,
        lead_time_days=lead_time_days,
        origin_country=_NONE,
        qualification=_NONE,
    )


def write(
    email: SupplierEmail,
    *,
    rfq_ref: str,
    thread_subject: str,
    requested: Sequence[Requested],
    valid_until: date,
    style: PriceStyle = "symbol",
) -> Written:
    """The e-mail the supplier sends, and what a correct extraction reads from it.

    Prices in the truth cite the number as written ("46,90"); the e-mail itself
    writes "R$ 46,90", as Brazilian suppliers do.
    """
    by_part = {item.part.part: item for item in requested}
    signature = f"Atenciosamente,\n{_CONTACT[email.supplier.key]}\n{email.supplier.name}"
    subject = (
        thread_subject if thread_subject.lower().startswith("re:") else f"Re: {thread_subject}"
    )
    if email.revision:
        subject = f"{subject} - {email.revision}"
    if email.amendment:
        (part, offer), *_ = email.lines.items()
        item = by_part[part]
        prefix = f"{email.revision}: " if email.revision else ""
        if email.kind == "lead_change":
            change = (
                f"revisamos somente o prazo do {item.label.lower()}, que passa a ser de "
                f"{offer.lead} dias. {PRESERVE_LEAD}."
            )
            line = _draft(item.position, lead_time_days=_ev(str(offer.lead), f"{offer.lead} dias"))
            preserve = PRESERVE_LEAD
        else:
            change = (
                f"{prefix}reduzimos o preço do {item.label.lower()} para "
                f"{price_text(offer.price, style)} por {item.part.unit}, {PRESERVE_PRICE}."
            )
            line = _draft(
                item.position,
                unit_price=_ev(format(offer.price, "f"), money(offer.price)),
            )
            preserve = PRESERVE_PRICE
        body = f"Prezados,\n\nSobre a {rfq_ref}: {change}\n\n{signature}"
        truth = QuoteExtractionOutput(
            is_quote=True,
            currency=_NONE,
            valid_until=_NONE,
            incoterm=_NONE,
            payment_terms=_NONE,
            lines=[line],
            reply_kind="amendment",
            unchanged_terms=_ev(preserve, preserve),
        )
        return Written(subject, body, truth)

    parts = [f"Prezados,\n\n{_OPENING[email.kind].format(ref=rfq_ref)}"]
    drafts: list[ProposalLineDraft] = []
    lines: list[str] = []
    for item in requested:
        offer = email.lines.get(item.part.part)
        if offer is None:
            if item.part.part in email.declined:
                lines.append(f"{item.label}: não temos condições de fornecer este item.")
            continue
        lines.append(_line_text(item, offer, style))
        unit = item.part.unit
        drafts.append(
            _draft(
                item.position,
                min_quantity=_ev(
                    format(item.part.to_buy.normalize(), "f"),
                    f"{quantity(item.part.to_buy)} {unit}",
                ),
                unit_price=_ev(format(offer.price, "f"), money(offer.price)),
                unit=_ev(unit, unit),
                lead_time_days=_ev(str(offer.lead), f"{offer.lead} dias"),
            )
        )
    parts.append("\n".join(lines))
    valid = f"Proposta válida até {day_text(valid_until)}"
    parts.append(f"{_TERMS}\n{valid}.")
    extras: list[ExtraOfferDraft] = []
    if email.extra:
        parts.append(_EXTRA_TEXT)
        extras.append(
            ExtraOfferDraft(
                description="Correia de equipamento (departamento de manutenção)",
                mpn=EXTRA_PART,
                quantity="15 pcs",
                unit_price="77,00",
                currency="BRL",
                conditions="entrega em 7 dias",
                quote=_EXTRA_TEXT,
            )
        )
    parts.append(signature)
    truth = QuoteExtractionOutput(
        is_quote=True,
        currency=_ev("BRL", "Moeda: BRL"),
        valid_until=_ev(valid_until.isoformat(), valid),
        incoterm=_ev("CIF", "Frete CIF Planta A"),
        payment_terms=_ev("28 dias após a NF", "Pagamento em 28 dias após a NF"),
        lines=drafts,
        extra_offers=extras,
    )
    return Written(subject, "\n\n".join(parts), truth)


@dataclass(frozen=True, slots=True)
class Expected:
    """What the final comparison must recommend per line (business rules, from the script)."""

    part: str
    supplier: str  # supplier key
    price: Decimal
    lead: int
    why: str
    alternatives: tuple[str, ...] = field(default_factory=tuple[str, ...])


def expected_recommendations(
    parts: Sequence[PartGolden],
    need_days: int,
    *,
    maxima: Mapping[str, Decimal],
    script: Sequence[SupplierEmail] = SCRIPT,
) -> dict[str, Expected]:
    """Per line: the cheapest offer that meets the need-by date (`need_days` from the
    evaluation day), among offers at or below the buyer's maximum; when none is on
    time, the cheapest late one. Suppliers that did not quote a line are not in it."""
    states = final_states(script)
    result: dict[str, Expected] = {}
    for part in parts:
        if not part.buy:
            continue
        offers = [
            (key, state[part.part])
            for key, state in states.items()
            if part.part in state and state[part.part].price <= maxima[part.part]
        ]
        if not offers:
            continue
        on_time = [(k, o) for k, o in offers if o.lead <= need_days]
        pool = on_time or offers
        key, offer = min(pool, key=lambda item: (item[1].price, item[1].lead, item[0]))
        why = "cheapest on time" if on_time else "cheapest, but late (no offer on time)"
        if on_time and len(on_time) < len(offers):
            why += "; a cheaper offer is late"
        result[part.part] = Expected(
            part=part.part,
            supplier=key,
            price=offer.price,
            lead=offer.lead,
            why=why,
            alternatives=tuple(sorted(k for k, _ in offers if k != key)),
        )
    return result
