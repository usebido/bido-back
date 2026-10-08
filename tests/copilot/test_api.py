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
from tests.agents.test_runner import Script, reply, script, text, tool_use
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
        reply(text(f"You have one RFQ: {rfq.ref}.")),
    )

    response = await api.http.post("/v1/copilot/messages", json={"message": "my RFQs?"})

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    types = [e["type"] for e in events]
    # The reply streams as text deltas, then comes whole in `answer`.
    deltas = [e for e in events if e["type"] == "text_delta"]
    assert len(deltas) > 1
    assert [t for t in types if t != "text_delta"] == [
        "conversation",
        "tool_called",
        "tool_result",
        "answer",
        "done",
    ]
    assert types.index("tool_result") < types.index("text_delta")
    assert max(i for i, t in enumerate(types) if t == "text_delta") < types.index("answer")
    by_type = {e["type"]: e for e in events}
    assert by_type["tool_result"]["output"]["rfqs"][0]["ref"] == rfq.ref
    assert by_type["answer"]["text"] == "You have one RFQ: RFQ-1."
    assert "".join(d["text"] for d in deltas) == by_type["answer"]["text"]
    assert by_type["done"]["ok"] is True and by_type["done"]["actions"] == []
    assert by_type["done"]["conversation_id"] == events[0]["conversation_id"]
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
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
        reply(text("The email to Rhein is waiting for an approver.")),
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
async def test_a_stream_that_breaks_mid_answer_fails_then_ends_with_done(
    api: Api, copilot_rows: People, script: Script
) -> None:
    from tests.agents.test_runner import StreamFailure

    script(StreamFailure("Let me look at", RuntimeError("connection reset")))

    response = await api.http.post("/v1/copilot/messages", json={"message": "hi"})

    types = [e["type"] for e in parse_sse(response.text)]
    assert "text_delta" in types
    assert "answer" not in types
    assert types[-2:] == ["run_failed", "done"]


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
    stub = script(reply(text("Olá! Como posso ajudar?")), reply(text("Você perguntou 'oi'.")))

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
