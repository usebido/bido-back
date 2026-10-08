"""Mailbox connections against a real Postgres: RLS, the tenant lookup, the sender
factory, thread binding, sync and the OAuth connect flow. Providers are MockTransport."""

# ruff: noqa: S105, S106 - fake OAuth tokens and keys, never real credentials

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.api import mailbox as mailbox_api
from bido_back.api.deps import ApiResources, get_db, get_optional_job_queue, get_principal
from bido_back.api.mailbox import get_mailbox_http, mailbox_router
from bido_back.auth import Principal
from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import Contact, Invite, MailboxConnection, Rfq, Supplier
from bido_back.email.types import OutboundEmail
from bido_back.mailbox.base import MailboxNotConnected, TokenSet, b64url_encode
from bido_back.mailbox.crypto import FernetTokenCipher, TokenContext, decrypt_tokens
from bido_back.mailbox.google import GmailMailbox
from bido_back.mailbox.oauth import STATE_TTL_S, OAuthState, StateSealer, new_code_verifier
from bido_back.mailbox.service import (
    active_mailbox_tenants,
    disconnect_mailbox,
    get_mailbox_sender,
    list_connections,
    store_connection,
    sync_rfq_threads,
)
from bido_back.rfqs.errors import MailboxNotConnected as RfqMailboxNotConnected
from bido_back.rfqs.mailbox import default_mailbox_sender

from ..approvals.conftest import FakeJobQueue
from ..db.conftest import TwoTenants
from .support import BUYER, FERNET_KEY, SUPPLIER, json_response, make_settings, raw_email

pytestmark = pytest.mark.db

CIPHER = FernetTokenCipher(FERNET_KEY)
SETTINGS = make_settings()
GMAIL_SCOPES = (
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
)


def _tokens(access: str = "access-1") -> TokenSet:
    return TokenSet(access_token=access, refresh_token="refresh-1", scopes=GMAIL_SCOPES)


async def _connect(
    engine: AsyncEngine, tenant_id: UUID, user_id: UUID, *, address: str = BUYER
) -> None:
    await store_connection(
        engine,
        tenant_id=tenant_id,
        user_id=user_id,
        provider="google",
        email_address=address,
        tokens=_tokens(),
        cipher=CIPHER,
    )


@dataclass(frozen=True)
class RfqFixture:
    tenant_id: UUID
    user_id: UUID
    open_invite: UUID
    closed_invite: UUID


async def _cleanup(migrator: AsyncEngine, tenant_ids: list[UUID]) -> None:
    for tenant_id in tenant_ids:
        async with tenant_session(migrator, tenant_id) as session:
            await session.execute(delete(Rfq).where(Rfq.tenant_id == tenant_id))
            await session.execute(delete(Supplier).where(Supplier.tenant_id == tenant_id))
            await session.execute(delete(Contact).where(Contact.tenant_id == tenant_id))
            await session.execute(
                delete(MailboxConnection).where(MailboxConnection.tenant_id == tenant_id)
            )


@pytest_asyncio.fixture
async def tenants(
    two_tenants: TwoTenants, migrator_engine: AsyncEngine
) -> AsyncIterator[TwoTenants]:
    try:
        yield two_tenants
    finally:
        await _cleanup(migrator_engine, [two_tenants.a.tenant_id, two_tenants.b.tenant_id])


async def _invite(
    session: AsyncSession, tenant_id: UUID, user_id: UUID, *, rfq_status: str, n: int
) -> UUID:
    rfq = Rfq(
        tenant_id=tenant_id,
        ref=f"RFQ-{n}-{uuid4().hex[:6]}",
        title="Bolts",
        status=rfq_status,
        created_by=user_id,
    )
    supplier = Supplier(tenant_id=tenant_id, name=f"Supplier {n}")
    contact = Contact(tenant_id=tenant_id, kind="supplier", email=SUPPLIER)
    session.add_all([rfq, supplier, contact])
    await session.flush()
    invite = Invite(
        tenant_id=tenant_id,
        rfq_id=rfq.id,
        supplier_id=supplier.id,
        contact_id=contact.id,
        reply_token=uuid4().hex[:12],
        status="pending_approval",
        thread_message_id=f"<rfq-{n}-{uuid4().hex}@quotes.usebido.com>",
    )
    session.add(invite)
    await session.flush()
    return invite.id


