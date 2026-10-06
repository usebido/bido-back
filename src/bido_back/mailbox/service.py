"""Mailbox connections: storing them, sending through them, syncing RFQ threads.

Scope (CTO decision, extended for onboarding): Bido sends from and reads the
buyer's own mailbox in RFQ threads Bido started. Reading means: for each
invite that has a provider thread id, the messages of that one thread. The
one exception is the onboarding scan, which the user starts themselves: a
one-off search of their SENT mail from the last 12 months for messages that
look like RFQs (`bido_back.onboarding`). It stores metadata only (subject,
recipients, date, attachment names), and reads the full message only for the
ones the user picks to build the RFQ template from. Nothing else in the
mailbox is listed, searched or stored. Every send still needs an approval; the
act tool that calls `send` runs only after the approval guard.

Sending: `get_mailbox_sender(session, tenant_id, user_id)` returns an
`EmailSender` for the user's active connection (`MailboxNotConnected`
otherwise). It sends as the buyer; the first RFQ email to a supplier starts a
thread, whose id is written to the invite whose `thread_message_id` is that
email's Message-ID (in the caller's session, so it commits with the caller's
transaction); a follow-up whose In-Reply-To / References point at an invite's
thread is sent inside that thread.

Tokens: refreshed tokens and a revoked status are written in their OWN
transaction (when the caller's session is bound to an engine), so they are
kept even when the caller's transaction rolls back: a refreshed token is
valid regardless of what the send did, and a revoked grant stays revoked.

Sync: `sync_rfq_threads(db, tenant_id, ingest=...)`, for each active
connection, fetches the new messages of the threads of its invites that are
`sent`/`responded` on an `open` RFQ, skips the buyer's own messages (From is
the connected address), hands everything else to `ingest(raw, tenant_id=,
invite_id=)` (intake's `InboundService.ingest_for_invite`, which deduplicates
by Message-ID), and records the handled provider message ids in
`mailbox_connections.sync_state` after each thread. A message whose ingest
fails is not marked handled and is retried on the next run; the thread's
other messages go on. Provider calls happen outside database transactions.

The scheduled job: `mailbox.sync`, put on the jobs queue by EventBridge
Scheduler every `MAILBOX_SYNC_INTERVAL_SECONDS`. The envelope must carry a
tenant id, so the schedule uses the all-zeros UUID (`ALL_TENANTS`), which
`handle_mailbox_sync` reads as "every tenant with an active connection",
found through the SECURITY DEFINER `active_mailbox_tenants()` (migration
0006; returns tenant ids only). The all-zeros id is never used as a tenant:
each tenant found is synced in its own `tenant_session`. A real tenant id
syncs just that tenant ("sync now"). Concurrent runs are safe: ingest
deduplicates, and the last write of `sync_state` wins.

Logs carry ids and counts only, never addresses, tokens or content.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Protocol, cast
from uuid import UUID

import httpx
from pydantic import JsonValue
from sqlalchemy import case, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import Invite, MailboxConnection, Membership, Message, Rfq
from bido_back.email.inbound import parse_inbound
from bido_back.email.types import OutboundEmail
from bido_back.mailbox.base import (
    ConnectionStatus,
    ConsentRevoked,
    MailboxNotConnected,
    MailboxSender,
    MailboxSentEmail,
    Provider,
    ProviderError,
    ThreadNotFound,
    ThreadState,
    TokenSet,
    same_address,
    with_seen,
)
from bido_back.mailbox.crypto import (
    TokenCipher,
    TokenContext,
    TokenDecryptionError,
    build_token_cipher,
    decrypt_tokens,
    encrypt_tokens,
)
from bido_back.mailbox.google import GmailMailbox
from bido_back.mailbox.http import AuthorizedHttp
from bido_back.mailbox.microsoft import GraphMailbox
from bido_back.mailbox.oauth import oauth_client, refresh_tokens, revoke
from bido_back.settings import Settings, get_settings
from bido_back.worker.envelope import Envelope

logger = logging.getLogger(__name__)

# The tenant id of the scheduled "every tenant" sync (never a real tenant).
ALL_TENANTS: Final = UUID(int=0)
SYNCED_INVITE_STATUSES: Final = ("sent", "responded")
_UNIQUE: Final = "mailbox_connections_tenant_user_provider_key"
_ACTIVE_TENANTS = text("SELECT tenant_id FROM active_mailbox_tenants()")

Db = AsyncEngine | async_sessionmaker[AsyncSession]


class Ingest(Protocol):
    """Intake's `InboundService.ingest_for_invite`."""

    async def __call__(self, raw: bytes, *, tenant_id: UUID, invite_id: UUID) -> object: ...


