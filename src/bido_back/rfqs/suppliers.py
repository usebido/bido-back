"""Suppliers and their contacts.

A supplier is a company and holds no personal data. People (names, emails,
phones) are `contacts` rows of kind `supplier`, linked through
`supplier_contacts`, so erasing a person is a change to one row.
"""

from collections import defaultdict
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import Contact, Supplier, SupplierContact
from bido_back.rfqs.errors import NotFound
from bido_back.rfqs.schemas import (
    ContactIn,
    ContactView,
    CreateSupplierRequest,
    SupplierType,
    SupplierView,
)

Db = AsyncEngine | async_sessionmaker[AsyncSession]


def _contact_view(contact: Contact) -> ContactView:
    return ContactView(id=contact.id, name=contact.name, email=contact.email)


def _supplier_view(supplier: Supplier, contacts: list[Contact]) -> SupplierView:
    return SupplierView(
        id=supplier.id,
        name=supplier.name,
        type=cast(SupplierType, supplier.type),
        country=supplier.country,
        language=supplier.language,
        email_domain=supplier.email_domain,
        contacts=[_contact_view(c) for c in contacts],
    )


async def _add_contact(
    session: AsyncSession, tenant_id: UUID, supplier_id: UUID, data: ContactIn
) -> Contact:
    contact = Contact(
        tenant_id=tenant_id, kind="supplier", name=data.name, email=data.email, phone=data.phone
    )
    session.add(contact)
    await session.flush()
    session.add(
        SupplierContact(tenant_id=tenant_id, supplier_id=supplier_id, contact_id=contact.id)
    )
    await session.flush()
    return contact


async def create_supplier(
    db: Db, principal: Principal, request: CreateSupplierRequest
) -> SupplierView:
    async with tenant_session(db, principal.tenant_id) as session:
        return await create_supplier_in(session, principal.tenant_id, request)


async def create_supplier_in(
    session: AsyncSession, tenant_id: UUID, request: CreateSupplierRequest
) -> SupplierView:
    """`create_supplier` in the caller's tenant session, as part of its transaction."""
    supplier = Supplier(
        tenant_id=tenant_id,
        name=request.name,
        type=request.type,
        country=request.country,
        language=request.language,
        email_domain=request.email_domain,
    )
    session.add(supplier)
    await session.flush()
    contacts = [
        await _add_contact(session, tenant_id, supplier.id, data) for data in request.contacts
    ]
    return _supplier_view(supplier, contacts)


async def add_contact(
    db: Db, principal: Principal, supplier_id: UUID, request: ContactIn
) -> ContactView:
    async with tenant_session(db, principal.tenant_id) as session:
        if await session.get(Supplier, supplier_id) is None:
            raise NotFound(f"supplier {supplier_id} not found")
        return await add_contact_in(session, principal.tenant_id, supplier_id, request)


async def add_contact_in(
    session: AsyncSession, tenant_id: UUID, supplier_id: UUID, request: ContactIn
) -> ContactView:
    """`add_contact` in the caller's tenant session, for a supplier the caller found in it."""
    return _contact_view(await _add_contact(session, tenant_id, supplier_id, request))


async def list_suppliers(db: Db, principal: Principal) -> list[SupplierView]:
    async with tenant_session(db, principal.tenant_id) as session:
        suppliers = list(
            await session.scalars(select(Supplier).order_by(Supplier.name, Supplier.id).limit(1000))
        )
        rows = await session.execute(
            select(SupplierContact.supplier_id, Contact)
            .join(Contact, Contact.id == SupplierContact.contact_id)
            .where(Contact.pseudonymized_at.is_(None))
            .order_by(Contact.created_at, Contact.id)
        )
        by_supplier: dict[UUID, list[Contact]] = defaultdict(list)
        for supplier_id, contact in rows:
            by_supplier[supplier_id].append(contact)
        return [_supplier_view(s, by_supplier[s.id]) for s in suppliers]