@pytest_asyncio.fixture
async def rfq(app_engine: AsyncEngine, tenants: TwoTenants) -> RfqFixture:
    a = tenants.a
    async with tenant_session(app_engine, a.tenant_id) as session:
        open_invite = await _invite(session, a.tenant_id, a.user_id, rfq_status="open", n=1)
        closed_invite = await _invite(session, a.tenant_id, a.user_id, rfq_status="closed", n=2)
    return RfqFixture(a.tenant_id, a.user_id, open_invite, closed_invite)


class FakeGmail:
    """Just enough of Gmail: send (new thread or given threadId), threads.get, messages.get."""

    def __init__(self) -> None:
        self.threads: dict[str, list[tuple[str, bytes]]] = {}
        self.requests: list[httpx.Request] = []
        self.valid_tokens = {"access-1"}
        self.refresh_revoked = False
        self._n = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "oauth2.googleapis.com":
            if self.refresh_revoked:
                return json_response(400, {"error": "invalid_grant"})
            self.valid_tokens.add("access-2")
            return json_response(200, {"access_token": "access-2", "expires_in": 3600})
        if (
            request.headers.get("Authorization", "").removeprefix("Bearer ")
            not in self.valid_tokens
        ):
            return httpx.Response(401)
        if path.endswith("/messages/send"):
            body = json.loads(request.content)
            self._n += 1
            thread = body.get("threadId") or f"thread{self._n}"
            message_id = f"sent{self._n}"
            self.threads.setdefault(thread, []).append((message_id, b""))
            return json_response(200, {"id": message_id, "threadId": thread})
        if "/threads/" in path:
            thread = path.rsplit("/", 1)[1]
            if thread not in self.threads:
                return httpx.Response(404)
            return json_response(200, {"messages": [{"id": m} for m, _ in self.threads[thread]]})
        if "/messages/" in path:
            message_id = path.rsplit("/", 1)[1]
            for messages in self.threads.values():
                for mid, raw in messages:
                    if mid == message_id:
                        if request.url.params.get("format") == "metadata":
                            return json_response(200, {"id": mid, "payload": {"headers": []}})
                        return json_response(200, {"raw": b64url_encode(raw)})
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


# --- RLS and the lookup ----------------------------------------------------------------


async def test_connections_are_tenant_isolated(
    app_engine: AsyncEngine, tenants: TwoTenants
) -> None:
    a, b = tenants.a, tenants.b
    await _connect(app_engine, a.tenant_id, a.user_id)

    async with tenant_session(app_engine, b.tenant_id) as session:
        assert (await session.scalars(select(MailboxConnection))).all() == []
    async with app_engine.connect() as conn:
        assert await conn.scalar(text("SELECT count(*) FROM mailbox_connections")) == 0
        # The lookup flag means nothing to the app role (the policy is not for it).
        await conn.execute(text("SELECT set_config('app.mailbox_lookup', 'on', true)"))
        assert await conn.scalar(text("SELECT count(*) FROM mailbox_connections")) == 0
    async with tenant_session(app_engine, a.tenant_id) as session:
        rows = (await session.scalars(select(MailboxConnection))).all()
        assert [r.user_id for r in rows] == [a.user_id]
        assert rows[0].token_ciphertext is not None
        assert b"access-1" not in rows[0].token_ciphertext


async def test_cannot_write_another_tenants_connection(
    app_engine: AsyncEngine, tenants: TwoTenants
) -> None:
    a, b = tenants.a, tenants.b
    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_session(app_engine, a.tenant_id) as session:
            session.add(
                MailboxConnection(
                    tenant_id=b.tenant_id,
                    user_id=b.user_id,
                    provider="google",
                    email_address=BUYER,
                    token_ciphertext=b"x",
                )
            )
            await session.flush()


async def test_active_connection_needs_tokens(app_engine: AsyncEngine, tenants: TwoTenants) -> None:
    a = tenants.a
    with pytest.raises(IntegrityError):
        async with tenant_session(app_engine, a.tenant_id) as session:
            session.add(
                MailboxConnection(
                    tenant_id=a.tenant_id,
                    user_id=a.user_id,
                    provider="google",
                    email_address=BUYER,
                    status="active",
                    token_ciphertext=None,
                )
            )
            await session.flush()


