"""Building the MIME message for an `OutboundEmail` (shared by SMTP and SES)."""

from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime, formataddr

from bido_back.email.addresses import address_domain, make_message_id
from bido_back.email.types import OutboundEmail


def build_mime(email: OutboundEmail, *, now: datetime | None = None) -> EmailMessage:
    """The full message, with our Message-ID and threading headers.

    Header values go through the stdlib's default policy, which refuses CR/LF
    in a header (no header injection from subjects or names).
    """
    if not email.to:
        raise ValueError("an email needs at least one recipient")
    message_id = email.message_id or make_message_id(
        address_domain(email.from_address) or "localhost"
    )

    msg = EmailMessage()
    msg["From"] = formataddr((email.from_name, email.from_address))
    msg["To"] = ", ".join(email.to)
    if email.cc:
        msg["Cc"] = ", ".join(email.cc)
    if email.reply_to:
        msg["Reply-To"] = email.reply_to
    msg["Subject"] = email.subject
    msg["Date"] = format_datetime(now or datetime.now(UTC))
    msg["Message-ID"] = message_id
    if email.in_reply_to:
        msg["In-Reply-To"] = email.in_reply_to
    references = list(email.references)
    if email.in_reply_to and email.in_reply_to not in references:
        references.append(email.in_reply_to)
    if references:
        msg["References"] = " ".join(references)
    msg.set_content(email.text_body)
    for attachment in email.attachments:
        maintype, _, subtype = attachment.content_type.partition("/")
        if not maintype or not subtype:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(
            attachment.data, maintype=maintype, subtype=subtype, filename=attachment.filename
        )
    return msg


def recipients(email: OutboundEmail) -> list[str]:
    """Envelope recipients: To and Cc (deduplicated, order kept)."""
    seen: list[str] = []
    for address in (*email.to, *email.cc):
        if address not in seen:
            seen.append(address)
    return seen
