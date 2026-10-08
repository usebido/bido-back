"""Scripted suppliers for the flow evals: who they are, what they quote, how they write.

Every number a supplier answers is decided here, by code (price per line per
round, lead time, declined lines); the text around it is a fixed template in
the supplier's language (de, it, fr, en). `reply_for` builds the email body and
attachments plus the extraction a correct model returns for it (mock mode
scripts that output; live mode reads the same email with the real model).

Domains are reserved test domains only (`*.example.test`), except where a
scenario needs an unconfirmed real-looking domain on purpose (recipient guard),
and those emails never leave the process either.
"""

from dataclasses import dataclass
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
from evals.quotes import _pdf, _xlsx  # pyright: ignore[reportPrivateUsage]

XLSX: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SupplierKind = Literal["authorized_distributor", "manufacturer", "reseller", "distributor"]
Attach = Literal["none", "xlsx", "pdf"]


@dataclass(frozen=True, slots=True)
class Item:
    """One requested line, with the buyer's limits (target, maximum, lead time)."""

    position: int
    description: str
    mpn: str
    quantity: Decimal
    target: Decimal
    max_price: Decimal
    max_lead: int


ITEMS: Final = (
    Item(
        1,
        "MLCC 100nF 50V X7R 0603",
        "GRM188R71H104KA93D",
        Decimal(10000),
        Decimal("0.040"),
        Decimal("0.060"),
        42,
    ),
    Item(
        2,
        "Board-to-board connector 40 pin",
        "DF40C-40DS-0.4V",
        Decimal(500),
        Decimal("1.00"),
        Decimal("1.50"),
        42,
    ),
)


@dataclass(frozen=True, slots=True)
class Quote:
    """One answer: a unit price per line position (absent = that line is declined)."""

    prices: dict[int, Decimal]
    lead: int


@dataclass(frozen=True, slots=True)
class Persona:
    key: str
    name: str
    language: str
    country: str
    kind: SupplierKind
    # quotes[r]: the answer after r negotiation emails (the last one repeats).
    quotes: tuple[Quote, ...]
    domain: str = ""
    # Another department's offers in the same email (to capture, not to price).
    noise: bool = False
    # Where the prices sit in round 0: the body, a spreadsheet or a PDF.
    attach: Attach = "none"
    contact: str = "Sales"

    @property
    def email(self) -> str:
        return f"sales@{self.domain or self.key + '.example.test'}"

    def quote(self, round: int) -> Quote:
        return self.quotes[min(round, len(self.quotes) - 1)]


def _q(prices: dict[int, str], lead: int) -> Quote:
    return Quote({p: Decimal(v) for p, v in prices.items()}, lead)


# --- The negotiation cast (golden expectations in evals.flow) ------------------------------

TOUGH: Final = Persona(
    "tough",
    "Rhein Elektronik GmbH",
    "de",
    "DE",
    "authorized_distributor",
    (
        _q({1: "0.055", 2: "1.40"}, 30),
        _q({1: "0.055", 2: "1.40"}, 30),
        _q({1: "0.055", 2: "1.40"}, 30),
        # Concedes only to the best-and-final request (round 3).
        _q({1: "0.041", 2: "1.02"}, 30),
    ),
    attach="xlsx",
)
CHEAP_LATE: Final = Persona(
    "cheaplate",
    "Fornitori Lombardi S.r.l.",
    "it",
    "IT",
    "authorized_distributor",
    (_q({1: "0.038", 2: "0.95"}, 70),),
)
RESELLER: Final = Persona(
    "reseller",
    "Global Parts Brokers Ltd",
    "en",
    "GB",
    "reseller",
    (
        _q({1: "0.030", 2: "1.20"}, 20),
        _q({1: "0.030", 2: "1.10"}, 20),
        _q({1: "0.030", 2: "1.10"}, 20),
        _q({1: "0.030", 2: "1.08"}, 20),
    ),
    noise=True,
)
EAGER: Final = Persona(
    "eager",
    "Composants Lyonnais SA",
    "fr",
    "FR",
    "authorized_distributor",
    (
        _q({1: "0.045", 2: "1.10"}, 25),
        # Takes our target on line 1 at once; line 2 only part of the way.
        _q({1: "0.040", 2: "1.05"}, 25),
    ),
)
DECLINER: Final = Persona(
    "decliner",
    "Northbridge Components Inc.",
    "en",
    "US",
    "manufacturer",
    (_q({1: "0.042"}, 28),),
    attach="pdf",
)
NEGOTIATION_CAST: Final = (TOUGH, CHEAP_LATE, RESELLER, EAGER, DECLINER)

