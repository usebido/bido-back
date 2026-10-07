"""Seed the demo sandbox tenant "Motori Demo S.p.A." and its simulated suppliers.

    uv run python scripts/seed_sandbox.py
    uv run python scripts/seed_sandbox.py --member user_2abc:investor@example.com:approver

Runs with ENVIRONMENT=local or dev, never prd. Uses DATABASE_URL (the app role,
through tenant_session, like the api). Idempotent: re-running finds the same
tenant, users and suppliers, and restores the demo personas.

The tenant is the one `scripts/seed_motor_demo.py` builds (same id, users and
suppliers), so the two seeds run in either order. When that seed created it
first, the tenant is not a sandbox yet: the app role cannot change
`tenants.sandbox`, so this script sets it once as the migration role
(MIGRATION_DATABASE_URL), an operator action no request can perform.

Creates:
  * the sandbox tenant (fixed id), or marks the existing demo tenant as one;
  * `user_sandbox_buyer` (member) and `user_sandbox_approver` (approver), each
    with a `buyer` contact, for the X-Dev-User header locally;
  * one membership per `--member CLERK_USER_ID:EMAIL:ROLE`, so real Clerk
    users (in dev) can sign in to the sandbox;
  * eight suppliers for an EV motor, each with a contact and a persona
    (see `bido_back.simulator.demo`).

Every email this tenant sends goes to the supplier simulator, never out.
"""

import argparse
import asyncio
import sys
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from bido_back.db import create_engine, tenant_session
from bido_back.db.models import (
    Contact,
    Membership,
    Supplier,
    SupplierContact,
    SupplierPersona,
    Tenant,
    User,
)
from bido_back.settings import Settings, get_settings
from bido_back.simulator.demo import SUPPLIERS, TENANT_ID, TENANT_NAME, USERS, DemoSupplier
from bido_back.simulator.personas import apply_persona

_ROLES = ("member", "approver", "owner")


async def _member(
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


async def _supplier(session: AsyncSession, demo: DemoSupplier) -> UUID:
    supplier = await session.scalar(select(Supplier).where(Supplier.name == demo.name))
    if supplier is None:
        supplier = Supplier(
            tenant_id=TENANT_ID,
            name=demo.name,
            type=demo.type,
            country=demo.country,
            language=demo.persona.language,
            email_domain=demo.domain,
        )
        session.add(supplier)
        await session.flush()
        contact = Contact(
            tenant_id=TENANT_ID, kind="supplier", name=demo.contact_name, email=demo.contact_email
        )
        session.add(contact)
        await session.flush()
        session.add(
            SupplierContact(tenant_id=TENANT_ID, supplier_id=supplier.id, contact_id=contact.id)
        )
    persona = await session.scalar(
        select(SupplierPersona).where(SupplierPersona.supplier_id == supplier.id)
    )
    if persona is None:
        persona = SupplierPersona(tenant_id=TENANT_ID, supplier_id=supplier.id)
        session.add(persona)
    apply_persona(persona, demo.persona)
    await session.flush()
    return supplier.id


async def _mark_sandbox(settings: Settings) -> None:
    """Set `tenants.sandbox` on the existing demo tenant, as the migration role."""
    if settings.migration_database_url is None:
        raise SystemExit(
            "the demo tenant exists but is not a sandbox; set MIGRATION_DATABASE_URL so this "
            "script can mark it (the app role cannot)"
        )
    engine = create_async_engine(str(settings.migration_database_url), poolclass=NullPool)
    try:
        async with tenant_session(engine, TENANT_ID) as session:
            await session.execute(update(Tenant).where(Tenant.id == TENANT_ID).values(sandbox=True))
    finally:
        await engine.dispose()


def _parse_member(value: str) -> tuple[str, str, str]:
    parts = value.split(":")
    if len(parts) != 3 or parts[2] not in _ROLES or "@" not in parts[1]:
        raise argparse.ArgumentTypeError(
            "expected CLERK_USER_ID:EMAIL:ROLE (member|approver|owner)"
        )
    return parts[0], parts[1], parts[2]


async def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the demo sandbox tenant.")
    parser.add_argument("--member", action="append", type=_parse_member, default=[])
    args = parser.parse_args()
    members: list[tuple[str, str, str]] = args.member

    settings = get_settings()
    if settings.environment not in ("local", "dev"):
        print("seed_sandbox.py runs only with ENVIRONMENT=local or dev", file=sys.stderr)
        return 1
    engine = create_engine(settings)
    try:
        async with tenant_session(engine, TENANT_ID) as session:
            tenant = await session.get(Tenant, TENANT_ID)
            existing_real = tenant is not None and not tenant.sandbox
        if existing_real:
            await _mark_sandbox(settings)
            print(f"tenant {TENANT_ID} marked as a sandbox")
        async with tenant_session(engine, TENANT_ID) as session:
            if await session.get(Tenant, TENANT_ID) is None:
                session.add(Tenant(id=TENANT_ID, name=TENANT_NAME, sandbox=True))
                await session.flush()
            users = [await _member(session, *user) for user in USERS]
            extra = [
                await _member(session, clerk_id, email, email.split("@")[0], role)
                for clerk_id, email, role in members
            ]
            suppliers = [await _supplier(session, demo) for demo in SUPPLIERS]
    finally:
        await engine.dispose()

    print(f"tenant    {TENANT_ID}  {TENANT_NAME}  (sandbox)")
    for (clerk_id, _, _, role), user_id in zip(USERS, users, strict=True):
        print(f"user      {user_id}  {role:<8}  X-Dev-User: {clerk_id}")
    for (clerk_id, _, role), user_id in zip(members, extra, strict=True):
        print(f"member    {user_id}  {role:<8}  clerk: {clerk_id}")
    for demo, supplier_id in zip(SUPPLIERS, suppliers, strict=True):
        behaviors = ", ".join(demo.persona.behaviors) or "standard"
        print(f"supplier  {supplier_id}  {demo.name}  ({behaviors})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