async def test_active_mailbox_tenants_returns_only_active_tenant_ids(
    app_engine: AsyncEngine, tenants: TwoTenants
) -> None:
    a, b = tenants.a, tenants.b
    await _connect(app_engine, a.tenant_id, a.user_id)
    await _connect(app_engine, b.tenant_id, b.user_id)
    await disconnect_mailbox(
        app_engine,
        b.tenant_id,
        b.user_id,
        "google",
        settings=SETTINGS,
        cipher=CIPHER,
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
    )

    found = await active_mailbox_tenants(app_engine)

    assert a.tenant_id in found
    assert b.tenant_id not in found
    async with app_engine.connect() as conn:
        columns = (await conn.execute(text("SELECT * FROM active_mailbox_tenants()"))).keys()
        assert list(columns) == ["tenant_id"]


# --- Connect / list / disconnect ------------------------------------------------------------


async def test_store_list_reconnect_and_disconnect(
    app_engine: AsyncEngine, tenants: TwoTenants
) -> None:
    a = tenants.a
    await _connect(app_engine, a.tenant_id, a.user_id)
    async with tenant_session(app_engine, a.tenant_id) as session:
        await session.execute(update(MailboxConnection).values(sync_state={"threads": {"t": {}}}))
    await _connect(app_engine, a.tenant_id, a.user_id)  # same account: state kept
    async with tenant_session(app_engine, a.tenant_id) as session:
        row = (await session.scalars(select(MailboxConnection))).one()
        assert row.sync_state == {"threads": {"t": {}}}
        context = TokenContext(a.tenant_id, a.user_id, "google")
        assert row.token_ciphertext is not None
        assert await decrypt_tokens(CIPHER, row.token_ciphertext, context) == _tokens()
    await _connect(app_engine, a.tenant_id, a.user_id, address="other@acme.example")
    views = await list_connections(app_engine, a.tenant_id, a.user_id)
    assert [(v.provider, v.email_address, v.status) for v in views] == [
        ("google", "other@acme.example", "active")
    ]

    revoked: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        revoked.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        assert await disconnect_mailbox(
            app_engine,
            a.tenant_id,
            a.user_id,
            "google",
            settings=SETTINGS,
            cipher=CIPHER,
            http=http,
        )
    async with tenant_session(app_engine, a.tenant_id) as session:
        row = (await session.scalars(select(MailboxConnection))).one()
        assert row.status == "revoked"
        assert row.token_ciphertext is None
        assert row.sync_state == {}
    assert [str(r.url) for r in revoked] == ["https://oauth2.googleapis.com/revoke"]


# --- Sender factory ------------------------------------------------------------------------


async def test_sender_factory_requires_a_connection(
    app_engine: AsyncEngine, tenants: TwoTenants
) -> None:
    a = tenants.a
    async with tenant_session(app_engine, a.tenant_id) as session:
        with pytest.raises(MailboxNotConnected):
            await get_mailbox_sender(session, a.tenant_id, a.user_id, settings=SETTINGS)
        # The RFQ flow's seam translates it into its own error.
        with pytest.raises(RfqMailboxNotConnected):
            await default_mailbox_sender(session, a.tenant_id, a.user_id)


async def test_sender_factory_sees_only_its_tenant(
    app_engine: AsyncEngine, tenants: TwoTenants
) -> None:
    a, b = tenants.a, tenants.b
    await _connect(app_engine, a.tenant_id, a.user_id)
    async with tenant_session(app_engine, b.tenant_id) as session:
        with pytest.raises(MailboxNotConnected):
            await get_mailbox_sender(session, a.tenant_id, a.user_id, settings=SETTINGS)


