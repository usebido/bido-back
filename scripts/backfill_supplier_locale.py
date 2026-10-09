"""Fill in the country and language of suppliers saved without either.

    uv run python scripts/backfill_supplier_locale.py --tenant <uuid> [--tenant <uuid>...]
    uv run python scripts/backfill_supplier_locale.py --tenant <uuid> --apply

Suppliers created from just a name and an email (the Copilot's `add_supplier_contact`
before it took a country, `POST /v1/suppliers` without one) have no country and the
column default language "en", so their RFQ documents go out in English whatever the
supplier is. This applies `infer_supplier_locale` (the email's country TLD, else an
unambiguous legal suffix in the name) to every supplier of the given tenants whose
country is null and whose language is the default, and prints what it would change.
Nothing is written without `--apply`. Suppliers it cannot infer are listed and left
alone: the buyer settles those in the chat.

Uses DATABASE_URL (the app role) and one tenant-scoped transaction per tenant: no
role sees across tenants, so the tenants are given explicitly. Safe to re-run.
"""

import argparse
import asyncio
import sys
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.db import create_engine, create_sessionmaker, tenant_session
from bido_back.db.models import Contact, Supplier, SupplierContact
from bido_back.rfqs.language import DEFAULT_LANGUAGE, infer_supplier_locale
from bido_back.settings import get_settings


@dataclass(frozen=True)
class Change:
    supplier_id: UUID
    name: str
    email: str | None
    country: str
    language: str


async def _first_emails(session: AsyncSession, supplier_ids: list[UUID]) -> dict[UUID, str]:
    """The oldest non-pseudonymized contact email of each supplier."""
    rows = await session.execute(
        select(SupplierContact.supplier_id, Contact.email)
        .join(Contact, Contact.id == SupplierContact.contact_id)
        .where(
            SupplierContact.supplier_id.in_(supplier_ids),
            Contact.pseudonymized_at.is_(None),
            Contact.email.is_not(None),
        )
        .order_by(Contact.created_at, Contact.id)
    )
    emails: dict[UUID, str] = {}
    for supplier_id, email in rows:
        if email:
            emails.setdefault(supplier_id, email)
    return emails


async def plan_tenant(session: AsyncSession) -> tuple[list[Change], list[Supplier]]:
    """What the backfill would set in this tenant, and the suppliers it cannot infer."""
    suppliers = list(
        await session.scalars(
            select(Supplier)
            .where(Supplier.country.is_(None), Supplier.language == DEFAULT_LANGUAGE)
            .order_by(Supplier.created_at, Supplier.id)
        )
    )
    emails = await _first_emails(session, [s.id for s in suppliers])
    changes: list[Change] = []
    unknown: list[Supplier] = []
    for supplier in suppliers:
        email = emails.get(supplier.id)
        country, language = infer_supplier_locale(supplier.name, email)
        if country is None:
            unknown.append(supplier)
            continue
        changes.append(
            Change(supplier.id, supplier.name, email, country, language or DEFAULT_LANGUAGE)
        )
    return changes, unknown


async def run(tenants: list[UUID], apply: bool) -> int:
    engine = create_engine(get_settings())
    db = create_sessionmaker(engine)
    try:
        for tenant_id in tenants:
            async with tenant_session(db, tenant_id) as session:
                changes, unknown = await plan_tenant(session)
                print(f"tenant {tenant_id}: {len(changes)} to set, {len(unknown)} unknown")
                for change in changes:
                    print(
                        f"  {change.supplier_id}  {change.name!r} <{change.email or '-'}>"
                        f"  -> {change.country}/{change.language}"
                    )
                for supplier in unknown:
                    print(f"  {supplier.id}  {supplier.name!r}: nothing to infer, left as is")
                if not apply:
                    continue
                by_id = {change.supplier_id: change for change in changes}
                for supplier in await session.scalars(
                    select(Supplier).where(Supplier.id.in_(list(by_id)))
                ):
                    supplier.country = by_id[supplier.id].country
                    supplier.language = by_id[supplier.id].language
                await session.flush()
                print(f"  applied {len(by_id)} change(s)")
    finally:
        await engine.dispose()
    if not apply:
        print("dry run: nothing written (pass --apply to write)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0])
    parser.add_argument(
        "--tenant", action="append", type=UUID, required=True, help="a tenant id; repeatable"
    )
    parser.add_argument("--apply", action="store_true", help="write the changes")
    args = parser.parse_args()
    return asyncio.run(run(args.tenant, args.apply))


if __name__ == "__main__":
    sys.exit(main())