# Early close: one supplier within target on every line, one above.
WITHIN: Final = Persona(
    "within",
    "Alpen Bauteile AG",
    "de",
    "AT",
    "authorized_distributor",
    (_q({1: "0.039", 2: "0.98"}, 21),),
)
ABOVE: Final = Persona(
    "above",
    "Atlantic Components Ltd",
    "en",
    "IE",
    "authorized_distributor",
    (_q({1: "0.050", 2: "1.30"}, 21),),
)


# --- Writing ---------------------------------------------------------------------------------


def number(value: Decimal, language: str) -> str:
    text = format(value, "f")
    return text if language == "en" else text.replace(".", ",")


def quantity(value: Decimal, language: str) -> str:
    whole = int(value)
    if language == "en":
        return f"{whole:,}"
    if language in ("de", "it"):
        return f"{whole:,}".replace(",", ".")
    return str(whole)


def written_date(value: date, language: str) -> str:
    if language == "de":
        return value.strftime("%d.%m.%Y")
    if language in ("it", "fr"):
        return value.strftime("%d/%m/%Y")
    return value.isoformat()


_UNIT: Final = {"de": "Stück", "it": "pz", "fr": "pièces", "en": "pcs"}
_LEAD: Final = {"de": "Tage", "it": "giorni", "fr": "jours", "en": "days"}

_TEXT: Final[dict[str, dict[str, str]]] = {
    "de": {
        "hello": "Sehr geehrte Damen und Herren,",
        "first": "vielen Dank für Ihre Anfrage {ref}. Gerne bieten wir an:",
        "again": "vielen Dank für Ihre Rückmeldung zu {ref}. Unser aktuelles Angebot:",
        "same": "zu {ref}: Leider können wir unsere Preise nicht weiter senken. Es bleibt bei:",
        "line": "Pos. {p} {desc} ({mpn}): {qty} {unit} zu {price} EUR pro Stück, "
        "Lieferzeit {lead} {days}.",
        "declined": "Pos. {p} ({mpn}): können wir leider nicht anbieten.",
        "terms": "Alle Preise in EUR, Lieferung DAP. Angebot gültig bis {valid}.",
        "sheet": "Unser Angebot zu {ref} finden Sie in der beigefügten Tabelle.",
        "bye": "Mit freundlichen Grüßen",
    },
    "it": {
        "hello": "Buongiorno,",
        "first": "grazie per la vostra richiesta {ref}. Ecco la nostra offerta:",
        "again": "grazie per il riscontro su {ref}. La nostra offerta aggiornata:",
        "same": "riguardo a {ref}: purtroppo non possiamo ridurre i prezzi. Confermiamo:",
        "line": "Pos. {p} {desc} ({mpn}): {qty} {unit} a {price} EUR cad., consegna {lead} {days}.",
        "declined": "Pos. {p} ({mpn}): purtroppo non possiamo quotarlo.",
        "terms": "Prezzi in EUR, resa DAP. Offerta valida fino al {valid}.",
        "sheet": "In allegato la nostra offerta per {ref}.",
        "bye": "Cordiali saluti",
    },
    "fr": {
        "hello": "Bonjour,",
        "first": "merci pour votre demande {ref}. Voici notre offre :",
        "again": "merci pour votre retour concernant {ref}. Notre offre mise à jour :",
        "same": "concernant {ref} : nous ne pouvons malheureusement pas baisser nos prix :",
        "line": "Pos. {p} {desc} ({mpn}) : {qty} {unit} à {price} EUR l'unité, "
        "délai {lead} {days}.",
        "declined": "Pos. {p} ({mpn}) : nous ne pouvons malheureusement pas le proposer.",
        "terms": "Prix en EUR, livraison DAP. Offre valable jusqu'au {valid}.",
        "sheet": "Vous trouverez notre offre pour {ref} en pièce jointe.",
        "bye": "Cordialement",
    },
    "en": {
        "hello": "Hello,",
        "first": "thank you for your request {ref}. Please find our offer below:",
        "again": "thank you for your feedback on {ref}. Our updated offer:",
        "same": "regarding {ref}: unfortunately we cannot lower our prices. Our offer stands:",
        "line": "Item {p} {desc} ({mpn}): {qty} {unit} at {price} EUR each, "
        "lead time {lead} {days}.",
        "declined": "Item {p} ({mpn}): unfortunately we cannot quote this item.",
        "terms": "All prices in EUR, delivery DAP. Offer valid until {valid}.",
        "sheet": "Please find our offer for {ref} attached.",
        "bye": "Best regards",
    },
}
_NOISE: Final = (
    "PS from our Power Systems department: this month we also offer the DC/DC module "
    "PSU-48-12 at EUR 18.50 per unit (min. 50 units)."
)


