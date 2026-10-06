"""The onboarding scan on Gmail and Graph: sent mail only, metadata only, bounded."""

# ruff: noqa: S105, S106 - fake OAuth tokens and keys, never real credentials

from datetime import UTC, datetime

import httpx

from bido_back.mailbox.base import SCAN_KEYWORDS, SentAttachment, SentSummary, b64url_encode
from bido_back.mailbox.google import GmailMailbox, sent_query
from bido_back.mailbox.microsoft import GraphMailbox, sent_search

from .support import BUYER, Recorder, authorized, json_response, raw_email

SINCE = datetime(2025, 10, 7, tzinfo=UTC)
NO_RECIPIENTS: list[dict[str, dict[str, str]]] = []


async def _collect(
    mailbox: GmailMailbox | GraphMailbox, limit: int = 1500
) -> list[list[SentSummary]]:
    return [
        page async for page in mailbox.list_sent(keywords=SCAN_KEYWORDS, since=SINCE, limit=limit)
    ]


def test_gmail_query_is_sent_mail_since_the_window_with_quoted_phrases() -> None:
    query = sent_query(["RFQ", "cotação", "request for quote"], SINCE)
    assert query == 'in:sent after:2025/10/07 (RFQ OR cotação OR "request for quote")'


def _gmail_message(message_id: str) -> dict[str, object]:
    return {
        "id": message_id,
        "internalDate": "1757673600000",  # 2025-09-12
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "Subject", "value": f"Cotação {message_id}"},
                {"name": "To", "value": "Vendas <vendas@metalflex.example>, a@b.example"},
                {"name": "Cc", "value": "c@d.example"},
                {"name": "From", "value": BUYER},
            ],
            "parts": [
                {"mimeType": "text/plain", "filename": ""},
                {
                    "mimeType": "application/vnd.ms-excel",
                    "filename": "itens.xlsx",
                },
            ],
        },
    }


async def test_gmail_list_sent_pages_ids_then_masked_metadata() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/messages") and request.method == "GET":
            if request.url.params.get("pageToken") == "p2":
                return json_response(200, {"messages": [{"id": "m3"}]})
            return json_response(
                200, {"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "p2"}
            )
        message_id = path.rsplit("/", 1)[-1]
        if message_id == "m2":
            return json_response(404, {})  # deleted meanwhile: skipped
        return json_response(200, _gmail_message(message_id))

    recorder = Recorder(route)
    http, _, _ = authorized("google", recorder)
    pages = await _collect(GmailMailbox(http, address=BUYER))

    assert [[s.provider_message_id for s in page] for page in pages] == [["m1"], ["m3"]]
    first = pages[0][0]
    assert first.subject == "Cotação m1"
    assert first.recipients == ("vendas@metalflex.example", "a@b.example", "c@d.example")
    assert first.sent_at == datetime(2025, 9, 12, 10, 40, tzinfo=UTC)
    assert first.attachments == (SentAttachment("itens.xlsx", "application/vnd.ms-excel"),)

    listing = recorder.requests[0]
    assert listing.url.params["q"].startswith("in:sent after:2025/10/07 (")
    detail = recorder.requests[1]
    assert detail.url.params["format"] == "full"
    # Bodies are masked out of every metadata call.
    assert "data" not in detail.url.params["fields"]
    assert "body" not in detail.url.params["fields"]


async def test_gmail_list_sent_stops_at_the_limit() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages"):
            count = int(request.url.params["maxResults"])
            return json_response(
                200,
                {"messages": [{"id": f"m{i}"} for i in range(count)], "nextPageToken": "more"},
            )
        return json_response(200, _gmail_message(request.url.path.rsplit("/", 1)[-1]))

    recorder = Recorder(route)
    http, _, _ = authorized("google", recorder)
    pages = await _collect(GmailMailbox(http, address=BUYER), limit=150)

    assert [len(page) for page in pages] == [100, 50]


async def test_gmail_get_message_returns_the_raw_bytes() -> None:
    raw = raw_email(from_address=BUYER, to="vendas@metalflex.example", subject="Cotação")

    def route(request: httpx.Request) -> httpx.Response:
        assert request.url.params["format"] == "raw"
        return json_response(200, {"id": "m1", "raw": b64url_encode(raw)})

    http, _, _ = authorized("google", Recorder(route))
    assert await GmailMailbox(http, address=BUYER).get_message("m1") == raw


def test_graph_search_is_kql_with_escaped_phrases_and_the_date() -> None:
    assert (
        sent_search(["RFQ", "request for quote"], SINCE)
        == '"(RFQ OR \\"request for quote\\") AND sent>=2025-10-07"'
    )


async def test_graph_list_sent_reads_sent_items_only_and_attachment_names() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/mailFolders/sentitems/messages"):
            if "skiptoken" in str(request.url):
                return json_response(200, {"value": []})
            return json_response(
                200,
                {
                    "value": [
                        {
                            "id": "g1",
                            "subject": "RFQ 2024-118",
                            "toRecipients": [
                                {"emailAddress": {"address": "cotacao@rolemax.example"}}
                            ],
                            "ccRecipients": NO_RECIPIENTS,
                            "sentDateTime": "2026-08-15T12:00:00Z",
                            "hasAttachments": True,
                        },
                        {
                            "id": "old",
                            "subject": "RFQ antiga",
                            "toRecipients": NO_RECIPIENTS,
                            "sentDateTime": "2024-01-01T00:00:00Z",
                            "hasAttachments": False,
                        },
                    ],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/mailFolders/sentitems/messages?$skiptoken=x",
                },
            )
        if path.endswith("/attachments"):
            assert request.url.params["$select"] == "name,contentType"
            return json_response(
                200, {"value": [{"name": "itens.pdf", "contentType": "application/pdf"}]}
            )
        raise AssertionError(f"unexpected call {path}")

    recorder = Recorder(route)
    http, _, _ = authorized("microsoft", recorder)
    pages = await _collect(GraphMailbox(http, address=BUYER))

    [summary] = pages[0]
    assert summary.provider_message_id == "g1"
    assert summary.recipients == ("cotacao@rolemax.example",)
    assert summary.attachments == (SentAttachment("itens.pdf", "application/pdf"),)
    first = recorder.requests[0]
    assert "body" not in first.url.params["$select"].lower()
    assert first.url.params["$search"].startswith('"(RFQ OR ')


async def test_graph_never_follows_a_next_link_off_graph() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        return json_response(
            200, {"value": [], "@odata.nextLink": "https://evil.example/steal?token=1"}
        )

    recorder = Recorder(route)
    http, _, _ = authorized("microsoft", recorder)
    await _collect(GraphMailbox(http, address=BUYER))
    assert len(recorder.requests) == 1
