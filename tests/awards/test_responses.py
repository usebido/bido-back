"""Reading a supplier's reply to a purchase order: grounded, compared, never applied. Pure."""

from datetime import date

from bido_back.agents.catalog.contracts import (
    PoResponseLine,
    PoResponseOrderedLine,
    PoResponseOutput,
    Quoted,
)
from bido_back.awards.responses import Reading, own_words, read_response

LINES = [
    PoResponseOrderedLine(
        position=1,
        description="MLCC 100nF",
        mpn="GRM188",
        quantity="4000",
        unit="pcs",
        unit_price="0.1",
        currency="EUR",
        delivery_date="2026-11-20",
    ),
    PoResponseOrderedLine(
        position=2,
        description="Connector",
        quantity="500",
        unit="pcs",
        unit_price="2.5",
        currency="EUR",
        delivery_date="2026-11-20",
    ),
]
KNOWN = {date(2026, 10, 9)}


def read(output: PoResponseOutput, body: str, language: str = "de"):  # noqa: ANN201
    return read_response(
        output,
        lines=LINES,
        subject="AW: PO-1",
        body=body,
        attachments_text="",
        language=language,
        known_dates=KNOWN,
    )


def confirmation(words: str, **fields: object) -> PoResponseOutput:
    return PoResponseOutput.model_validate(
        {
            "kind": "confirmation",
            "confirmation": {"value": None, "quote": words},
            "summary": "Confirms PO-1.",
            **fields,
        }
    )


def test_a_plain_grounded_confirmation_acknowledges() -> None:
    body = "Wir bestätigen Ihre Bestellung PO-1. Auftragsbestätigung AB-4711."
    reading = read(
        confirmation(
            "Wir bestätigen Ihre Bestellung PO-1",
            supplier_order_ref={"value": "AB-4711", "quote": "AB-4711"},
        ),
        body,
    )
    assert reading.plain_confirmation
    assert reading.supplier_order_ref == "AB-4711"


def test_words_not_in_the_email_do_not_count() -> None:
    reading = read(
        confirmation(
            "We confirm the order",
            supplier_order_ref={"value": "XYZ-1", "quote": "XYZ-1"},
        ),
        "Danke für Ihre Bestellung.",
    )
    assert not reading.confirmed and not reading.plain_confirmation
    assert reading.supplier_order_ref is None


def test_a_later_date_is_a_change_for_the_buyer_not_applied() -> None:
    body = "Wir bestätigen Ihre Bestellung PO-1. Liefertermin Position 2: 04.12.2026."
    output = PoResponseOutput(
        kind="confirmation_with_changes",
        confirmation=Quoted(value=None, quote="Wir bestätigen Ihre Bestellung PO-1"),
        lines=[
            PoResponseLine(
                position=2, delivery_date=Quoted(value="04.12.2026", quote="Position 2: 04.12.2026")
            )
        ],
        summary="Line 2 moves to 2026-12-04.",
    )
    reading = read(output, body)
    assert not reading.plain_confirmation
    [change] = reading.changes
    assert (change.position, change.field, change.ordered, change.stated) == (
        2,
        "delivery_date",
        "2026-11-20",
        "2026-12-04",
    )
    assert not change.unverified
    assert reading.other_dates == []


def test_the_same_date_written_another_way_is_no_change() -> None:
    body = "Liefertermin Position 1: 20.11.2026 wie bestellt."
    output = confirmation(
        "wie bestellt",
        lines=[{"position": 1, "delivery_date": {"value": "20.11.2026", "quote": "20.11.2026"}}],
    )
    assert read(output, body).plain_confirmation


def test_a_price_in_decimal_comma_is_compared_as_a_number() -> None:
    body = "Bestätigt. Position 2: Einzelpreis 2,75 EUR."
    output = confirmation(
        "Bestätigt",
        lines=[{"position": 2, "unit_price": {"value": "2,75", "quote": "Einzelpreis 2,75 EUR"}}],
    )
    [change] = read(output, body).changes
    assert (change.field, change.ordered, change.stated) == ("unit_price", "2.5", "2.75")
    same = confirmation(
        "Bestätigt",
        lines=[{"position": 2, "unit_price": {"value": "2,50", "quote": "Einzelpreis 2,50"}}],
    )
    assert read(same, "Bestätigt. Position 2: Einzelpreis 2,50 EUR.").plain_confirmation
    # A number that is not in the words it claims to come from is not read.
    claimed = confirmation(
        "Bestätigt",
        lines=[{"position": 2, "unit_price": {"value": "2,50", "quote": "Bestätigt"}}],
    )
    [change] = read(claimed, body).changes
    assert change.unverified


