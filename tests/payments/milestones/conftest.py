"""Milestone tests: a payment on the tooling plan, funded on the fake chain, and cleanup.

`milestone_rows` deletes the milestone tables (as the migrator: the app role may not
DELETE them) before `payment_rows` deletes the payments. `funded` is a PO payment with
the tooling plan (Tool start 30%, T1 30%, PPAP 40%), approved, charged and advanced on
`FakePaymentChain` until its escrow is funded: milestone 0 can be sent.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import pytest_asyncio
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.db import tenant_session
from bido_back.db.models import (
    PoFactoryLink,
    PoMilestoneDocument,
    PoMilestoneSubmission,
    PoPayment,
    PoPaymentMilestone,
)
from bido_back.payments.pipeline import PaymentAdvanceHandler
from bido_back.payments.schemas import tooling_plan
from tests.approvals.conftest import FakeJobQueue, People
from tests.awards.test_awards_db import World
from tests.payments.fakes import FakePaymentChain, FakeProviders
from tests.payments.flow.conftest import (
    app_engine,
    award_rows,
    load_payment,
    migrator_engine,
    payment_rows,
    people,
    propose_payment,
    providers,
    rfq_rows,
    two_tenants,
)
from tests.payments.flow.test_execute_db import Paying, start_paying

__all__ = [
    "app_engine",
    "award_rows",
    "migrator_engine",
    "payment_rows",
    "people",
    "providers",
    "rfq_rows",
    "two_tenants",
]


@pytest_asyncio.fixture
async def milestone_rows(
    migrator_engine: AsyncEngine, payment_rows: People
) -> AsyncIterator[People]:
    yield payment_rows
    for tenant_id in (payment_rows.tenant, payment_rows.other_tenant):
        async with tenant_session(migrator_engine, tenant_id) as session:
            await session.execute(
                delete(PoMilestoneDocument).where(PoMilestoneDocument.tenant_id == tenant_id)
            )
            await session.execute(
                update(PoPaymentMilestone)
                .where(PoPaymentMilestone.tenant_id == tenant_id)
                # `open` with no ids passes every check, whatever state the test left.
                .values(state="open", submission_id=None, approval_id=None, evidence_hash=None)
            )
            await session.execute(
                delete(PoMilestoneSubmission).where(PoMilestoneSubmission.tenant_id == tenant_id)
            )
            await session.execute(
                delete(PoPaymentMilestone).where(PoPaymentMilestone.tenant_id == tenant_id)
            )
            await session.execute(delete(PoFactoryLink).where(PoFactoryLink.tenant_id == tenant_id))


@pytest_asyncio.fixture
async def world(app_engine: AsyncEngine, milestone_rows: People) -> World:
    return await World(app_engine, milestone_rows).setup()


@dataclass
class Funded:
    """A funded payment on the tooling plan: its escrow holds the total on the fake chain."""

    paying: Paying
    payment_id: UUID
    approval_id: UUID

    @property
    def world(self) -> World:
        return self.paying.world

    @property
    def engine(self) -> AsyncEngine:
        return self.paying.engine

    @property
    def tenant(self) -> UUID:
        return self.paying.tenant

    @property
    def providers(self) -> FakeProviders:
        return self.paying.providers

    @property
    def chain(self) -> FakePaymentChain:
        return self.paying.providers.chain

    @property
    def jobs(self) -> FakeJobQueue:
        return self.paying.world.jobs

    @property
    def people(self) -> People:
        return self.paying.world.people

    @property
    def po_id(self) -> UUID:
        return self.paying.po.id

    async def payment(self) -> PoPayment:
        return await load_payment(self.engine, self.tenant, self.payment_id)


async def fund(world: World, providers: FakeProviders) -> Funded:
    """Propose, approve, charge and fund a payment on the tooling plan."""
    paying = await start_paying(world, providers)
    proposed = await propose_payment(
        world.engine,
        paying.po,
        requested_by=world.people.owner,
        payment_method_id=paying.method_id,
        payout_account_id=paying.account_id,
        milestones=tooling_plan(),
    )
    await world.approve(proposed.approval_id)
    handler = PaymentAdvanceHandler(world.engine, providers.bundle(), None, poll_budget=0)
    state = ""
    for _ in range(4):
        state = await handler.advance(world.people.tenant, proposed.payment_id)
        if state == "funded":
            break
    assert state == "funded", state
    return Funded(paying, proposed.payment_id, proposed.approval_id)


@pytest_asyncio.fixture
async def funded(world: World, providers: FakeProviders) -> Funded:
    return await fund(world, providers)
