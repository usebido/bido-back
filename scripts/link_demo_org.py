"""Link a Clerk organization to a company (tenant), so its members can sign in.

This is how a pilot customer is let in on prd. There AUTH_AUTO_PROVISION is
off, so an organization nobody linked gets 403 "organization not enabled"
until an operator runs this once for it. In prd the run must be deliberate:

    # prd: a new, empty company, named explicitly; nothing else is created
    python scripts/link_demo_org.py --confirm-environment prd \\
        --clerk-org-id org_2abc --company-name "Acme S.p.A." --operator arthur

    # local or dev
    # the demo company with the EV motor data (scripts/seed_motor_demo.py)
    uv run python scripts/link_demo_org.py --clerk-org-id org_2abc --with-motor-demo
    # an existing tenant
    uv run python scripts/link_demo_org.py --clerk-org-id org_2abc --tenant-id <uuid>
    # a new, empty company (--name is the same flag)
    uv run python scripts/link_demo_org.py --clerk-org-id org_2abc --company-name "Acme S.p.A."

The organization id is in the Clerk dashboard (Organizations, `org_...`).
Once linked, any member of that organization who signs in gets a membership
in the company (Clerk admins as owners), whether or not AUTH_AUTO_PROVISION
is on; that setting only decides whether UNlinked organizations may create a
company on first login. CLERK_ALLOWED_ORG_IDS, when set, must list the
organization too.

Runs with ENVIRONMENT=local, dev or prd. In prd it needs
`--confirm-environment prd`, `--company-name` and `--operator`, and refuses
`--tenant-id` and `--with-motor-demo`: no demo data, no defaults. A
`--confirm-environment` that does not match ENVIRONMENT is refused anywhere.

A new company is created with DATABASE_URL (the app role, the same insert as
a first login with AUTH_AUTO_PROVISION). Linking an existing tenant needs
MIGRATION_DATABASE_URL: the app role cannot UPDATE tenants (migration 0017).

Idempotent: linking the same organization to the same company again changes
nothing and says so; an organization already linked to another company (or a
tenant already linked to another organization) is refused. Every link that
changes something writes a `tenant.linked` audit event (organization id,
operator, whether the company was created) in the same transaction, and a log
line with the same ids.
"""

import argparse
import asyncio
import getpass
import sys
import unicodedata
from uuid import UUID

from sqlalchemy.ext.asyncio import create_async_engine

from bido_back.db import create_engine, create_sessionmaker
from bido_back.orgs import OrgLink, link_new_company, link_org, tenant_for_org
from bido_back.settings import Settings, get_settings

ENVIRONMENTS = ("local", "dev", "prd")
MAX_COMPANY_NAME = 200
MAX_OPERATOR = 100


def _args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--clerk-org-id", required=True, help="org_... from the Clerk dashboard")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--with-motor-demo", action="store_true", help="the Motori Demo company (local/dev only)"
    )
    target.add_argument("--tenant-id", type=UUID, help="an existing tenant (local/dev only)")
    target.add_argument(
        "--company-name",
        "--name",
        dest="company_name",
        help="create a new, empty company with this name (the only target in prd)",
    )
    parser.add_argument(
        "--confirm-environment",
        choices=ENVIRONMENTS,
        help="required in prd: must equal ENVIRONMENT (refused when it does not)",
    )
    parser.add_argument(
        "--operator",
        help="who runs this, recorded in the audit event (required in prd; "
        "default elsewhere: the local user)",
    )
    return parser.parse_args(argv)


def _clean(value: str, flag: str, limit: int) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > limit:
        raise ValueError(f"{flag} must be 1 to {limit} characters")
    if any(unicodedata.category(ch).startswith("C") for ch in cleaned):
        raise ValueError(f"{flag} must not contain control characters")
    return cleaned


def _local_user() -> str:
    try:
        return getpass.getuser()
    except (OSError, KeyError):
        return "unknown"


