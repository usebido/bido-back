"""Types and errors shared by the mailbox providers, the service and the api.

A connected mailbox is used in three ways. Two are confined to RFQ threads
Bido started:

* sending (`MailboxSender.send`): the first RFQ email starts a new thread in
  the buyer's mailbox, a follow-up replies inside the stored thread;
* reading (`fetch_thread_new_messages`): the messages of ONE given thread that
  were not handled yet.

The third is the onboarding scan, which the user starts themselves to teach
Bido what their RFQs look like:

* `list_sent`: metadata (subject, recipients, date, attachment names) of SENT
  messages from a recent window that match RFQ keywords, bounded by a limit;
* `get_message`: the raw bytes of one of those messages, only for the ones the
  user picked.

There is deliberately no method that lists, searches or reads anything else
(no inbox, no other folders).

Error messages carry provider names, HTTP statuses and ids, never tokens,
addresses or message content.
"""

import base64
import json
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from email.utils import getaddresses
from typing import Literal, Protocol, cast

from pydantic import JsonValue

from bido_back.email.mime import build_mime
from bido_back.email.types import OutboundEmail, SentEmail

Provider = Literal["google", "microsoft"]
PROVIDERS: tuple[Provider, ...] = ("google", "microsoft")
ConnectionStatus = Literal["active", "revoked", "error"]

# Refresh a little before the provider says the access token expires.
EXPIRY_SKEW_S = 60


# --- Errors -------------------------------------------------------------------


class MailboxError(Exception):
    """Base class. Messages are safe for logs and HTTP responses."""


class MailboxNotConnected(MailboxError):
    """The user has no active mailbox connection (for this provider)."""


class MailboxNotConfigured(MailboxError):
    """The provider's OAuth client, or token encryption, is not configured."""


class ConsentRevoked(MailboxError):
    """The provider refused the refresh token (`invalid_grant`): consent is gone."""


class ProviderError(MailboxError):
    """The provider answered with an error we do not handle (status kept)."""

    def __init__(self, provider: Provider, status: int, operation: str) -> None:
        super().__init__(f"{provider} {operation} failed with HTTP {status}")
        self.provider = provider
        self.status = status
        self.operation = operation


class ThreadNotFound(MailboxError):
    """The thread no longer exists in the mailbox (deleted by the buyer)."""


# --- Tokens ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenSet:
    """OAuth tokens for one connection. Never logged: tokens are excluded from repr."""

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    # Unix seconds; None when the provider did not say.
    expires_at: int | None = None
    scopes: tuple[str, ...] = ()

    def expired(self, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now if now is not None else time.time()) >= self.expires_at - EXPIRY_SKEW_S

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
                "scopes": list(self.scopes),
            },
            separators=(",", ":"),
        ).encode()

    @classmethod
    def from_json(cls, data: bytes) -> "TokenSet":
        raw: object = json.loads(data)
        if not isinstance(raw, dict):
            raise ValueError("token set is not a JSON object")
        obj = cast(dict[str, object], raw)
        access = obj.get("access_token")
        refresh = obj.get("refresh_token")
        expires = obj.get("expires_at")
        scopes = obj.get("scopes", [])
        if not isinstance(access, str) or not access:
            raise ValueError("token set has no access token")
        if refresh is not None and not isinstance(refresh, str):
            raise ValueError("token set refresh token is not a string")
        if expires is not None and not isinstance(expires, int):
            raise ValueError("token set expiry is not an integer")
        if not isinstance(scopes, list):
            raise ValueError("token set scopes is not a list")
        return cls(
            access_token=access,
            refresh_token=refresh,
            expires_at=expires,
            scopes=tuple(str(s) for s in cast(list[object], scopes)),
        )


class TokenStore(Protocol):
    """Where a connection's tokens are persisted when they change."""

    async def save(self, tokens: TokenSet) -> None: ...

    async def mark_revoked(self) -> None: ...

    async def mark_error(self) -> None: ...


# --- Sending --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProviderSent:
    """What the provider reports for a sent message."""

    provider_message_id: str
    thread_id: str
    # The Message-ID header as the provider sent it (None if it did not say).
    internet_message_id: str | None


@dataclass(frozen=True, slots=True)
class MailboxSentEmail(SentEmail):
    """`SentEmail` plus where the message lives in the buyer's mailbox."""

    provider: Provider = "google"
    provider_message_id: str = ""
    provider_thread_id: str = ""


class ThreadBinding(Protocol):
    """Links outbound emails to provider threads (the service backs it with `invites`)."""

    async def thread_for(self, email: OutboundEmail) -> str | None:
        """The provider thread a follow-up belongs in, or None for a new thread."""
        ...

    async def bind(self, email: OutboundEmail, sent: MailboxSentEmail) -> None:
        """Record the provider thread of a just-sent email."""
        ...


class NoThreadBinding:
    """Every email starts a new thread; nothing is recorded."""

    async def thread_for(self, email: OutboundEmail) -> str | None:
        return None

    async def bind(self, email: OutboundEmail, sent: MailboxSentEmail) -> None:
        return None


