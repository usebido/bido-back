"""The suppliers' side of the negotiation scenarios: their emails, and what a correct model reads.

Every number a supplier writes is decided here, by code (`Quoted`: price and
lead time per line); the words around it are fixed templates in the supplier's
language, or, for `rfq6_*`, the exact text of the real RFQ-6 thread
(`.context/negotiation/rfq6-thread.txt`, 2026-10-08) with the reference
substituted. Each builder returns the email and the extraction a correct model
returns for it under the current contract (`QuoteExtractionOutput`), which
mock mode scripts; live mode reads the same email with the real model.

"Correct" means what the contract can express: a relative validity ("valid for
15 days") has no ISO date to give, so a correct model leaves `valid_until`
empty; "7 business days" is written as 7 with its source, and deterministic
code decides what business days mean.

The mock writers (`followup_writer`, `negotiation_writer`) follow the released
writer prompts literally, in Portuguese, German or English: they sign "with
the buyer's name and company" and mention a reply-by date only where the
prompt asks for one. Mock mode therefore checks the harness and the
deterministic code, never a model.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from bido_back.agents.catalog.contracts import (
    ExtractedValue,
    ExtraOfferDraft,
    FollowupEmailInput,
    FollowupEmailOutput,
    NegotiationEmailInput,
    NegotiationEmailOutput,
    ProposalLineDraft,
    QuoteExtractionInput,
    QuoteExtractionOutput,
    SourceRef,
)
from evals.scripted import ScriptedModel

# --- What the buyer asks for -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Part:
    """One requested line and the buyer's limits for it (in the RFQ's currency)."""

    position: int
    part_number: str
    description: str
    quantity: Decimal
    unit: str
    need_by_days: int  # calendar days after the RFQ goes out
    target: Decimal
    maximum: Decimal


@dataclass(frozen=True, slots=True)
class Quoted:
    """A supplier's answer for one line: decided by code."""

    position: int
    price: Decimal
    lead: int  # days as the supplier writes them


@dataclass(frozen=True, slots=True)
class Thread:
    """What a supplier knows when it writes: the RFQ reference, the day it was sent."""

    ref: str
    start: date
    parts: tuple[Part, ...]
    supplier: str
    contact: str

    def part(self, position: int) -> Part:
        return next(p for p in self.parts if p.position == position)


@dataclass(frozen=True, slots=True)
class Email:
    """A supplier email and its correct extraction."""

    body: str
    truth: QuoteExtractionOutput
    # Added to the reply subject ("Re: [RFQ-1] ... - Revisão 2").
    subject_suffix: str = ""


def q(position: int, price: str, lead: int) -> Quoted:
    return Quoted(position, Decimal(price), lead)


# --- Evidence ----------------------------------------------------------------------------------

NONE: Final = ExtractedValue(value=None, source=None, confidence=0)


def ev(value: str, quote: str, confidence: float = 0.95) -> ExtractedValue:
    return ExtractedValue(
        value=value, source=SourceRef(kind="email_body", quote=quote), confidence=confidence
    )


def line_draft(
    position: int,
    *,
    min_quantity: ExtractedValue = NONE,
    unit_price: ExtractedValue = NONE,
    unit: ExtractedValue = NONE,
    lead_time_days: ExtractedValue = NONE,
    moq: ExtractedValue = NONE,
    offered_mpn: ExtractedValue = NONE,
    is_alternative: bool = False,
) -> ProposalLineDraft:
    """A line as a correct extraction reads it: what the email states, nothing else."""
    return ProposalLineDraft(
        rfq_line_position=position,
        offered_mpn=offered_mpn,
        is_alternative=is_alternative,
        min_quantity=min_quantity,
        unit_price=unit_price,
        unit=unit,
        tooling_cost=NONE,
        moq=moq,
        lead_time_days=lead_time_days,
        origin_country=NONE,
        qualification=NONE,
    )


def missing_quotes(email: Email) -> list[str]:
    """Quotes of the truth that are not in the email body (a harness bug when not empty)."""
    body = " ".join(email.body.split()).casefold()
    values: list[ExtractedValue] = [
        email.truth.currency,
        email.truth.valid_until,
        email.truth.incoterm,
        email.truth.payment_terms,
        email.truth.unchanged_terms,
        email.truth.update_evidence,
    ]
    for line in email.truth.lines:
        values += [
            line.offered_mpn,
            line.min_quantity,
            line.unit_price,
            line.unit,
            line.moq,
            line.lead_time_days,
        ]
    quotes = [v.source.quote for v in values if v.source is not None]
    quotes += [o.quote for o in email.truth.extra_offers]
    return [text for text in quotes if " ".join(text.split()).casefold() not in body]


