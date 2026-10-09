"""The ledger over HTTP: the follow-up view, answering and dismissing supplier
questions, and taking a held offer as shown."""

from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.api.deps import ApiResources, get_db, get_principal
from bido_back.api.followups import followups_router
from bido_back.api.proposals import proposals_router
from bido_back.auth import Principal
from bido_back.db import create_sessionmaker
from bido_back.settings import Settings
from tests.db.conftest import app_engine, migrator_engine
from tests.followups.test_ledger_db import (
    INCOMPLETE,
    QUESTION,
    UPDATE,
    Thread,
    add_member,
    incomplete,
    question,
    scripted,
    thread,
    update_without_statement,
)

__all__ = ["app_engine", "migrator_engine", "scripted", "thread"]

UNUSED_DB = "postgresql+asyncpg://app@localhost:5432/unused"


def build_app() -> FastAPI:
    app = FastAPI()
    app.state.resources = ApiResources(
        Settings.model_validate({"environment": "test", "database_url": UNUSED_DB})
    )
    app.include_router(followups_router())
    app.include_router(proposals_router())
    return app


async def test_the_new_routes_require_authentication() -> None:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        responses = [
            await http.post(f"/v1/rfqs/{uuid4()}/invites/{uuid4()}/answers", json={"answer": "x"}),
            await http.post(f"/v1/rfqs/{uuid4()}/questions/{uuid4()}/dismiss", json={}),
            await http.post(f"/v1/proposals/{uuid4()}/use-current-offer", json={}),
        ]
    assert [r.status_code for r in responses] == [401, 401, 401]


class Caller:
    """Who the requests are from (switchable per request)."""

    def __init__(self, principal: Principal) -> None:
        self.principal = principal

    def __call__(self) -> Principal:
        return self.principal


@pytest_asyncio.fixture
async def client(thread: Thread) -> AsyncIterator[tuple[httpx.AsyncClient, Caller]]:
    app = build_app()
    sessionmaker = create_sessionmaker(thread.world.engine)

    def db() -> async_sessionmaker[AsyncSession]:
        return sessionmaker

    caller = Caller(thread.world.principal)
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_principal] = caller
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http, caller


@pytest.mark.db
async def test_questions_over_http(
    thread: Thread, client: tuple[httpx.AsyncClient, Caller]
) -> None:
    http, _ = client
    rfq_id, invite_id = thread.rfq.id, thread.invite_id
    nothing = await http.post(
        f"/v1/rfqs/{rfq_id}/invites/{invite_id}/answers", json={"answer": "Hello"}
    )
    assert nothing.status_code == 409  # no open question

    await thread.reply(QUESTION, question(), timedelta(hours=3))
    view = (await http.get(f"/v1/rfqs/{rfq_id}/followups")).json()
    [invite] = view["invites"]
    assert invite["phase"] == "needs_your_answer"
    [asked] = invite["questions"]
    assert (asked["status"], asked["question"]) == (
        "open",
        "Can you send the drawing of the shaft (000102)?",
    )

    assert (
        await http.post(f"/v1/rfqs/{rfq_id}/invites/{invite_id}/answers", json={"answer": " "})
    ).status_code == 422
    answered = await http.post(
        f"/v1/rfqs/{rfq_id}/invites/{invite_id}/answers",
        json={"answer": "Please find the drawing attached to the RFQ.", "question_ids": []},
    )
    assert answered.status_code == 201, answered.text
    body = answered.json()
    assert body["question_ids"] == [asked["id"]] and body["approval_id"]
    view = (await http.get(f"/v1/rfqs/{rfq_id}/followups")).json()
    [invite] = view["invites"]
    assert invite["phase"] == "email_pending"
    # The published kind stays one of the two first names; the exact one is separate.
    assert [(m["kind"], m["followup_kind"]) for m in invite["messages"]] == [
        ("clarification", "answer")
    ]

    dismissed = await http.post(f"/v1/rfqs/{rfq_id}/questions/{asked['id']}/dismiss", json={})
    assert dismissed.status_code == 409  # answered already
    unknown = await http.post(f"/v1/rfqs/{rfq_id}/questions/{uuid4()}/dismiss", json={})
    assert unknown.status_code == 404


@pytest.mark.db
async def test_use_current_offer_over_http(
    thread: Thread, client: tuple[httpx.AsyncClient, Caller]
) -> None:
    http, caller = client
    await thread.reply(INCOMPLETE, incomplete(), timedelta(hours=1))
    proposal, _ = await thread.latest()
    not_held = await http.post(f"/v1/proposals/{proposal.id}/use-current-offer", json={})
    assert not_held.status_code == 409

    await thread.reply(UPDATE, update_without_statement(), timedelta(hours=2))
    proposal, _ = await thread.latest()
    shown = (await http.get(f"/v1/proposals/{proposal.id}")).json()
    assert shown["holds"] == ["amendment_without_explicit_preservation"]
    view = (await http.get(f"/v1/rfqs/{thread.rfq.id}/followups")).json()
    assert view["invites"][0]["phase"] == "needs_your_decision"
    assert view["invites"][0]["needs_your_decision"][0]["kind"] == "offer_held"

    caller.principal = await add_member(thread.world.engine, thread.world.tenant_id, "member")
    forbidden = await http.post(f"/v1/proposals/{proposal.id}/use-current-offer", json={})
    assert forbidden.status_code == 403
    caller.principal = thread.world.principal
    released = await http.post(
        f"/v1/proposals/{proposal.id}/use-current-offer", json={"reason": "checked by phone"}
    )
    assert released.status_code == 200, released.text
    assert released.json()["holds"] == [] and released.json()["released_at"]
    assert (
        await http.post(f"/v1/proposals/{uuid4()}/use-current-offer", json={})
    ).status_code == 404
