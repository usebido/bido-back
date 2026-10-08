"""Outbound MIME building, the SMTP/SES senders, and parsing it back."""

from collections.abc import Mapping
from email import message_from_bytes, policy

import pytest

from bido_back.email import smtp as smtp_module
from bido_back.email.inbound import parse_inbound
from bido_back.email.ses import SesEmailSender
from bido_back.email.smtp import SmtpEmailSender
from bido_back.email.types import OutboundAttachment, OutboundEmail

DOMAIN = "quotes.example.test"
REPLY = f"rfq-abcdefghijk2@{DOMAIN}"


def _email(**overrides: object) -> OutboundEmail:
    fields: dict[str, object] = {
        "from_name": "Acme Procurement via Bido",
        "from_address": REPLY,
        "to": ("sales@supplier.test",),
        "cc": ("buyer@acme.test",),
        "reply_to": REPLY,
        "subject": "RFQ R-1001: M8 bolts",
        "text_body": "Please quote 1000 / 2000 pcs.\nThank you.",
        "message_id": f"<rfq-1@{DOMAIN}>",
        "in_reply_to": f"<prev@{DOMAIN}>",
        "references": (f"<root@{DOMAIN}>",),
        "attachments": (
            OutboundAttachment("drawing.pdf", "application/pdf", b"%PDF-1.7 fake"),
            OutboundAttachment("spec.xlsx", "bogus", b"PK\x03\x04 sheet"),
        ),
    }
    fields.update(overrides)
    return OutboundEmail(**fields)  # pyright: ignore[reportArgumentType]


class FakeSmtp:
    sent: list[tuple[str, list[str], bytes]] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host, self.port = host, port

    def __enter__(self) -> "FakeSmtp":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def sendmail(self, from_addr: str, to_addrs: list[str], msg: bytes) -> None:
        FakeSmtp.sent.append((from_addr, to_addrs, msg))


async def test_smtp_sender_builds_threaded_mime_and_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeSmtp.sent = []
    monkeypatch.setattr(smtp_module.smtplib, "SMTP", FakeSmtp)

    sent = await SmtpEmailSender("localhost", 1025).send(_email())

    assert sent.message_id == f"<rfq-1@{DOMAIN}>"
    [(envelope_from, envelope_to, raw)] = FakeSmtp.sent
    assert raw == sent.raw
    assert envelope_from == REPLY
    assert envelope_to == ["sales@supplier.test", "buyer@acme.test"]

    headers = message_from_bytes(raw, policy=policy.default)
    assert headers["Reply-To"] == REPLY
    assert headers["Cc"] == "buyer@acme.test"
    assert headers["References"] == f"<root@{DOMAIN}> <prev@{DOMAIN}>"

    parsed = parse_inbound(raw)
    assert parsed.message_id == f"<rfq-1@{DOMAIN}>"
    assert parsed.in_reply_to == f"<prev@{DOMAIN}>"
    assert parsed.references == (f"<root@{DOMAIN}>", f"<prev@{DOMAIN}>")
    assert parsed.from_address == REPLY
    assert parsed.from_name == "Acme Procurement via Bido"
    assert parsed.to == ("sales@supplier.test",)
    assert parsed.cc == ("buyer@acme.test",)
    assert parsed.subject == "RFQ R-1001: M8 bolts"
    assert parsed.text_body.strip() == "Please quote 1000 / 2000 pcs.\nThank you."
    assert parsed.date is not None and parsed.date.tzinfo is not None
    assert [(a.filename, a.content_type, a.data) for a in parsed.attachments] == [
        ("drawing.pdf", "application/pdf", b"%PDF-1.7 fake"),
        ("spec.xlsx", "application/octet-stream", b"PK\x03\x04 sheet"),
    ]
    assert not parsed.is_auto_reply and not parsed.is_bounce


async def test_message_id_is_generated_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSmtp.sent = []
    monkeypatch.setattr(smtp_module.smtplib, "SMTP", FakeSmtp)
    sent = await SmtpEmailSender("localhost", 1025).send(_email(message_id=None))
    assert sent.message_id.startswith("<") and sent.message_id.endswith(f"@{DOMAIN}>")
    assert parse_inbound(sent.raw).message_id == sent.message_id


async def test_header_injection_is_refused() -> None:
    with pytest.raises(ValueError):
        await SmtpEmailSender("localhost", 1).send(_email(subject="hi\r\nBcc: x@evil.test"))


class FakeSes:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def send_email(self, **kwargs: object) -> Mapping[str, object]:
        self.calls.append(kwargs)
        return {"MessageId": "0107019abc-def"}


async def test_ses_sender_sends_raw_and_returns_the_ses_message_id() -> None:
    ses = FakeSes()
    sent = await SesEmailSender(region="eu-central-1", ses=ses).send(_email())

    [call] = ses.calls
    assert call["FromEmailAddress"] == REPLY
    assert call["Destination"] == {
        "ToAddresses": ["sales@supplier.test"],
        "CcAddresses": ["buyer@acme.test"],
    }
    assert call["Content"] == {"Raw": {"Data": sent.raw}}
    assert sent.message_id == "<0107019abc-def@eu-central-1.amazonses.com>"


def test_an_html_body_goes_as_multipart_alternative_next_to_the_text() -> None:
    from bido_back.email.mime import build_mime
    from bido_back.email.types import OutboundEmail

    msg = build_mime(
        OutboundEmail(
            from_name="Ana Lima",
            from_address="ana@buyer.example.test",
            to=("vendas@supplier.example.test",),
            subject="[RFQ-3] Solicitação de cotação",
            text_body="Itens solicitados:\n1 | Aço",
            html_body=(
                "<!doctype html><html><body><table><tr><td>Aço</td></tr></table></body></html>"
            ),
        )
    )

    assert msg.get_content_type() == "multipart/alternative"
    plain = msg.get_body(preferencelist=("plain",))
    rich = msg.get_body(preferencelist=("html",))
    assert plain is not None and "1 | Aço" in plain.get_content()
    assert rich is not None and "<table>" in rich.get_content()


def test_without_html_the_message_stays_plain_text() -> None:
    from bido_back.email.mime import build_mime
    from bido_back.email.types import OutboundEmail

    msg = build_mime(
        OutboundEmail(
            from_name="Ana",
            from_address="ana@buyer.example.test",
            to=("x@supplier.example.test",),
            subject="s",
            text_body="t",
        )
    )

    assert msg.get_content_type() == "text/plain"
