"""Parsing a raw inbound email (RFC 5322 bytes) into our own `InboundEmail`.

Parsing never raises on a malformed message: headers that the stdlib cannot
interpret fall back to their raw text, and a missing part simply yields an
empty value. The caller decides what an incomplete email means.

Auto-replies (out of office, autoresponders) and bounces (delivery status
reports) are flagged, not dropped: they are kept for the audit trail but must
never be treated as a supplier's answer.
"""

import mimetypes
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import cast

_MSG_ID_RE = re.compile(r"<[^<>\s]+>")

# Subjects of common out-of-office / autoresponder mails, anchored at the start
# (a real reply starts with "Re:"). English, German, French, Spanish,
# Portuguese, Italian, Dutch.
_AUTO_SUBJECT_RE = re.compile(
    r"^\s*(?:"
    r"out of (?:the )?office|automatic reply|auto(?:matic)?[- ]?(?:reply|response)"
    r"|autoreply|abwesenheit|automatische antwort|r[ée]ponse automatique"
    r"|absence|respuesta autom[áa]tica|resposta autom[áa]tica|risposta automatica"
    r"|fuori ufficio|afwezig|automatisch antwoord"
    r")",
    re.IGNORECASE,
)
_BOUNCE_SENDERS = frozenset({"mailer-daemon", "postmaster"})
_AUTO_PRECEDENCE = frozenset({"auto_reply", "bulk", "junk"})


@dataclass(frozen=True, slots=True)
class InboundAttachment:
    filename: str
    content_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class InboundEmail:
    message_id: str | None
    in_reply_to: str | None
    references: tuple[str, ...]
    from_address: str | None
    from_name: str | None
    to: tuple[str, ...]
    cc: tuple[str, ...]
    delivered_to: tuple[str, ...]
    subject: str | None
    date: datetime | None
    text_body: str
    attachments: tuple[InboundAttachment, ...]
    is_auto_reply: bool
    is_bounce: bool

    @property
    def is_automated(self) -> bool:
        """An auto-reply or a bounce: never a supplier's answer."""
        return self.is_auto_reply or self.is_bounce

    @property
    def recipients(self) -> tuple[str, ...]:
        """Every address the mail was delivered or addressed to."""
        return (*self.delivered_to, *self.to, *self.cc)


def parse_inbound(raw: bytes) -> InboundEmail:
    msg = BytesParser(policy=policy.default).parsebytes(raw)

    from_name, from_address = _first_address(msg, "From")
    in_reply_to_ids = _message_ids(_header(msg, "In-Reply-To"))
    message_ids = _message_ids(_header(msg, "Message-ID"))
    body_part, text_body = _body(msg)

    return InboundEmail(
        message_id=message_ids[0] if message_ids else None,
        in_reply_to=in_reply_to_ids[0] if in_reply_to_ids else None,
        references=tuple(_message_ids(_header(msg, "References"))),
        from_address=from_address,
        from_name=from_name,
        to=_addresses(msg, "To"),
        cc=_addresses(msg, "Cc"),
        delivered_to=_addresses(msg, "Delivered-To") + _addresses(msg, "X-Original-To"),
        subject=_header(msg, "Subject"),
        date=_date(msg),
        text_body=text_body,
        attachments=tuple(_attachments(msg, body_part)),
        is_auto_reply=_is_auto_reply(msg),
        is_bounce=_is_bounce(msg, from_address),
    )


# --- headers -----------------------------------------------------------------


def _header(msg: Message, name: str) -> str | None:
    """The decoded header value, or its raw text if the stdlib cannot parse it."""
    try:
        value = msg.get(name)
        return None if value is None else str(value).strip()
    except Exception:  # noqa: BLE001 - malformed header: fall back to raw text
        for key, raw in msg.raw_items():
            if key.lower() == name.lower():
                return str(raw).strip()
        return None


def _all_header_values(msg: Message, name: str) -> list[str]:
    try:
        return [str(v) for v in msg.get_all(name) or []]
    except Exception:  # noqa: BLE001 - malformed header: fall back to raw text
        return [str(raw) for key, raw in msg.raw_items() if key.lower() == name.lower()]


def _parsed_addresses(msg: Message, name: str) -> list[tuple[str, str]]:
    pairs = getaddresses(_all_header_values(msg, name))
    return [(display, addr.strip()) for display, addr in pairs if "@" in addr]


def _addresses(msg: Message, name: str) -> tuple[str, ...]:
    return tuple(addr.lower() for _, addr in _parsed_addresses(msg, name))


def _first_address(msg: Message, name: str) -> tuple[str | None, str | None]:
    pairs = _parsed_addresses(msg, name)
    if not pairs:
        return None, None
    display, addr = pairs[0]
    return (display.strip() or None), addr.lower()


def _message_ids(value: str | None) -> list[str]:
    return _MSG_ID_RE.findall(value) if value else []


