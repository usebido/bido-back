"""POST /v1/copilot/messages over HTTP: the SSE event stream, against a scripted model.

The model is the adapter's real Agno OpenAI model with a stub client (no
network); the database is real; the RFQ agents and the mailbox are fakes.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.config import RunnerConfig
from bido_back.api.copilot import copilot_router, get_copilot_runner_config
from bido_back.api.deps import (
    ApiResources,
    get_db,
    get_mailbox_senders,
    get_principal,
    get_rfq_agents,
    get_storage,
)
from bido_back.auth import Principal
from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import Approval
from bido_back.rfqs.agents import RfqAgents
from bido_back.rfqs.mailbox import MailboxSenderFactory
from bido_back.settings import Settings
from bido_back.storage import BlobStorage
from tests.agents.test_runner import Script, final, reply, script, tool_use
from tests.approvals.conftest import People
from tests.db.conftest import agno_engine
from tests.onboarding.conftest import FakeStorage
from tests.rfqs.conftest import FakeMailboxes, fake_agents

from .conftest import buyer_of, make_rfq, supplier

__all__ = ["agno_engine", "script"]

UNUSED_DB = "postgresql+asyncpg://app@localhost:5432/unused"
CONFIG = RunnerConfig(openai_api_key="test-not-a-real-key")


def build_app() -> FastAPI:
    app = FastAPI()
    app.state.resources = ApiResources(
        Settings.model_validate({"environment": "test", "database_url": UNUSED_DB})
    )
    app.include_router(copilot_router())
    return app


def parse_sse(body: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        data = json.loads(lines["data"])
        assert data["type"] == lines["event"]
        events.append(data)
    return events


@dataclass
class Api:
    http: httpx.AsyncClient
    mailboxes: FakeMailboxes
    principal: Principal
    runner_config: RunnerConfig = CONFIG


@pytest_asyncio.fixture
async def api(app_engine: AsyncEngine, copilot_rows: People) -> AsyncIterator[Api]:
    app = build_app()
    sessionmaker = create_sessionmaker(app_engine)
    agents = fake_agents()
    mailboxes = FakeMailboxes()
    storage = FakeStorage()
    principal = buyer_of(copilot_rows)

    def db() -> async_sessionmaker[AsyncSession]:
        return sessionmaker

    def rfq_agents() -> RfqAgents:
        return agents

    def mailbox_senders() -> MailboxSenderFactory:
        return mailboxes

    def blobs() -> BlobStorage:
        return storage

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        state = Api(http=http, mailboxes=mailboxes, principal=principal)

        def current_principal() -> Principal:
            return state.principal

        def runner_config() -> RunnerConfig:
            return state.runner_config

        app.dependency_overrides[get_principal] = current_principal
        app.dependency_overrides[get_db] = db
        app.dependency_overrides[get_rfq_agents] = rfq_agents
        app.dependency_overrides[get_mailbox_senders] = mailbox_senders
        app.dependency_overrides[get_storage] = blobs
        app.dependency_overrides[get_copilot_runner_config] = runner_config
        yield state


async def test_requires_authentication() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post("/v1/copilot/messages", json={"message": "hi"})
    assert response.status_code == 401


@pytest.mark.db
async def test_a_lookup_streams_in_order(
    api: Api, app_engine: AsyncEngine, copilot_rows: People, script: Script
) -> None:
    rfq = await make_rfq(app_engine, api.principal)
    stub = script(
        reply(tool_use("fc_1", "list_rfqs", limit=5)),
        final(answer=f"You have one RFQ: {rfq.ref}."),
    )

    response = await api.http.post("/v1/copilot/messages", json={"message": "my RFQs?"})

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    assert [e["type"] for e in events] == [
        "conversation",
        "tool_called",
        "tool_result",
        "answer",
        "done",
    ]
    assert events[2]["output"]["rfqs"][0]["ref"] == rfq.ref
    assert events[3]["text"] == "You have one RFQ: RFQ-1."
    assert events[4]["ok"] is True and events[4]["actions"] == []
    assert events[4]["conversation_id"] == events[0]["conversation_id"]
    # The model saw the buyer's message as our input contract.
    first_input = json.dumps(stub.calls[0]["input"])
    assert "my RFQs?" in first_input


@pytest.mark.db
async def test_preparing_a_send_creates_approvals_not_emails(
    api: Api, app_engine: AsyncEngine, copilot_rows: People, script: Script
) -> None:
    rfq = await make_rfq(app_engine, api.principal)
    rhein = await supplier(app_engine, api.principal, "Rhein")
    script(
        reply(tool_use("fc_1", "invite_suppliers", rfq=rfq.ref, supplier_ids=[str(rhein.id)])),
        reply(tool_use("fc_2", "prepare_send", rfq=rfq.ref)),
        final(answer="The email to Rhein is waiting for an approver."),
    )

    response = await api.http.post(
        "/v1/copilot/messages",
        json={"message": "invite Rhein and send it", "conversation_id": str(uuid4())},
    )

    events = parse_sse(response.text)
    done = events[-1]
    assert done["type"] == "done" and done["ok"] is True
    assert [a["tool"] for a in done["actions"]] == ["invite_suppliers", "prepare_send"]
    assert [e["kind"] for e in events if e["type"] == "artifact"] == ["approvals"]
    assert "approval_requested" not in {e["type"] for e in events}
    assert api.mailboxes.sender.sent == []
    async with tenant_session(app_engine, api.principal.tenant_id) as session:
        statuses = list(await session.scalars(select(Approval.status)))
    assert statuses == ["pending"]


@pytest.mark.db
async def test_a_failed_run_still_ends_with_done(
    api: Api, copilot_rows: People, script: Script
) -> None:
    script(RuntimeError("provider down"))

    response = await api.http.post("/v1/copilot/messages", json={"message": "hi"})

    events = parse_sse(response.text)
    assert [e["type"] for e in events] == ["conversation", "run_failed", "done"]
    assert events[-1]["ok"] is False


@pytest.mark.db
async def test_unknown_attachment_is_404_before_streaming(api: Api, copilot_rows: People) -> None:
    response = await api.http.post(
        "/v1/copilot/messages", json={"message": "read this", "attachment_ids": [str(uuid4())]}
    )
    assert response.status_code == 404


@pytest.mark.db
async def test_a_conversation_keeps_its_history_for_its_owner_only(
    api: Api, agno_engine: AsyncEngine, copilot_rows: People, script: Script
) -> None:
    api.runner_config = RunnerConfig(
        openai_api_key="test-not-a-real-key",
        agno_database_url=agno_engine.url.render_as_string(hide_password=False),
    )
    conversation = str(uuid4())
    stub = script(final(answer="Olá! Como posso ajudar?"), final(answer="Você perguntou 'oi'."))

    for message in ("oi", "o que eu perguntei?"):
        response = await api.http.post(
            "/v1/copilot/messages", json={"message": message, "conversation_id": conversation}
        )
        assert parse_sse(response.text)[-1]["ok"] is True

    # The second request replays the first turn from Agno's storage.
    assert '\\"message\\":\\"oi\\"' in json.dumps(stub.calls[1]["input"])
    history = await api.http.get(f"/v1/copilot/conversations/{conversation}")
    assert history.status_code == 200, history.text
    turns = history.json()["turns"]
    assert [(t["message"], t["answer"]) for t in turns] == [
        ("oi", "Olá! Como posso ajudar?"),
        ("o que eu perguntei?", "Você perguntou 'oi'."),
    ]

    # Same tenant, another user: not theirs.
    api.principal = Principal(
        tenant_id=copilot_rows.tenant, user_id=copilot_rows.member, role="member"
    )
    assert (await api.http.get(f"/v1/copilot/conversations/{conversation}")).status_code == 404


@pytest.mark.db
async def test_conversations_are_listed_for_their_owner_newest_first(
    api: Api, copilot_rows: People, script: Script
) -> None:
    first, second = str(uuid4()), str(uuid4())
    script(
        final(answer="one"),
        final(answer="two"),
        final(answer="three"),
    )
    long_message = "preciso de uma análise de compra pro motor EV-120 " * 4
    for conversation, message in (
        (first, long_message),
        (second, "segunda conversa"),
        (first, "continuando a primeira"),
    ):
        response = await api.http.post(
            "/v1/copilot/messages", json={"message": message, "conversation_id": conversation}
        )
        assert parse_sse(response.text)[-1]["type"] == "done"

    listed = await api.http.get("/v1/copilot/conversations")

    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    # The first conversation had the latest activity; its title is its first message.
    assert [i["conversation_id"] for i in items] == [first, second]
    assert [i["turns"] for i in items] == [2, 1]
    assert items[0]["title"].startswith("preciso de uma análise de compra")
    assert len(items[0]["title"]) <= 80 and items[0]["title"].endswith("…")
    assert items[1]["title"] == "segunda conversa"
    assert listed.json()["next_before"] is None


@pytest.mark.db
async def test_conversation_list_pages_with_before(
    api: Api, copilot_rows: People, script: Script
) -> None:
    script(*(final(answer=str(n)) for n in range(3)))
    ids = [str(uuid4()) for _ in range(3)]
    for conversation in ids:
        await api.http.post(
            "/v1/copilot/messages", json={"message": "oi", "conversation_id": conversation}
        )

    page_one = (await api.http.get("/v1/copilot/conversations", params={"limit": 2})).json()
    assert [i["conversation_id"] for i in page_one["items"]] == [ids[2], ids[1]]
    assert page_one["next_before"] is not None
    page_two = (
        await api.http.get(
            "/v1/copilot/conversations", params={"limit": 2, "before": page_one["next_before"]}
        )
    ).json()
    assert [i["conversation_id"] for i in page_two["items"]] == [ids[0]]
    assert page_two["next_before"] is None


@pytest.mark.db
async def test_conversations_are_private_to_user_and_tenant(
    api: Api, copilot_rows: People, script: Script
) -> None:
    conversation = str(uuid4())
    script(final(answer="ok"), final(answer="ok"))
    await api.http.post(
        "/v1/copilot/messages", json={"message": "minha", "conversation_id": conversation}
    )

    # Same tenant, another user: sees nothing, and reusing the id starts their own row.
    api.principal = Principal(
        tenant_id=copilot_rows.tenant, user_id=copilot_rows.member, role="member"
    )
    assert (await api.http.get("/v1/copilot/conversations")).json()["items"] == []
    await api.http.post(
        "/v1/copilot/messages", json={"message": "do outro", "conversation_id": conversation}
    )
    mine = (await api.http.get("/v1/copilot/conversations")).json()["items"]
    assert [(i["title"], i["turns"]) for i in mine] == [("do outro", 1)]

    # Another tenant sees nothing.
    api.principal = Principal(
        tenant_id=copilot_rows.other_tenant, user_id=copilot_rows.other_approver, role="approver"
    )
    assert (await api.http.get("/v1/copilot/conversations")).json()["items"] == []

    # The first owner still has their row, untouched by the other user.
    api.principal = buyer_of(copilot_rows)
    owner = (await api.http.get("/v1/copilot/conversations")).json()["items"]
    assert [(i["title"], i["turns"]) for i in owner] == [("minha", 1)]


async def test_listing_requires_authentication() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/v1/copilot/conversations")
    assert response.status_code == 401


def test_conversation_title_collapses_and_truncates() -> None:
    from datetime import date

    from bido_back.copilot.contracts import CopilotInput
    from bido_back.copilot.service import TITLE_CHARS, conversation_title

    today = date(2026, 10, 7)
    assert conversation_title(CopilotInput(message="  oi\n  tudo   bem? ", today=today)) == (
        "oi tudo bem?"
    )
    long = conversation_title(CopilotInput(message="x" * 200, today=today))
    assert len(long) == TITLE_CHARS and long.endswith("…")
    files_only = CopilotInput(message="", today=today, attachment_ids=[uuid4()])
    assert conversation_title(files_only) == "(attachments)"
