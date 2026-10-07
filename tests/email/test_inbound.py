"""Inbound parsing: body choice, HTML fallback, auto-reply and bounce detection."""

from email.message import EmailMessage

import pytest

from bido_back.email.inbound import html_to_text, parse_inbound


def _reply(**headers: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "Jane Supplier <jane@supplier.test>"
    msg["To"] = "rfq-abcdefghijk2@quotes.example.test"
    msg["Subject"] = "Re: RFQ R-1001"
    msg["Message-ID"] = "<reply-1@supplier.test>"
    msg["Date"] = "Tue, 06 Oct 2026 10:00:00 +0200"
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    msg.set_content("Price 1.20 EUR/pc.")
    return msg


def test_plain_reply_is_not_automated() -> None:
    parsed = parse_inbound(_reply().as_bytes())
    assert not parsed.is_auto_reply
    assert not parsed.is_bounce
    assert parsed.text_body.strip() == "Price 1.20 EUR/pc."
    assert parsed.date is not None and parsed.date.utcoffset() is not None


def test_prefers_text_plain_over_html() -> None:
    msg = _reply()
    msg.add_alternative("<p>HTML <b>version</b></p>", subtype="html")
    assert parse_inbound(msg.as_bytes()).text_body.strip() == "Price 1.20 EUR/pc."


def test_html_only_body_is_converted_to_text() -> None:
    msg = _reply()
    msg.set_content(
        "<html><head><style>p{color:red}</style><script>x()</script></head><body>"
        "<p>Dear buyer,</p><table><tr><th>Qty</th><th>Price</th></tr>"
        "<tr><td>1000</td><td>1.20&nbsp;EUR</td></tr></table>Regards<br>Jane</body></html>",
        subtype="html",
    )
    text = parse_inbound(msg.as_bytes()).text_body
    assert "color" not in text and "x()" not in text
    assert text.splitlines() == [
        "Dear buyer,",
        "",
        "Qty\tPrice",
        "1000\t1.20 EUR",
        "",
        "Regards",
        "Jane",
    ]


def test_html_to_text_handles_entities_and_whitespace() -> None:
    assert html_to_text("<div>  a&amp;b \n c </div><div>d</div>") == "a&b c\n\nd"


@pytest.mark.parametrize(
    "headers",
    [
        {"Auto_Submitted": "auto-replied"},
        {"Auto_Submitted": "auto-generated; owner-email=x@y"},
        {"X_Autoreply": "yes"},
        {"X_Autorespond": "1"},
        {"Precedence": "auto_reply"},
        {"Precedence": "bulk"},
        {"Subject": "Out of Office: back Monday"},
        {"Subject": "Automatic reply: RFQ R-1001"},
        {"Subject": "Abwesenheitsnotiz: RFQ R-1001"},
        {"Subject": "Réponse automatique : RFQ"},
    ],
)
def test_auto_replies_are_flagged(headers: dict[str, str]) -> None:
    msg = _reply()
    for name, value in headers.items():
        del msg[name.replace("_", "-")]
        msg[name.replace("_", "-")] = value
    parsed = parse_inbound(msg.as_bytes())
    assert parsed.is_auto_reply
    assert parsed.is_automated


def test_auto_submitted_no_is_a_person() -> None:
    assert not parse_inbound(_reply(Auto_Submitted="no").as_bytes()).is_auto_reply


BOUNCE = b"""From: Mail Delivery System <MAILER-DAEMON@mx.supplier.test>\r
To: rfq-abcdefghijk2@quotes.example.test\r
Subject: Undelivered Mail Returned to Sender\r
Message-ID: <bounce-1@mx.supplier.test>\r
MIME-Version: 1.0\r
Content-Type: multipart/report; report-type=delivery-status; boundary="B"\r
\r
--B\r
Content-Type: text/plain\r
\r
Your message could not be delivered.\r
--B\r
Content-Type: message/delivery-status\r
\r
Reporting-MTA: dns; mx.supplier.test\r
\r
Final-Recipient: rfc822; x@supplier.test\r
Action: failed\r
Status: 5.1.1\r
--B--\r
"""


def test_delivery_status_report_is_a_bounce() -> None:
    parsed = parse_inbound(BOUNCE)
    assert parsed.is_bounce
    assert parsed.attachments == ()
    assert parsed.text_body.strip() == "Your message could not be delivered."
    # Detected by the report, not only by the MAILER-DAEMON sender.
    assert parse_inbound(BOUNCE.replace(b"MAILER-DAEMON@", b"notify@")).is_bounce


def test_mailer_daemon_sender_is_a_bounce() -> None:
    msg = _reply()
    msg.replace_header("From", "postmaster@supplier.test")
    assert parse_inbound(msg.as_bytes()).is_bounce


def test_attachments_with_odd_names_and_nested_parts() -> None:
    msg = _reply()
    msg.add_alternative("<p>html</p>", subtype="html")
    msg.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="../../quote.pdf")
    msg.add_attachment(b"\x89PNG", maintype="image", subtype="png")  # no filename
    parsed = parse_inbound(msg.as_bytes())
    assert [(a.filename, a.content_type) for a in parsed.attachments] == [
        ("../../quote.pdf", "application/pdf"),
        ("attachment-2.png", "image/png"),
    ]


def test_garbage_does_not_raise() -> None:
    parsed = parse_inbound(b"\x00\xff not an email at all")
    assert parsed.message_id is None
    assert parsed.from_address is None
    assert not parsed.is_automated


def test_recipients_include_delivered_to() -> None:
    msg = _reply(Delivered_To="rfq-zzzzzzzzzzzz@quotes.example.test", Cc="Buyer <b@acme.test>")
    parsed = parse_inbound(msg.as_bytes())
    assert parsed.recipients == (
        "rfq-zzzzzzzzzzzz@quotes.example.test",
        "rfq-abcdefghijk2@quotes.example.test",
        "b@acme.test",
    )