async def test_send_binds_the_thread_and_follow_up_reuses_it(
    app_engine: AsyncEngine, rfq: RfqFixture
) -> None:
    await _connect(app_engine, rfq.tenant_id, rfq.user_id)
    gmail = FakeGmail()
    async with tenant_session(app_engine, rfq.tenant_id) as session:
        invite = await session.get(Invite, rfq.open_invite)
        assert invite is not None
        thread_message_id = invite.thread_message_id
    first = OutboundEmail(
        from_name="Acme via Bido",
        from_address="rfq-x@quotes.usebido.com",
        to=(SUPPLIER,),
        subject="RFQ",
        text_body="Please quote.",
        message_id=thread_message_id,
    )

    async with gmail.client() as http, tenant_session(app_engine, rfq.tenant_id) as session:
        sender = await get_mailbox_sender(
            session, rfq.tenant_id, rfq.user_id, settings=SETTINGS, cipher=CIPHER, http=http
        )
        assert isinstance(sender, GmailMailbox)
        sent = await sender.send(first)
        follow_up = OutboundEmail(
            from_name="Acme via Bido",
            from_address="rfq-x@quotes.usebido.com",
            to=(SUPPLIER,),
            subject="Re: RFQ",
            text_body="Any news?",
            message_id="<f1@quotes.usebido.com>",
            in_reply_to=thread_message_id,
        )
        again = await sender.send(follow_up)

    async with tenant_session(app_engine, rfq.tenant_id) as session:
        invite = await session.get(Invite, rfq.open_invite)
        assert invite is not None
        assert invite.provider_thread_id == sent.provider_thread_id == "thread1"
        assert invite.mailbox_connection_id is not None
    sends = [json.loads(r.content) for r in gmail.requests if r.url.path.endswith("/send")]
    assert "threadId" not in sends[0]
    assert sends[1]["threadId"] == "thread1"
    assert again.provider_thread_id == "thread1"


async def test_refreshed_token_survives_a_rolled_back_caller(
    app_engine: AsyncEngine, rfq: RfqFixture
) -> None:
    await _connect(app_engine, rfq.tenant_id, rfq.user_id)
    gmail = FakeGmail()
    gmail.valid_tokens = set()  # access-1 is expired at the provider
    email = OutboundEmail(
        from_name="Acme",
        from_address="x@quotes.usebido.com",
        to=(SUPPLIER,),
        subject="RFQ",
        text_body="b",
        message_id="<unbound@quotes.usebido.com>",
    )

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        async with gmail.client() as http, tenant_session(app_engine, rfq.tenant_id) as session:
            sender = await get_mailbox_sender(
                session, rfq.tenant_id, rfq.user_id, settings=SETTINGS, cipher=CIPHER, http=http
            )
            await sender.send(email)
            raise Boom

    async with tenant_session(app_engine, rfq.tenant_id) as session:
        row = (await session.scalars(select(MailboxConnection))).one()
        assert row.token_ciphertext is not None
        tokens = await decrypt_tokens(
            CIPHER, row.token_ciphertext, TokenContext(rfq.tenant_id, rfq.user_id, "google")
        )
    assert tokens.access_token == "access-2"


# --- Sync -------------------------------------------------------------------------------------


async def _bind_thread(engine: AsyncEngine, tenant_id: UUID, invite_id: UUID, thread: str) -> None:
    async with tenant_session(engine, tenant_id) as session:
        connection = (await session.scalars(select(MailboxConnection))).one()
        invite = await session.get(Invite, invite_id)
        assert invite is not None
        invite.provider_thread_id = thread
        invite.mailbox_connection_id = connection.id
        invite.status = "sent"
        await session.flush()


async def test_sync_ingests_supplier_replies_of_open_rfqs_only(
    app_engine: AsyncEngine, rfq: RfqFixture
) -> None:
    await _connect(app_engine, rfq.tenant_id, rfq.user_id)
    await _bind_thread(app_engine, rfq.tenant_id, rfq.open_invite, "threadopen")
    await _bind_thread(app_engine, rfq.tenant_id, rfq.closed_invite, "threadclosed")
    reply = raw_email(from_address=SUPPLIER, to=BUYER, message_id="<reply@s.example>")
    own = raw_email(from_address=BUYER, to=SUPPLIER, message_id="<own@acme.example>")
    gmail = FakeGmail()
    gmail.threads["threadopen"] = [("own1", own), ("reply1", reply)]
    gmail.threads["threadclosed"] = [("late1", reply)]
    gmail.threads["unrelated"] = [("secret1", reply)]
    calls: list[tuple[bytes, UUID, UUID]] = []

    async def ingest(raw: bytes, *, tenant_id: UUID, invite_id: UUID) -> object:
        calls.append((raw, tenant_id, invite_id))
        return None

    async with gmail.client() as http:
        report = await sync_rfq_threads(
            app_engine, rfq.tenant_id, ingest=ingest, settings=SETTINGS, cipher=CIPHER, http=http
        )
        again = await sync_rfq_threads(
            app_engine, rfq.tenant_id, ingest=ingest, settings=SETTINGS, cipher=CIPHER, http=http
        )

    assert calls == [(reply, rfq.tenant_id, rfq.open_invite)]
    assert (report.threads, report.ingested, report.skipped_own) == (1, 1, 1)
    assert again.ingested == 0
    touched = {r.url.path.rsplit("/", 1)[1] for r in gmail.requests}
    assert touched == {"threadopen", "own1", "reply1"}
    async with tenant_session(app_engine, rfq.tenant_id) as session:
        row = (await session.scalars(select(MailboxConnection))).one()
        assert row.sync_state["threads"] == {"threadopen": {"seen": ["own1", "reply1"]}}


