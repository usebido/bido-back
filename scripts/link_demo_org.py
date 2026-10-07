"""Link a Clerk organization to a company (tenant), so its members can sign in.

    # the demo company with the EV motor data (scripts/seed_motor_demo.py)
    uv run python scripts/link_demo_org.py --clerk-org-id org_2abc --with-motor-demo
    # an existing tenant
    uv run python scripts/link_demo_org.py --clerk-org-id org_2abc --tenant-id <uuid>
    # a new, empty company
    uv run python scripts/link_demo_org.py --clerk-org-id org_2abc --name "Acme S.p.A."

The organization id is in the Clerk dashboard (Organizations, `org_...`).
Once linked, any member of that organization who signs in gets a membership
in the company (Clerk admins as owners), whether or not AUTH_AUTO_PROVISION
is on; that setting only decides whether UNlinked organizations may create a
company on first login.

Runs with ENVIRONMENT=local or dev (refuses prd). Uses DATABASE_URL, the app
role, through tenant-scoped transactions like the api. Idempotent: linking
the same organization to the same tenant again changes nothing; an
organization already linked to another tenant is refused.
"""

import argparse
import asyncio
import sys
from uuid import UUID, uuid4

from seed_motor_demo import TENANT_ID as MOTOR_DEMO_TENANT_ID
from seed_motor_demo import seed as seed_motor_demo

from bido_back.db import create_engine, create_sessionmaker, tenant_session
from bido_back.db.models import Tenant
from bido_back.orgs import link_org, tenant_for_org
from bido_back.settings import get_settings


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--clerk-org-id", required=True, help="org_... from the Clerk dashboard")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--with-motor-demo", action="store_true", help="the Motori Demo company")
    target.add_argument("--tenant-id", type=UUID, help="an existing tenant")
    target.add_argument("--name", help="create a new, empty company with this name")
    return parser.parse_args()


async def main() -> int:
    args = _args()
    org_id: str = args.clerk_org_id.strip()
    if not org_id.startswith("org_") or len(org_id) > 255:
        print("--clerk-org-id must look like org_...", file=sys.stderr)
        return 1
    settings = get_settings()
    if settings.environment not in ("local", "dev"):
        print("link_demo_org.py runs only with ENVIRONMENT=local or dev", file=sys.stderr)
        return 1

    engine = create_engine(settings)
    db = create_sessionmaker(engine)
    try:
        if args.with_motor_demo:
            await seed_motor_demo(engine)
            tenant_id = MOTOR_DEMO_TENANT_ID
        elif args.tenant_id is not None:
            tenant_id = args.tenant_id
        else:
            tenant_id = uuid4()
            async with tenant_session(db, tenant_id) as session:
                session.add(Tenant(id=tenant_id, name=args.name))

        linked = await tenant_for_org(db, org_id)
        if linked is not None and linked != tenant_id:
            print(f"{org_id} is already linked to tenant {linked}", file=sys.stderr)
            return 1
        await link_org(db, tenant_id, org_id)
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        await engine.dispose()

    print(f"linked    {org_id} -> tenant {tenant_id}")
    print("next      members of that organization sign in on the frontend; the first")
    print("          request creates their membership (Clerk admins become owners)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
