"""Answer the latest RFQ email in Mailpit as the supplier, with a synthetic quote.

    uv run python scripts/dev_reply.py            # reply to the newest RFQ email
    uv run python scripts/dev_reply.py --list     # show the RFQ emails in Mailpit

LOCAL ONLY. With EMAIL_TRANSPORT=smtp the api sends RFQ emails to Mailpit
(http://localhost:8025). This script reads the newest one through Mailpit's
API, builds a reply in the same thread (In-Reply-To / References) from the
supplier to the buyer, attaches a small .xlsx quote, and posts the raw email
to the api's `/dev/inbound`, which stands in for the mailbox sync. The worker
then runs quote extraction (needs OPENAI_API_KEY in .env).

The quote is synthetic: one price per requested line, made up from the RFQ's
own `[RFQ-n]` marker. Nothing here reaches a real mailbox.
"""

import argparse
import io
import re
import sys
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from typing import Any, cast

import httpx

MAILPIT = "http://localhost:8025"
API = "http://localhost:8080"
_RFQ_MARKER = re.compile(r"\[RFQ-\d+\]")
_LINE = re.compile(r"^\s*(?:Position|Pos\.?|Item|Line)\s*(\d+)\s*[:.-]\s*(.+)$", re.I | re.M)


def _rfq_messages(client: httpx.Client) -> list[dict[str, Any]]:
    data = cast(
        dict[str, Any], client.get(f"{MAILPIT}/api/v1/messages", params={"limit": 50}).json()
    )
    messages = cast(list[dict[str, Any]], data.get("messages", []))
    return [
        m
        for m in messages
        if _RFQ_MARKER.search(str(m.get("Subject", "")))
        and not str(m.get("Subject", "")).lower().startswith(("re:", "aw:"))
    ]


def _xlsx(lines: list[tuple[str, str]]) -> bytes:
    from openpyxl import Workbook  # pyright: ignore[reportMissingTypeStubs]

    workbook = cast(Any, Workbook())
    sheet = workbook.active
    sheet.title = "Quote"
    sheet.append(["Pos", "Description", "Qty", "Unit", "Unit price (EUR)", "Lead time (weeks)"])
    for index, (position, description) in enumerate(lines):
        sheet.append(
            [int(position), description[:60], 10000, "pcs", round(0.05 + 0.01 * index, 4), 6]
        )
    sheet.append([])
    sheet.append(["Tooling: none. MOQ 4000 pcs. Origin: DE."])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _reply(original: dict[str, Any], body_text: str) -> bytes:
    subject = str(original["Subject"])
    buyer = cast(dict[str, str], original["From"])
    supplier = cast(list[dict[str, str]], original["To"])[0]
    message_id = str(original["MessageID"])
    lines = [(m.group(1), m.group(2).strip()) for m in _LINE.finditer(body_text)] or [("1", "Item")]
    valid_until = (datetime.now(UTC) + timedelta(days=45)).strftime("%d.%m.%Y")

    reply = EmailMessage()
    reply["From"] = f"{supplier.get('Name') or 'Supplier Sales'} <{supplier['Address']}>"
    reply["To"] = f"{buyer.get('Name') or 'Buyer'} <{buyer['Address']}>"
    reply["Subject"] = f"Re: {subject}"
    reply["Date"] = format_datetime(datetime.now(UTC))
    reply["Message-ID"] = make_msgid(domain="example.test")
    reply["In-Reply-To"] = f"<{message_id}>"
    reply["References"] = f"<{message_id}>"
    reply.set_content(
        "Hello,\n\n"
        "thank you for your request. Please find our quote attached.\n"
        f"Prices in EUR, valid until {valid_until}, Incoterm DAP, payment 30 days net.\n\n"
        "Best regards\nSupplier Sales\n"
    )
    reply.add_attachment(
        _xlsx(lines),
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="quote.xlsx",
    )
    return reply.as_bytes()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reply to the newest RFQ email in Mailpit as the supplier."
    )
    parser.add_argument("--list", action="store_true", help="list RFQ emails in Mailpit")
    args = parser.parse_args()

    with httpx.Client(timeout=30) as client:
        try:
            messages = _rfq_messages(client)
        except httpx.ConnectError:
            print(f"Mailpit is not reachable at {MAILPIT}: run 'dev: services up'", file=sys.stderr)
            return 1
        if args.list:
            for m in messages:
                print(f"{m['Created'][:19]}  {m['Subject']}  -> {m['To'][0]['Address']}")
            return 0
        if not messages:
            print("No RFQ email in Mailpit yet: create an RFQ, invite, send and approve first.")
            return 1

        newest = messages[0]
        full = cast(dict[str, Any], client.get(f"{MAILPIT}/api/v1/message/{newest['ID']}").json())
        raw = _reply(full, str(full.get("Text", "")))
        try:
            response = client.post(
                f"{API}/dev/inbound", content=raw, headers={"content-type": "message/rfc822"}
            )
        except httpx.ConnectError:
            print(f"The api is not reachable at {API}: run 'dev: api'", file=sys.stderr)
            return 1
        print(f"Replied to: {newest['Subject']}")
        print(f"/dev/inbound -> {response.status_code} {response.text}")
        return 0 if response.is_success else 1


if __name__ == "__main__":
    sys.exit(main())
