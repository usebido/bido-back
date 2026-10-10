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
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.config import RunnerConfig
from bido_back.api.copilot import copilot_router, get_copilot_runner_config, get_copilot_titler
from bido_back.api.deps import (
    ApiResources,
    get_db,
    get_mailbox_senders,
    get_principal,
    get_rfq_agents,
    get_storage,
)
from bido_back.auth import Principal
from bido_back.copilot.contracts import (
    ATTACHMENTS_ONLY_MESSAGE,
    MAX_ATTACHMENTS,
    MAX_MESSAGE_CHARS,
    CopilotMessageRequest,
)
from bido_back.copilot.titles import Titler
from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import Approval, CopilotProposal, Invite, Rfq, TemplateUpload
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
    storage: FakeStorage
    runner_config: RunnerConfig = CONFIG
    # None keeps titles as the first message; tests/copilot/test_titles.py sets a fake.
    titler: Titler | None = None


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
        state = Api(http=http, mailboxes=mailboxes, principal=principal, storage=storage)

        def current_principal() -> Principal:
            return state.principal

        def runner_config() -> RunnerConfig:
            return state.runner_config

        def titler() -> Titler | None:
            return state.titler

        app.dependency_overrides[get_principal] = current_principal
        app.dependency_overrides[get_db] = db
        app.dependency_overrides[get_rfq_agents] = rfq_agents
        app.dependency_overrides[get_mailbox_senders] = mailbox_senders
        app.dependency_overrides[get_storage] = blobs
        app.dependency_overrides[get_copilot_runner_config] = runner_config
        app.dependency_overrides[get_copilot_titler] = titler
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
    conversation = str(uuid4())
    invite = {"rfq": rfq.ref, "supplier_ids": [str(rhein.id)]}
    # First message: the invite is only proposed; nothing changes until the buyer answers.
    script(
        reply(tool_use("fc_1", "invite_suppliers", **invite)),
        reply(text("I will invite Rhein (its contact's email). Confirm?")),
    )
    await api.http.post(
        "/v1/copilot/messages",
        json={"message": "invite Rhein and send it", "conversation_id": conversation},
    )
    async with tenant_session(app_engine, api.principal.tenant_id) as session:
        [proposal] = list(await session.scalars(select(CopilotProposal)))
        invites = list(await session.scalars(select(Invite).where(Invite.rfq_id == rfq.id)))
    assert proposal.action == "invite_suppliers" and proposal.used_at is None
    assert invites == []

    # The buyer's next message confirms it.
    script(
        reply(tool_use("fc_1", "invite_suppliers", **invite, proposal_id=str(proposal.id))),
        reply(tool_use("fc_2", "prepare_send", rfq=rfq.ref)),
        reply(text("The email to Rhein is waiting for an approver.")),
    )
    response = await api.http.post(
        "/v1/copilot/messages", json={"message": "yes", "conversation_id": conversation}
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
async def test_a_proposed_rfq_is_no_card_and_no_rfq(
    api: Api, app_engine: AsyncEngine, copilot_rows: People, script: Script
) -> None:
    script(
        reply(tool_use("fc_1", "create_rfq", text="4000 MLCC 100nF", currency="EUR")),
        reply(text("Here is the RFQ I would create. Confirm?")),
    )

    response = await api.http.post(
        "/v1/copilot/messages",
        json={"message": "make an RFQ for 4000 MLCC", "conversation_id": str(uuid4())},
    )

    events = parse_sse(response.text)
    [result] = [e for e in events if e["type"] == "tool_result"]
    assert result["ok"] is True and result["output"]["awaiting_confirmation"] is True
    preview = result["output"]["preview"]
    assert [(line["description"], line["unit"]) for line in preview["lines"]] == [
        ("MLCC 100nF 50V X7R 0603", "pcs"),
        ("Board-to-board connector 40 pin", "pcs"),
    ]
    # No rfq_draft card for something that does not exist yet, and no RFQ row.
    assert [e for e in events if e["type"] == "artifact"] == []
    async with tenant_session(app_engine, api.principal.tenant_id) as session:
        assert list(await session.scalars(select(Rfq))) == []


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


def test_a_message_carries_up_to_25_files() -> None:
    # ERP exports come one file per transaction (MB52, ME2N, MD04, ...).
    accepted = CopilotMessageRequest(attachment_ids=[uuid4() for _ in range(25)])
    assert len(accepted.attachment_ids) == MAX_ATTACHMENTS == 25
    with pytest.raises(ValidationError):
        CopilotMessageRequest(attachment_ids=[uuid4() for _ in range(26)])


@pytest.mark.db
async def test_26_files_are_refused_before_streaming(api: Api, copilot_rows: People) -> None:
    response = await api.http.post(
        "/v1/copilot/messages",
        json={"message": "", "attachment_ids": [str(uuid4()) for _ in range(26)]},
    )
    assert response.status_code == 422, response.text
    assert "attachment_ids" in response.text


@pytest.mark.db
async def test_limits_tell_the_front_what_a_message_may_carry(
    api: Api, copilot_rows: People
) -> None:
    response = await api.http.get("/v1/copilot/limits")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "max_attachments": 25,
        "max_message_chars": MAX_MESSAGE_CHARS,
        "max_upload_bytes": 100 * 1024 * 1024,
        "accepted_extensions": ["csv", "docx", "pdf", "xls", "xlsx"],
    }


