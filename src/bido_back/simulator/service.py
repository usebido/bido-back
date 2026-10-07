"""The sandbox api's operations: personas, the simulated outbox, reply-now, reset.

Every function runs in the caller's tenant and refuses (`NotSandbox`) unless
that tenant is a sandbox tenant, so these endpoints do nothing for a real
company. None of them can make a tenant a sandbox.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import SandboxOutbound, Supplier, SupplierPersona
from bido_back.simulator.demo import PERSONAS_BY_NAME
from bido_back.simulator.personas import Persona, apply_persona, persona_from_row
from bido_back.simulator.schemas import OutboundView, PersonaView, ResetResult
from bido_back.simulator.sender import ReplyScheduler, is_sandbox

Db = AsyncEngine | async_sessionmaker[AsyncSession]


class SandboxError(Exception):
    pass


class NotSandbox(SandboxError):
    """The tenant is not a sandbox tenant (answered as 404)."""


class NotFound(SandboxError):
    pass


class InvalidState(SandboxError):
    pass


async def _require_sandbox(session: AsyncSession, principal: Principal) -> None:
    if not await is_sandbox(session, principal.tenant_id):
        raise NotSandbox("not a sandbox tenant")


async def list_personas(db: Db, principal: Principal) -> list[PersonaView]:
    async with tenant_session(db, principal.tenant_id) as session:
        await _require_sandbox(session, principal)
        rows = await session.execute(
            select(Supplier, SupplierPersona)
            .outerjoin(SupplierPersona, SupplierPersona.supplier_id == Supplier.id)
            .order_by(Supplier.name)
        )
        return [
            PersonaView(
                supplier_id=supplier.id, supplier_name=supplier.name, persona=persona_from_row(row)
            )
            for supplier, row in rows.all()
        ]


async def put_persona(
    db: Db, principal: Principal, supplier_id: UUID, persona: Persona
) -> PersonaView:
    async with tenant_session(db, principal.tenant_id) as session:
        await _require_sandbox(session, principal)
        supplier = await session.get(Supplier, supplier_id)
        if supplier is None:
            raise NotFound(f"supplier {supplier_id} not found")
        row = await session.scalar(
            select(SupplierPersona).where(SupplierPersona.supplier_id == supplier_id)
        )
        if row is None:
            row = SupplierPersona(tenant_id=principal.tenant_id, supplier_id=supplier_id)
            session.add(row)
        apply_persona(row, persona)
        row.updated_at = datetime.now(UTC)
        await session.flush()
        return PersonaView(supplier_id=supplier.id, supplier_name=supplier.name, persona=persona)


async def list_outbound(db: Db, principal: Principal, invite_id: UUID | None) -> list[OutboundView]:
    async with tenant_session(db, principal.tenant_id) as session:
        await _require_sandbox(session, principal)
        query = select(SandboxOutbound).order_by(SandboxOutbound.created_at.desc()).limit(200)
        if invite_id is not None:
            query = query.where(SandboxOutbound.invite_id == invite_id)
        return [OutboundView.from_row(row) for row in (await session.scalars(query)).all()]


async def reply_now(
    db: Db,
    principal: Principal,
    outbound_id: UUID,
    schedulers: Callable[[], ReplyScheduler],
) -> OutboundView:
    """Make a pending email due now and schedule the simulated answer."""
    async with tenant_session(db, principal.tenant_id) as session:
        await _require_sandbox(session, principal)
        row = await session.get(SandboxOutbound, outbound_id)
        if row is None:
            raise NotFound(f"sandbox email {outbound_id} not found")
        if row.status != "pending":
            raise InvalidState(f"sandbox email {outbound_id} is {row.status}, not pending")
        row.due_at = datetime.now(UTC)
        await session.flush()
        view = OutboundView.from_row(row)
    await schedulers().schedule(principal.tenant_id, outbound_id, 0)
    return view


async def reset(db: Db, principal: Principal) -> ResetResult:
    """Cancel pending simulated answers and restore the demo personas."""
    async with tenant_session(db, principal.tenant_id) as session:
        await _require_sandbox(session, principal)
        cancelled = (
            await session.execute(
                update(SandboxOutbound)
                .where(SandboxOutbound.status == "pending")
                .values(status="cancelled")
                .returning(SandboxOutbound.id)
            )
        ).all()
        restored = 0
        for supplier in (await session.scalars(select(Supplier))).all():
            persona = PERSONAS_BY_NAME.get(supplier.name)
            if persona is None:
                continue
            row = await session.scalar(
                select(SupplierPersona).where(SupplierPersona.supplier_id == supplier.id)
            )
            if row is None:
                row = SupplierPersona(tenant_id=principal.tenant_id, supplier_id=supplier.id)
                session.add(row)
            apply_persona(row, persona)
            row.updated_at = datetime.now(UTC)
            restored += 1
        await session.flush()
        return ResetResult(cancelled=len(cancelled), personas_restored=restored)
