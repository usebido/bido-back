"""Fixtures and fakes for the onboarding tests.

The database fixtures come from tests/db and tests/approvals (skipped on a
laptop without Postgres, failing in CI). `onboarding_rows` gives tenant A's
owner an active Gmail connection and deletes every onboarding row of both
tenants afterwards. The mailbox, the agents, the queue and storage are fakes:
no model, no network.
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import pytest_asyncio
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.catalog.contracts import (
    RfqCandidateClassifierInput,
    RfqCandidateClassifierOutput,
    RfqCandidateVerdict,
    RfqTemplateBuilderInput,
    RfqTemplateBuilderOutput,
)
from bido_back.agents.context import ToolContext
from bido_back.db import tenant_session
from bido_back.db.models import (
    MailboxConnection,
    Membership,
    RfqTemplate,
    TemplateScan,
    TemplateUpload,
)
from bido_back.mailbox.base import ProviderError, SentAttachment, SentSummary
from bido_back.onboarding.agents import AgentFailed, OnboardingAgents
from bido_back.storage import BlobNotFound
from bido_back.worker.envelope import Envelope
from tests.approvals.conftest import People, people
from tests.db.conftest import app_engine, migrator_engine, two_tenants

__all__ = ["app_engine", "migrator_engine", "people", "two_tenants"]


def summary(
    message_id: str,
    subject: str,
    *,
    recipients: Sequence[str] = ("vendas@metalflex.example",),
    day: int = 12,
    attachments: Sequence[str] = ("itens.xlsx",),
) -> SentSummary:
    return SentSummary(
        provider_message_id=message_id,
        subject=subject,
        recipients=tuple(recipients),
        sent_at=datetime(2026, 9, day, 10, 0, tzinfo=UTC),
        attachments=tuple(SentAttachment(name, "application/octet-stream") for name in attachments),
    )


@dataclass
class FakeMailbox:
    pages: list[list[SentSummary]] = field(default_factory=list[list[SentSummary]])
    messages: dict[str, bytes] = field(default_factory=dict[str, bytes])
    fetched: list[str] = field(default_factory=list[str])
    list_error: Exception | None = None
    listed_with: list[tuple[tuple[str, ...], datetime, int]] = field(
        default_factory=list[tuple[tuple[str, ...], datetime, int]]
    )

    async def list_sent(
        self, *, keywords: Sequence[str], since: datetime, limit: int
    ) -> AsyncIterator[list[SentSummary]]:
        self.listed_with.append((tuple(keywords), since, limit))
        if self.list_error is not None:
            raise self.list_error
        for page in self.pages:
            yield page

    async def get_message(self, provider_message_id: str) -> bytes:
        self.fetched.append(provider_message_id)
        if provider_message_id not in self.messages:
            raise ProviderError("google", 404, "message")
        return self.messages[provider_message_id]


@dataclass
class FakeAgents:
    """Classifier: a reply prefix means 'not sure'; 'Newsletter' means surely not; else an RFQ."""

    output: RfqTemplateBuilderOutput = field(
        default_factory=lambda: RfqTemplateBuilderOutput(
            language="pt",
            greeting="Prezados,  segue solicitação de cotação para os itens abaixo.",
            columns=["Item", "Qtd.", "Un.", "Prazo", "Item"],
            footer_fields=["Validade da proposta", "Frete"],
            fields=["Item e descrição", "Quantidade", "Frete", "frete"],
        )
    )
    fail_build: bool = False
    classified: list[RfqCandidateClassifierInput] = field(
        default_factory=list[RfqCandidateClassifierInput]
    )
    built: list[RfqTemplateBuilderInput] = field(default_factory=list[RfqTemplateBuilderInput])

    async def classify(
        self, input: RfqCandidateClassifierInput, context: ToolContext
    ) -> RfqCandidateClassifierOutput:
        self.classified.append(input)
        verdicts: list[RfqCandidateVerdict] = []
        for c in input.candidates:
            if c.subject.startswith("Newsletter"):
                verdicts.append(RfqCandidateVerdict(id=c.id, is_rfq=False, confident=True))
            elif c.subject.lower().startswith("re:"):
                verdicts.append(
                    RfqCandidateVerdict(
                        id=c.id,
                        is_rfq=False,
                        confident=False,
                        note="Parece uma resposta, não um RFQ",
                    )
                )
            else:
                verdicts.append(RfqCandidateVerdict(id=c.id, is_rfq=True, confident=True))
        return RfqCandidateClassifierOutput(verdicts=verdicts)

    async def build(
        self, input: RfqTemplateBuilderInput, context: ToolContext
    ) -> RfqTemplateBuilderOutput:
        self.built.append(input)
        if self.fail_build:
            raise AgentFailed("invalid_output")
        return self.output

    def seam(self) -> OnboardingAgents:
        return OnboardingAgents(classify=self.classify, build=self.build)


class FakeQueue:
    def __init__(self) -> None:
        self.envelopes: list[Envelope] = []

    async def enqueue(self, envelope: Envelope) -> None:
        self.envelopes.append(envelope)


class FakeStorage:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.blobs[key] = data

    async def get(self, key: str) -> bytes:
        if key not in self.blobs:
            raise BlobNotFound(key)
        return self.blobs[key]


@dataclass(frozen=True)
class Onboarding:
    people: People
    connection_id: UUID


@pytest_asyncio.fixture
async def onboarding_rows(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, people: People
) -> AsyncIterator[Onboarding]:
    async with tenant_session(app_engine, people.tenant) as session:
        connection = MailboxConnection(
            tenant_id=people.tenant,
            user_id=people.owner,
            provider="google",
            email_address="owner@acme.example",
            token_ciphertext=b"not-real-ciphertext",
            status="active",
        )
        session.add(connection)
        await session.flush()
        connection_id = connection.id
    yield Onboarding(people=people, connection_id=connection_id)
    for tenant_id in (people.tenant, people.other_tenant):
        async with tenant_session(migrator_engine, tenant_id) as session:
            for model in (RfqTemplate, TemplateScan, TemplateUpload, MailboxConnection):
                await session.execute(delete(model).where(model.tenant_id == tenant_id))
            await session.execute(
                update(Membership)
                .where(Membership.tenant_id == tenant_id)
                .values(onboarded_at=None)
            )
