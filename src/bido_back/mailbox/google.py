"""Gmail: send as the buyer, and read one RFQ thread. Plain REST over httpx.

Calls used, and nothing else (no list, no search, no history):

* `POST users/me/messages/send` with the raw MIME, and `threadId` for a
  follow-up. Gmail threads a message into `threadId` only if In-Reply-To /
  References point into the thread and the subject matches; `build_mime`
  sets both headers from the `OutboundEmail`.
* `GET users/me/messages/{id}?format=metadata&metadataHeaders=Message-ID` for
  the message just sent, to return the Message-ID Gmail actually used (it
  keeps ours; this guards against it ever rewriting it).
* `GET users/me/threads/{threadId}?format=metadata&metadataHeaders=From`
  lists the message ids, labels and senders of ONE thread.
* `GET users/me/messages/{id}?format=raw` for each new message in it.

Drafts (label DRAFT) are never read. Messages from the buyer's own address
are skipped without downloading their content.
"""

import re
from typing import cast
from urllib.parse import quote

from bido_back.mailbox.base import (
    FetchedMessage,
    MailboxSender,
    Provider,
    ProviderError,
    ProviderSent,
    ThreadBinding,
    ThreadFetch,
    ThreadNotFound,
    ThreadState,
    b64url_decode,
    b64url_encode,
    same_address,
    seen_ids,
)
from bido_back.mailbox.http import AuthorizedHttp, json_object, list_field, str_field

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _id(value: str) -> str:
    if not _ID_RE.match(value):
        raise ValueError("not a Gmail id")
    return quote(value, safe="")


class GmailMailbox(MailboxSender):
    provider: Provider = "google"

    def __init__(
        self, http: AuthorizedHttp, *, address: str, threads: ThreadBinding | None = None
    ) -> None:
        super().__init__(address=address, threads=threads)
        self._http = http

    async def send_raw(self, raw: bytes, *, thread_id: str | None) -> ProviderSent:
        body: dict[str, str] = {"raw": b64url_encode(raw)}
        if thread_id is not None:
            body["threadId"] = thread_id
        response = await self._http.request(
            "POST", f"{GMAIL_API}/messages/send", operation="send", idempotent=False, json=body
        )
        sent = json_object(response)
        message_id = str_field(sent, "id")
        sent_thread = str_field(sent, "threadId")
        if message_id is None or sent_thread is None:
            raise ProviderError("google", response.status_code, "send (no message id)")
        return ProviderSent(
            provider_message_id=message_id,
            thread_id=sent_thread,
            internet_message_id=await self._message_id_header(message_id),
        )

    async def _message_id_header(self, message_id: str) -> str | None:
        try:
            response = await self._http.request(
                "GET",
                f"{GMAIL_API}/messages/{_id(message_id)}",
                operation="sent metadata",
                params={"format": "metadata", "metadataHeaders": "Message-ID"},
            )
        except ProviderError:
            # The message is sent; the caller falls back to our own Message-ID.
            return None
        return _header(json_object(response), "Message-ID")

    async def fetch_thread_new_messages(
        self, thread_id: str, since_state: ThreadState | None, *, exclude_from: str | None = None
    ) -> ThreadFetch:
        try:
            response = await self._http.request(
                "GET",
                f"{GMAIL_API}/threads/{_id(thread_id)}",
                operation="thread",
                params={"format": "metadata", "metadataHeaders": "From"},
            )
        except ProviderError as exc:
            if exc.status == 404:
                raise ThreadNotFound("gmail thread not found") from None
            raise
        seen = seen_ids(since_state)
        messages: list[FetchedMessage] = []
        skipped: list[str] = []
        for meta in list_field(json_object(response), "messages"):
            message_id = str_field(meta, "id")
            if message_id is None or message_id in seen:
                continue
            labels = meta.get("labelIds")
            if isinstance(labels, list) and "DRAFT" in labels:
                continue
            if exclude_from and same_address(_header(meta, "From"), exclude_from):
                skipped.append(message_id)
                continue
            messages.append(FetchedMessage(message_id, await self._raw(message_id)))
        return ThreadFetch(messages=tuple(messages), skipped_ids=tuple(skipped))

    async def _raw(self, message_id: str) -> bytes:
        response = await self._http.request(
            "GET",
            f"{GMAIL_API}/messages/{_id(message_id)}",
            operation="message",
            params={"format": "raw"},
        )
        raw = str_field(json_object(response), "raw")
        if raw is None:
            raise ProviderError("google", response.status_code, "message (no raw)")
        return b64url_decode(raw)


def _header(message: dict[str, object], name: str) -> str | None:
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return None
    for header in list_field(cast(dict[str, object], payload), "headers"):
        if str(header.get("name", "")).lower() == name.lower():
            value = header.get("value")
            return value if isinstance(value, str) else None
    return None