def _ev(value: str, quote: str, at: dict[str, object] | None = None) -> ExtractedValue:
    """A value read from the email body, or from the attachment locator `at`."""
    where = at or {"kind": "email_body"}
    return ExtractedValue(
        value=value, source=SourceRef.model_validate({**where, "quote": quote}), confidence=0.95
    )


_NONE: Final = ExtractedValue(value=None, source=None, confidence=0)
_EXTRA: Final = ExtraOfferDraft(
    description="DC/DC module PSU-48-12 (Power Systems department)",
    mpn="PSU-48-12",
    quantity="min. 50 units",
    unit_price="18.50",
    currency="EUR",
    conditions="this month",
    quote="DC/DC module PSU-48-12 at EUR 18.50 per unit (min. 50 units)",
)


@dataclass(frozen=True, slots=True)
class Reply:
    subject: str
    body: str
    attachments: tuple[tuple[str, str, bytes], ...]
    # What a correct extraction returns for this email (mock mode scripts it).
    truth: QuoteExtractionOutput
    quote: Quote


def _text(language: str, key: str, **values: object) -> str:
    return _TEXT[language][key].format(**values)


def reply_subject(subject: str) -> str:
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def reply_for(
    persona: Persona,
    *,
    rfq_ref: str,
    subject: str,
    round: int,
    valid_until: date,
    items: tuple[Item, ...] = ITEMS,
    previous: Quote | None = None,
    drop: tuple[str, ...] = (),
) -> Reply:
    """The supplier's answer after `round` negotiation emails (0: the first quote).

    In round 0 a persona with `attach` puts its prices in a spreadsheet or a
    PDF instead of the body. `drop` leaves fields out, for the clarification
    scenario: "lead" (every line's lead time) or "valid" (the offer's validity).
    """
    lang = persona.language
    quote = persona.quote(round)
    opening = "same" if previous == quote else "again" if round else "first"
    attach: Attach = persona.attach if round == 0 else "none"
    priced: list[str] = []
    declined: list[str] = []
    drafts: list[ProposalLineDraft] = []
    sheet: list[list[object]] = [["Pos", "MPN", "Qty", "Unit", "Unit price EUR", "Lead days"]]
    for item in items:
        price = quote.prices.get(item.position)
        if price is None:
            declined.append(_text(lang, "declined", p=item.position, mpn=item.mpn))
            continue
        qty_text = quantity(item.quantity, lang)
        price_text = number(price, lang)
        line = _text(
            lang,
            "line",
            p=item.position,
            desc=item.description,
            mpn=item.mpn,
            qty=qty_text,
            unit=_UNIT[lang],
            price=price_text,
            lead=quote.lead,
            days=_LEAD[lang],
        )
        if "lead" in drop:
            line = line.rsplit(", ", 1)[0] + "."
        priced.append(line)
        lead_days = None if "lead" in drop else str(quote.lead)
        if attach == "xlsx":
            row = len(sheet) + 1
            sheet.append(
                [item.position, item.mpn, int(item.quantity), "Stk.", float(price), quote.lead]
            )

            def cell(column: str, value: str, row: int = row) -> ExtractedValue:
                at: dict[str, object] = {
                    "kind": "sheet", "attachment": "offer.xlsx", "sheet": "Angebot",
                    "cell": f"{column}{row}",
                }  # fmt: skip
                return _ev(value, value, at)

            drafts.append(
                ProposalLineDraft(
                    rfq_line_position=item.position,
                    offered_mpn=cell("B", item.mpn),
                    min_quantity=cell("C", str(int(item.quantity))),
                    # The cell holds a number: its text is the shortest form ("1.4").
                    unit_price=cell("E", f"{float(price):g}"),
                    unit=cell("D", "Stk."),
                    tooling_cost=_NONE,
                    moq=_NONE,
                    lead_time_days=cell("F", str(quote.lead)),
                    origin_country=_NONE,
                    qualification=_NONE,
                )
            )
            continue
        at: dict[str, object] | None = (
            {"kind": "pdf", "attachment": "quotation.pdf", "page": 1} if attach == "pdf" else None
        )
        drafts.append(
            ProposalLineDraft(
                rfq_line_position=item.position,
                offered_mpn=_ev(item.mpn, item.mpn, at),
                min_quantity=_ev(str(int(item.quantity)), f"{qty_text} {_UNIT[lang]}", at),
                unit_price=_ev(format(price, "f"), f"{price_text} EUR", at),
                unit=_ev(_UNIT[lang], _UNIT[lang], at),
                tooling_cost=_NONE,
                moq=_NONE,
                lead_time_days=_NONE
                if lead_days is None
                else _ev(lead_days, f"{lead_days} {_LEAD[lang]}", at),
                origin_country=_NONE,
                qualification=_NONE,
            )
        )

    valid_text = written_date(valid_until, lang)
    terms = _text(lang, "terms", valid=valid_text)
    if "valid" in drop:
        terms = terms.rsplit(". ", 1)[0] + "."
    parts = [_text(lang, "hello") + "\n\n" + _text(lang, opening, ref=rfq_ref)]
    if attach == "none":
        parts.extend(priced)
    else:
        parts.append(_text(lang, "sheet", ref=rfq_ref))
    parts.extend(declined)
    parts.append(terms)
    if persona.noise:
        parts.append(_NOISE)
    parts.append(f"{_text(lang, 'bye')}\n{persona.contact}\n{persona.name}")

    attachments: list[tuple[str, str, bytes]] = []
    if attach == "xlsx":
        attachments.append(("offer.xlsx", XLSX, _xlsx("Angebot", sheet)))
    elif attach == "pdf":
        pdf = [f"Quotation {rfq_ref} - {persona.name}", *priced]
        attachments.append(("quotation.pdf", "application/pdf", _pdf(pdf)))
    truth = QuoteExtractionOutput(
        is_quote=bool(drafts),
        declined=not drafts,
        currency=_ev("EUR", terms.split(",")[0]),
        valid_until=_NONE if "valid" in drop else _ev(valid_until.isoformat(), valid_text),
        incoterm=_ev("DAP", "DAP"),
        payment_terms=_NONE,
        lines=drafts,
        extra_offers=[_EXTRA] if persona.noise else [],
    )
    return Reply(
        subject=reply_subject(subject),
        body="\n\n".join(parts),
        attachments=tuple(attachments),
        truth=truth,
        quote=quote,
    )


# --- Non-quotes -----------------------------------------------------------------------------

_OUT_OF_OFFICE: Final = {
    "de": "Ich bin bis einschließlich {back} nicht im Büro und habe keinen Zugriff auf "
    "meine E-Mails. Ihre Nachricht wird nicht weitergeleitet.",
    "it": "Sono fuori ufficio fino al {back} compreso, senza accesso alla posta.",
    "fr": "Je suis absent jusqu'au {back} inclus, sans accès à mes e-mails.",
    "en": "I am out of the office until {back} with no access to email.",
}


def out_of_office(persona: Persona, *, subject: str, back: date) -> tuple[str, str]:
    """(subject, body) of an auto-reply stating the return date."""
    prefix = {"de": "Abwesenheitsnotiz", "it": "Fuori sede", "fr": "Absence"}.get(
        persona.language, "Out of office"
    )
    return (
        f"{prefix}: {subject}",
        _OUT_OF_OFFICE[persona.language].format(back=written_date(back, persona.language)),
    )