def _date(msg: Message) -> datetime | None:
    value = _header(msg, "Date")
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# --- automated mail ----------------------------------------------------------


def _is_auto_reply(msg: Message) -> bool:
    auto_submitted = (_header(msg, "Auto-Submitted") or "").split(";", 1)[0].strip().lower()
    if auto_submitted and auto_submitted != "no":
        return True
    if _header(msg, "X-Autoreply") is not None or _header(msg, "X-Autorespond") is not None:
        return True
    if (_header(msg, "Precedence") or "").strip().lower() in _AUTO_PRECEDENCE:
        return True
    return bool(_AUTO_SUBJECT_RE.match(_header(msg, "Subject") or ""))


def _is_bounce(msg: Message, from_address: str | None) -> bool:
    if (
        msg.get_content_type() == "multipart/report"
        and str(msg.get_param("report-type", "")).lower() == "delivery-status"
    ):
        return True
    if any(part.get_content_type() == "message/delivery-status" for part in msg.walk()):
        return True
    if from_address:
        local = from_address.partition("@")[0]
        return local in _BOUNCE_SENDERS
    return False


# --- body and attachments ----------------------------------------------------


def _body(msg: EmailMessage) -> tuple[Message | None, str]:
    """The body part used and its text: text/plain first, else HTML as text."""
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        return plain, _part_text(plain)
    html = msg.get_body(preferencelist=("html",))
    if html is not None:
        return html, html_to_text(_part_text(html))
    return None, ""


def _part_text(part: Message) -> str:
    try:
        content: object = cast(EmailMessage, part).get_content()
        if isinstance(content, str):
            return content
    except (LookupError, ValueError, AttributeError):
        pass
    payload = part.get_payload(decode=True)
    return payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""


def _attachments(msg: Message, body_part: Message | None) -> list[InboundAttachment]:
    found: list[InboundAttachment] = []
    _collect(msg, body_part, found, top=True)
    return found


def _collect(
    part: Message, body: Message | None, out: list[InboundAttachment], *, top: bool
) -> None:
    ctype = part.get_content_type()
    filename = part.get_filename()
    is_attachment = part.get_content_disposition() == "attachment" or filename is not None

    if ctype == "message/rfc822" and not top:
        if is_attachment:
            inner = part.get_payload(0) if part.is_multipart() else None
            data = inner.as_bytes() if isinstance(inner, Message) else b""
            out.append(_attachment(filename, ctype, data, len(out)))
        return
    if ctype.startswith("message/") and not top:
        return  # delivery-status reports and similar: not files
    if part.is_multipart():
        for sub in cast(list[Message], part.get_payload()):
            _collect(sub, body, out, top=False)
        return
    if part is body or not is_attachment:
        return
    payload = part.get_payload(decode=True)
    out.append(
        _attachment(filename, ctype, payload if isinstance(payload, bytes) else b"", len(out))
    )


def _attachment(filename: str | None, ctype: str, data: bytes, index: int) -> InboundAttachment:
    name = (filename or "").strip()
    if not name:
        ext = ".eml" if ctype == "message/rfc822" else (mimetypes.guess_extension(ctype) or "")
        name = f"attachment-{index + 1}{ext}"
    return InboundAttachment(filename=name, content_type=ctype, data=data)


# --- HTML to text ------------------------------------------------------------

# Paragraph-level elements start and end with a blank line; line-level ones
# start a new line; table cells are separated by tabs.
_PARAGRAPH_TAGS = frozenset(
    {
        "address", "article", "blockquote", "div", "dl", "footer", "h1", "h2", "h3",
        "h4", "h5", "h6", "header", "hr", "ol", "p", "pre", "section", "table", "ul",
    }
)  # fmt: skip
_LINE_TAGS = frozenset({"br", "tr", "li", "dt", "dd"})
_CELL_TAGS = frozenset({"td", "th"})
_SKIP_TAGS = frozenset({"script", "style", "head", "title"})
_WHITESPACE_RE = re.compile(r"\s+")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _PARAGRAPH_TAGS:
            self._chunks.append("\n\n")
        elif tag in _LINE_TAGS:
            self._chunks.append("\n")
        elif tag in _CELL_TAGS:
            self._chunks.append("\t")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _PARAGRAPH_TAGS:
            self._chunks.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            # Source newlines are just whitespace in HTML.
            self._chunks.append(_WHITESPACE_RE.sub(" ", data))

    def text(self) -> str:
        lines: list[str] = []
        for line in "".join(self._chunks).split("\n"):
            cells = [" ".join(cell.split()) for cell in line.split("\t")]
            cleaned = "\t".join(c for c in cells if c)
            # At most one blank line in a row.
            if cleaned or (lines and lines[-1]):
                lines.append(cleaned)
        return "\n".join(lines).strip()


def html_to_text(html: str) -> str:
    """Readable text from an HTML body (tables keep cells tab-separated)."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()
