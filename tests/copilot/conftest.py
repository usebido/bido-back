"""Fixtures for the copilot tests: two tenants with RFQ data, fake agents and mailbox.

Database fixtures come from tests/db, tests/approvals and tests/rfqs (skipped
without Postgres). `copilot_rows` also removes the RFQ templates, uploads and
conversation rows the copilot may create. `seed_quote` writes a supplier's proposal directly,
as quote extraction would.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.context import ToolContext
from bido_back.agents.tools import AnyTool
from bido_back.auth import Principal
from bido_back.copilot.tools import CopilotToolDeps, copilot_tools
from bido_back.db import tenant_session
from bido_back.db.models import (
    CopilotConversation,
    Invite,
    Message,
    Proposal,
    ProposalLine,
    RfqTemplate,
    TemplateUpload,
)
from bido_back.rfqs import service
from bido_back.rfqs.schemas import RfqCreated, SupplierView
from tests.approvals.conftest import People, people
from tests.db.conftest import app_engine, migrator_engine, two_tenants
from tests.rfqs.conftest import FakeIntake, FakeMailboxes, fake_agents, rfq_rows
from tests.rfqs.test_service import make_supplier

__all__ = ["app_engine", "migrator_engine", "people", "rfq_rows", "two_tenants"]

MPN = "GRM188R71H104KA93D"  # line 1 of tests.rfqs.conftest.INTAKE_OUTPUT


@pytest_asyncio.fixture
async def copilot_rows(migrator_engine: AsyncEngine, rfq_rows: People) -> AsyncIterator[People]:
    yield rfq_rows
    for tenant_id in (rfq_rows.tenant, rfq_rows.other_tenant):
        async with tenant_session(migrator_engine, tenant_id) as session:
            for model in (RfqTemplate, TemplateUpload, CopilotConversation):
                await session.execute(delete(model).where(model.tenant_id == tenant_id))


def buyer_of(people: People) -> Principal:
    return Principal(tenant_id=people.tenant, user_id=people.owner, role="owner")


def other_buyer_of(people: People) -> Principal:
    return Principal(tenant_id=people.other_tenant, user_id=people.other_approver, role="approver")


def context_of(principal: Principal) -> ToolContext:
    return ToolContext(tenant_id=principal.tenant_id, user_id=principal.user_id, run_id=UUID(int=1))


@dataclass
class Tools:
    tools: dict[str, AnyTool]
    mailboxes: FakeMailboxes
    intake: FakeIntake

    async def call(self, ctx: ToolContext, name: str, **args: object) -> Any:
        tool = self.tools[name]
        return await tool.invoke(ctx, tool.parse_input(args))


def make_tools(engine: AsyncEngine, attachments_text: str = "") -> Tools:
    mailboxes = FakeMailboxes()
    agents = fake_agents()
    assert isinstance(agents.intake, FakeIntake)
    tools = copilot_tools(
        CopilotToolDeps(
            db=engine,
            agents=agents,
            mailboxes=mailboxes,
            attachments_text=attachments_text,
            today=lambda: date(2026, 10, 7),
        )
    )
    return Tools({tool.name: tool for tool in tools}, mailboxes, agents.intake)


async def make_rfq(engine: AsyncEngine, principal: Principal) -> RfqCreated:
    return await service.create_rfq_from_text(engine, principal, "Need MLCC", FakeIntake())


async def invite(
    engine: AsyncEngine, principal: Principal, rfq: RfqCreated, supplier: SupplierView
) -> UUID:
    view = await service.add_invite(engine, principal, rfq.id, supplier.id, supplier.contacts[0].id)
    return view.id


async def seed_quote(
    engine: AsyncEngine,
    tenant_id: UUID,
    invite_id: UUID,
    rfq_line_id: UUID,
    price: str,
    *,
    quoted_at: datetime | None = None,
    missing_fields: list[str] | None = None,
    needs_review: bool = False,
) -> None:
    """A responded invite with one proposal quoting one line, as extraction stores it."""
    when = quoted_at or datetime.now(UTC)
    async with tenant_session(engine, tenant_id) as session:
        found = await session.scalar(select(Invite).where(Invite.id == invite_id))
        assert found is not None
        found.status = "responded"
        found.responded_at = when
        message = Message(
            tenant_id=tenant_id,
            invite_id=invite_id,
            direction="in",
            message_id_header=f"<{invite_id}-{price}@supplier.example.test>",
            occurred_at=when,
        )
        session.add(message)
        await session.flush()
        proposal = Proposal(
            tenant_id=tenant_id,
            invite_id=invite_id,
            message_id=message.id,
            version=1,
            currency="EUR",
            valid_until=date(2026, 12, 31),
            incoterm="DAP",
            fields={"_review": {"missing_fields": missing_fields or []}},
            created_at=when,
        )
        session.add(proposal)
        await session.flush()
        session.add(
            ProposalLine(
                tenant_id=tenant_id,
                proposal_id=proposal.id,
                rfq_line_id=rfq_line_id,
                offered_mpn=MPN,
                min_quantity=Decimal(4000),
                unit_price=Decimal(price),
                unit="pcs",
                lead_time_days=42,
                needs_review=needs_review,
            )
        )
        await session.flush()


async def supplier(engine: AsyncEngine, principal: Principal, name: str) -> SupplierView:
    return await make_supplier(engine, principal, name)