async def test_limits_require_authentication() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/v1/copilot/limits")
    assert response.status_code == 401


@pytest.mark.db
@pytest.mark.parametrize(
    "body", [{}, {"message": ""}, {"message": "  \n "}, {"message": "", "attachment_ids": []}]
)
async def test_a_message_needs_text_or_files(
    api: Api, copilot_rows: People, body: dict[str, Any]
) -> None:
    response = await api.http.post("/v1/copilot/messages", json=body)
    assert response.status_code == 422, response.text


@pytest.mark.db
async def test_files_alone_are_analysed_never_imported(
    api: Api,
    app_engine: AsyncEngine,
    agno_engine: AsyncEngine,
    copilot_rows: People,
    script: Script,
) -> None:
    api.runner_config = RunnerConfig(
        openai_api_key="test-not-a-real-key",
        agno_database_url=agno_engine.url.render_as_string(hide_password=False),
    )
    async with tenant_session(app_engine, api.principal.tenant_id) as session:
        upload = TemplateUpload(
            tenant_id=api.principal.tenant_id,
            user_id=api.principal.user_id,
            filename="pecas.csv",
            content_type="text/csv",
            size_bytes=24,
            storage_key="uploads/pecas.csv",
        )
        session.add(upload)
        await session.flush()
        upload_id = str(upload.id)
    api.storage.blobs["uploads/pecas.csv"] = b"part,qty\n6205,400\n"
    # The model tries to store the file anyway: the tool refuses (2026-10-10: the
    # standing instruction said "Importe", and 8 SAP exports were imported unasked).
    stub = script(
        reply(tool_use("fc_1", "import_spreadsheet", upload_id=upload_id)),
        reply(text("Analisei pecas.csv.")),
    )
    conversation = str(uuid4())

    response = await api.http.post(
        "/v1/copilot/messages",
        json={"message": " ", "attachment_ids": [upload_id], "conversation_id": conversation},
    )

    assert parse_sse(response.text)[-1]["ok"] is True
    # The model is asked to analyse the files, in Portuguese; never to import them.
    sent = json.dumps(stub.calls[0]["input"], ensure_ascii=False)
    assert ATTACHMENTS_ONLY_MESSAGE in sent and "[pecas.csv A2] 6205" in sent
    assert "Importe" not in ATTACHMENTS_ONLY_MESSAGE
    refused = json.dumps(stub.calls[1]["input"], ensure_ascii=False)
    assert "without asking to store them" in refused
    # The buyer's turn shows the files, not the instruction; the title names them.
    turns = (await api.http.get(f"/v1/copilot/conversations/{conversation}")).json()["turns"]
    assert [(t["message"], t["attachments"]) for t in turns] == [("", ["pecas.csv"])]
    items = (await api.http.get("/v1/copilot/conversations")).json()["items"]
    assert [i["title"] for i in items] == ["pecas.csv"]


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
    assert [(t["message"], t["answer"], t["attachments"]) for t in turns] == [
        ("oi", "Olá! Como posso ajudar?", []),
        ("o que eu perguntei?", "Você perguntou 'oi'.", []),
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
        reply(text("one")),
        reply(text("two")),
        reply(text("three")),
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
    script(*(reply(text(str(n))) for n in range(3)))
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
    script(reply(text("ok")), reply(text("ok")))
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
    dropped = CopilotInput(
        message=ATTACHMENTS_ONLY_MESSAGE,
        today=today,
        attachments=["bom.xlsx", "estoque.csv"],
        attachment_ids=[uuid4(), uuid4()],
    )
    assert conversation_title(dropped) == "bom.xlsx, estoque.csv"


@pytest.mark.db
async def test_a_turn_is_logged_with_ids_and_counts_but_no_content(
    api: Api, copilot_rows: People, script: Script
) -> None:
    import logging

    from bido_back.logs import JsonFormatter

    secret = "customer-content-7f3a"  # noqa: S105 - a marker, not a credential

    class Lines(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.setFormatter(JsonFormatter(service="api", release=None))
            self.text: list[str] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.text.append(self.format(record))

    handler = Lines()
    logger = logging.getLogger("bido_back")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        script(
            # A tool error that quotes what the model typed, and invalid arguments.
            reply(tool_use("fc_1", "invite_suppliers", rfq=secret, supplier_ids=[str(uuid4())])),
            reply(tool_use("fc_2", "list_rfqs", limit=999)),
            reply(text(f"I could not find {secret}.")),
        )
        response = await api.http.post(
            "/v1/copilot/messages", json={"message": f"invite {secret} to it"}
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    assert parse_sse(response.text)[-1]["ok"] is True
    lines = [json.loads(line) for line in handler.text]
    assert secret not in "\n".join(handler.text)
    by_msg: dict[str, list[dict[str, Any]]] = {}
    for line in lines:
        by_msg.setdefault(line["msg"], []).append(line)
    calls = by_msg["tool call"]
    assert [(c["tool"], c["status"]) for c in calls] == [
        ("invite_suppliers", "error"),
        ("list_rfqs", "invalid_args"),
    ]
    assert calls[0]["reason"] == "no RFQ '…'; use list_rfqs to find it"
    # Calls with invalid arguments never reach the event stream; only their log line.
    (run,) = by_msg["agent run"]
    assert (run["agent"], run["outcome"], run["tool_calls"]) == ("copilot", "completed", 1)
    (finished,) = by_msg["copilot turn finished"]
    assert (finished["outcome"], finished["tool_errors"]) == ("ok", 1)
    # Three scripted requests of 10 input and 5 output tokens each, priced.
    assert (finished["model_calls"], finished["input_tokens"], finished["output_tokens"]) == (
        3,
        30,
        15,
    )
    assert finished["cost_usd"] > 0
    assert (run["input_tokens"], run["cost_usd"]) == (30, finished["cost_usd"])
    assert finished["tools"] == ["invite_suppliers"]
    assert finished["conversation_id"] == parse_sse(response.text)[0]["conversation_id"]


@pytest.mark.db
@pytest.mark.parametrize("capture", [False, True])
async def test_a_turn_is_traced_with_content_only_when_capture_is_on(
    api: Api, copilot_rows: People, script: Script, capture: bool
) -> None:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from bido_back.agents import tracing

    secret = "customer-content-5d1e"  # noqa: S105 - a marker, not a credential
    exporter = InMemorySpanExporter()
    tracing.configure(exporter, capture_content=capture, environment="dev", batch=False)
    try:
        script(
            reply(tool_use("fc_1", "invite_suppliers", rfq=secret, supplier_ids=[str(uuid4())])),
            reply(text(f"I could not find {secret}.")),
        )
        response = await api.http.post(
            "/v1/copilot/messages", json={"message": f"invite {secret} to it"}
        )
    finally:
        tracing.reset()

    events = parse_sse(response.text)
    assert events[-1]["ok"] is True
    spans = exporter.get_finished_spans()
    attributes = "\n".join(
        str(value) for span in spans for value in (span.attributes or {}).values()
    )
    assert (secret in attributes) is capture
    (root,) = [s for s in spans if s.name == "agent copilot"]
    attrs = dict(root.attributes or {})
    assert root.parent is None
    assert attrs["langfuse.session.id"] == events[0]["conversation_id"]
    assert attrs["langfuse.user.id"] == str(copilot_rows.owner)
    assert attrs["langfuse.trace.metadata.tenant_id"] == str(copilot_rows.tenant)
    assert [s.name for s in spans if s.name.startswith("tool ")] == ["tool invite_suppliers"]
    assert len([s for s in spans if s.name.startswith("model ")]) == 2
