"""Fixtures and fakes for the RFQ flow tests.

The database fixtures come from tests/db and tests/approvals (skipped on a
laptop without Postgres, failing in CI). `people` gives tenant A an owner
(used as the requesting buyer), two approvers and a member, and tenant B an
approver. `rfq_rows` deletes every RFQ-flow row of both tenants afterwards,
before `people` and `two_tenants` delete the users and tenants.

The agents and the mailbox are fakes: no model, no network.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bido_back.agents.catalog.contracts import (
    RfqEmailLocalizeInput,
    RfqEmailLocalizeOutput,
    RfqIntakeInput,
    RfqIntakeOutput,
    RfqLineDraft,
)
from bido_back.agents.context import ToolContext
from bido_back.db import tenant_session
from bido_back.db.models import (
    Attachment,
    Contact,
    FollowupMessage,
    Invite,
    MailboxConnection,
    Message,
    Proposal,
    ProposalLine,
    Rfq,
    RfqLine,
    RfqTemplate,
    RfqTemplateVersion,
    Supplier,
    SupplierContact,
    User,
)
from bido_back.email.types import EmailSender, OutboundEmail, SentEmail
from bido_back.rfqs.agents import RfqAgents
from bido_back.rfqs.errors import MailboxNotConnected
from tests.approvals.conftest import People, people
from tests.db.conftest import app_engine, migrator_engine, two_tenants

__all__ = ["app_engine", "migrator_engine", "people", "two_tenants"]

INTAKE_OUTPUT = RfqIntakeOutput(
    title="Capacitors and connectors for Q1 build",
    response_due=date(2026, 11, 1),
    lines=[
        RfqLineDraft(
            description="MLCC 100nF 50V X7R 0603",
            part_number="CAP-0603-104",
            mpn="GRM188R71H104KA93D",
            manufacturer="Murata",
            quantities=[Decimal(4000), Decimal(10000)],
            incoterm="DAP",
            delivery_location="Curitiba",
        ),
        RfqLineDraft(
            description="Board-to-board connector 40 pin",
            quantities=[Decimal(500)],
            unit="pcs",
        ),
    ],
    questions=["Which delivery address?"],
)


class FakeIntake:
    def __init__(self, output: RfqIntakeOutput = INTAKE_OUTPUT) -> None:
        self.output = output
        self.calls: list[tuple[RfqIntakeInput, ToolContext]] = []

    async def __call__(self, input: RfqIntakeInput, context: ToolContext) -> RfqIntakeOutput:
        self.calls.append((input, context))
        return self.output


class FakeWriter:
    """Translates by tagging: "Olá" into de is "<de> Olá". Records every task."""

    def __init__(self) -> None:
        self.calls: list[RfqEmailLocalizeInput] = []

    async def __call__(
        self, input: RfqEmailLocalizeInput, context: ToolContext
    ) -> RfqEmailLocalizeOutput:
        self.calls.append(input)
        tag = f"<{input.target_language}>"
        return RfqEmailLocalizeOutput(
            greeting=f"{tag} {input.greeting}", labels=[f"{tag} {label}" for label in input.labels]
        )


def fake_agents() -> RfqAgents:
    return RfqAgents(intake=FakeIntake(), email_writer=FakeWriter())


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[OutboundEmail] = []

    async def send(self, email: OutboundEmail) -> SentEmail:
        self.sent.append(email)
        return SentEmail(message_id=email.message_id or "<missing@test>", raw=b"")


@dataclass
class FakeMailboxes:
    """The mailbox factory seam: every user is connected unless listed."""

    sender: FakeSender = field(default_factory=FakeSender)
    disconnected: set[UUID] = field(default_factory=set[UUID])
    calls: list[tuple[UUID, UUID]] = field(default_factory=list[tuple[UUID, UUID]])

    async def __call__(self, session: AsyncSession, tenant_id: UUID, user_id: UUID) -> EmailSender:
        self.calls.append((tenant_id, user_id))
        if user_id in self.disconnected:
            raise MailboxNotConnected(f"user {user_id} has no connected mailbox")
        return self.sender


async def user_email(engine: AsyncEngine, user_id: UUID) -> str:
    async with engine.connect() as conn:
        email = await conn.scalar(select(User.email).where(User.id == user_id))
    assert email is not None
    return email


@pytest_asyncio.fixture
async def rfq_rows(migrator_engine: AsyncEngine, people: People) -> AsyncIterator[People]:
    yield people
    for tenant_id in (people.tenant, people.other_tenant):
        async with tenant_session(migrator_engine, tenant_id) as session:
            for model in (
                MailboxConnection,
                FollowupMessage,
                ProposalLine,
                Proposal,
                Attachment,
                Message,
                Invite,
                RfqLine,
                Rfq,
                RfqTemplateVersion,
                RfqTemplate,
                SupplierContact,
                Supplier,
                Contact,
            ):
                await session.execute(delete(model).where(model.tenant_id == tenant_id))
