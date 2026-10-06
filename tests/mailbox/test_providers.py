"""Gmail and Graph: sending as the buyer, threading, reading one thread, retries."""

# ruff: noqa: S105, S106 - fake OAuth tokens and keys, never real credentials

import base64
import json
from email import message_from_bytes

import httpx
import pytest

from bido_back.email.types import OutboundEmail
from bido_back.mailbox.base import (
    ConsentRevoked,
    MailboxSentEmail,
    ProviderError,
    ThreadNotFound,
    TokenSet,
    b64url_decode,
    b64url_encode,
)
from bido_back.mailbox.google import GmailMailbox
from bido_back.mailbox.microsoft import GraphMailbox, conversation_filter

from .support import BUYER, SUPPLIER, Recorder, Refresher, authorized, json_response, raw_email

FIRST = OutboundEmail(
    from_name="Acme via Bido",
    from_address="rfq-abcdefghijkl@quotes.usebido.com",
    to=(SUPPLIER,),
    subject="RFQ 42: M8 bolts",
    text_body="Please quote.",
    message_id="<rfq-42@quotes.usebido.com>",
)
FOLLOW_UP = OutboundEmail(
    from_name="Acme via Bido",
    from_address="rfq-abcdefghijkl@quotes.usebido.com",
    to=(SUPPLIER,),
    subject="Re: RFQ 42: M8 bolts",
    text_body="Any update?",
    message_id="<rfq-42-f1@quotes.usebido.com>",
    in_reply_to="<rfq-42@quotes.usebido.com>",
)


class FakeThreads:
    def __init__(self, thread: str | None = None) -> None:
        self.thread = thread
        self.bound: list[tuple[str | None, MailboxSentEmail]] = []

    async def thread_for(self, email: OutboundEmail) -> str | None:
        return self.thread if email.in_reply_to else None

    async def bind(self, email: OutboundEmail, sent: MailboxSentEmail) -> None:
        self.bound.append((email.message_id, sent))


