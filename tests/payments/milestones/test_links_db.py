"""The factory's link: issued for a funded payment, rotated, revoked, expired; one answer for
every bad token; the factory confirms its order once; a token reaches its own tenant only."""

import hashlib
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update

from bido_back.db import tenant_session
from bido_back.db.models import PoFactoryLink, PoPayment
from bido_back.payments.errors import FactoryLinkInvalid, PaymentInvalidState, PaymentNotFound
from bido_back.payments.milestones.links import (
    FACTORY_ORDER_ACCEPTED,
    INVALID_LINK,
    FactoryAccess,
    accept_order,
    factory_token_hash,
    issue_factory_link,
    new_factory_token,
    resolve_factory_link,
    revoke_factory_link,
)
from bido_back.payments.milestones.proposals import FACTORY_LINK_ISSUED, FACTORY_LINK_REVOKED
from bido_back.payments.milestones.service import factory_order
from tests.awards.test_awards_db import buyer
from tests.payments.flow.test_service_db import member
from tests.payments.milestones.conftest import Funded
from tests.payments.milestones.test_documents_db import (
    FACTORY_PAGE,
    SETTINGS,
    audit_payloads,
    factory_link,
    outsider,
    release_settings,
)

pytestmark = pytest.mark.db


async def link_rows(funded: Funded) -> list[PoFactoryLink]:
    async with tenant_session(funded.engine, funded.tenant) as session:
        return list(
            await session.scalars(
                select(PoFactoryLink)
                .where(PoFactoryLink.payment_id == funded.payment_id)
                .order_by(PoFactoryLink.created_at)
            )
        )


def test_a_token_is_256_random_bits_and_bido_keeps_its_hash() -> None:
    first, second = new_factory_token(), new_factory_token()
    assert first != second
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", first)
    assert factory_token_hash(first) == hashlib.sha256(first.encode()).hexdigest()


async def test_the_buyer_issues_a_link_shown_once(funded: Funded) -> None:
    before = datetime.now(UTC)
    link = await issue_factory_link(
        funded.engine, member(funded.people), funded.po_id, settings=SETTINGS
    )

    [row] = await link_rows(funded)
    assert link.url == f"{FACTORY_PAGE}#{link.token}"
    assert row.token_hash == factory_token_hash(link.token)
    assert row.created_by == funded.people.member and row.accepted_at is None
    ttl = timedelta(days=SETTINGS.factory_link_ttl_days)
    assert before + ttl <= link.expires_at == row.expires_at <= datetime.now(UTC) + ttl
    # Nothing Bido keeps holds the token: the row has its hash, the audit event ids only.
    async with tenant_session(funded.engine, funded.tenant) as session:
        stored = await session.scalar(
            text("SELECT row_to_json(l)::text FROM po_factory_links AS l WHERE l.id = :id"),
            {"id": row.id},
        )
    assert isinstance(stored, str) and link.token not in stored
    [event] = await audit_payloads(funded, FACTORY_LINK_ISSUED)
    assert event["link_id"] == str(row.id) and event["payment_id"] == str(funded.payment_id)
    assert event["replaced_link_id"] is None
    assert link.token not in str(event)

    access = await resolve_factory_link(funded.engine, link.token)
    assert access == FactoryAccess(
        tenant_id=funded.tenant,
        link_id=row.id,
        payment_id=funded.payment_id,
        expires_at=row.expires_at,
    )


async def test_without_a_page_url_only_the_token_is_returned(funded: Funded) -> None:
    link = await issue_factory_link(
        funded.engine,
        buyer(funded.people),
        funded.po_id,
        settings=release_settings(factory_page_url=None),
    )
    assert link.url is None and link.token


async def test_a_link_needs_a_funded_payment_of_an_order_of_the_tenant(funded: Funded) -> None:
    with pytest.raises(PaymentNotFound):
        await issue_factory_link(funded.engine, outsider(funded), funded.po_id, settings=SETTINGS)
    async with tenant_session(funded.engine, funded.tenant) as session:
        await session.execute(
            update(PoPayment)
            .where(PoPayment.id == funded.payment_id)
            .values(state="escrow_created", funded_at=None)
        )
    with pytest.raises(PaymentInvalidState, match="funded"):
        await issue_factory_link(
            funded.engine, buyer(funded.people), funded.po_id, settings=SETTINGS
        )
    assert await link_rows(funded) == []


async def test_a_new_link_stops_the_previous_one(funded: Funded) -> None:
    first, first_access = await factory_link(funded)
    second = await issue_factory_link(
        funded.engine, buyer(funded.people), funded.po_id, settings=SETTINGS
    )

    old, new = await link_rows(funded)
    assert (old.revoked_at is not None, old.revoked_by) == (True, funded.people.owner)
    assert (new.revoked_at, new.accepted_at) == (None, None)
    with pytest.raises(FactoryLinkInvalid):
        await resolve_factory_link(funded.engine, first.token)
    with pytest.raises(FactoryLinkInvalid):
        await accept_order(funded.engine, first_access)
    access = await resolve_factory_link(funded.engine, second.token)
    # The new link confirms the order again.
    order = await factory_order(funded.engine, access, settings=SETTINGS)
    assert order.accepted_at is None
    events = await audit_payloads(funded, FACTORY_LINK_ISSUED)
    assert [e["replaced_link_id"] for e in events] == [None, str(old.id)]