def _check(args: argparse.Namespace, settings: Settings) -> tuple[str, str | None, str]:
    """Refuse anything but a deliberate run; return (org id, company name, operator)."""
    org_id: str = args.clerk_org_id.strip()
    if not org_id.startswith("org_") or len(org_id) > 255:
        raise ValueError("--clerk-org-id must look like org_...")
    environment = settings.environment
    if environment not in ENVIRONMENTS:
        raise ValueError("link_demo_org.py runs only with ENVIRONMENT=local, dev or prd")
    confirmed: str | None = args.confirm_environment
    if confirmed is not None and confirmed != environment:
        raise ValueError(
            f"--confirm-environment {confirmed} does not match ENVIRONMENT={environment}; "
            "nothing was changed"
        )
    if environment == "prd":
        if confirmed is None:
            raise ValueError(
                "ENVIRONMENT=prd: pass --confirm-environment prd to link a customer's "
                "organization in production"
            )
        if args.company_name is None:
            raise ValueError(
                "ENVIRONMENT=prd: name the company with --company-name "
                "(--tenant-id and --with-motor-demo are local/dev only)"
            )
        if args.operator is None:
            raise ValueError("ENVIRONMENT=prd: say who runs this with --operator <name>")
    name = (
        None
        if args.company_name is None
        else _clean(args.company_name, "--company-name", MAX_COMPANY_NAME)
    )
    operator = _clean(args.operator or _local_user(), "--operator", MAX_OPERATOR)
    return org_id, name, operator


async def _link_existing(
    settings: Settings, org_id: str, operator: str, *, tenant_id: UUID | None
) -> OrgLink:
    """--tenant-id or --with-motor-demo (local/dev): link a tenant that exists, or the demo."""
    if settings.migration_database_url is None:
        raise ValueError("MIGRATION_DATABASE_URL is required to link an existing tenant")
    engine = create_engine(settings)
    migrator = create_async_engine(str(settings.migration_database_url))
    try:
        if tenant_id is None:
            # Imported here: demo data never loads on the prd path.
            from seed_motor_demo import TENANT_ID as MOTOR_DEMO_TENANT_ID
            from seed_motor_demo import seed as seed_motor_demo

            await seed_motor_demo(engine)
            tenant_id = MOTOR_DEMO_TENANT_ID
        linked = await tenant_for_org(create_sessionmaker(engine), org_id)
        if linked is not None and linked != tenant_id:
            raise LookupError(f"{org_id} is already linked to tenant {linked}")
        return await link_org(create_sessionmaker(migrator), tenant_id, org_id, operator=operator)
    finally:
        await engine.dispose()
        await migrator.dispose()


async def _link_new(settings: Settings, org_id: str, name: str, operator: str) -> OrgLink:
    engine = create_engine(settings)
    try:
        return await link_new_company(create_sessionmaker(engine), org_id, name, operator=operator)
    finally:
        await engine.dispose()


async def main(argv: list[str] | None = None) -> int:
    args = _args(argv)
    settings = get_settings()
    try:
        org_id, name, operator = _check(args, settings)
        if name is not None:
            link = await _link_new(settings, org_id, name, operator)
        else:
            link = await _link_existing(settings, org_id, operator, tenant_id=args.tenant_id)
    except (ValueError, LookupError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not link.changed:
        print(f"unchanged {org_id} is already linked to tenant {link.tenant_id}; nothing written")
        return 0
    print(f"linked    {org_id} -> tenant {link.tenant_id} ({settings.environment})")
    print(f"recorded  audit event tenant.linked, operator {operator}")
    if settings.clerk_allowed_org_ids and org_id not in settings.clerk_allowed_org_ids:
        print(f"warning   CLERK_ALLOWED_ORG_IDS is set and does not list {org_id}: add it")
    print("next      members of that organization sign in on the frontend; the first")
    print("          request creates their membership (Clerk admins become owners)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