async def test_sync_marks_revoked_consent(app_engine: AsyncEngine, rfq: RfqFixture) -> None:
    await _connect(app_engine, rfq.tenant_id, rfq.user_id)
    await _bind_thread(app_engine, rfq.tenant_id, rfq.open_invite, "threadopen")
    gmail = FakeGmail()
    gmail.valid_tokens = set()
    gmail.refresh_revoked = True

    async def ingest(raw: bytes, *, tenant_id: UUID, invite_id: UUID) -> object:
        raise AssertionError("nothing to ingest")

    async with gmail.client() as http:
        report = await sync_rfq_threads(
            app_engine, rfq.tenant_id, ingest=ingest, settings=SETTINGS, cipher=CIPHER, http=http
        )

    assert report.revoked == 1
    async with tenant_session(app_engine, rfq.tenant_id) as session:
        row = (await session.scalars(select(MailboxConnection))).one()
        assert row.status == "revoked"
        assert row.token_ciphertext is None
    assert rfq.tenant_id not in await active_mailbox_tenants(app_engine)


# --- The API ---------------------------------------------------------------------------------


@dataclass
class Api:
    http: httpx.AsyncClient
    # The signed-in caller; tests switch it to act as another user.
    principal: Principal
    tenants: TwoTenants
    app: FastAPI