async def test_a_revoked_link_answers_like_an_unknown_one(funded: Funded) -> None:
    link, _ = await factory_link(funded)
    await revoke_factory_link(funded.engine, buyer(funded.people), funded.po_id)
    await revoke_factory_link(funded.engine, buyer(funded.people), funded.po_id)  # nothing left

    [row] = await link_rows(funded)
    assert row.revoked_at is not None and row.revoked_by == funded.people.owner
    [event] = await audit_payloads(funded, FACTORY_LINK_REVOKED)
    assert event["link_id"] == str(row.id)
    answers: list[str] = []
    for token in (
        link.token,  # revoked
        new_factory_token(),  # unknown
        None,  # missing
        "",
        "short",
        link.token + "x",  # too long
        "é" * 43,  # not base64url
        "' OR 1=1 --" + "a" * 32,
    ):
        with pytest.raises(FactoryLinkInvalid) as refused:
            await resolve_factory_link(funded.engine, token)
        answers.append(str(refused.value))
    assert set(answers) == {INVALID_LINK}
    with pytest.raises(PaymentNotFound):
        await revoke_factory_link(funded.engine, outsider(funded), funded.po_id)


async def test_an_expired_link_answers_like_an_unknown_one(funded: Funded) -> None:
    days = SETTINGS.factory_link_ttl_days
    link = await issue_factory_link(
        funded.engine,
        buyer(funded.people),
        funded.po_id,
        settings=SETTINGS,
        now=datetime.now(UTC) - timedelta(days=days, minutes=1),
    )
    with pytest.raises(FactoryLinkInvalid) as refused:
        await resolve_factory_link(funded.engine, link.token)
    assert str(refused.value) == INVALID_LINK


async def test_reading_touches_last_used_at_at_most_once_a_minute(funded: Funded) -> None:
    link, _ = await factory_link(funded, accept=False)
    [row] = await link_rows(funded)
    first = row.last_used_at
    await resolve_factory_link(funded.engine, link.token)
    [row] = await link_rows(funded)
    assert first is not None and row.last_used_at == first

    async with tenant_session(funded.engine, funded.tenant) as session:
        await session.execute(
            update(PoFactoryLink)
            .where(PoFactoryLink.id == row.id)
            .values(last_used_at=first - timedelta(minutes=2))
        )
    await resolve_factory_link(funded.engine, link.token)
    [row] = await link_rows(funded)
    assert row.last_used_at is not None and row.last_used_at > first - timedelta(minutes=2)
    # Nothing else changes on a read.
    assert (row.accepted_at, row.revoked_at) == (None, None)


async def test_the_factory_confirms_its_order_once(funded: Funded) -> None:
    _, access = await factory_link(funded, accept=False)

    first = await accept_order(funded.engine, access)
    again = await accept_order(funded.engine, access)

    assert again.accepted_at == first.accepted_at
    [row] = await link_rows(funded)
    assert row.accepted_at == first.accepted_at
    [event] = await audit_payloads(funded, FACTORY_ORDER_ACCEPTED)
    assert event["link_id"] == str(access.link_id)
    assert event["payment_id"] == str(funded.payment_id)


async def test_a_token_reaches_its_own_tenant_only(funded: Funded) -> None:
    link, access = await factory_link(funded)
    token_hash = factory_token_hash(link.token)

    # Outside any tenant the links table shows nothing; the SECURITY DEFINER lookup
    # names the tenant of an active link, and nothing else.
    async with funded.engine.connect() as conn:
        rows = (await conn.execute(select(PoFactoryLink.id))).all()
        tenant = await conn.scalar(
            text("SELECT factory_link_tenant(:token_hash)"), {"token_hash": token_hash}
        )
        nobody = await conn.scalar(
            text("SELECT factory_link_tenant(:token_hash)"),
            {"token_hash": factory_token_hash(new_factory_token())},
        )
    assert rows == [] and tenant == funded.tenant and nobody is None

    # Access forged for another tenant finds no link, no order, no payment there.
    forged = FactoryAccess(
        tenant_id=funded.people.other_tenant,
        link_id=access.link_id,
        payment_id=access.payment_id,
        expires_at=access.expires_at,
    )
    with pytest.raises(FactoryLinkInvalid):
        await factory_order(funded.engine, forged, settings=SETTINGS)
    with pytest.raises(FactoryLinkInvalid):
        await accept_order(funded.engine, forged)