# --- Writing numbers and dates ---------------------------------------------------------------


def pt_number(value: Decimal) -> str:
    """Brazilian notation with two decimals for money: 1234.5 -> 1.234,50."""
    text = f"{value:,.2f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def pt_quantity(value: Decimal) -> str:
    return format(value.normalize(), "f").replace(".", ",")


def de_number(value: Decimal) -> str:
    return f"{value:.2f}".replace(".", ",")


def pt_date(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def de_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def plain_number(value: Decimal) -> str:
    """Machine form for the truth: 19.50 -> "19.50", 12.5 -> "12.5"."""
    return format(value, "f")


# --- Brazilian suppliers (Portuguese) --------------------------------------------------------

_PT_TERMS: Final = "Moeda: BRL. Pagamento em 28 dias após a NF."
_PT_PAYMENT: Final = "Pagamento em 28 dias após a NF"


def _pt_unit(part: Part) -> tuple[str, str]:
    """(plural, singular) of the line's unit as Brazilian suppliers write it."""
    return ("kg", "kg") if part.unit == "kg" else ("peças", "peça")


def _pt_label(part: Part) -> str:
    return f"Item {part.position} - {part.part_number} {part.description}"


def _pt_line(t: Thread, quoted: Quoted, *, with_lead: bool = True) -> tuple[str, ProposalLineDraft]:
    part = t.part(quoted.position)
    plural, single = _pt_unit(part)
    qty = f"{pt_quantity(part.quantity)} {plural}"
    price = f"R$ {pt_number(quoted.price)}"
    lead = f"{quoted.lead} dias"
    text = f"{_pt_label(part)}: {qty} a {price} por {single}"
    text += f", prazo de entrega de {lead}." if with_lead else "."
    draft = line_draft(
        part.position,
        min_quantity=ev(plain_number(part.quantity), qty),
        unit_price=ev(plain_number(quoted.price), price),
        unit=ev(single, f"por {single}"),
        lead_time_days=ev(str(quoted.lead), lead) if with_lead else NONE,
    )
    return text, draft


def _pt_signature(t: Thread) -> str:
    return f"Atenciosamente,\n{t.contact}\n{t.supplier}"


def pt_offer(
    t: Thread,
    lines: Sequence[Quoted],
    *,
    opening: str,
    valid_days: int | None = 45,
    with_lead: bool = True,
    after: Sequence[str] = (),
    extras: Sequence[ExtraOfferDraft] = (),
) -> Email:
    """A whole offer (a replacement): every line it prices, currency, payment, and (unless
    left out) the lead times and validity."""
    texts: list[str] = []
    drafts: list[ProposalLineDraft] = []
    for quoted in lines:
        text, draft = _pt_line(t, quoted, with_lead=with_lead)
        texts.append(text)
        drafts.append(draft)
    valid_until = t.start + timedelta(days=valid_days) if valid_days is not None else None
    valid = f"Proposta válida até {pt_date(valid_until)}" if valid_until is not None else None
    body = "\n\n".join(
        [
            f"Prezados,\n\n{opening.format(ref=t.ref)}",
            "\n".join(texts),
            *after,
            f"{_PT_TERMS}\n{valid}." if valid is not None else _PT_TERMS,
            _pt_signature(t),
        ]
    )
    truth = QuoteExtractionOutput(
        is_quote=True,
        currency=ev("BRL", "Moeda: BRL"),
        valid_until=ev(valid_until.isoformat(), valid)
        if valid_until is not None and valid is not None
        else NONE,
        incoterm=NONE,
        payment_terms=ev("28 dias após a NF", _PT_PAYMENT),
        lines=drafts,
        extra_offers=list(extras),
    )
    return Email(body, truth)


def pt_answers(t: Thread, *, valid_days: int, lead: int, positions: Sequence[int]) -> Email:
    """The answer to a clarification: the validity and one lead time for every line, and
    nothing else. No 'other conditions unchanged': nothing else was asked or changed.

    A correct extraction under the current contract reads it as an amendment (it changes
    no price), without the explicit preservation statement the supplier never wrote."""
    valid_until = t.start + timedelta(days=valid_days)
    valid = f"a proposta é válida até {pt_date(valid_until)}"
    days = f"{lead} dias"
    answer = f"{valid} e o prazo de entrega é de {days} para os três itens"
    body = (
        f"Prezados,\n\nRespondendo à sua mensagem sobre a {t.ref}: {answer}.\n\n{_pt_signature(t)}"
    )
    truth = QuoteExtractionOutput(
        is_quote=True,
        currency=NONE,
        valid_until=ev(valid_until.isoformat(), valid),
        incoterm=NONE,
        payment_terms=NONE,
        lines=[line_draft(p, lead_time_days=ev(str(lead), days)) for p in positions],
        reply_kind="amendment",
        update_evidence=ev(answer, answer),
    )
    return Email(body, truth)


OFFER: Final = "Obrigado pela solicitação {ref}. Segue nossa proposta:"
REVISED: Final = "Sobre a {ref}, segue nossa proposta revisada:"
SAME: Final = "Sobre a {ref}: não conseguimos reduzir os preços. Mantemos a mesma proposta:"
FINAL: Final = "Sobre a {ref}, segue nossa melhor e última oferta:"
PRESERVE_PRICE: Final = "mantendo as demais condições"
PRESERVE_ALL: Final = "As demais condições permanecem inalteradas"


def pt_price_change(t: Thread, quoted: Quoted, *, revision: int | None = None) -> Email:
    """An amendment: one line's new price, every other condition kept (stated)."""
    part = t.part(quoted.position)
    _, single = _pt_unit(part)
    price = f"R$ {pt_number(quoted.price)}"
    prefix = f"Revisão {revision}: " if revision is not None else ""
    change = (
        f"{prefix}reduzimos o preço do {_pt_label(part).lower()} para {price} por {single}, "
        f"{PRESERVE_PRICE}."
    )
    body = f"Prezados,\n\nSobre a {t.ref}: {change}\n\n{_pt_signature(t)}"
    truth = QuoteExtractionOutput(
        is_quote=True,
        currency=NONE,
        valid_until=NONE,
        incoterm=NONE,
        payment_terms=NONE,
        lines=[line_draft(part.position, unit_price=ev(plain_number(quoted.price), price))],
        reply_kind="amendment",
        unchanged_terms=ev(PRESERVE_PRICE, PRESERVE_PRICE),
        update_evidence=ev(change, change),
    )
    return Email(body, truth, f" - Revisão {revision}" if revision is not None else "")


def pt_lead_change(t: Thread, positions: Sequence[int], lead: int) -> Email:
    """An amendment: a shorter lead time on the given lines, everything else kept (stated)."""
    days = f"{lead} dias"
    change = f"Conseguimos reduzir o prazo de entrega para {days} em todos os itens"
    body = f"Prezados,\n\nSobre a {t.ref}: {change}. {PRESERVE_ALL}.\n\n{_pt_signature(t)}"
    truth = QuoteExtractionOutput(
        is_quote=True,
        currency=NONE,
        valid_until=NONE,
        incoterm=NONE,
        payment_terms=NONE,
        lines=[line_draft(p, lead_time_days=ev(str(lead), days)) for p in positions],
        reply_kind="amendment",
        unchanged_terms=ev(PRESERVE_ALL, PRESERVE_ALL),
        update_evidence=ev(change, change),
    )
    return Email(body, truth)


def pt_question(t: Thread) -> Email:
    """No prices: two technical questions about the shaft, before quoting anything."""
    first = "Vocês podem nos enviar o desenho técnico do eixo com as tolerâncias de usinagem?"
    second = "O material do eixo deve ser aço SAE 1045 ou SAE 4140?"
    body = (
        f"Prezados,\n\nRecebemos a solicitação {t.ref}. Antes de cotar, precisamos esclarecer "
        f"dois pontos sobre o item 2 (000102 Eixo motor MTR-100):\n\n1. {first}\n2. {second}\n\n"
        "Assim que recebermos essas informações, enviaremos a cotação completa dos três itens."
        f"\n\n{_pt_signature(t)}"
    )
    truth = QuoteExtractionOutput(
        is_quote=False,
        currency=NONE,
        valid_until=NONE,
        incoterm=NONE,
        payment_terms=NONE,
        reply_kind="clarification",
        update_evidence=ev(first, first),
        technical_questions=[first, second],
    )
    return Email(body, truth)


QUESTION_WORDS: Final = ("desenho", "SAE 4140")


def pt_partial(t: Thread, lines: Sequence[Quoted], missing: int) -> Email:
    """Two of three lines priced; the third promised for tomorrow."""
    part = t.part(missing)
    later = (
        f"O {_pt_label(part).lower()} cotaremos amanhã: estamos confirmando a disponibilidade "
        "com a produção."
    )
    return pt_offer(t, lines, opening=OFFER, after=[later])


def pt_completion(t: Thread, quoted: Quoted) -> Email:
    """The promised line, alone, everything else kept (stated): an amendment adding a line."""
    text, draft = _pt_line(t, quoted)
    opening = f"Conforme combinado, segue a cotação que faltava para a {t.ref}:"
    body = f"Prezados,\n\n{opening}\n\n{text}\n\n{PRESERVE_ALL}.\n\n{_pt_signature(t)}"
    truth = QuoteExtractionOutput(
        is_quote=True,
        currency=NONE,
        valid_until=NONE,
        incoterm=NONE,
        payment_terms=NONE,
        lines=[draft],
        reply_kind="amendment",
        unchanged_terms=ev(PRESERVE_ALL, PRESERVE_ALL),
        update_evidence=ev(text, text),
    )
    return Email(body, truth)


ALTERNATIVE_MPN: Final = "EX-4140-15"


def pt_alternative(t: Thread, lines: Sequence[Quoted], alternative: Quoted) -> Email:
    """The requested shaft declined; another part offered for it, said so plainly."""
    part = t.part(alternative.position)
    plural, single = _pt_unit(part)
    qty = f"{pt_quantity(part.quantity)} {plural}"
    price = f"R$ {pt_number(alternative.price)}"
    lead = f"{alternative.lead} dias"
    decline = (
        f"Não fabricamos o {_pt_label(part).lower()} conforme especificado. Como alternativa, "
        f"oferecemos o eixo {ALTERNATIVE_MPN} (fabricante Metalúrgica Delta): {qty} a {price} "
        f"por {single}, prazo de entrega de {lead}."
    )
    email = pt_offer(t, lines, opening=OFFER, after=[decline])
    alt = line_draft(
        part.position,
        offered_mpn=ev(ALTERNATIVE_MPN, ALTERNATIVE_MPN),
        is_alternative=True,
        min_quantity=ev(plain_number(part.quantity), qty),
        unit_price=ev(plain_number(alternative.price), price),
        unit=ev(single, f"por {single}"),
        lead_time_days=ev(str(alternative.lead), lead),
    )
    truth = email.truth.model_copy(update={"lines": [*email.truth.lines, alt]})
    return Email(email.body, truth)


EXTRA_PART: Final = "000302"
_EXTRA_TEXT: Final = (
    "Aproveitando o contato, nosso departamento de manutenção também oferece a correia de "
    f"equipamento cód. {EXTRA_PART}: 15 peças a R$ 77,00 por peça, entrega em 7 dias."
)
EXTRA_PRICE: Final = Decimal("77.00")


def pt_offer_with_extra(t: Thread, lines: Sequence[Quoted]) -> Email:
    """A whole offer plus another department's item nobody asked for."""
    extra = ExtraOfferDraft(
        description="Correia de equipamento (departamento de manutenção)",
        mpn=EXTRA_PART,
        quantity="15 peças",
        unit_price="77,00",
        currency="BRL",
        conditions="entrega em 7 dias",
        quote=_EXTRA_TEXT,
    )
    return pt_offer(t, lines, opening=OFFER, after=[_EXTRA_TEXT], extras=[extra])


# --- RFQ-6, the real thread (English, from a Brazilian supplier) ------------------------------

RFQ6_PRICES: Final = {1: Decimal("19.50"), 2: Decimal("89.00"), 3: Decimal("95.00")}
RFQ6_MOQ: Final = {1: "10", 2: "10", 3: "5"}
RFQ6_APPROX_DAYS: Final = 10
# The second reply's lead times per line, in business days.
RFQ6_BUSINESS_DAYS: Final = {1: 7, 2: 12, 3: 8}
# The shafts' lead time after Bido's lead-time ask (business days).
RFQ6_SHAFT_CONFIRMED: Final = 7
RFQ6_VALIDITY_DAYS: Final = 15
_RFQ6_SIGNATURE: Final = "Best regards,\nSales Team\nMetalNova Teste"


def rfq6_first_reply(t: Thread) -> Email:
    """The supplier's first answer, verbatim: per-unit prices, "valid for 15 days", Net 30,
    MOQ, packaging, "approximately 10 business days" for all and "a few additional days"
    for the shafts."""
    p = {k: f"BRL {v:.2f}" for k, v in RFQ6_PRICES.items()}
    lead = f"Lead time is approximately {RFQ6_APPROX_DAYS} business days after order confirmation"
    body = (
        "Hello,\n\nThank you for reaching out!\n\n"
        f"We can supply the requested items. Our current prices are {p[1]} per piece for the "
        f"bearings (000101), {p[2]} per piece for the shafts (000102), and {p[3]} per kg for "
        "the copper wire (000104).\n\n"
        f"Our standard payment terms are Net 30, and the quotation is valid for "
        f"{RFQ6_VALIDITY_DAYS} days. The MOQ is {RFQ6_MOQ[1]} pieces for bearings and shafts, "
        f"and {RFQ6_MOQ[3]} kg for copper wire. All items are supplied in standard industrial "
        "packaging.\n\n"
        f"{lead}. We may need a few additional days for the shafts, depending on production "
        "availability.\n\n"
        f"Let us know if these conditions work for you.\n\n{_RFQ6_SIGNATURE}"
    )
    moq_pieces = f"The MOQ is {RFQ6_MOQ[1]} pieces for bearings and shafts"
    moq_kg = f"{RFQ6_MOQ[3]} kg for copper wire"
    lines = [
        line_draft(
            1,
            offered_mpn=ev("000101", "bearings (000101)"),
            unit_price=ev(plain_number(RFQ6_PRICES[1]), f"{p[1]} per piece"),
            unit=ev("piece", f"{p[1]} per piece"),
            moq=ev(RFQ6_MOQ[1], moq_pieces),
            # Approximate, and in business days: written as given.
            lead_time_days=ev(str(RFQ6_APPROX_DAYS), lead, 0.8),
        ),
        line_draft(
            2,
            offered_mpn=ev("000102", "shafts (000102)"),
            unit_price=ev(plain_number(RFQ6_PRICES[2]), f"{p[2]} per piece"),
            unit=ev("piece", f"{p[2]} per piece"),
            moq=ev(RFQ6_MOQ[2], moq_pieces),
            # "a few additional days": no number to give.
        ),
        line_draft(
            3,
            offered_mpn=ev("000104", "copper wire (000104)"),
            unit_price=ev(plain_number(RFQ6_PRICES[3]), f"{p[3]} per kg"),
            unit=ev("kg", f"{p[3]} per kg"),
            moq=ev(RFQ6_MOQ[3], moq_kg),
            lead_time_days=ev(str(RFQ6_APPROX_DAYS), lead, 0.8),
        ),
    ]
    truth = QuoteExtractionOutput(
        is_quote=True,
        currency=ev("BRL", p[1]),
        # "valid for 15 days": a duration from a date the model is not given.
        valid_until=NONE,
        incoterm=NONE,
        payment_terms=ev("Net 30", "Our standard payment terms are Net 30"),
        lines=lines,
        missing_fields=["lead_time"],
    )
    return Email(body, truth)


def rfq6_second_reply(t: Thread) -> Email:
    """The supplier's answer to the clarification, verbatim: validity relative to the
    original offer, the requested quantities, and 7/12/8 business days per line."""
    p = {k: f"BRL {v:.2f}" for k, v in RFQ6_PRICES.items()}
    days = RFQ6_BUSINESS_DAYS
    bearings = (
        f"For the bearings (000101), {p[1]} per piece applies to the requested 70 pieces, with a "
        f"lead time of {days[1]} business days."
    )
    shafts = (
        f"For the shafts (000102), {p[2]} per piece applies to the requested 15 pieces, with a "
        f"lead time of {days[2]} business days due to our current production schedule."
    )
    wire = (
        f"For the copper wire (000104), {p[3]} per kg applies to the requested 12.5 kg, with a "
        f"lead time of {days[3]} business days."
    )
    body = (
        "Hello Biduuuuu,\n\nThank you for your follow-up.\n\n"
        f"Our quotation is valid for {RFQ6_VALIDITY_DAYS} calendar days from the date of our "
        "original offer.\n\n"
        f"The quoted prices apply to the quantities requested in {t.ref}:\n\n"
        f"{bearings}\n\n{shafts}\n\n{wire}\n\n"
        "All lead times are counted from purchase order confirmation.\n\n"
        f"Please let us know if you would like to proceed.\n\n{_RFQ6_SIGNATURE}"
    )

    def answered(position: int, part: str, unit: str, qty: str, quantity: str) -> ProposalLineDraft:
        price = p[position]
        return line_draft(
            position,
            offered_mpn=ev(part, f"({part})"),
            min_quantity=ev(quantity, f"the requested {qty}"),
            unit_price=ev(plain_number(RFQ6_PRICES[position]), f"{price} per {unit}"),
            unit=ev(unit, f"{price} per {unit}"),
            lead_time_days=ev(str(days[position]), f"lead time of {days[position]} business days"),
        )

    truth = QuoteExtractionOutput(
        is_quote=True,
        currency=ev("BRL", p[1]),
        valid_until=NONE,
        incoterm=NONE,
        payment_terms=NONE,
        lines=[
            answered(1, "000101", "piece", "70 pieces", "70"),
            answered(2, "000102", "piece", "15 pieces", "15"),
            answered(3, "000104", "kg", "12.5 kg", "12.5"),
        ],
    )
    return Email(body, truth)


RFQ6_KEPT: Final = "All other conditions remain unchanged"


def rfq6_shaft_lead_time(t: Thread) -> Email:
    """After a lead-time ask: the shafts within 7 business days, everything else kept."""
    change = (
        f"we can now deliver the shafts (000102) within {RFQ6_SHAFT_CONFIRMED} business days "
        "after purchase order confirmation"
    )
    body = (
        f"Hello,\n\nRegarding {t.ref}: our production schedule was freed up and {change}. "
        f"{RFQ6_KEPT}.\n\n{_RFQ6_SIGNATURE}"
    )
    truth = QuoteExtractionOutput(
        is_quote=True,
        currency=NONE,
        valid_until=NONE,
        incoterm=NONE,
        payment_terms=NONE,
        lines=[
            line_draft(
                2,
                lead_time_days=ev(
                    str(RFQ6_SHAFT_CONFIRMED), f"within {RFQ6_SHAFT_CONFIRMED} business days"
                ),
            )
        ],
        reply_kind="amendment",
        unchanged_terms=ev(RFQ6_KEPT, RFQ6_KEPT),
        update_evidence=ev(change, change),
    )
    return Email(body, truth)


# --- A German supplier -------------------------------------------------------------------------

_DE_SIGNATURE: Final = "Mit freundlichen Grüßen\nVertrieb\n{supplier}"
_DE_TERMS: Final = "Alle Preise in EUR, Zahlungsziel 30 Tage netto"


def de_offer(t: Thread, lines: Sequence[Quoted], *, opening: str, valid_days: int | None) -> Email:
    """A whole offer in German; without `valid_days` it states no validity."""
    texts: list[str] = []
    drafts: list[ProposalLineDraft] = []
    for quoted in lines:
        part = t.part(quoted.position)
        qty = f"{pt_quantity(part.quantity)} Stück"
        price = f"{de_number(quoted.price)} EUR pro Stück"
        lead = f"Lieferzeit {quoted.lead} Tage"
        texts.append(
            f"Pos. {part.position} {part.description} ({part.part_number}): {qty} zu {price}, "
            f"{lead}."
        )
        drafts.append(
            line_draft(
                part.position,
                min_quantity=ev(plain_number(part.quantity), qty),
                unit_price=ev(plain_number(quoted.price), price),
                unit=ev("Stück", "pro Stück"),
                lead_time_days=ev(str(quoted.lead), lead),
            )
        )
    footer = [f"{_DE_TERMS}."]
    valid_until = t.start + timedelta(days=valid_days) if valid_days is not None else None
    valid = f"Angebot gültig bis {de_date(valid_until)}" if valid_until is not None else None
    if valid is not None:
        footer.append(f"{valid}.")
    body = "\n\n".join(
        [
            f"Sehr geehrte Damen und Herren,\n\n{opening.format(ref=t.ref)}",
            "\n".join(texts),
            " ".join(footer),
            _DE_SIGNATURE.format(supplier=t.supplier),
        ]
    )
    truth = QuoteExtractionOutput(
        is_quote=True,
        currency=ev("EUR", _DE_TERMS),
        valid_until=ev(valid_until.isoformat(), valid)
        if valid_until is not None and valid is not None
        else NONE,
        incoterm=NONE,
        payment_terms=ev("30 Tage netto", "Zahlungsziel 30 Tage netto"),
        lines=drafts,
    )
    return Email(body, truth)


DE_OFFER: Final = "vielen Dank für Ihre Anfrage {ref}. Gerne bieten wir an:"
DE_REVISED: Final = "vielen Dank für Ihre Rückmeldung zu {ref}. Unser überarbeitetes Angebot:"
DE_FINAL: Final = "zu {ref} senden wir Ihnen unser bestes und letztes Angebot:"


# --- Mock writers: the released prompts, followed literally ----------------------------------

_SIGN_OFF: Final = {"pt": "Atenciosamente,", "de": "Mit freundlichen Grüßen", "en": "Best regards,"}


def _lang(language: str) -> str:
    return language if language in _SIGN_OFF else "en"


def _signature(language: str) -> str:
    """The closing formula only: the v2 prompts leave the signature to code
    (`bido_back.templates.signature.signed` puts the buyer's name lines under it)."""
    return _SIGN_OFF[language]


def _date(language: str, value: date) -> str:
    return {"pt": pt_date, "de": de_date}.get(language, date.isoformat)(value)


def _number(language: str, value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if language == "en" else text.replace(".", ",")


_FIELD: Final[dict[str, dict[str, str]]] = {
    "pt": {
        "currency": "a moeda dos preços",
        "valid_until": "por quanto tempo a proposta é válida",
        "unit_price": "o preço unitário",
        "quantity": "a quantidade (ou faixa de quantidade) a que o preço se aplica",
        "lead_time": "o prazo de entrega",
    },
    "de": {
        "currency": "die Währung der Preise",
        "valid_until": "wie lange das Angebot gültig ist",
        "unit_price": "den Stückpreis",
        "quantity": "die Menge bzw. Mengenstaffel, für die der Preis gilt",
        "lead_time": "die Lieferzeit",
    },
    "en": {
        "currency": "the currency of the prices",
        "valid_until": "how long it is valid",
        "unit_price": "the unit price",
        "quantity": "the quantity or quantity tier the price applies to",
        "lead_time": "the lead time",
    },
}
_WHERE: Final = {"pt": ("Proposta", "Item"), "de": ("Angebot", "Pos."), "en": ("Offer", "Position")}
_CONFIRM: Final = {
    "pt": "confirmar {x}",
    "de": "bitte bestätigen Sie {x}",
    "en": "please confirm {x}",
}


def followup_writer(task: FollowupEmailInput) -> FollowupEmailOutput:
    """The follow-up writer (v2 prompt): a chase, or exactly the open items."""
    lang = _lang(task.supplier_language)
    sign = _signature(lang)
    if task.kind == "chase":
        sent = _date(lang, task.original_sent_on) if task.original_sent_on else ""
        due = _date(lang, task.response_due) if task.response_due else None
        text = {
            "pt": (
                f"Prezados, {task.supplier_name},\n\nGostaríamos de lembrar a nossa solicitação "
                f"de cotação {task.rfq_ref} ({task.rfq_title}), enviada em {sent}. Poderiam nos "
                "informar se enviarão uma proposta?"
                + (f" Pedimos o retorno até {due}." if due else "")
                + (" Em breve vamos comparar as propostas recebidas." if task.chase_number > 1
                   else "")
            ),
            "de": (
                f"Sehr geehrte Damen und Herren der {task.supplier_name},\n\nwir möchten Sie an "
                f"unsere Anfrage {task.rfq_ref} ({task.rfq_title}) vom {sent} erinnern. Werden Sie "
                "uns ein Angebot senden?"
                + (f" Bitte antworten Sie bis zum {due}." if due else "")
                + (" In Kürze vergleichen wir die eingegangenen Angebote."
                   if task.chase_number > 1 else "")
            ),
            "en": (
                f"Dear {task.supplier_name},\n\nWe would like to remind you of our request for "
                f"quotation {task.rfq_ref} ({task.rfq_title}), sent on {sent}. Could you let us "
                "know whether you will send a quote?"
                + (f" Please answer by {due}." if due else "")
                + (" We will soon compare the offers received." if task.chase_number > 1 else "")
            ),
        }[lang]  # fmt: skip
        return FollowupEmailOutput(body=f"{text}\n\n{sign}")
    fields = _FIELD[lang]
    offer, position = _WHERE[lang]
    items: list[str] = []
    for item in task.open_items:
        wanted = fields[item.field]
        if item.problem == "unclear":
            wanted = _CONFIRM[lang].format(x=wanted)
        where = offer if item.position is None else f"{position} {item.position} – {item.line}"
        items.append(f"{where}: {wanted}.")
    intro = {
        "pt": f"Prezados, {task.supplier_name},\n\nObrigado pela proposta para a {task.rfq_ref}. "
        "Para concluirmos a análise, poderiam nos informar:",
        "de": f"Sehr geehrte Damen und Herren der {task.supplier_name},\n\nvielen Dank für Ihr "
        f"Angebot zu {task.rfq_ref}. Bitte teilen Sie uns noch Folgendes mit:",
        "en": f"Dear {task.supplier_name},\n\nThank you for your offer for {task.rfq_ref}. Could "
        "you please provide the following missing information?",
    }[lang]
    return FollowupEmailOutput(body=intro + "\n" + "\n".join(items) + f"\n\n{sign}")


def negotiation_writer(task: NegotiationEmailInput) -> NegotiationEmailOutput:
    """The negotiation writer (v2 prompt): every number as given, by `reply_by`."""
    lang = _lang(task.supplier_language)
    sign = _signature(lang)
    words = {
        "pt": {
            "hello": f"Prezados, {task.supplier_name},",
            "thanks": f"Obrigado pela proposta para a {task.rfq_ref}.",
            "competition": "Recebemos propostas mais competitivas.",
            "counter": "Pedimos que revisem as condições abaixo:",
            "bafo": "Esta é a última rodada antes da nossa decisão: pedimos a sua melhor e "
            "última oferta para os itens abaixo:",
            "price": "{where}: preço unitário de {price} por {unit}, para {qty} {unit}",
            "lead": "{where}: prazo de entrega de no máximo {days} dias",
            "where": "Item {p} – {line}",
            "by_counter": "Por favor, enviem a proposta revisada até {date}.",
            "by_bafo": "Por favor, enviem a sua melhor e última oferta até {date}.",
        },
        "de": {
            "hello": "Sehr geehrte Damen und Herren,",
            "thanks": f"vielen Dank für Ihr Angebot zu {task.rfq_ref}.",
            "competition": "Uns liegen wettbewerbsfähigere Angebote vor.",
            "counter": "Wir bitten Sie, folgende Punkte zu überprüfen:",
            "bafo": "Dies ist die letzte Runde vor unserer Entscheidung: Bitte senden Sie uns "
            "Ihr bestes und letztes Angebot für folgende Positionen:",
            "price": "{where}: Stückpreis {price} pro {unit} für {qty} {unit}",
            "lead": "{where}: Lieferzeit von höchstens {days} Tagen",
            "where": "Pos. {p} {line}",
            "by_counter": "Bitte senden Sie uns Ihr überarbeitetes Angebot bis zum {date}.",
            "by_bafo": "Bitte senden Sie uns Ihr bestes und letztes Angebot bis zum {date}.",
        },
        "en": {
            "hello": f"Dear {task.supplier_name},",
            "thanks": f"Thank you for your offer for {task.rfq_ref}.",
            "competition": "We have received more competitive offers.",
            "counter": "We ask you to review the following:",
            "bafo": "This is the last round before we decide: please send your best and final "
            "offer for the items below:",
            "price": "{where}: a unit price of {price} per {unit}, for {qty} {unit}",
            "lead": "{where}: a lead time of at most {days} days",
            "where": "Position {p} – {line}",
            "by_counter": "Please send a revised offer by {date}.",
            "by_bafo": "Please send your best and final offer by {date}.",
        },
    }[lang]
    head = [words["thanks"]]
    if task.mention_competition:
        head.append(words["competition"])
    head.append(words[task.action])
    lines: list[str] = []
    for ask in task.lines:
        where = words["where"].format(p=ask.position, line=ask.line)
        if ask.target_unit_price is not None:
            amount = _number(lang, ask.target_unit_price)
            price = f"R$ {amount}" if ask.currency == "BRL" else f"{amount} {ask.currency}"
            lines.append(
                "- "
                + words["price"].format(
                    where=where, price=price, unit=ask.unit, qty=_number(lang, ask.quantity)
                )
            )
        if ask.required_lead_time_days is not None:
            lines.append("- " + words["lead"].format(where=where, days=ask.required_lead_time_days))
    tail: list[str] = []
    if task.reply_by is not None:
        key = "by_bafo" if task.action == "bafo" else "by_counter"
        tail.append(words[key].format(date=_date(lang, task.reply_by)))
    body = "\n".join([words["hello"], "", " ".join(head), *lines, *tail, "", sign])
    return NegotiationEmailOutput(body=body)


class ScenarioModel(ScriptedModel):
    """The flow evals' scripted model with these writers; counts extractions it had no
    script for (a harness bug: the email changed after its truth was registered)."""

    def __init__(self) -> None:
        super().__init__()
        self.unscripted: list[str] = []
        self._scripts["rfq_followup_writer"] = followup_writer
        self._scripts["rfq_negotiation_writer"] = negotiation_writer

    def _extract(self, task: QuoteExtractionInput) -> QuoteExtractionOutput | None:
        found = self.extraction.get(task.email_body.strip())
        if found is None:
            self.unscripted.append(task.email_subject)
        return found
