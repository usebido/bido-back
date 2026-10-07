"""Seed a local development tenant: two users, two suppliers with contacts.

    uv run python scripts/seed_dev.py

LOCAL ONLY: refuses unless ENVIRONMENT=local. Uses DATABASE_URL (the app
role, through tenant_session, like the api). Idempotent: re-running finds the
same tenant, users and suppliers instead of adding new ones.

Creates:
  * tenant "Dev Procurement GmbH" (fixed id, so re-runs find it);
  * user `user_dev_buyer` (role member) and `user_dev_approver` (role approver),
    each with a `buyer` contact carrying their name;
  * two suppliers, each with one contact (example.test addresses).

Then prints the X-Dev-User values. With AUTH_DEV_MODE=true and
ENVIRONMENT=local the api accepts `X-Dev-User: <clerk_user_id>`. With
EMAIL_TRANSPORT=smtp, RFQ emails go to Mailpit; with "mailbox", the buyer
needs a connected mailbox to prepare sends.
"""

import asyncio
import sys
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.db import create_engine, tenant_session
from bido_back.db.models import Contact, Membership, Supplier, SupplierContact, Tenant, User
from bido_back.settings import get_settings

TENANT_ID = UUID("00000000-0000-4000-8000-00000000d001")
TENANT_NAME = "Dev Procurement GmbH"
USERS = (
    # clerk_user_id, email, name, role
    ("user_dev_buyer", "buyer@dev-procurement.example.test", "Anna Buyer", "member"),
    ("user_dev_approver", "approver@dev-procurement.example.test", "Max Approver", "approver"),
)
SUPPLIERS = (
    # name, type, country, language, email_domain, contact name, contact email
    (
        "Rhein Elektronik Vertrieb",
        "authorized_distributor",
        "DE",
        "de",
        "rhein-elektronik.example.test",
        "Jonas Weber",
        "jonas.weber@rhein-elektronik.example.test",
    ),
    (
        "Lyon Composants",
        "distributor",
        "FR",
        "fr",
        "lyon-composants.example.test",
        "Claire Martin",
        "claire.martin@lyon-composants.example.test",
    ),
)


async def _user(
    session: AsyncSession, clerk_user_id: str, email: str, name: str, role: str
) -> UUID:
    user = await session.scalar(select(User).where(User.clerk_user_id == clerk_user_id))
    if user is None:
        user = User(clerk_user_id=clerk_user_id, email=email)
        session.add(user)
        await session.flush()
    membership = await session.scalar(
        select(Membership).where(Membership.tenant_id == TENANT_ID, Membership.user_id == user.id)
    )
    if membership is None:
        session.add(Membership(tenant_id=TENANT_ID, user_id=user.id, role=role))
    contact = await session.scalar(
        select(Contact).where(Contact.kind == "buyer", Contact.email == email)
    )
    if contact is None:
        session.add(Contact(tenant_id=TENANT_ID, kind="buyer", name=name, email=email))
    await session.flush()
    return user.id


async def _supplier(
    session: AsyncSession,
    name: str,
    kind: str,
    country: str,
    language: str,
    domain: str,
    contact_name: str,
    contact_email: str,
) -> UUID:
    supplier = await session.scalar(select(Supplier).where(Supplier.name == name))
    if supplier is None:
        supplier = Supplier(
            tenant_id=TENANT_ID,
            name=name,
            type=kind,
            country=country,
            language=language,
            email_domain=domain,
        )
        session.add(supplier)
        await session.flush()
        contact = Contact(
            tenant_id=TENANT_ID, kind="supplier", name=contact_name, email=contact_email
        )
        session.add(contact)
        await session.flush()
        session.add(
            SupplierContact(tenant_id=TENANT_ID, supplier_id=supplier.id, contact_id=contact.id)
        )
        await session.flush()
    return supplier.id


async def main() -> int:
    settings = get_settings()
    if settings.environment != "local":
        print("seed_dev.py runs only with ENVIRONMENT=local", file=sys.stderr)
        return 1
    engine = create_engine(settings)
    try:
        async with tenant_session(engine, TENANT_ID) as session:
            if await session.get(Tenant, TENANT_ID) is None:
                session.add(Tenant(id=TENANT_ID, name=TENANT_NAME))
                await session.flush()
            users = [await _user(session, *user) for user in USERS]
            suppliers = [await _supplier(session, *supplier) for supplier in SUPPLIERS]
    finally:
        await engine.dispose()

    print(f"tenant    {TENANT_ID}  {TENANT_NAME}")
    for (clerk_id, _, _, role), user_id in zip(USERS, users, strict=True):
        print(f"user      {user_id}  {role:<8}  X-Dev-User: {clerk_id}")
    for (name, *_), supplier_id in zip(SUPPLIERS, suppliers, strict=True):
        print(f"supplier  {supplier_id}  {name}")
    if not settings.auth_dev_mode:
        print("note: set AUTH_DEV_MODE=true to use the X-Dev-User header")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
