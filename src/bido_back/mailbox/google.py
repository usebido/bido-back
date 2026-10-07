"""Gmail: send as the buyer, read one RFQ thread, scan sent mail. Plain REST over httpx.

Calls used, and nothing else (no history, no inbox listing):

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

The onboarding scan (`list_sent`, started by the user):

* `GET users/me/messages?q=in:sent after:<date> (<keywords>)` for ids, paged;
* `GET users/me/messages/{id}?format=full&fields=<headers and part names>`
  per id: the `fields` mask leaves out every body, so only the subject,
  recipients, date and attachment names and types come back;
* `get_message` reuses `format=raw`, only for the messages the user picked.
"""

import asyncio
import re
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from email.utils import getaddresses, parsedate_to_datetime
from typing import cast
from urllib.parse import quote

from bido_back.mailbox.base import (
    FetchedMessage,
    MailboxSender,
    Provider,
    ProviderError,
    ProviderSent,
    SentAttachment,
    SentSummary,
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
_LIST_PAGE = 100
# Metadata calls in flight at once during a scan (Gmail allows far more).
_SCAN_CONCURRENCY = 8
# Headers and part names/types only: every body is masked out.
_SUMMARY_FIELDS = (
    "id,internalDate,payload(headers,filename,mimeType,"
    "parts(filename,mimeType,parts(filename,mimeType,parts(filename,mimeType))))"
)
_SUMMARY_HEADERS = frozenset({"subject", "to", "cc", "date"})


def sent_query(keywords: Sequence[str], since: datetime) -> str:
    """The Gmail search for sent messages since `since` matching any keyword."""
    terms = " OR ".join(f'"{k}"' if " " in k else k for k in keywords)
    return f"in:sent after:{since:%Y/%m/%d} ({terms})"


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

    async def list_sent(
        self, *, keywords: Sequence[str], since: datetime, limit: int
    ) -> AsyncIterator[list[SentSummary]]:
        query = sent_query(keywords, since)
        page_token: str | None = None
        listed = 0
        gate = asyncio.Semaphore(_SCAN_CONCURRENCY)
        while listed < limit:
            params = {"q": query, "maxResults": str(min(_LIST_PAGE, limit - listed))}
            if page_token:
                params["pageToken"] = page_token
            response = await self._http.request(
                "GET", f"{GMAIL_API}/messages", operation="list sent", params=params
            )
            body = json_object(response)
            ids = [i for i in (str_field(m, "id") for m in list_field(body, "messages")) if i]
            if not ids:
                return
            listed += len(ids)

            async def summary(message_id: str) -> SentSummary | None:
                async with gate:
                    return await self._summary(message_id)

            found = await asyncio.gather(*(summary(i) for i in ids))
            yield [s for s in found if s is not None]
            page_token = str_field(body, "nextPageToken")
            if page_token is None:
                return

    async def _summary(self, message_id: str) -> SentSummary | None:
        try:
            response = await self._http.request(
                "GET",
                f"{GMAIL_API}/messages/{_id(message_id)}",
                operation="sent summary",
                params={"format": "full", "fields": _SUMMARY_FIELDS},
            )
        except ProviderError as exc:
            if exc.status == 404:
                return None
            raise
        message = json_object(response)
        payload = message.get("payload")
        part = cast(dict[str, object], payload) if isinstance(payload, dict) else {}
        headers = {
            str(h.get("name", "")).lower(): h.get("value")
            for h in list_field(part, "headers")
            if str(h.get("name", "")).lower() in _SUMMARY_HEADERS
        }
        values = [headers.get("to"), headers.get("cc")]
        recipients = tuple(
            address
            for _, address in getaddresses([v for v in values if isinstance(v, str)])
            if address
        )
        subject = headers.get("subject")
        return SentSummary(
            provider_message_id=message_id,
            subject=subject if isinstance(subject, str) else "",
            recipients=recipients,
            sent_at=_sent_at(message, headers.get("date")),
            attachments=tuple(_attachments(part)),
        )

    async def get_message(self, provider_message_id: str) -> bytes:
        return await self._raw(provider_message_id)

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


def _sent_at(message: dict[str, object], date_header: object) -> datetime | None:
    internal = message.get("internalDate")
    if isinstance(internal, str) and internal.isdigit():
        return datetime.fromtimestamp(int(internal) / 1000, tz=UTC)
    if isinstance(date_header, str):
        try:
            return parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            return None
    return None


def _attachments(part: dict[str, object], depth: int = 0) -> list[SentAttachment]:
    """Named parts (attachments) of a message, from the masked payload tree."""
    found: list[SentAttachment] = []
    filename = part.get("filename")
    if isinstance(filename, str) and filename:
        mime = part.get("mimeType")
        found.append(SentAttachment(filename, mime if isinstance(mime, str) else ""))
    if depth < 3:
        for child in list_field(part, "parts"):
            found.extend(_attachments(child, depth + 1))
    return found
