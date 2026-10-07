"""Migration 0004: the inbound lookup functions find exactly one invite and leak nothing else."""

from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.db import tenant_session
from bido_back.db.models import Contact, Invite, SupplierContact

from .conftest import WorldFactory

pytestmark = pytest.mark.db

FUNCTIONS = (
    "resolve_invite_by_token(text)",
    "resolve_invite_by_message_id(text)",
    "resolve_invite_by_rfq_ref(text, text)",
)


async def _rows(engine: AsyncEngine, sql: str, **params: str) -> list[tuple[object, ...]]:
    async with engine.begin() as conn:
        return [tuple(r) for r in (await conn.execute(text(sql), params)).all()]


async def test_token_lookup_without_tenant(
    app_engine: AsyncEngine, make_world: WorldFactory
) -> None:
    w = await make_world()
    sql = "SELECT tenant_id, invite_id FROM resolve_invite_by_token(:t)"
    assert await _rows(app_engine, sql, t=w.reply_token) == [(w.tenant_id, w.invite_id)]
    assert await _rows(app_engine, sql, t=w.reply_token.upper()) == [(w.tenant_id, w.invite_id)]
    assert await _rows(app_engine, sql, t="zzzzzzzzzzzz") == []
    assert await _rows(app_engine, sql, t="x" * 100) == []


async def test_function_returns_only_the_two_ids(
    app_engine: AsyncEngine, make_world: WorldFactory
) -> None:
    w = await make_world()
    async with app_engine.begin() as conn:
        result = await conn.execute(
            text("SELECT * FROM resolve_invite_by_token(:t)"), {"t": w.reply_token}
        )
        assert list(result.keys()) == ["tenant_id", "invite_id"]


async def test_message_id_lookup_matches_thread_and_outbound_only(
    app_engine: AsyncEngine, make_world: WorldFactory
) -> None:
    w = await make_world()
    sql = "SELECT tenant_id, invite_id FROM resolve_invite_by_message_id(:m)"
    expected = [(w.tenant_id, w.invite_id)]
    assert await _rows(app_engine, sql, m=w.thread_message_id) == expected
    assert await _rows(app_engine, sql, m=w.followup_message_id) == expected
    assert await _rows(app_engine, sql, m=f"<{uuid4()}@nowhere.test>") == []


async def test_rfq_ref_lookup_needs_a_known_sender(
    app_engine: AsyncEngine, make_world: WorldFactory
) -> None:
    ref = f"RFQ-{uuid4().int % 10**12}"
    a = await make_world(rfq_ref=ref)
    b = await make_world(rfq_ref=ref)  # same ref in another tenant
    sql = "SELECT tenant_id, invite_id FROM resolve_invite_by_rfq_ref(:r, :s)"

    assert await _rows(app_engine, sql, r=ref, s=a.contact_email.upper()) == [
        (a.tenant_id, a.invite_id)
    ]
    assert await _rows(app_engine, sql, r=ref, s=b.contact_email) == [(b.tenant_id, b.invite_id)]
    assert await _rows(app_engine, sql, r=ref, s="stranger@supplier.test") == []

    # A second contact linked to the supplier also counts.
    colleague = f"colleague-{uuid4().hex[:6]}@supplier.test"
    async with tenant_session(app_engine, a.tenant_id) as session:
        contact = Contact(id=uuid4(), tenant_id=a.tenant_id, kind="supplier", email=colleague)
        session.add(contact)
        await session.flush()
        session.add(
            SupplierContact(tenant_id=a.tenant_id, supplier_id=a.supplier_id, contact_id=contact.id)
        )
    assert await _rows(app_engine, sql, r=ref, s=colleague) == [(a.tenant_id, a.invite_id)]


async def test_the_flag_unlocks_nothing_for_the_app_or_outside_the_function(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, make_world: WorldFactory
) -> None:
    w = await make_world()
    query = select(Invite.id).where(Invite.id == w.invite_id)
    # The app role setting the flag itself gains nothing: the policy is not TO it.
    async with app_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.inbound_lookup', 'on', true)"))
        assert (await conn.execute(query)).all() == []
    # The owner role does see across tenants while the flag is on; that is the
    # mechanism the functions use, so it must stay limited to them.
    async with migrator_engine.begin() as conn:
        await conn.execute(text("SELECT set_config('app.inbound_lookup', 'on', true)"))
        assert (await conn.execute(query)).all() == [(w.invite_id,)]
    # Without the flag, even the migrator sees nothing without a tenant.
    async with migrator_engine.begin() as conn:
        assert (await conn.execute(select(Invite.id).where(Invite.id == w.invite_id))).all() == []
    # And the function turns the flag off again before returning.
    async with app_engine.begin() as conn:
        await conn.execute(text("SELECT * FROM resolve_invite_by_token(:t)"), {"t": w.reply_token})
        flag = await conn.execute(text("SELECT current_setting('app.inbound_lookup', true)"))
        assert flag.scalar_one() == "off"


async def test_functions_are_definer_pinned_and_app_only(migrator_engine: AsyncEngine) -> None:
    async with migrator_engine.begin() as conn:
        for signature in FUNCTIONS:
            row = (
                await conn.execute(
                    text(
                        "SELECT p.prosecdef, p.provolatile, p.proconfig,"
                        " has_function_privilege('bido_app', p.oid, 'EXECUTE') AS app,"
                        " has_function_privilege('bido_agno', p.oid, 'EXECUTE') AS agno"
                        " FROM pg_proc p WHERE p.oid = CAST(:sig AS regprocedure)"
                    ),
                    {"sig": f"public.{signature}"},
                )
            ).one()
            assert row.prosecdef is True
            assert row.provolatile in ("s", b"s")  # pg "char" comes back as bytes
            assert "search_path=public, pg_temp" in row.proconfig
            assert row.app is True
            assert row.agno is False
