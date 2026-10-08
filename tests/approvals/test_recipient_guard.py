"""Outbound recipient guard: emails go only to test domains or confirmed domains."""

from uuid import UUID

import pytest
from pydantic import JsonValue
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.approvals import ApprovalRecipientsUnconfirmed, decide_approval
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import OutboundDomainConfirmation, Tenant
from bido_back.email.types import OutboundEmail, SentEmail
from bido_back.outbound.domains import (
    RecipientDomainsNotConfirmed,
    check_recipients,
    confirm_domains,
    unconfirmed_domains,
)
from bido_back.outbound.sender import guarded

from .conftest import FakeJobQueue, People, load, propose

pytestmark = pytest.mark.db


def owner(people: People) -> Principal:
    return Principal(tenant_id=people.tenant, user_id=people.owner)


def email_args(*to: str) -> dict[str, JsonValue]:
    return {"to": list(to), "cc": [], "subject": "RFQ-1", "body": "hello"}


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[OutboundEmail] = []
        self.address = "buyer@acme.test"

    async def send(self, email: OutboundEmail) -> SentEmail:
        self.sent.append(email)
        return SentEmail(message_id="<m@x>", raw=b"")


class StubResolver:
    async def has_mx(self, domain: str) -> bool | None:
        return {"rame-italia.it": True, "nomail-xyz.com": False}.get(domain)


async def _guard_off(migrator_engine: AsyncEngine, tenant_id: UUID) -> None:
    async with tenant_session(migrator_engine, tenant_id) as session:
        await session.execute(
            update(Tenant)
            .where(Tenant.id == tenant_id)
            .values(outbound_requires_confirmed_domains=False)
        )


async def test_test_domains_pass_and_real_domains_need_confirmation(
    app_engine: AsyncEngine, people: People
) -> None:
    async with tenant_session(app_engine, people.tenant) as session:
        blocked = await unconfirmed_domains(
            session, people.tenant, ["a@supplier.test", "b@rame-italia.it", "c@Rame-Italia.it"]
        )
        assert blocked == ["rame-italia.it"]
        await confirm_domains(session, people.tenant, people.owner, ["Rame-Italia.IT"])
    async with tenant_session(app_engine, people.tenant) as session:
        assert await unconfirmed_domains(session, people.tenant, ["b@rame-italia.it"]) == []


async def test_check_recipients_reports_dns_and_what_would_send(
    app_engine: AsyncEngine, people: People
) -> None:
    async with tenant_session(app_engine, people.tenant) as session:
        checks = await check_recipients(
            session,
            people.tenant,
            ["a@supplier.test", "b@rame-italia.it", "c@nomail-xyz.com", "d@slow.com"],
            StubResolver(),
        )
    assert [(c.status, c.will_send) for c in checks] == [
        ("test", True),
        ("real_domain", False),
        ("no_mail", False),
        ("unknown", False),
    ]


async def test_guard_off_lets_every_domain_through(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, people: People
) -> None:
    await _guard_off(migrator_engine, people.tenant)
    async with tenant_session(app_engine, people.tenant) as session:
        assert await unconfirmed_domains(session, people.tenant, ["b@rame-italia.it"]) == []


async def test_guarded_sender_holds_unconfirmed_and_sends_the_rest(
    app_engine: AsyncEngine, people: People
) -> None:
    inner = RecordingSender()

    async def factory(*_: object) -> RecordingSender:
        return inner

    make = guarded(factory)
    async with tenant_session(app_engine, people.tenant) as session:
        sender = await make(session, people.tenant, people.owner)
        assert getattr(sender, "address", None) == "buyer@acme.test"
        with pytest.raises(RecipientDomainsNotConfirmed) as raised:
            await sender.send(
                OutboundEmail(
                    from_name="B",
                    from_address="buyer@acme.test",
                    to=("s@rame-italia.it",),
                    subject="x",
                    text_body="y",
                )
            )
        assert raised.value.domains == ["rame-italia.it"]
        await sender.send(
            OutboundEmail(
                from_name="B",
                from_address="buyer@acme.test",
                to=("s@supplier.test",),
                subject="x",
                text_body="y",
            )
        )
    assert [e.to for e in inner.sent] == [("s@supplier.test",)]


async def test_approving_an_email_to_an_unconfirmed_domain_is_refused(
    app_engine: AsyncEngine, people: People
) -> None:
    approval_id = await propose(
        app_engine,
        people.tenant,
        people.owner,
        email_args("s@rame-italia.it", "t@supplier.test"),
        tool="send_rfq_email",
    )
    jobs = FakeJobQueue()

    with pytest.raises(ApprovalRecipientsUnconfirmed) as raised:
        await decide_approval(app_engine, owner(people), approval_id, "approve", jobs=jobs)

    assert raised.value.domains == ["rame-italia.it"]
    assert (await load(app_engine, people.tenant, approval_id)).status == "pending"
    assert jobs.jobs == []


