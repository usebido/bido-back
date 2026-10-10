"""The payment routes over the ASGI transport, with fake providers.

Without payment settings the provider routes answer 503 and the config says so;
with fakes injected (`get_payment_providers`), a buyer saves a card, an approver
adds the supplier's payout account, and the buyer proposes paying a sent PO.
"""

from collections.abc import AsyncIterator, Callable
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.api.awards import awards_router
from bido_back.api.deps import ApiResources, get_db, get_payment_providers, get_principal
from bido_back.api.payments import payments_router
from bido_back.auth import Principal
from bido_back.db import create_sessionmaker
from bido_back.payments.config import PaymentProviders
from bido_back.settings import Settings
from tests.awards.test_awards_db import World, approver, buyer
from tests.awards.test_lifecycle import sent_po
from tests.payments.fakes import FakeProviders
from tests.payments.flow.conftest import add_payment_method
from tests.payments.flow.test_service_db import ACCOUNT, ACCOUNT_NUMBER, member, usd_rate

__all__ = ["usd_rate"]

UNUSED_DB = "postgresql+asyncpg://app@localhost:5432/unused"
ACCOUNT_JSON = ACCOUNT.model_dump(mode="json", exclude_none=True)


def build_app() -> FastAPI:
    app = FastAPI()
    app.state.resources = ApiResources(
        Settings.model_validate({"environment": "test", "database_url": UNUSED_DB})
    )
    app.include_router(payments_router())
    app.include_router(awards_router())
    return app


def as_user(app: FastAPI, principal: Principal) -> httpx.AsyncClient:
    def current() -> Principal:
        return principal

    app.dependency_overrides[get_principal] = current
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def pay_body(card: UUID | str, account: UUID | str) -> dict[str, str]:
    return {"payment_method_id": str(card), "payout_account_id": str(account)}


async def test_payment_routes_require_authentication() -> None:
    transport = httpx.ASGITransport(app=build_app())
    some = uuid4()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        responses = [
            await http.get("/v1/payments/config"),
            await http.get("/v1/payment-methods"),
            await http.post("/v1/payment-methods", json={"token_id": "tok_x"}),
            await http.delete(f"/v1/payment-methods/{some}"),
            await http.get(f"/v1/suppliers/{some}/payout-accounts"),
            await http.post(f"/v1/suppliers/{some}/payout-accounts", json=ACCOUNT_JSON),
            await http.post(f"/v1/suppliers/{some}/payout-accounts/{some}/disable"),
            await http.post(f"/v1/purchase-orders/{some}/payment", json=pay_body(some, some)),
            await http.get(f"/v1/purchase-orders/{some}/payment"),
        ]
    assert [r.status_code for r in responses] == [401] * 9


async def test_without_payment_settings_the_provider_routes_answer_503() -> None:
    principal = Principal(tenant_id=uuid4(), user_id=uuid4(), role="owner")
    some = uuid4()
    async with as_user(build_app(), principal) as http:
        config = await http.get("/v1/payments/config")
        refused = [
            await http.post("/v1/payment-methods", json={"token_id": "tok_x"}),
            await http.delete(f"/v1/payment-methods/{some}"),
            await http.post(f"/v1/suppliers/{some}/payout-accounts", json=ACCOUNT_JSON),
            await http.post(f"/v1/purchase-orders/{some}/payment", json=pay_body(some, some)),
        ]
    assert config.status_code == 200
    assert config.json() == {"enabled": False, "card": None, "chain": None}
    assert [r.status_code for r in refused] == [503] * 4
    assert {r.json()["detail"] for r in refused} == {"payments are not set up on this server"}


@pytest_asyncio.fixture
async def clients(
    app_engine: AsyncEngine, world: World, providers: FakeProviders
) -> AsyncIterator[Callable[[Principal], httpx.AsyncClient]]:
    sessionmaker = create_sessionmaker(app_engine)
    bundle = providers.bundle()

    def db() -> async_sessionmaker[AsyncSession]:
        return sessionmaker

    def payment_providers() -> PaymentProviders:
        return bundle

    opened: list[httpx.AsyncClient] = []

    def client(principal: Principal) -> httpx.AsyncClient:
        app = build_app()
        app.dependency_overrides[get_db] = db
        app.dependency_overrides[get_payment_providers] = payment_providers
        opened.append(as_user(app, principal))
        return opened[-1]

    yield client
    for http in opened:
        await http.aclose()