# --- Reading --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FetchedMessage:
    provider_message_id: str
    raw: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ThreadFetch:
    # New messages to hand to intake, oldest first as the provider lists them.
    messages: tuple[FetchedMessage, ...]
    # Messages seen and deliberately not downloaded (the buyer's own).
    skipped_ids: tuple[str, ...]


ThreadState = Mapping[str, JsonValue]
MAX_SEEN_PER_THREAD = 1000


def seen_ids(state: ThreadState | None) -> frozenset[str]:
    """Provider message ids already handled in a thread, from its sync state."""
    if not state:
        return frozenset()
    seen = state.get("seen")
    if not isinstance(seen, list):
        return frozenset()
    return frozenset(s for s in seen if isinstance(s, str))


def with_seen(state: ThreadState | None, ids: Sequence[str]) -> dict[str, JsonValue]:
    """The thread state with `ids` marked handled (order kept, newest last, capped)."""
    previous = state.get("seen") if state else None
    seen: list[str] = (
        [s for s in previous if isinstance(s, str)] if isinstance(previous, list) else []
    )
    for message_id in ids:
        if message_id not in seen:
            seen.append(message_id)
    kept: list[JsonValue] = list(seen[-MAX_SEEN_PER_THREAD:])
    return {"seen": kept}


# --- The onboarding scan (sent mail) ------------------------------------------------

# Words an RFQ subject or body usually carries (pt-BR and English).
SCAN_KEYWORDS: tuple[str, ...] = (
    "RFQ",
    "cotação",
    "cotacao",
    "orçamento",
    "orcamento",
    "quotation",
    "request for quote",
    "quote request",
)


@dataclass(frozen=True, slots=True)
class SentAttachment:
    name: str
    content_type: str


@dataclass(frozen=True, slots=True)
class SentSummary:
    """Metadata of one sent message. No body, no attachment content."""

    provider_message_id: str
    subject: str
    # To and Cc addresses, in header order.
    recipients: tuple[str, ...]
    sent_at: datetime | None
    attachments: tuple[SentAttachment, ...] = ()


def same_address(header_value: str | None, address: str) -> bool:
    """True if the (first) address in a From header value is `address` (case-insensitive)."""
    if not header_value:
        return False
    parsed = [a for _, a in getaddresses([header_value]) if a]
    return bool(parsed) and parsed[0].strip().lower() == address.strip().lower()


# --- The sender -----------------------------------------------------------------


class MailboxSender(ABC):
    """`EmailSender` that sends as the buyer through their own mailbox.

    From is forced to the connected address (the provider would rewrite or
    refuse anything else); the display name, recipients, Reply-To, subject,
    body, attachments and our Message-ID/threading headers are the caller's,
    built by the shared `build_mime`. A follow-up (In-Reply-To/References
    pointing at one of our RFQ threads) is sent inside the stored provider
    thread; anything else starts a new thread, which `ThreadBinding.bind`
    records.

    `SentEmail.message_id` is the Message-ID the provider reports for the sent
    message (ours whenever the provider keeps it), because that is what the
    supplier's reply will reference.
    """

    provider: Provider

    def __init__(self, *, address: str, threads: ThreadBinding | None = None) -> None:
        self._address = address
        self._threads: ThreadBinding = threads or NoThreadBinding()

    @property
    def address(self) -> str:
        return self._address

    async def send(self, email: OutboundEmail) -> MailboxSentEmail:
        as_buyer = replace(email, from_address=self._address)
        msg = build_mime(as_buyer)
        raw = msg.as_bytes()
        thread_id = await self._threads.thread_for(email)
        sent = await self.send_raw(raw, thread_id=thread_id)
        result = MailboxSentEmail(
            message_id=sent.internet_message_id or str(msg["Message-ID"]),
            raw=raw,
            provider=self.provider,
            provider_message_id=sent.provider_message_id,
            provider_thread_id=sent.thread_id,
        )
        await self._threads.bind(email, result)
        return result

    @abstractmethod
    async def send_raw(self, raw: bytes, *, thread_id: str | None) -> ProviderSent:
        """Send RFC 5322 bytes, in `thread_id` when given."""

    @abstractmethod
    async def fetch_thread_new_messages(
        self, thread_id: str, since_state: ThreadState | None, *, exclude_from: str | None = None
    ) -> ThreadFetch:
        """Messages of `thread_id` not in `since_state`, as raw RFC 822 bytes.

        Drafts are never returned. Messages whose From is `exclude_from` are
        reported in `skipped_ids` without being downloaded, when the provider
        exposes the sender in its metadata.
        """

    @abstractmethod
    def list_sent(
        self, *, keywords: Sequence[str], since: datetime, limit: int
    ) -> AsyncIterator[list[SentSummary]]:
        """Sent messages since `since` matching any of `keywords`, page by page.

        Metadata only. Stops after `limit` messages in total.
        """

    @abstractmethod
    async def get_message(self, provider_message_id: str) -> bytes:
        """The raw RFC 822 bytes of one message."""


def b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")