async def test_confirming_the_domain_with_the_decision_approves_and_records_it(
    app_engine: AsyncEngine, people: People
) -> None:
    approval_id = await propose(
        app_engine,
        people.tenant,
        people.owner,
        email_args("s@rame-italia.it"),
        tool="send_rfq_email",
    )
    jobs = FakeJobQueue()

    outcome = await decide_approval(
        app_engine,
        owner(people),
        approval_id,
        "approve",
        jobs=jobs,
        # A domain the email does not write to is ignored.
        confirmed_domains=["RAME-ITALIA.IT", "other.com"],
    )

    assert outcome.status == "approved"
    async with tenant_session(app_engine, people.tenant) as session:
        rows = list(await session.scalars(select(OutboundDomainConfirmation)))
    assert [(r.domain, r.confirmed_by, r.approval_id) for r in rows] == [
        ("rame-italia.it", people.owner, approval_id)
    ]


async def test_rejecting_needs_no_confirmation(app_engine: AsyncEngine, people: People) -> None:
    approval_id = await propose(
        app_engine,
        people.tenant,
        people.owner,
        email_args("s@rame-italia.it"),
        tool="send_rfq_email",
    )

    outcome = await decide_approval(
        app_engine, owner(people), approval_id, "reject", jobs=FakeJobQueue()
    )

    assert outcome.status == "rejected"


async def test_acts_without_email_are_not_checked(app_engine: AsyncEngine, people: People) -> None:
    approval_id = await propose(app_engine, people.tenant, people.owner)

    outcome = await decide_approval(
        app_engine, owner(people), approval_id, "approve", jobs=FakeJobQueue()
    )

    assert outcome.status == "approved"


# --- Free mailbox providers: the exact address, never the domain ----------------------------


async def test_a_consumer_address_is_confirmed_alone_never_its_domain(
    app_engine: AsyncEngine, people: People
) -> None:
    async with tenant_session(app_engine, people.tenant) as session:
        blocked = await unconfirmed_domains(
            session, people.tenant, ["Joao.Supplier@Gmail.com", "b@rame-italia.it"]
        )
        # The block names the address at gmail.com, the domain elsewhere.
        assert blocked == ["joao.supplier@gmail.com", "rame-italia.it"]
        # A bare consumer domain cannot be confirmed: it would open every address there.
        assert await confirm_domains(session, people.tenant, people.owner, ["gmail.com"]) == []
        assert await confirm_domains(
            session, people.tenant, people.owner, ["joao.supplier@GMAIL.com"]
        ) == ["joao.supplier@gmail.com"]
    async with tenant_session(app_engine, people.tenant) as session:
        assert await unconfirmed_domains(session, people.tenant, ["joao.supplier@gmail.com"]) == []
        # Another gmail.com address is still blocked.
        assert await unconfirmed_domains(session, people.tenant, ["someone.else@gmail.com"]) == [
            "someone.else@gmail.com"
        ]


async def test_a_consumer_domain_confirmed_before_the_rule_matches_nothing(
    app_engine: AsyncEngine, people: People
) -> None:
    async with tenant_session(app_engine, people.tenant) as session:
        session.add(
            OutboundDomainConfirmation(
                tenant_id=people.tenant, domain="gmail.com", confirmed_by=people.owner
            )
        )
    async with tenant_session(app_engine, people.tenant) as session:
        assert await unconfirmed_domains(session, people.tenant, ["x@gmail.com"]) == ["x@gmail.com"]


async def test_check_recipients_says_what_to_confirm(
    app_engine: AsyncEngine, people: People
) -> None:
    class GmailHasMx:
        async def has_mx(self, domain: str) -> bool | None:
            return True

    async with tenant_session(app_engine, people.tenant) as session:
        checks = await check_recipients(
            session,
            people.tenant,
            ["a@supplier.test", "b@rame-italia.it", "C@Gmail.com", "d@yahoo.com.br"],
            GmailHasMx(),
        )
    assert [(c.confirm_scope, c.confirm_key) for c in checks] == [
        (None, None),
        ("domain", "rame-italia.it"),
        ("address", "c@gmail.com"),
        ("address", "d@yahoo.com.br"),
    ]


async def test_approving_to_a_gmail_supplier_confirms_only_that_address(
    app_engine: AsyncEngine, people: People
) -> None:
    approval_id = await propose(
        app_engine,
        people.tenant,
        people.owner,
        email_args("fornecedor.a@gmail.com"),
        tool="send_rfq_email",
    )
    jobs = FakeJobQueue()

    # Confirming the bare domain does not unlock it.
    with pytest.raises(ApprovalRecipientsUnconfirmed) as raised:
        await decide_approval(
            app_engine,
            owner(people),
            approval_id,
            "approve",
            jobs=jobs,
            confirmed_domains=["gmail.com"],
        )
    assert raised.value.domains == ["fornecedor.a@gmail.com"]

    outcome = await decide_approval(
        app_engine,
        owner(people),
        approval_id,
        "approve",
        jobs=jobs,
        confirmed_domains=["Fornecedor.A@gmail.com"],
    )
    assert outcome.status == "approved"
    async with tenant_session(app_engine, people.tenant) as session:
        rows = list(await session.scalars(select(OutboundDomainConfirmation)))
        assert [r.domain for r in rows] == ["fornecedor.a@gmail.com"]
        assert await unconfirmed_domains(session, people.tenant, ["fornecedor.b@gmail.com"]) == [
            "fornecedor.b@gmail.com"
        ]
