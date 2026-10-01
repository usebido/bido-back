"""POST /v1/approvals/{id}/decision over the ASGI transport."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.api.app import create_app
from bido_back.api.deps import get_db, get_job_queue, get_principal
from bido_back.auth import Principal
from bido_back.db import create_sessionmaker
from bido_back.settings import Settings
from bido_back.worker.jobs import JobQueue

from .conftest import FakeJobQueue, People, load, propose, tamper


def _app() -> FastAPI:
    settings = Settings.model_validate(
        {"environment": "test", "database_url": "postgresql://app@localhost:5432/unused"}
    )
    return create_app(settings)


def _url(approval_id: UUID) -> str:
    return f"/v1/approvals/{approval_id}/decision"


async def test_decision_requires_authentication() -> None:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post(_url(uuid4()), json={"decision": "approve"})

    assert response.status_code == 401
    assert response.json() == {"detail": "authentication not implemented"}
    assert response.headers["www-authenticate"] == "Bearer"


@dataclass
class Api:
    http: httpx.AsyncClient
    app: FastAPI
    jobs: FakeJobQueue
    # Who the overridden get_principal says is calling; set per request.
    principal: Principal | None = None

    async def decide(self, principal: Principal, approval_id: UUID, body: object) -> httpx.Response:
        self.principal = principal
        return await self.http.post(_url(approval_id), json=body)


@pytest_asyncio.fixture
async def api(app_engine: AsyncEngine) -> AsyncIterator[Api]:
    app = _app()
    sessionmaker = create_sessionmaker(app_engine)
    jobs = FakeJobQueue()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        state = Api(http=http, app=app, jobs=jobs)

        def principal() -> Principal:
            assert state.principal is not None
            return state.principal

        def db() -> async_sessionmaker[AsyncSession]:
            return sessionmaker

        def queue() -> JobQueue:
            return jobs

        app.dependency_overrides[get_principal] = principal
        app.dependency_overrides[get_db] = db
        app.dependency_overrides[get_job_queue] = queue
        yield state


def who(people: People, user_id: UUID, tenant_id: UUID | None = None) -> Principal:
    return Principal(tenant_id=tenant_id or people.tenant, user_id=user_id)


@pytest.mark.db
async def test_approver_approves(api: Api, app_engine: AsyncEngine, people: People) -> None:
    approval_id = await propose(app_engine, people.tenant, people.owner)

    response = await api.decide(who(people, people.approver), approval_id, {"decision": "approve"})

    assert response.status_code == 200
    assert response.json() == {"approval_id": str(approval_id), "status": "approved"}
    assert [job.idempotency_key for job in api.jobs.jobs] == [f"approval:{approval_id}"]

    replay = await api.decide(who(people, people.approver), approval_id, {"decision": "approve"})
    assert replay.status_code == 409
    assert len(api.jobs.jobs) == 1


@pytest.mark.db
async def test_approver_rejects(api: Api, app_engine: AsyncEngine, people: People) -> None:
    approval_id = await propose(app_engine, people.tenant, people.owner)

    response = await api.decide(who(people, people.approver), approval_id, {"decision": "reject"})

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert api.jobs.jobs == []


@pytest.mark.db
async def test_refusals_map_to_http_statuses(
    api: Api, app_engine: AsyncEngine, migrator_engine: AsyncEngine, people: People
) -> None:
    approval_id = await propose(app_engine, people.tenant, people.approver)
    approve = {"decision": "approve"}

    member = await api.decide(who(people, people.member), approval_id, approve)
    own = await api.decide(who(people, people.approver), approval_id, approve)
    foreign = await api.decide(
        who(people, people.other_approver, people.other_tenant), approval_id, approve
    )
    await tamper(migrator_engine, people.tenant, approval_id)
    tampered = await api.decide(who(people, people.second_approver), approval_id, approve)

    assert [r.status_code for r in (member, own, foreign, tampered)] == [403, 403, 404, 409]
    assert (await load(app_engine, people.tenant, approval_id)).status == "pending"
    assert api.jobs.jobs == []


@pytest.mark.db
@pytest.mark.parametrize(
    "body",
    [
        {"decision": "yes"},
        {},
        # The browser cannot name the approver, the payload, or the status.
        {"decision": "approve", "decided_by": "00000000-0000-0000-0000-000000000000"},
        {"decision": "approve", "args": {"amount_cents": 1}},
        {"decision": "approve", "status": "executed"},
    ],
)
async def test_only_the_decision_is_accepted_from_the_client(
    api: Api, app_engine: AsyncEngine, people: People, body: object
) -> None:
    approval_id = await propose(app_engine, people.tenant, people.owner)

    response = await api.decide(who(people, people.approver), approval_id, body)

    assert response.status_code == 422
    assert (await load(app_engine, people.tenant, approval_id)).status == "pending"


@pytest.mark.db
async def test_no_job_queue_means_no_decision(
    api: Api, app_engine: AsyncEngine, people: People
) -> None:
    del api.app.dependency_overrides[get_job_queue]  # the real one: SQS_QUEUE_URL is unset
    approval_id = await propose(app_engine, people.tenant, people.owner)

    response = await api.decide(who(people, people.approver), approval_id, {"decision": "approve"})

    assert response.status_code == 503
    assert (await load(app_engine, people.tenant, approval_id)).status == "pending"
