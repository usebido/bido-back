"""Microsoft 365 (Graph): send as the buyer, and read one RFQ thread. Plain REST.

Sending: MIME draft, then send
------------------------------
`POST /me/messages` with the base64 MIME as `text/plain` creates a draft from
our exact message, and the response carries its `id`, `conversationId` and
`internetMessageId`; `POST /me/messages/{id}/send` then sends it (202).

Why not `sendMail` with MIME: it answers 202 with no body, so we would learn
neither the conversation (thread) id nor the Message-ID that went out. Why
not `createReply`: Exchange builds the reply itself (its own Message-ID, its
own quoted body), so the approved text would not be what is sent byte for
byte and our Message-ID would be lost. With a MIME draft the body, headers,
In-Reply-To and References are ours; `SentEmail.message_id` is the
`internetMessageId` Graph reports (Exchange keeps the MIME Message-ID when
there is one).

Threading caveat: Exchange computes `conversationId` itself (from Thread-Index
/ In-Reply-To / References). A follow-up normally lands in the original
conversation, but Exchange may start a new one; the stored thread stays the
first message's conversation, which is where supplier replies to the RFQ go.

If the send step fails after the draft was created, the draft is deleted
(best effort) so nothing half-sent lingers in the buyer's Drafts.

Reading: `GET /me/messages?$filter=conversationId eq '<id>'` (one thread only,
`$select` of id, isDraft and from; `@odata.nextLink` followed only on
graph.microsoft.com, bounded), then `GET /me/messages/{id}/$value` (the MIME)
for each new message. Drafts are never read; the buyer's own messages are
skipped without downloading their content.
"""

import base64
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
    ThreadState,
    seen_ids,
)
from bido_back.mailbox.http import AuthorizedHttp, json_object, list_field, str_field

GRAPH_API = "https://graph.microsoft.com/v1.0/me"
_GRAPH_ORIGIN = "https://graph.microsoft.com/"
_PAGE_SIZE = 50
_MAX_PAGES = 10


def _id(value: str) -> str:
    if not value or len(value) > 512:
        raise ValueError("not a Graph id")
    return quote(value, safe="")


def conversation_filter(conversation_id: str) -> str:
    """The OData filter for one conversation (single quotes doubled)."""
    if not conversation_id or len(conversation_id) > 512:
        raise ValueError("not a Graph conversation id")
    escaped = conversation_id.replace("'", "''")
    return f"conversationId eq '{escaped}'"


class GraphMailbox(MailboxSender):
    provider: Provider = "microsoft"

    def __init__(
        self, http: AuthorizedHttp, *, address: str, threads: ThreadBinding | None = None
    ) -> None:
        super().__init__(address=address, threads=threads)
        self._http = http

    async def send_raw(self, raw: bytes, *, thread_id: str | None) -> ProviderSent:
        # `thread_id` is not passed to Graph: Exchange threads by the MIME
        # In-Reply-To / References headers (see the module docstring).
        response = await self._http.request(
            "POST",
            f"{GRAPH_API}/messages",
            operation="create draft",
            idempotent=False,
            content=base64.b64encode(raw),
            headers={"Content-Type": "text/plain"},
            ok=(200, 201),
        )
        draft = json_object(response)
        draft_id = str_field(draft, "id")
        conversation_id = str_field(draft, "conversationId")
        if draft_id is None or conversation_id is None:
            raise ProviderError("microsoft", response.status_code, "create draft (no id)")
        try:
            await self._http.request(
                "POST",
                f"{GRAPH_API}/messages/{_id(draft_id)}/send",
                operation="send",
                idempotent=False,
                ok=(202,),
            )
        except Exception:
            await self._delete_draft(draft_id)
            raise
        return ProviderSent(
            provider_message_id=draft_id,
            thread_id=conversation_id,
            internet_message_id=str_field(draft, "internetMessageId"),
        )

    async def _delete_draft(self, draft_id: str) -> None:
        try:
            await self._http.request(
                "DELETE",
                f"{GRAPH_API}/messages/{_id(draft_id)}",
                operation="delete draft",
                ok=(204, 404),
            )
        except Exception:  # noqa: BLE001, S110 - best effort; the send error is what matters
            pass

    async def fetch_thread_new_messages(
        self, thread_id: str, since_state: ThreadState | None, *, exclude_from: str | None = None
    ) -> ThreadFetch:
        seen = seen_ids(since_state)
        messages: list[FetchedMessage] = []
        skipped: list[str] = []
        url: str | None = f"{GRAPH_API}/messages"
        params: dict[str, str] | None = {
            "$filter": conversation_filter(thread_id),
            "$select": "id,isDraft,from",
            "$top": str(_PAGE_SIZE),
        }
        pages = 0
        while url is not None and pages < _MAX_PAGES:
            response = await self._http.request("GET", url, operation="thread", params=params)
            body = json_object(response)
            pages += 1
            for meta in list_field(body, "value"):
                message_id = str_field(meta, "id")
                if message_id is None or message_id in seen or meta.get("isDraft") is True:
                    continue
                if exclude_from and _from_address(meta) == exclude_from.strip().lower():
                    skipped.append(message_id)
                    continue
                messages.append(FetchedMessage(message_id, await self._raw(message_id)))
            next_link = str_field(body, "@odata.nextLink")
            # Never send the bearer token anywhere but Graph.
            url = next_link if next_link and next_link.startswith(_GRAPH_ORIGIN) else None
            params = None
        return ThreadFetch(messages=tuple(messages), skipped_ids=tuple(skipped))

    async def _raw(self, message_id: str) -> bytes:
        response = await self._http.request(
            "GET", f"{GRAPH_API}/messages/{_id(message_id)}/$value", operation="message"
        )
        return response.content


def _from_address(meta: dict[str, object]) -> str | None:
    sender = meta.get("from")
    if not isinstance(sender, dict):
        return None
    address = cast(dict[str, object], sender).get("emailAddress")
    if not isinstance(address, dict):
        return None
    value = cast(dict[str, object], address).get("address")
    return value.strip().lower() if isinstance(value, str) else None