@dataclass(frozen=True, slots=True)
class ConnectionView:
    provider: Provider
    email_address: str
    status: ConnectionStatus
    scopes: tuple[str, ...]
    created_at: datetime
    updated_at: datetime


def _sessionmaker(db: Db) -> async_sessionmaker[AsyncSession]:
    return db if isinstance(db, async_sessionmaker) else create_sessionmaker(db)


def _view(row: MailboxConnection) -> ConnectionView:
    return ConnectionView(
        provider=cast(Provider, row.provider),
        email_address=row.email_address,
        status=cast(ConnectionStatus, row.status),
        scopes=tuple(row.scopes),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# --- Token persistence --------------------------------------------------------------


class _ConnectionTokenStore:
    """Writes a connection's tokens/status in a separate tenant transaction when it can."""

    def __init__(
        self,
        *,
        tenant_id: UUID,
        connection_id: UUID,
        context: TokenContext,
        cipher: TokenCipher,
        db: Db | None,
        session: AsyncSession | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._connection_id = connection_id
        self._context = context
        self._cipher = cipher
        self._db = db
        self._session = session

    async def save(self, tokens: TokenSet) -> None:
        ciphertext = await encrypt_tokens(self._cipher, tokens, self._context)
        await self._write(token_ciphertext=ciphertext, status="active")

    async def mark_revoked(self) -> None:
        await self._write(token_ciphertext=None, status="revoked")

    async def mark_error(self) -> None:
        await self._write(status="error")

    async def _write(self, **values: object) -> None:
        stmt = (
            update(MailboxConnection)
            .where(MailboxConnection.id == self._connection_id)
            .values(**values, updated_at=func.now())
        )
        if self._db is not None:
            async with tenant_session(self._db, self._tenant_id) as session:
                await session.execute(stmt)
        elif self._session is not None:
            await self._session.execute(stmt)


# --- Thread binding (invites) -----------------------------------------------------------


class _InviteThreads:
    """Backs `ThreadBinding` with `invites.provider_thread_id`, in the caller's session."""

    def __init__(self, session: AsyncSession, connection_id: UUID) -> None:
        self._session = session
        self._connection_id = connection_id

    async def thread_for(self, email: OutboundEmail) -> str | None:
        refs = [r for r in (email.in_reply_to, *email.references) if r]
        if not refs:
            return None
        thread = await self._session.scalar(
            select(Invite.provider_thread_id)
            .where(
                Invite.thread_message_id.in_(refs),
                Invite.mailbox_connection_id == self._connection_id,
                Invite.provider_thread_id.is_not(None),
            )
            .limit(1)
        )
        if thread is None:
            thread = await self._session.scalar(
                select(Invite.provider_thread_id)
                .join(Message, Message.invite_id == Invite.id)
                .where(
                    Message.direction == "out",
                    Message.message_id_header.in_(refs),
                    Invite.mailbox_connection_id == self._connection_id,
                    Invite.provider_thread_id.is_not(None),
                )
                .limit(1)
            )
        return thread

    async def bind(self, email: OutboundEmail, sent: MailboxSentEmail) -> None:
        if not email.message_id or not sent.provider_thread_id:
            return
        await self._session.execute(
            update(Invite)
            .where(
                Invite.thread_message_id == email.message_id,
                Invite.provider_thread_id.is_(None),
            )
            .values(
                provider_thread_id=sent.provider_thread_id,
                mailbox_connection_id=self._connection_id,
            )
        )


# --- Opening a connection -------------------------------------------------------------


def _open_mailbox(
    row: MailboxConnection,
    tokens: TokenSet,
    *,
    settings: Settings,
    store: _ConnectionTokenStore,
    http: httpx.AsyncClient | None,
    threads: _InviteThreads | None = None,
) -> MailboxSender:
    provider = cast(Provider, row.provider)
    client = oauth_client(settings, provider)

    async def refresh(current: TokenSet) -> TokenSet:
        if http is not None:
            return await refresh_tokens(http, client, current)
        async with httpx.AsyncClient() as own:
            return await refresh_tokens(own, client, current)

    authorized = AuthorizedHttp(
        provider=provider, tokens=tokens, refresh=refresh, store=store, client=http
    )
    if provider == "google":
        return GmailMailbox(authorized, address=row.email_address, threads=threads)
    return GraphMailbox(authorized, address=row.email_address, threads=threads)


def _context(row: MailboxConnection) -> TokenContext:
    return TokenContext(
        tenant_id=row.tenant_id, user_id=row.user_id, provider=cast(Provider, row.provider)
    )


async def get_mailbox_sender(
    session: AsyncSession,
    tenant_id: UUID,
    user_id: UUID,
    *,
    provider: Provider | None = None,
    settings: Settings | None = None,
    cipher: TokenCipher | None = None,
    http: httpx.AsyncClient | None = None,
) -> MailboxSender:
    """An `EmailSender` for the user's active mailbox. Raises MailboxNotConnected.

    `session` is the caller's tenant-scoped session (RLS limits it to
    `tenant_id`); it must stay open until `send` has returned. With several
    active connections and no `provider`, the most recently updated one wins.
    """
    resolved = settings or get_settings()
    query = select(MailboxConnection).where(
        MailboxConnection.tenant_id == tenant_id,
        MailboxConnection.user_id == user_id,
        MailboxConnection.status == "active",
    )
    if provider is not None:
        query = query.where(MailboxConnection.provider == provider)
    row = await session.scalar(query.order_by(MailboxConnection.updated_at.desc()).limit(1))
    if row is None or row.token_ciphertext is None:
        raise MailboxNotConnected(f"user {user_id} has no active mailbox connection")

    used_cipher = cipher or build_token_cipher(resolved)
    context = _context(row)
    try:
        tokens = await decrypt_tokens(used_cipher, row.token_ciphertext, context)
    except TokenDecryptionError:
        logger.error("mailbox tokens do not decrypt tenant=%s connection=%s", tenant_id, row.id)
        raise MailboxNotConnected(f"user {user_id} must reconnect their mailbox") from None

    bind = session.bind
    store = _ConnectionTokenStore(
        tenant_id=tenant_id,
        connection_id=row.id,
        context=context,
        cipher=used_cipher,
        db=bind if isinstance(bind, AsyncEngine) else None,
        session=session,
    )
    return _open_mailbox(
        row,
        tokens,
        settings=resolved,
        store=store,
        http=http,
        threads=_InviteThreads(session, row.id),
    )


async def open_connection(
    db: Db,
    tenant_id: UUID,
    connection_id: UUID,
    *,
    settings: Settings,
    cipher: TokenCipher | None = None,
    http: httpx.AsyncClient | None = None,
) -> MailboxSender:
    """A mailbox for one active connection, outside any caller transaction.

    For background reads (the onboarding scan): refreshed tokens and a revoked
    status are written in their own transactions. Raises MailboxNotConnected
    when the connection is gone, inactive, or its tokens do not decrypt.
    """
    async with tenant_session(db, tenant_id) as session:
        row = await session.scalar(
            select(MailboxConnection).where(
                MailboxConnection.id == connection_id, MailboxConnection.status == "active"
            )
        )
    if row is None or row.token_ciphertext is None:
        raise MailboxNotConnected(f"connection {connection_id} is not active")
    used_cipher = cipher or build_token_cipher(settings)
    context = _context(row)
    try:
        tokens = await decrypt_tokens(used_cipher, row.token_ciphertext, context)
    except TokenDecryptionError:
        logger.error("mailbox tokens do not decrypt tenant=%s connection=%s", tenant_id, row.id)
        raise MailboxNotConnected(f"connection {connection_id} must be reconnected") from None
    store = _ConnectionTokenStore(
        tenant_id=tenant_id, connection_id=row.id, context=context, cipher=used_cipher, db=db
    )
    return _open_mailbox(row, tokens, settings=settings, store=store, http=http)


# --- Connect / list / disconnect ----------------------------------------------------------


async def user_is_member(db: Db, tenant_id: UUID, user_id: UUID) -> bool:
    async with tenant_session(db, tenant_id) as session:
        found = await session.scalar(
            select(Membership.id).where(
                Membership.tenant_id == tenant_id, Membership.user_id == user_id
            )
        )
        return found is not None


async def store_connection(
    db: Db,
    *,
    tenant_id: UUID,
    user_id: UUID,
    provider: Provider,
    email_address: str,
    tokens: TokenSet,
    cipher: TokenCipher,
) -> ConnectionView:
    """Create or replace the user's connection for `provider` (active, encrypted tokens).

    Reconnecting the same account keeps the sync state; connecting a
    different account resets it.
    """
    context = TokenContext(tenant_id=tenant_id, user_id=user_id, provider=provider)
    ciphertext = await encrypt_tokens(cipher, tokens, context)
    stmt = insert(MailboxConnection).values(
        tenant_id=tenant_id,
        user_id=user_id,
        provider=provider,
        email_address=email_address,
        token_ciphertext=ciphertext,
        scopes=list(tokens.scopes),
        status="active",
    )
    stmt = stmt.on_conflict_do_update(
        constraint=_UNIQUE,
        set_={
            "email_address": stmt.excluded.email_address,
            "token_ciphertext": stmt.excluded.token_ciphertext,
            "scopes": stmt.excluded.scopes,
            "status": "active",
            "sync_state": case(
                (
                    MailboxConnection.email_address == stmt.excluded.email_address,
                    MailboxConnection.sync_state,
                ),
                else_=text("'{}'::jsonb"),
            ),
            "updated_at": func.now(),
        },
    ).returning(MailboxConnection)
    async with tenant_session(db, tenant_id) as session:
        row = (await session.scalars(stmt)).one()
        view = _view(row)
    logger.info("mailbox connected tenant=%s user=%s provider=%s", tenant_id, user_id, provider)
    return view


async def list_connections(db: Db, tenant_id: UUID, user_id: UUID) -> list[ConnectionView]:
    async with tenant_session(db, tenant_id) as session:
        rows = await session.scalars(
            select(MailboxConnection)
            .where(MailboxConnection.tenant_id == tenant_id, MailboxConnection.user_id == user_id)
            .order_by(MailboxConnection.provider)
        )
        return [_view(r) for r in rows]


async def disconnect_mailbox(
    db: Db,
    tenant_id: UUID,
    user_id: UUID,
    provider: Provider,
    *,
    settings: Settings,
    cipher: TokenCipher | None = None,
    http: httpx.AsyncClient | None = None,
) -> bool:
    """Erase the tokens and mark the connection revoked; then revoke at the provider.

    Returns False if there was no connection. The local revocation commits
    first; the provider call is best effort (see `oauth.revoke`).
    """
    tokens: TokenSet | None = None
    async with tenant_session(db, tenant_id) as session:
        row = await session.scalar(
            select(MailboxConnection)
            .where(
                MailboxConnection.tenant_id == tenant_id,
                MailboxConnection.user_id == user_id,
                MailboxConnection.provider == provider,
            )
            .with_for_update()
        )
        if row is None:
            return False
        if row.token_ciphertext is not None:
            try:
                used = cipher or build_token_cipher(settings)
                tokens = await decrypt_tokens(used, row.token_ciphertext, _context(row))
            except Exception:  # noqa: BLE001 - erasing locally must not depend on decrypting
                tokens = None
        row.token_ciphertext = None
        row.status = "revoked"
        row.updated_at = datetime.now(UTC)
        await session.flush()
    logger.info("mailbox disconnected tenant=%s user=%s provider=%s", tenant_id, user_id, provider)

    if tokens is not None:
        try:
            client = oauth_client(settings, provider)
            if http is not None:
                await revoke(http, client, tokens)
            else:
                async with httpx.AsyncClient() as own:
                    await revoke(own, client, tokens)
        except Exception:  # noqa: BLE001 - best effort
            logger.warning("provider revocation failed provider=%s tenant=%s", provider, tenant_id)
    return True


# --- Sync --------------------------------------------------------------------------------


@dataclass(slots=True)
class SyncReport:
    connections: int = 0
    threads: int = 0
    ingested: int = 0
    skipped_own: int = 0
    failed: int = 0
    revoked: int = 0


@dataclass(frozen=True, slots=True)
class ThreadSyncResult:
    state: dict[str, JsonValue]
    ingested: int
    skipped_own: int
    failed: int


async def sync_thread(
    mailbox: MailboxSender,
    thread_id: str,
    state: ThreadState | None,
    *,
    tenant_id: UUID,
    invite_id: UUID,
    ingest: Ingest,
) -> ThreadSyncResult:
    """New messages of one thread: skip the buyer's own, ingest the rest, advance state."""
    fetch = await mailbox.fetch_thread_new_messages(thread_id, state, exclude_from=mailbox.address)
    handled = list(fetch.skipped_ids)
    ingested = 0
    skipped = len(fetch.skipped_ids)
    failed = 0
    for message in fetch.messages:
        if same_address(parse_inbound(message.raw).from_address, mailbox.address):
            skipped += 1
            handled.append(message.provider_message_id)
            continue
        try:
            await ingest(message.raw, tenant_id=tenant_id, invite_id=invite_id)
        except Exception:
            # Not marked handled: retried on the next run. Ids only in the log.
            logger.exception(
                "mailbox ingest failed tenant=%s invite=%s message=%s",
                tenant_id,
                invite_id,
                message.provider_message_id,
            )
            failed += 1
            continue
        ingested += 1
        handled.append(message.provider_message_id)
    return ThreadSyncResult(
        state=with_seen(state, handled), ingested=ingested, skipped_own=skipped, failed=failed
    )


@dataclass(frozen=True, slots=True)
class _Thread:
    invite_id: UUID
    thread_id: str


async def _load_sync_work(db: Db, tenant_id: UUID) -> list[tuple[MailboxConnection, list[_Thread]]]:
    async with tenant_session(db, tenant_id) as session:
        connections = list(
            await session.scalars(
                select(MailboxConnection)
                .where(
                    MailboxConnection.tenant_id == tenant_id,
                    MailboxConnection.status == "active",
                    MailboxConnection.token_ciphertext.is_not(None),
                )
                .order_by(MailboxConnection.id)
            )
        )
        if not connections:
            return []
        rows = await session.execute(
            select(Invite.id, Invite.provider_thread_id, Invite.mailbox_connection_id)
            .join(Rfq, Rfq.id == Invite.rfq_id)
            .where(
                Invite.mailbox_connection_id.in_([c.id for c in connections]),
                Invite.provider_thread_id.is_not(None),
                Invite.status.in_(SYNCED_INVITE_STATUSES),
                Rfq.status == "open",
            )
            .order_by(Invite.created_at, Invite.id)
        )
        by_connection: dict[UUID, list[_Thread]] = {c.id: [] for c in connections}
        for invite_id, thread_id, connection_id in rows:
            if thread_id is not None and connection_id is not None:
                by_connection[connection_id].append(_Thread(invite_id, thread_id))
        return [(c, by_connection[c.id]) for c in connections]


def _thread_states(sync_state: dict[str, object]) -> dict[str, dict[str, JsonValue]]:
    threads = sync_state.get("threads")
    if not isinstance(threads, dict):
        return {}
    return {
        str(k): cast(dict[str, JsonValue], v)
        for k, v in cast(dict[object, object], threads).items()
        if isinstance(v, dict)
    }


async def _save_sync_state(
    db: Db, tenant_id: UUID, connection_id: UUID, threads: dict[str, dict[str, JsonValue]]
) -> None:
    state: dict[str, JsonValue] = {
        "threads": cast(dict[str, JsonValue], threads),
        "synced_at": datetime.now(UTC).isoformat(),
    }
    async with tenant_session(db, tenant_id) as session:
        await session.execute(
            update(MailboxConnection)
            .where(MailboxConnection.id == connection_id)
            .values(sync_state=state, updated_at=func.now())
        )


async def sync_rfq_threads(
    db: Db,
    tenant_id: UUID,
    *,
    ingest: Ingest,
    settings: Settings | None = None,
    cipher: TokenCipher | None = None,
    http: httpx.AsyncClient | None = None,
) -> SyncReport:
    """Sync every RFQ thread of the tenant's active connections (see the module docstring)."""
    resolved = settings or get_settings()
    used_cipher = cipher or build_token_cipher(resolved)
    report = SyncReport()
    for row, threads in await _load_sync_work(db, tenant_id):
        report.connections += 1
        context = _context(row)
        store = _ConnectionTokenStore(
            tenant_id=tenant_id,
            connection_id=row.id,
            context=context,
            cipher=used_cipher,
            db=db,
        )
        if row.token_ciphertext is None:
            continue
        try:
            tokens = await decrypt_tokens(used_cipher, row.token_ciphertext, context)
        except TokenDecryptionError:
            logger.error("mailbox tokens do not decrypt tenant=%s connection=%s", tenant_id, row.id)
            await store.mark_error()
            report.failed += 1
            continue
        mailbox = _open_mailbox(row, tokens, settings=resolved, store=store, http=http)

        previous = _thread_states(cast(dict[str, object], row.sync_state or {}))
        # Only threads still being synced are kept: closed RFQs drop out.
        current: dict[str, dict[str, JsonValue]] = {
            t.thread_id: previous[t.thread_id] for t in threads if t.thread_id in previous
        }
        for thread in threads:
            try:
                result = await sync_thread(
                    mailbox,
                    thread.thread_id,
                    current.get(thread.thread_id),
                    tenant_id=tenant_id,
                    invite_id=thread.invite_id,
                    ingest=ingest,
                )
            except ConsentRevoked:
                # AuthorizedHttp already marked the connection revoked.
                report.revoked += 1
                break
            except ThreadNotFound:
                logger.warning(
                    "mailbox thread gone tenant=%s invite=%s", tenant_id, thread.invite_id
                )
                continue
            except ProviderError as exc:
                report.failed += 1
                logger.warning(
                    "mailbox sync failed tenant=%s connection=%s status=%s op=%s",
                    tenant_id,
                    row.id,
                    exc.status,
                    exc.operation,
                )
                if exc.status == 403:
                    # Permission withdrawn (e.g. an admin policy): needs reconnecting.
                    await store.mark_error()
                    break
                continue
            report.threads += 1
            report.ingested += result.ingested
            report.skipped_own += result.skipped_own
            report.failed += result.failed
            current[thread.thread_id] = result.state
            await _save_sync_state(db, tenant_id, row.id, current)
    logger.info(
        "mailbox sync tenant=%s connections=%s threads=%s ingested=%s skipped_own=%s "
        "failed=%s revoked=%s",
        tenant_id,
        report.connections,
        report.threads,
        report.ingested,
        report.skipped_own,
        report.failed,
        report.revoked,
    )
    return report


async def active_mailbox_tenants(db: Db) -> list[UUID]:
    """Tenants with an active connection, via the 0006 SECURITY DEFINER function."""
    async with _sessionmaker(db)() as session, session.begin():
        result = await session.execute(_ACTIVE_TENANTS)
        tenants: list[UUID] = []
        for (tenant_id,) in result:
            if not isinstance(tenant_id, UUID):
                raise TypeError("active_mailbox_tenants returned a non-uuid")
            tenants.append(tenant_id)
        return tenants


# --- The worker job --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MailboxSyncDeps:
    db: Db
    ingest: Ingest
    settings: Settings
    cipher: TokenCipher | None = None
    http: httpx.AsyncClient | None = field(default=None)


async def handle_mailbox_sync(envelope: Envelope, deps: MailboxSyncDeps) -> None:
    """`mailbox.sync`: ALL_TENANTS = every tenant with an active connection; else that one.

    For the scheduled all-tenants run, a tenant that fails is logged and the
    others go on (the next tick retries it). A single-tenant run re-raises so
    the queue retries it.
    """
    single = envelope.tenant_id != ALL_TENANTS
    tenants: Sequence[UUID] = (
        [envelope.tenant_id] if single else await active_mailbox_tenants(deps.db)
    )
    if not tenants:
        return
    cipher = deps.cipher or build_token_cipher(deps.settings)
    for tenant_id in tenants:
        try:
            await sync_rfq_threads(
                deps.db,
                tenant_id,
                ingest=deps.ingest,
                settings=deps.settings,
                cipher=cipher,
                http=deps.http,
            )
        except Exception:
            logger.exception(
                "mailbox sync failed tenant=%s key=%s", tenant_id, envelope.idempotency_key
            )
            if single:
                raise


class MailboxSyncHandler:
    """`handle_mailbox_sync` bound to its dependencies, for the worker's registry."""

    def __init__(self, deps: MailboxSyncDeps) -> None:
        self._deps = deps

    async def __call__(self, envelope: Envelope) -> None:
        await handle_mailbox_sync(envelope, self._deps)