@pytest.mark.db
async def test_pay_a_purchase_order_over_http(
    world: World,
    providers: FakeProviders,
    clients: Callable[[Principal], httpx.AsyncClient],
    usd_rate: object,
) -> None:
    people = world.people
    po = await sent_po(world)
    http = clients(buyer(people))

    saved = await http.post("/v1/payment-methods", json={"token_id": "tok_http"})
    assert saved.status_code == 201
    card = saved.json()
    assert (card["brand"], card["last4"], card["status"]) == ("visa", "4242", "active")
    assert [m["id"] for m in (await http.get("/v1/payment-methods")).json()] == [card["id"]]
    duplicate = await http.post("/v1/payment-methods", json={"token_id": "tok_http"})
    assert duplicate.status_code == 409

    accounts = f"/v1/suppliers/{po.supplier_id}/payout-accounts"
    forbidden = await clients(member(people)).post(accounts, json=ACCOUNT_JSON)
    assert forbidden.status_code == 403
    added = await clients(approver(people)).post(accounts, json=ACCOUNT_JSON)
    assert added.status_code == 201
    account = added.json()
    assert (account["beneficiary_display"], account["account_last4"]) == ("Shen…", "7890")
    assert ACCOUNT_NUMBER not in added.text and ACCOUNT.beneficiary_name not in added.text
    assert [a["id"] for a in (await http.get(accounts)).json()] == [account["id"]]
    invalid = await http.post(accounts, json={**ACCOUNT_JSON, "bank_code": "1045"})
    assert invalid.status_code == 422  # a CNAPS code has 12 digits

    others_card = await add_payment_method(world.engine, people.tenant, people.member)
    not_mine = await http.post(
        f"/v1/purchase-orders/{po.id}/payment", json=pay_body(others_card, account["id"])
    )
    assert not_mine.status_code == 422

    paying = f"/v1/purchase-orders/{po.id}/payment"
    proposed = await http.post(paying, json=pay_body(card["id"], account["id"]))
    assert proposed.status_code == 202
    body = proposed.json()
    payment = body["payment"]
    assert (payment["state"], payment["charge_currency"], payment["simulated"]) == (
        "pending_approval",
        "USD",
        False,
    )
    assert payment["approval_id"] == body["approval_id"]
    assert (payment["card"]["payment_method_id"], payment["payout"]["payout_account_id"]) == (
        card["id"],
        account["id"],
    )
    again = await http.post(paying, json=pay_body(card["id"], account["id"]))
    assert again.status_code == 409

    shown = await http.get(paying)
    assert shown.status_code == 200 and shown.json()["id"] == payment["id"]
    order = (await http.get(f"/v1/purchase-orders/{po.id}")).json()
    assert (order["payment"]["payment_id"], order["payment"]["state"]) == (
        payment["id"],
        "pending_approval",
    )
    [listed] = (await http.get("/v1/purchase-orders")).json()
    assert listed["payment_state"] == "pending_approval"

    # The card may still be charged: it stays. The account can always be disabled.
    assert (await http.delete(f"/v1/payment-methods/{card['id']}")).status_code == 409
    disabled = await http.post(f"{accounts}/{account['id']}/disable")
    assert disabled.status_code == 200 and disabled.json()["status"] == "disabled"

    assert (await http.get(f"/v1/purchase-orders/{uuid4()}/payment")).status_code == 404
    assert (await http.post(f"{accounts}/{uuid4()}/disable")).status_code == 404
    assert providers.card.enrolled == ["tok_http", "tok_http"]
    assert providers.card.charges == {}  # nothing is charged before a person approves


@pytest.mark.db
async def test_removing_a_card_over_http(
    world: World,
    providers: FakeProviders,
    clients: Callable[[Principal], httpx.AsyncClient],
) -> None:
    people = world.people
    http = clients(buyer(people))
    card = (await http.post("/v1/payment-methods", json={"token_id": "tok_gone"})).json()
    # A member sees the company's cards but removes only their own.
    other = clients(member(people))
    assert [m["id"] for m in (await other.get("/v1/payment-methods")).json()] == [card["id"]]
    assert (await other.delete(f"/v1/payment-methods/{card['id']}")).status_code == 403
    removed = await http.delete(f"/v1/payment-methods/{card['id']}")
    assert removed.status_code == 204 and removed.content == b""
    assert providers.card.removed == ["pm_tok_gone"]
    assert (await http.get("/v1/payment-methods")).json() == []
    assert (await http.delete(f"/v1/payment-methods/{card['id']}")).status_code == 404