def _gmail_route(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.method == "POST" and path.endswith("/messages/send"):
        body = json.loads(request.content)
        return json_response(200, {"id": "msg-1", "threadId": body.get("threadId", "thread-new")})
    if request.method == "GET" and path.endswith("/messages/msg-1"):
        return json_response(
            200,
            {
                "id": "msg-1",
                "payload": {
                    "headers": [{"name": "Message-ID", "value": "<rfq-42@quotes.usebido.com>"}]
                },
            },
        )
    return httpx.Response(404)


# --- Gmail send --------------------------------------------------------------------------


async def test_gmail_first_send_starts_a_thread_as_the_buyer() -> None:
    recorder = Recorder(_gmail_route)
    http, _, _ = authorized("google", recorder)
    threads = FakeThreads()
    sent = await GmailMailbox(http, address=BUYER, threads=threads).send(FIRST)

    send = recorder.requests[0]
    assert send.url.path == "/gmail/v1/users/me/messages/send"
    assert send.headers["Authorization"] == "Bearer access-1"
    body = json.loads(send.content)
    assert "threadId" not in body
    mime = message_from_bytes(b64url_decode(body["raw"]))
    assert BUYER in str(mime["From"])
    assert "Acme via Bido" in str(mime["From"])
    assert mime["Message-ID"] == "<rfq-42@quotes.usebido.com>"
    assert mime["To"] == SUPPLIER

    assert sent.provider == "google"
    assert sent.provider_thread_id == "thread-new"
    assert sent.provider_message_id == "msg-1"
    assert sent.message_id == "<rfq-42@quotes.usebido.com>"
    assert threads.bound == [("<rfq-42@quotes.usebido.com>", sent)]


async def test_gmail_follow_up_replies_in_the_stored_thread() -> None:
    recorder = Recorder(_gmail_route)
    http, _, _ = authorized("google", recorder)
    sent = await GmailMailbox(http, address=BUYER, threads=FakeThreads("thread-42")).send(FOLLOW_UP)

    body = json.loads(recorder.requests[0].content)
    assert body["threadId"] == "thread-42"
    mime = message_from_bytes(b64url_decode(body["raw"]))
    assert mime["In-Reply-To"] == "<rfq-42@quotes.usebido.com>"
    assert mime["References"] == "<rfq-42@quotes.usebido.com>"
    assert sent.provider_thread_id == "thread-42"


async def test_gmail_send_is_not_retried_on_server_error() -> None:
    recorder = Recorder(lambda request: httpx.Response(500))
    http, _, _ = authorized("google", recorder)

    with pytest.raises(ProviderError):
        await GmailMailbox(http, address=BUYER).send(FIRST)
    assert len(recorder.requests) == 1


# --- Gmail read ----------------------------------------------------------------------------


def _gmail_thread_route(raws: dict[str, bytes]) -> Recorder:
    def route(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/gmail/v1/users/me/threads/thread-42":
            assert request.url.params["format"] == "metadata"
            messages = [
                {
                    "id": "m-out",
                    "labelIds": ["SENT"],
                    "payload": {"headers": [{"name": "From", "value": f"Acme <{BUYER.upper()}>"}]},
                },
                {
                    "id": "m-seen",
                    "labelIds": ["INBOX"],
                    "payload": {"headers": [{"name": "From", "value": SUPPLIER}]},
                },
                {
                    "id": "m-draft",
                    "labelIds": ["DRAFT"],
                    "payload": {"headers": [{"name": "From", "value": SUPPLIER}]},
                },
                {
                    "id": "m-new",
                    "labelIds": ["INBOX"],
                    "payload": {"headers": [{"name": "From", "value": f"Sales <{SUPPLIER}>"}]},
                },
            ]
            return json_response(200, {"id": "thread-42", "messages": messages})
        if path.startswith("/gmail/v1/users/me/messages/"):
            message_id = path.rsplit("/", 1)[1]
            assert request.url.params["format"] == "raw"
            return json_response(200, {"id": message_id, "raw": b64url_encode(raws[message_id])})
        return httpx.Response(404)

    return Recorder(route)


async def test_gmail_fetch_reads_only_the_given_thread() -> None:
    raw = raw_email(from_address=SUPPLIER, to=BUYER)
    recorder = _gmail_thread_route({"m-new": raw})
    http, _, _ = authorized("google", recorder)
    fetch = await GmailMailbox(http, address=BUYER).fetch_thread_new_messages(
        "thread-42", {"seen": ["m-seen"]}, exclude_from=BUYER
    )

    assert recorder.paths() == [
        "GET /gmail/v1/users/me/threads/thread-42",
        "GET /gmail/v1/users/me/messages/m-new",
    ]
    assert [m.provider_message_id for m in fetch.messages] == ["m-new"]
    assert fetch.messages[0].raw == raw
    assert fetch.skipped_ids == ("m-out",)


async def test_gmail_missing_thread() -> None:
    http, _, _ = authorized("google", Recorder(lambda request: httpx.Response(404)))
    with pytest.raises(ThreadNotFound):
        await GmailMailbox(http, address=BUYER).fetch_thread_new_messages("gone", None)


async def test_gmail_rejects_ids_that_could_change_the_path() -> None:
    http, _, _ = authorized("google", Recorder(lambda request: httpx.Response(500)))
    with pytest.raises(ValueError):
        await GmailMailbox(http, address=BUYER).fetch_thread_new_messages("../messages", None)


# --- Graph send ------------------------------------------------------------------------------


def _graph_route(fail_send: bool = False) -> Recorder:
    def route(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/v1.0/me/messages":
            return json_response(
                201,
                {
                    "id": "draft-1",
                    "conversationId": "conv-1",
                    "internetMessageId": "<rfq-42@quotes.usebido.com>",
                },
            )
        if request.method == "POST" and path == "/v1.0/me/messages/draft-1/send":
            return httpx.Response(500 if fail_send else 202)
        if request.method == "DELETE" and path == "/v1.0/me/messages/draft-1":
            return httpx.Response(204)
        return httpx.Response(404)

    return Recorder(route)


async def test_graph_send_creates_a_mime_draft_then_sends() -> None:
    recorder = _graph_route()
    http, _, _ = authorized("microsoft", recorder)
    threads = FakeThreads()
    sent = await GraphMailbox(http, address=BUYER, threads=threads).send(FOLLOW_UP)

    assert recorder.paths() == ["POST /v1.0/me/messages", "POST /v1.0/me/messages/draft-1/send"]
    create = recorder.requests[0]
    assert create.headers["Content-Type"] == "text/plain"
    mime = message_from_bytes(base64.b64decode(create.content))
    assert BUYER in str(mime["From"])
    assert mime["Message-ID"] == "<rfq-42-f1@quotes.usebido.com>"
    assert mime["In-Reply-To"] == "<rfq-42@quotes.usebido.com>"
    assert sent.provider == "microsoft"
    assert sent.provider_thread_id == "conv-1"
    assert sent.message_id == "<rfq-42@quotes.usebido.com>"
    assert len(threads.bound) == 1


async def test_graph_failed_send_deletes_the_draft() -> None:
    recorder = _graph_route(fail_send=True)
    http, _, _ = authorized("microsoft", recorder)

    with pytest.raises(ProviderError):
        await GraphMailbox(http, address=BUYER).send(FIRST)
    assert recorder.paths() == [
        "POST /v1.0/me/messages",
        "POST /v1.0/me/messages/draft-1/send",
        "DELETE /v1.0/me/messages/draft-1",
    ]


# --- Graph read --------------------------------------------------------------------------------


async def test_graph_fetch_filters_one_conversation() -> None:
    raw = raw_email(from_address=SUPPLIER, to=BUYER)

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1.0/me/messages" and request.url.host == "graph.microsoft.com":
            if request.url.params.get("$skip") == "50":
                return json_response(
                    200,
                    {
                        "value": [
                            {
                                "id": "g-2",
                                "isDraft": False,
                                "from": {"emailAddress": {"address": SUPPLIER}},
                            },
                        ],
                        "@odata.nextLink": "https://evil.example/steal?x=1",
                    },
                )
            return json_response(
                200,
                {
                    "value": [
                        {
                            "id": "g-out",
                            "isDraft": False,
                            "from": {"emailAddress": {"address": BUYER}},
                        },
                        {
                            "id": "g-draft",
                            "isDraft": True,
                            "from": {"emailAddress": {"address": BUYER}},
                        },
                        {
                            "id": "g-1",
                            "isDraft": False,
                            "from": {"emailAddress": {"address": SUPPLIER}},
                        },
                    ],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?$skip=50",
                },
            )
        if request.url.path.endswith("/$value"):
            return httpx.Response(200, content=raw)
        return httpx.Response(404)

    recorder = Recorder(route)
    http, _, _ = authorized("microsoft", recorder)
    fetch = await GraphMailbox(http, address=BUYER).fetch_thread_new_messages(
        "conv-1", None, exclude_from=BUYER
    )

    first = recorder.requests[0]
    assert first.url.params["$filter"] == "conversationId eq 'conv-1'"
    assert "id" in first.url.params["$select"]
    assert [m.provider_message_id for m in fetch.messages] == ["g-1", "g-2"]
    assert fetch.skipped_ids == ("g-out",)
    assert all(r.url.host == "graph.microsoft.com" for r in recorder.requests)
    assert "/v1.0/me/messages/g-1/$value" in recorder.paths()[1]


def test_conversation_filter_escapes_quotes() -> None:
    assert conversation_filter("a'b") == "conversationId eq 'a''b'"


# --- Authorization: 401, revoked, throttling ------------------------------------------------


async def test_401_refreshes_and_retries_once() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer access-1":
            return httpx.Response(401)
        return _gmail_route(request)

    recorder = Recorder(route)
    http, store, refresher = authorized("google", recorder)
    sent = await GmailMailbox(http, address=BUYER).send(FIRST)

    assert refresher.calls == 1
    assert [t.access_token for t in store.saved] == ["access-2"]
    assert [r.headers["Authorization"] for r in recorder.requests[:2]] == [
        "Bearer access-1",
        "Bearer access-2",
    ]
    assert sent.provider_thread_id == "thread-new"


async def test_second_401_is_an_error_not_a_loop() -> None:
    recorder = Recorder(lambda request: httpx.Response(401))
    http, _, refresher = authorized("google", recorder)

    with pytest.raises(ProviderError) as info:
        await GmailMailbox(http, address=BUYER).fetch_thread_new_messages("t1", None)
    assert info.value.status == 401
    assert refresher.calls == 1
    assert len(recorder.requests) == 2


async def test_revoked_consent_marks_the_connection_revoked() -> None:
    recorder = Recorder(lambda request: httpx.Response(401))
    http, store, _ = authorized("google", recorder, refresher=Refresher(revoked=True))

    with pytest.raises(ConsentRevoked):
        await GmailMailbox(http, address=BUYER).fetch_thread_new_messages("t1", None)
    assert store.revoked is True
    assert store.saved == []


async def test_expired_token_is_refreshed_before_the_call() -> None:
    recorder = Recorder(lambda request: json_response(200, {"messages": []}))
    expired = TokenSet(access_token="access-1", refresh_token="r", expires_at=1)
    http, _, refresher = authorized("google", recorder, tokens=expired)

    await GmailMailbox(http, address=BUYER).fetch_thread_new_messages("t1", None)
    assert refresher.calls == 1
    assert recorder.requests[0].headers["Authorization"] == "Bearer access-2"


async def test_throttling_backs_off_then_succeeds() -> None:
    responses = [httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(503)]

    def route(request: httpx.Request) -> httpx.Response:
        if responses:
            return responses.pop(0)
        return json_response(200, {"messages": []})

    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    recorder = Recorder(route)
    http, _, _ = authorized("google", recorder, sleep=sleep)
    await GmailMailbox(http, address=BUYER).fetch_thread_new_messages("t1", None)

    assert len(recorder.requests) == 3
    assert delays == [2.0, 1.0]


async def test_throttling_gives_up_after_bounded_retries() -> None:
    recorder = Recorder(lambda request: httpx.Response(503))
    http, _, _ = authorized("google", recorder)

    with pytest.raises(ProviderError):
        await GmailMailbox(http, address=BUYER).fetch_thread_new_messages("t1", None)
    assert len(recorder.requests) == 4
