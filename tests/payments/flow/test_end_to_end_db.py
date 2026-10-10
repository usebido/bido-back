"""A purchase order paid end to end: service, approval guard, executor and pipeline together.

The card gateway, vault, wallet provider and chain are the fakes of
`tests/payments/fakes.py`; everything else is the production code path. The buyer
records the supplier's account and a card, asks to pay the sent PO, approves it
(self-approval is the default), the worker's executor charges the card once, and
the `payment.advance` jobs mint the stablecoin, create the escrow and fund it.
"""

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.approvals import decide_approval
from bido_back.awards import service as award_service
from bido_back.awards.send import AWARD_REJECT_HOOKS
from bido_back.db import tenant_session
from bido_back.db.models import AuditEvent, FxRate
from bido_back.fx.ecb import DailyRates, upsert_rates
from bido_back.payments import service
from bido_back.payments.execute import (
    PAYMENT_REJECT_HOOKS,
    PaymentActDeps,
    register_payment_act_tools,
)
from bido_back.payments.pipeline import PaymentAdvanceHandler
from bido_back.payments.proposals import PAYMENT_ADVANCE
from bido_back.payments.schemas import (
    CreatePayoutAccountRequest,
    MilestoneInput,
    PayPurchaseOrderRequest,
)
from bido_back.settings import Settings
from tests.approvals.conftest import FakeJobQueue
from tests.awards.test_awards_db import World, buyer
from tests.awards.test_lifecycle import sent_po
from tests.payments.fakes import FakeProviders, address
from tests.payments.flow.test_service_db import card_token

pytestmark = pytest.mark.db

RATE_DAY = date(2026, 10, 7)
SETTINGS = Settings.model_validate(
    {
        "environment": "test",
        "database_url": "postgresql+asyncpg://app@localhost:5432/unused",
        "solana_stablecoin_mint": address("mint"),
    }
)
ACCOUNT = CreatePayoutAccountRequest(
    beneficiary_name="Ningbo Precision Tooling Co., Ltd.",
    bank_name="Bank of China",
    account_number="6217 8500 0000 1234 567",
    country="CN",
    currency="CNY",
    swift_bic="BKCHCNBJ92A",
    bank_code="104332000017",
)


@pytest_asyncio.fixture
async def usd_rate(app_engine: AsyncEngine, migrator_engine: AsyncEngine) -> AsyncIterator[None]:
    await upsert_rates(app_engine, DailyRates(RATE_DAY, {"USD": Decimal("1.10")}))
    yield
    async with migrator_engine.begin() as conn:
        await conn.execute(delete(FxRate).where(FxRate.rate_date == RATE_DAY))


async def test_a_buyer_pays_a_po_by_card_and_its_escrow_holds_the_money(
    world: World, providers: FakeProviders, usd_rate: None
) -> None:
    po = await sent_po(world)
    owner = buyer(world.people)
    account = await service.create_payout_account(
        world.engine, owner, po.supplier_id, ACCOUNT, providers.bank_details
    )
    card = await service.enroll_payment_method(
        world.engine,
        owner,
        card_token("e2e-card"),
        providers.card,
        consumer_country="US",
    )
    requested = await service.request_po_payment(
        world.engine,
        owner,
        po.id,
        PayPurchaseOrderRequest(
            payment_method_id=card.id,
            payout_account_id=account.id,
            milestones=[
                MilestoneInput(name="Tooling start", bps=3_000),
                MilestoneInput(name="PPAP approval", bps=7_000),
            ],
        ),
        providers.bundle(),
        settings=SETTINGS,
        today=RATE_DAY,
    )
    assert requested.payment.state == "pending_approval"
    assert requested.payment.charge_amount == (po.total * Decimal("1.10")).quantize(Decimal("0.01"))
    assert providers.card.charge_calls == 0  # nothing moves before a person decides

    wakes = FakeJobQueue()
    register_payment_act_tools(
        world.registry, PaymentActDeps(db=world.engine, providers=providers.bundle(), jobs=wakes)
    )
    decision = FakeJobQueue()
    await decide_approval(
        world.engine,
        owner,  # the buyer approves what they asked for (the default policy)
        requested.approval_id,
        "approve",
        jobs=decision,
        on_reject={**AWARD_REJECT_HOOKS, **PAYMENT_REJECT_HOOKS},
    )
    [execute] = decision.jobs
    await world.executor(execute)
    await world.executor(execute)  # SQS redelivers: still one charge
    assert len(providers.card.charges) == 1

    pipeline = PaymentAdvanceHandler(world.engine, providers.bundle(), wakes, poll_budget=0)
    steps = 0
    while wakes.jobs:
        envelope = wakes.jobs.pop(0)
        assert envelope.type == PAYMENT_ADVANCE
        await pipeline(envelope)
        steps += 1
        assert steps <= 6, "the pipeline keeps waking itself"

    payment = await service.get_po_payment(world.engine, owner, po.id, SETTINGS)
    assert payment.state == "funded"
    assert payment.needs_attention is False
    assert [(t.step, t.state) for t in payment.chain.transactions] == [
        ("mint", "confirmed"),
        ("create_escrow", "confirmed"),
        ("fund", "confirmed"),
    ]
    assert all("cluster=devnet" in t.explorer_url for t in payment.chain.transactions)
    base_units = int(payment.charge_amount * 10**6)
    assert providers.chain.minted_total == base_units  # minted exactly once
    assert payment.chain.escrow_address is not None
    escrow = providers.chain.escrows[payment.chain.escrow_address]
    assert (escrow.status, escrow.total, escrow.buyer) == (
        "funded",
        base_units,
        payment.chain.wallet_address,
    )
    assert providers.chain.balances[escrow.vault] == base_units
    assert len(providers.wallets.wallets) == 1  # the company's one invisible wallet

    order = await award_service.get_purchase_order(world.engine, owner, po.id)
    assert order.payment is not None
    assert (order.payment.state, order.payment.payment_id) == ("funded", payment.id)

    async with tenant_session(world.engine, world.people.tenant) as session:
        actions = list(
            await session.scalars(
                select(AuditEvent.action)
                .where(AuditEvent.action.like("purchase_order.%"))
                .where(AuditEvent.action.like("%payment%") | AuditEvent.action.like("%escrow%"))
                .order_by(AuditEvent.created_at)
            )
        )
    assert actions == [
        "purchase_order.payment_requested",
        "purchase_order.payment_charged",
        "purchase_order.payment_minted",
        "purchase_order.escrow_created",
        "purchase_order.escrow_funded",
    ]
