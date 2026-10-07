"""Rendering a simulated reply: numbers by code, readable by our own extraction."""

from datetime import date
from decimal import Decimal

from bido_back.documents import extract_text
from bido_back.email.inbound import InboundEmail, parse_inbound
from bido_back.simulator.offers import LineSpec, Offer, decide_offer
from bido_back.simulator.personas import Behavior, Language, Persona, QuoteFormat
from bido_back.simulator.render import body_table, build_reply, number, reply_subject

LINES = [
    LineSpec(
        position=1, description="Magnete NdFeB N42SH", mpn="MAG-42", quantities=(Decimal(2000),)
    ),
    LineSpec(position=2, description="Resolver", mpn="RS-1", quantities=(Decimal(500),)),
]


def _offer(language: Language, *behaviors: Behavior) -> Offer:
    return decide_offer(
        Persona(language=language, behaviors=behaviors),
        LINES,
        round_number=1,
        currency="EUR",
        today=date(2026, 10, 7),
    )


def _reply(
    quote_format: QuoteFormat, language: Language = "it", *behaviors: Behavior
) -> InboundEmail:
    raw = build_reply(
        offer=_offer(language, *behaviors),
        language=language,
        quote_format=quote_format,
        opening="Buongiorno,\ngrazie per la richiesta.",
        closing="Cordiali saluti\nMarco",
        supplier_company="Acciaierie Lombarde S.p.A.",
        from_name="Marco Rossi",
        from_address="marco@acciaierie.example.test",
        to_name="Chiara Rinaldi",
        to_address="chiara@motori.example.test",
        subject="[RFQ-3] Motore",
        message_id="<sim-1@acciaierie.example.test>",
        in_reply_to="<rfq-3@motori.example.test>",
        references=[],
    )
    return parse_inbound(raw)


def test_numbers_follow_the_language() -> None:
    assert number(Decimal("2.3500"), "it") == "2,3500"
    assert number(Decimal("2.3500"), "en") == "2.3500"


def test_the_reply_is_threaded() -> None:
    reply = _reply("xlsx")

    assert reply.subject == "R: [RFQ-3] Motore"
    assert reply.in_reply_to == "<rfq-3@motori.example.test>"
    assert reply.references == ("<rfq-3@motori.example.test>",)
    assert reply.message_id == "<sim-1@acciaierie.example.test>"
    assert reply.from_address == "marco@acciaierie.example.test"
    assert reply_subject("R: [RFQ-3] Motore", "it") == "R: [RFQ-3] Motore"


def _attachment(reply: InboundEmail) -> tuple[str, bytes]:
    [attachment] = reply.attachments
    return attachment.filename, attachment.data


def test_xlsx_and_pdf_quotes_are_readable_by_extraction() -> None:
    offer = _offer("it")
    price = number(offer.lines[0].tiers[0].unit_price, "it")

    name, data = _attachment(_reply("xlsx"))
    xlsx = extract_text(
        name, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", data
    )
    assert xlsx.readable and "MAG-42" in xlsx.text

    name, data = _attachment(_reply("pdf"))
    pdf = extract_text(name, "application/pdf", data)
    assert pdf.readable and "MAG-42" in pdf.text and price in pdf.text


def test_body_quotes_and_noise_are_in_the_text() -> None:
    text = _reply("body", "it", "noisy").text_body

    assert "MAG-42" in text and "2000" in text
    assert "Reparto utensili" in text
    assert "Valuta: EUR" in body_table(_offer("it"), "it")
