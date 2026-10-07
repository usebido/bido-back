"""TEMPLATE_SCAN and TEMPLATE_BUILD against Postgres, with a fake mailbox and fake agents."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.db import tenant_session
from bido_back.db.models import RfqTemplate, TemplateScan, TemplateUpload
from bido_back.mailbox.base import SCAN_KEYWORDS, ConsentRevoked, MailboxNotConnected
from bido_back.onboarding.jobs import (
    SCAN_LIMIT,
    OnboardingJobDeps,
    ScanMailbox,
    handle_template_build,
    handle_template_scan,
)
from bido_back.worker.envelope import Envelope
from bido_back.worker.job_types import TEMPLATE_BUILD, TEMPLATE_SCAN
from tests.documents.fixtures import build_xlsx
from tests.mailbox.support import raw_email

from .conftest import FakeAgents, FakeMailbox, FakeStorage, Onboarding, summary

pytestmark = pytest.mark.db


def deps(
    engine: AsyncEngine,
    mailbox: FakeMailbox | None = None,
    agents: FakeAgents | None = None,
    storage: FakeStorage | None = None,
    opened: list[tuple[UUID, UUID]] | None = None,
) -> OnboardingJobDeps:
    async def open_mailbox(tenant_id: UUID, connection_id: UUID) -> ScanMailbox:
        if opened is not None:
            opened.append((tenant_id, connection_id))
        if mailbox is None:
            raise MailboxNotConnected("no mailbox")
        return mailbox

    return OnboardingJobDeps(
        db=engine,
        storage=storage or FakeStorage(),
        agents=(agents or FakeAgents()).seam(),
        open_mailbox=open_mailbox,
    )


async def new_scan(engine: AsyncEngine, rows: Onboarding) -> UUID:
    async with tenant_session(engine, rows.people.tenant) as session:
        scan = TemplateScan(
            tenant_id=rows.people.tenant,
            user_id=rows.people.owner,
            mailbox_connection_id=rows.connection_id,
        )
        session.add(scan)
        await session.flush()
        return scan.id


async def load_scan(engine: AsyncEngine, tenant_id: UUID, scan_id: UUID) -> TemplateScan:
    async with tenant_session(engine, tenant_id) as session:
        scan = await session.scalar(select(TemplateScan).where(TemplateScan.id == scan_id))
        assert scan is not None
        return scan


def scan_envelope(tenant_id: UUID, scan_id: UUID) -> Envelope:
    return Envelope(
        type=TEMPLATE_SCAN,
        tenant_id=tenant_id,
        idempotency_key=f"{TEMPLATE_SCAN}:{scan_id}",
        payload={"scan_id": str(scan_id)},
    )


def build_envelope(tenant_id: UUID, template_id: UUID) -> Envelope:
    return Envelope(
        type=TEMPLATE_BUILD,
        tenant_id=tenant_id,
        idempotency_key=f"{TEMPLATE_BUILD}:{template_id}",
        payload={"template_id": str(template_id)},
    )


PAGES = [
    [
        summary("m1", 'Cotação – Válvulas esfera 2" (Pedido 4471)', day=12),
        summary("m2", "Newsletter de setembro", day=11, attachments=()),
        summary("m3", "Re: Proposta comercial – Manutenção", day=2, attachments=()),
    ],
    [summary("m4", "RFQ 2024-118 – Rolamentos 6205", day=15)],
]


async def test_scan_keeps_rfqs_and_doubts_and_reports_progress(
    app_engine: AsyncEngine, onboarding_rows: Onboarding
) -> None:
    rows = onboarding_rows
    scan_id = await new_scan(app_engine, rows)
    mailbox = FakeMailbox(pages=PAGES)
    agents = FakeAgents()
    opened: list[tuple[UUID, UUID]] = []

    await handle_template_scan(
        scan_envelope(rows.people.tenant, scan_id), deps(app_engine, mailbox, agents, opened=opened)
    )

    scan = await load_scan(app_engine, rows.people.tenant, scan_id)
    assert scan.status == "done" and scan.error is None
    assert (scan.checked, scan.found) == (4, 3)
    assert opened == [(rows.people.tenant, rows.connection_id)]
    keywords, since, limit = mailbox.listed_with[0]
    assert keywords == SCAN_KEYWORDS and limit == SCAN_LIMIT
    assert datetime.now(UTC) - since > timedelta(days=360)
    # Newest first; the newsletter is gone; the reply is kept, unticked, with a note.
    by_subject = {c["subject"]: c for c in scan.candidates}
    assert [c["provider_message_id"] for c in scan.candidates] == ["m4", "m1", "m3"]
    assert by_subject["Re: Proposta comercial – Manutenção"]["preselected"] is False
    assert by_subject["Re: Proposta comercial – Manutenção"]["note"]
    assert by_subject["RFQ 2024-118 – Rolamentos 6205"]["preselected"] is True
    # The model saw metadata only: never the addresses.
    assert "vendas@" not in agents.classified[0].model_dump_json()
    # Bodies are never stored.
    assert all(set(c) >= {"id", "subject"} and "body" not in c for c in scan.candidates)


async def test_scan_runs_once_and_a_redelivery_does_nothing(
    app_engine: AsyncEngine, onboarding_rows: Onboarding
) -> None:
    rows = onboarding_rows
    scan_id = await new_scan(app_engine, rows)
    mailbox = FakeMailbox(pages=PAGES)
    envelope = scan_envelope(rows.people.tenant, scan_id)

    await handle_template_scan(envelope, deps(app_engine, mailbox))
    await handle_template_scan(envelope, deps(app_engine, mailbox))

    assert len(mailbox.listed_with) == 1


async def test_a_fresh_claim_blocks_a_concurrent_delivery_but_a_stale_one_does_not(
    app_engine: AsyncEngine, onboarding_rows: Onboarding
) -> None:
    rows = onboarding_rows
    scan_id = await new_scan(app_engine, rows)
    async with tenant_session(app_engine, rows.people.tenant) as session:
        await session.execute(
            update(TemplateScan)
            .where(TemplateScan.id == scan_id)
            .values(started_at=datetime.now(UTC))
        )
    mailbox = FakeMailbox(pages=PAGES)
    envelope = scan_envelope(rows.people.tenant, scan_id)

    await handle_template_scan(envelope, deps(app_engine, mailbox))
    assert mailbox.listed_with == []

    async with tenant_session(app_engine, rows.people.tenant) as session:
        await session.execute(
            update(TemplateScan)
            .where(TemplateScan.id == scan_id)
            .values(started_at=datetime.now(UTC) - timedelta(hours=1))
        )
    await handle_template_scan(envelope, deps(app_engine, mailbox))
    assert len(mailbox.listed_with) == 1
    assert (await load_scan(app_engine, rows.people.tenant, scan_id)).status == "done"


@pytest.mark.parametrize(
    ("mailbox", "code"),
    [
        (None, "mailbox_not_connected"),
        (FakeMailbox(list_error=ConsentRevoked("gone")), "mailbox_revoked"),
    ],
)
async def test_scan_failures_end_the_scan_with_a_code(
    app_engine: AsyncEngine, onboarding_rows: Onboarding, mailbox: FakeMailbox | None, code: str
) -> None:
    rows = onboarding_rows
    scan_id = await new_scan(app_engine, rows)

    await handle_template_scan(
        scan_envelope(rows.people.tenant, scan_id), deps(app_engine, mailbox)
    )

    scan = await load_scan(app_engine, rows.people.tenant, scan_id)
    assert (scan.status, scan.error) == ("failed", code)


async def test_another_tenant_cannot_run_a_scan(
    app_engine: AsyncEngine, onboarding_rows: Onboarding
) -> None:
    rows = onboarding_rows
    scan_id = await new_scan(app_engine, rows)
    mailbox = FakeMailbox(pages=PAGES)

    await handle_template_scan(
        scan_envelope(rows.people.other_tenant, scan_id), deps(app_engine, mailbox)
    )

    assert mailbox.listed_with == []
    assert (await load_scan(app_engine, rows.people.tenant, scan_id)).status == "running"


# --- TEMPLATE_BUILD ---------------------------------------------------------------------


async def new_template(
    engine: AsyncEngine, rows: Onboarding, source: str, refs: dict[str, object]
) -> UUID:
    async with tenant_session(engine, rows.people.tenant) as session:
        template = RfqTemplate(
            tenant_id=rows.people.tenant,
            created_by=rows.people.owner,
            source=source,
            source_refs=refs,
        )
        session.add(template)
        await session.flush()
        return template.id


async def load_template(engine: AsyncEngine, tenant_id: UUID, template_id: UUID) -> RfqTemplate:
    async with tenant_session(engine, tenant_id) as session:
        template = await session.scalar(select(RfqTemplate).where(RfqTemplate.id == template_id))
        assert template is not None
        return template


async def test_build_from_picked_candidates_reads_only_listed_messages(
    app_engine: AsyncEngine, onboarding_rows: Onboarding
) -> None:
    rows = onboarding_rows
    scan_id = await new_scan(app_engine, rows)
    mailbox = FakeMailbox(
        pages=PAGES,
        messages={
            "m1": raw_email(
                from_address="owner@acme.example",
                to="vendas@metalflex.example",
                subject="Cotação – Válvulas",
                body="Prezados, segue solicitação de cotação. Favor informar frete.",
            )
        },
    )
    await handle_template_scan(
        scan_envelope(rows.people.tenant, scan_id), deps(app_engine, mailbox)
    )
    scan = await load_scan(app_engine, rows.people.tenant, scan_id)
    ids = {c["provider_message_id"]: c["id"] for c in scan.candidates}

    agents = FakeAgents()
    template_id = await new_template(
        app_engine,
        rows,
        "search",
        # "m2" was never a candidate: a forged id is ignored, never fetched.
        {"scan_id": str(scan_id), "candidate_ids": [ids["m1"], ids["m3"], "m2", "c999"]},
    )
    await handle_template_build(
        build_envelope(rows.people.tenant, template_id), deps(app_engine, mailbox, agents)
    )

    assert mailbox.fetched == ["m1", "m3"]  # m3 is gone (404) and skipped
    template = await load_template(app_engine, rows.people.tenant, template_id)
    assert template.status == "ready", template.error
    assert template.greeting == "Prezados, segue solicitação de cotação para os itens abaixo."
    assert template.columns == ["Item", "Qtd.", "Un.", "Prazo"]
    assert template.footer == "Validade da proposta · Frete"
    assert template.fields == ["Item e descrição", "Quantidade", "Frete"]
    assert template.language == "pt"
    [task] = agents.built
    assert "Favor informar frete" in task.examples


async def test_build_from_uploads(app_engine: AsyncEngine, onboarding_rows: Onboarding) -> None:
    rows = onboarding_rows
    storage = FakeStorage()
    storage.blobs["onboarding/x.xlsx"] = build_xlsx({"RFQ": [["Item", "Qtd."], ["Rolamento", 200]]})
    async with tenant_session(app_engine, rows.people.tenant) as session:
        upload = TemplateUpload(
            tenant_id=rows.people.tenant,
            user_id=rows.people.owner,
            filename="pedido.xlsx",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=10,
            storage_key="onboarding/x.xlsx",
        )
        session.add(upload)
        await session.flush()
        upload_id = upload.id
    agents = FakeAgents()
    template_id = await new_template(app_engine, rows, "upload", {"upload_ids": [str(upload_id)]})

    await handle_template_build(
        build_envelope(rows.people.tenant, template_id),
        deps(app_engine, agents=agents, storage=storage),
    )

    assert (await load_template(app_engine, rows.people.tenant, template_id)).status == "ready"
    assert "Rolamento" in agents.built[0].examples


@pytest.mark.parametrize(
    ("fail_build", "code"), [(True, "agent_failed"), (False, "nothing_readable")]
)
async def test_build_failures_end_the_template_with_a_code(
    app_engine: AsyncEngine, onboarding_rows: Onboarding, fail_build: bool, code: str
) -> None:
    rows = onboarding_rows
    storage = FakeStorage()
    storage.blobs["k"] = build_xlsx({"RFQ": [["Item"]]}) if fail_build else b"\x00\x01 junk"
    async with tenant_session(app_engine, rows.people.tenant) as session:
        upload = TemplateUpload(
            tenant_id=rows.people.tenant,
            user_id=rows.people.owner,
            filename="pedido.xls" if not fail_build else "pedido.xlsx",
            content_type="application/vnd.ms-excel",
            size_bytes=10,
            storage_key="k",
        )
        session.add(upload)
        await session.flush()
        upload_id = upload.id
    template_id = await new_template(app_engine, rows, "upload", {"upload_ids": [str(upload_id)]})

    await handle_template_build(
        build_envelope(rows.people.tenant, template_id),
        deps(app_engine, agents=FakeAgents(fail_build=fail_build), storage=storage),
    )

    template = await load_template(app_engine, rows.people.tenant, template_id)
    assert (template.status, template.error) == ("failed", code)


async def test_build_runs_once(app_engine: AsyncEngine, onboarding_rows: Onboarding) -> None:
    rows = onboarding_rows
    agents = FakeAgents()
    template_id = await new_template(app_engine, rows, "upload", {"upload_ids": [str(uuid4())]})
    envelope = build_envelope(rows.people.tenant, template_id)

    await handle_template_build(envelope, deps(app_engine, agents=agents))
    await handle_template_build(envelope, deps(app_engine, agents=agents))

    template = await load_template(app_engine, rows.people.tenant, template_id)
    assert (template.status, template.error) == ("failed", "no_examples")
    assert agents.built == []