def test_an_unread_value_is_flagged_unverified() -> None:
    body = "Confirmed. Line 1 ships in about four weeks."
    output = confirmation(
        "Confirmed",
        lines=[{"position": 1, "delivery_date": {"value": "4 weeks", "quote": "four weeks"}}],
    )
    [change] = read(output, body, "en").changes
    assert change.unverified and change.stated == "4 weeks"


def test_a_date_the_reader_missed_still_stops_the_acknowledgement() -> None:
    body = "We confirm PO-1. Please note: dispatch on 15.01.2027."
    reading = read(confirmation("We confirm PO-1"), body, "en")
    assert reading.other_dates == [date(2027, 1, 15)]
    assert not reading.plain_confirmation


def test_dates_in_the_quoted_order_are_not_the_suppliers_words() -> None:
    body = (
        "We confirm PO-1 as ordered.\n\n"
        "On Fri, 9 Oct 2026 Ana <ana@buyer.example.test> wrote:\n"
        "> Need by 2026-12-31"
    )
    reading = read(confirmation("We confirm PO-1 as ordered"), body, "en")
    assert reading.plain_confirmation
    assert own_words(body) == "We confirm PO-1 as ordered.\n"


def test_unknown_lines_are_ignored_and_refusals_are_not_confirmations() -> None:
    output = PoResponseOutput(
        kind="rejection",
        lines=[PoResponseLine(position=9, unit_price=Quoted(value="1", quote="1"))],
        summary="Cannot supply.",
    )
    reading = read(output, "Leider können wir nicht liefern. 1")
    assert reading.changes == [] and not reading.plain_confirmation


# PO-4 in dev (2026-10-12): an Italian-registered supplier answered in English.
PO4_LINE = PoResponseOrderedLine(
    position=1,
    description="Capacitor 100uF 50V",
    mpn="EEE-FK1H101P",
    quantity="2000",
    unit="pcs",
    unit_price="2.75",
    currency="BRL",
    delivery_date="2026-11-07",
)
# The order as we sent it, quoted under the reply: Italian numbers prove nothing about theirs.
PO4_QUOTED = (
    "\n\nIl giorno lun 12 ott 2026, Ana <ana@buyer.example.test> ha scritto:\n"
    "> Ordine di acquisto PO-4: 2.000 pz a 2,75 BRL, totale 5.500,00 BRL"
)


def read_po4(
    words: str, quantity: str, unit_price: str | None = None, ordered: str = "2000"
) -> Reading:
    line: dict[str, object] = {"position": 1, "quantity": {"value": quantity, "quote": words}}
    if unit_price is not None:
        line["unit_price"] = {"value": unit_price, "quote": words}
    output = confirmation(
        words,
        supplier_order_ref={"value": "ET-7781", "quote": "ET-7781"},
        lines=[line],
    )
    return read_response(
        output,
        lines=[PO4_LINE.model_copy(update={"quantity": ordered})],
        subject="Re: Ordine di acquisto PO-4",
        body=f"{words}\nOur order reference: ET-7781.{PO4_QUOTED}",
        attachments_text="",
        language="it",
        known_dates=KNOWN,
    )


def test_an_english_reply_to_an_italian_supplier_reads_english_numbers() -> None:
    words = (
        "we confirm purchase order PO-4: 2,000 pcs Panasonic EEE-FK1H101P at BRL 2.75 "
        "per piece, total BRL 5,500.00"
    )
    reading = read_po4(words, quantity="2,000", unit_price="2.75")
    assert reading.changes == []
    assert reading.plain_confirmation
    assert reading.supplier_order_ref == "ET-7781"


def test_an_italian_reply_reads_italian_numbers() -> None:
    reading = read_po4("confermiamo 2.000 pz a 2,75 BRL", quantity="2.000", unit_price="2,75")
    assert reading.changes == [] and reading.plain_confirmation


def test_a_real_quantity_change_in_italian_is_read_with_the_proven_comma() -> None:
    reading = read_po4("confermiamo 1.500 pz a 2,75 BRL", quantity="1.500", unit_price="2,75")
    [change] = reading.changes
    assert (change.field, change.ordered, change.stated) == ("quantity", "2000", "1500")
    assert not change.unverified and not change.ambiguous
    assert not reading.plain_confirmation


def test_an_ambiguous_number_matching_the_order_is_no_change() -> None:
    # Nothing in the supplier's words proves the separator: 2000 or 2.
    reading = read_po4("we confirm 2,000 pcs", quantity="2,000")
    assert reading.changes == [] and reading.plain_confirmation


def test_an_ambiguous_number_not_matching_the_order_waits_as_written() -> None:
    reading = read_po4("we confirm 2,000 pcs", quantity="2,000", ordered="3000")
    [change] = reading.changes
    assert (change.field, change.ordered, change.stated) == ("quantity", "3000", "2,000")
    assert change.unverified and change.ambiguous
    assert change.as_json()["ambiguous"] is True
    assert not reading.plain_confirmation