@pytest_asyncio.fixture
async def api(app_engine: AsyncEngine, tenants: TwoTenants) -> AsyncIterator[Api]:
    app = FastAPI()
    app.state.resources = ApiResources(make_settings())
    app.include_router(mailbox_router())
    sessionmaker: async_sessionmaker[AsyncSession] = create_sessionmaker(app_engine)

    def provider(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            form = parse_qs(request.content.decode())
            assert form["code"] == ["the-code"]
            assert len(form["code_verifier"][0]) >= 43
            return json_response(
                200,
                {
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                    "expires_in": 3600,
                    "scope": " ".join(GMAIL_SCOPES),
                },
            )
        if request.url.path.endswith("/profile"):
            return json_response(200, {"emailAddress": BUYER})
        return httpx.Response(200)

    async def provider_http() -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            yield client

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        state = Api(
            http=http,
            principal=Principal(tenant_id=tenants.a.tenant_id, user_id=tenants.a.user_id),
            tenants=tenants,
            app=app,
        )
        app.dependency_overrides[get_db] = lambda: sessionmaker
        app.dependency_overrides[get_principal] = lambda: state.principal
        app.dependency_overrides[get_mailbox_http] = provider_http
        yield state


def _state_of(connect: httpx.Response) -> str:
    url: object = connect.json()["authorization_url"]
    assert isinstance(url, str)
    return parse_qs(urlsplit(url).query)["state"][0]


def _fragment(redirect: httpx.Response) -> dict[str, str]:
    location = redirect.headers["location"]
    assert location.startswith("https://app.bido.example/settings/mailbox#")
    return {k: v[0] for k, v in parse_qs(urlsplit(location).fragment).items()}


async def test_callback_only_forwards_code_and_state_to_the_frontend(api: Api) -> None:
    state = _state_of(await api.http.get("/v1/mailbox/connect/google"))

    callback = await api.http.get(
        "/v1/mailbox/callback/google", params={"code": "the-code", "state": state}
    )
    declined = await api.http.get(
        "/v1/mailbox/callback/google",
        params={"error": "access_denied", "error_description": "<script>", "state": state},
    )

    assert callback.status_code == 303
    assert callback.headers["cache-control"] == "no-store"
    assert _fragment(callback) == {"provider": "google", "code": "the-code", "state": state}
    assert _fragment(declined) == {"provider": "google", "error": "declined"}
    # Nothing was linked by the redirect itself.
    assert (await api.http.get("/v1/mailbox")).json()["connections"] == []


async def test_connect_complete_status_and_delete(api: Api) -> None:
    connect = await api.http.get("/v1/mailbox/connect/google")
    assert connect.status_code == 200
    assert connect.headers["cache-control"] == "no-store"
    state = _state_of(connect)

    def complete(provider: str, state: str) -> dict[str, str]:
        return {"provider": provider, "code": "the-code", "state": state}

    tampered = await api.http.post(
        "/v1/mailbox/complete", json=complete("google", state[:-4] + "AAAA")
    )
    other_provider = await api.http.post("/v1/mailbox/complete", json=complete("microsoft", state))
    assert [r.status_code for r in (tampered, other_provider)] == [400, 400]

    completed = await api.http.post("/v1/mailbox/complete", json=complete("google", state))
    assert completed.status_code == 200, completed.text
    body = completed.json()
    assert body["provider"] == "google"
    assert body["email_address"] == BUYER
    assert body["status"] == "active"
    assert "access-1" not in completed.text

    status_response = await api.http.get("/v1/mailbox")
    assert [c["email_address"] for c in status_response.json()["connections"]] == [BUYER]
    assert "token" not in status_response.text

    deleted = await api.http.delete("/v1/mailbox/google")
    assert deleted.status_code == 204
    missing = await api.http.delete("/v1/mailbox/microsoft")
    assert missing.status_code == 404
    after = await api.http.get("/v1/mailbox")
    assert after.json()["connections"][0]["status"] == "revoked"


async def test_complete_releases_mandate_emails_waiting_for_the_mailbox(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = FakeJobQueue()
    released: list[UUID] = []

    async def release(db: object, tenant_id: UUID, *, jobs: object) -> None:
        released.append(tenant_id)
        raise RuntimeError("best effort: the mailbox sync releases them too")

    monkeypatch.setattr(mailbox_api, "auto_approve_pending", release)
    api.app.dependency_overrides[get_optional_job_queue] = lambda: jobs
    state = _state_of(await api.http.get("/v1/mailbox/connect/google"))

    completed = await api.http.post(
        "/v1/mailbox/complete", json={"provider": "google", "code": "the-code", "state": state}
    )

    assert completed.status_code == 200, completed.text
    assert released == [api.tenants.a.tenant_id]


async def test_complete_refuses_a_user_other_than_the_one_who_started(api: Api) -> None:
    state = _state_of(await api.http.get("/v1/mailbox/connect/google"))

    # Another tenant's user, and the same tenant's id with another user id.
    api.principal = Principal(tenant_id=api.tenants.b.tenant_id, user_id=api.tenants.b.user_id)
    other_tenant = await api.http.post(
        "/v1/mailbox/complete", json={"provider": "google", "code": "the-code", "state": state}
    )
    api.principal = Principal(tenant_id=api.tenants.a.tenant_id, user_id=api.tenants.b.user_id)
    other_user = await api.http.post(
        "/v1/mailbox/complete", json={"provider": "google", "code": "the-code", "state": state}
    )

    assert [other_tenant.status_code, other_user.status_code] == [403, 403]
    api.principal = Principal(tenant_id=api.tenants.a.tenant_id, user_id=api.tenants.a.user_id)
    assert (await api.http.get("/v1/mailbox")).json()["connections"] == []


async def test_complete_refuses_an_expired_state(api: Api) -> None:
    old = StateSealer(FERNET_KEY, clock=lambda: time.time() - STATE_TTL_S - 60).seal(
        OAuthState(
            tenant_id=api.principal.tenant_id,
            user_id=api.principal.user_id,
            provider="google",
            code_verifier=new_code_verifier(),
        )
    )

    expired = await api.http.post(
        "/v1/mailbox/complete", json={"provider": "google", "code": "the-code", "state": old}
    )

    assert expired.status_code == 400
    assert "expired" in expired.json()["detail"]


async def test_complete_refuses_a_user_no_longer_in_the_tenant(
    api: Api, migrator_engine: AsyncEngine
) -> None:
    state = _state_of(await api.http.get("/v1/mailbox/connect/google"))
    async with tenant_session(migrator_engine, api.principal.tenant_id) as session:
        await session.execute(
            text("DELETE FROM memberships WHERE user_id = :u"), {"u": api.principal.user_id}
        )

    completed = await api.http.post(
        "/v1/mailbox/complete", json={"provider": "google", "code": "the-code", "state": state}
    )
    assert completed.status_code == 403
