"""A retried send looks in the Sent folder first: a supplier never gets a second copy."""

# ruff: noqa: S105, S106 - fake OAuth tokens and keys, never real credentials

import json
from dataclasses import replace

import httpx
import pytest

from bido_back.mailbox.base import ProviderError
from bido_back.mailbox.google import GmailMailbox, bare_message_id
from bido_back.mailbox.microsoft import GraphMailbox, message_id_filter

from .support import BUYER, Recorder, authorized, json_response
from .test_providers import FIRST, FakeThreads

RETRY = replace(FIRST, possibly_sent=True)


def _gmail(found: bool) -> Recorder:
    def route(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/messages"):
            messages = [{"id": "msg-1", "threadId": "thread-1"}] if found else []
            return json_response(200, {"messages": messages} if messages else {})
        if request.method == "POST" and path.endswith("/messages/send"):
            return json_response(200, {"id": "msg-2", "threadId": "thread-2"})
        if request.method == "GET" and path.endswith("/messages/msg-2"):
            return json_response(200, {"id": "msg-2", "payload": {"headers": []}})
        return httpx.Response(404)

    return Recorder(route)


def _graph(found: bool) -> Recorder:
    def route(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/mailFolders/sentitems/messages"):
            value = (
                [
                    {
                        "id": "m-1",
                        "conversationId": "conv-1",
                        "internetMessageId": "<rfq-42@quotes.usebido.com>",
                    }
                ]
                if found
                else []
            )
            return json_response(200, {"value": value})
        if request.method == "POST" and path.endswith("/me/messages"):
            return json_response(
                201,
                {"id": "d-2", "conversationId": "conv-2", "internetMessageId": FIRST.message_id},
            )
        if request.method == "POST" and path.endswith("/send"):
            return httpx.Response(202)
        return httpx.Response(404)

    return Recorder(route)


async def test_gmail_retry_already_in_sent_is_not_sent_again() -> None:
    recorder = _gmail(found=True)
    http, _, _ = authorized("google", recorder)
    threads = FakeThreads()
    sent = await GmailMailbox(http, address=BUYER, threads=threads).send(RETRY)

    assert recorder.paths() == ["GET /gmail/v1/users/me/messages"]
    params = recorder.requests[0].url.params
    assert params["q"] == "in:sent rfc822msgid:rfq-42@quotes.usebido.com"
    assert params["maxResults"] == "1"
    assert sent.provider_message_id == "msg-1"
    assert sent.provider_thread_id == "thread-1"
    assert sent.message_id == FIRST.message_id
    # The thread is still bound, as after a real send.
    assert threads.bound == [(FIRST.message_id, sent)]


async def test_gmail_retry_not_in_sent_is_sent() -> None:
    recorder = _gmail(found=False)
    http, _, _ = authorized("google", recorder)
    sent = await GmailMailbox(http, address=BUYER, threads=FakeThreads()).send(RETRY)

    assert recorder.paths()[:2] == [
        "GET /gmail/v1/users/me/messages",
        "POST /gmail/v1/users/me/messages/send",
    ]
    assert sent.provider_message_id == "msg-2"


async def test_gmail_first_attempt_does_not_look_up() -> None:
    recorder = _gmail(found=True)
    http, _, _ = authorized("google", recorder)
    await GmailMailbox(http, address=BUYER, threads=FakeThreads()).send(FIRST)

    assert recorder.paths()[0] == "POST /gmail/v1/users/me/messages/send"


async def test_gmail_lookup_failure_does_not_send() -> None:
    recorder = Recorder(lambda request: httpx.Response(500))
    http, _, _ = authorized("google", recorder)
    with pytest.raises(ProviderError):
        await GmailMailbox(http, address=BUYER, threads=FakeThreads()).send(RETRY)
    assert all(r.method == "GET" for r in recorder.requests)


async def test_graph_retry_already_in_sent_is_not_sent_again() -> None:
    recorder = _graph(found=True)
    http, _, _ = authorized("microsoft", recorder)
    sent = await GraphMailbox(http, address=BUYER, threads=FakeThreads()).send(RETRY)

    assert recorder.paths() == ["GET /v1.0/me/mailFolders/sentitems/messages"]
    params = recorder.requests[0].url.params
    assert params["$filter"] == "internetMessageId eq '<rfq-42@quotes.usebido.com>'"
    assert sent.provider_message_id == "m-1"
    assert sent.provider_thread_id == "conv-1"


async def test_graph_retry_not_in_sent_is_sent() -> None:
    recorder = _graph(found=False)
    http, _, _ = authorized("microsoft", recorder)
    sent = await GraphMailbox(http, address=BUYER, threads=FakeThreads()).send(RETRY)

    assert [p for p in recorder.paths() if p.startswith("POST")] == [
        "POST /v1.0/me/messages",
        "POST /v1.0/me/messages/d-2/send",
    ]
    assert sent.provider_message_id == "d-2"


def test_message_id_lookups_are_quoted_safely() -> None:
    assert bare_message_id("<a.b-c@x.example>") == "a.b-c@x.example"
    for bad in ("<a b@x>", '<a"b@x>', "<a)b@x>", "<>"):
        with pytest.raises(ValueError, match="Message-ID"):
            bare_message_id(bad)
    assert message_id_filter("<it's@x>") == "internetMessageId eq '<it''s@x>'"
    assert json.dumps(message_id_filter("<a@b>"))  # plain string
