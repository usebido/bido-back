"""Clerk organizations are companies: `tenants.clerk_org_id` and its lookup.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-07

The frontend gives every session a Clerk organization, and the organization
is the company. A tenant is linked to at most one organization through
`tenants.clerk_org_id` (unique; NULL for tenants that predate this, such as the
seeded dev tenants).

Authentication knows the organization id from the verified token but not the
tenant id, and `tenants` is under forced RLS, so the app role cannot find the
tenant by organization. `tenant_for_clerk_org(clerk_org_id)` returns exactly
that one tenant id for an exact match. It uses the 0005 pattern: SECURITY
DEFINER, owned by the migration role, which FORCE RLS still binds; a
SELECT-only policy `clerk_org_lookup` on `tenants` admits rows to the owner
role only (never the app role) and only while the transaction-local flag
`app.clerk_org_lookup` is 'on', which the function sets right before its one
query and clears right after. It returns the id only, never the row.

Everything else in provisioning (creating the tenant, the user and the
membership) the app role already could do inside a tenant-scoped
transaction, so this migration grants nothing beyond the lookup.

`search_path` is pinned and the table is schema-qualified. EXECUTE is revoked
from PUBLIC and granted to the app role only.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "bido_app"
SIGNATURE = "tenant_for_clerk_org(text)"
LOOKUP_FLAG = "(select current_setting('app.clerk_org_lookup', true) = 'on')"


def upgrade() -> None:
    op.execute("ALTER TABLE tenants ADD COLUMN clerk_org_id text")
    op.execute("ALTER TABLE tenants ADD CONSTRAINT tenants_clerk_org_id_key UNIQUE (clerk_org_id)")
    op.execute(
        "ALTER TABLE tenants ADD CONSTRAINT tenants_clerk_org_id_check "
        "CHECK (clerk_org_id IS NULL OR length(clerk_org_id) BETWEEN 1 AND 255)"
    )
    op.execute(
        "CREATE POLICY clerk_org_lookup ON tenants FOR SELECT TO CURRENT_USER "
        f"USING ({LOOKUP_FLAG})"
    )
    op.execute(
        """
        CREATE FUNCTION tenant_for_clerk_org(clerk_org_id text)
        RETURNS uuid
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        DECLARE
            found uuid;
        BEGIN
            IF clerk_org_id IS NULL OR length(clerk_org_id) = 0
               OR length(clerk_org_id) > 255 THEN
                RETURN NULL;
            END IF;
            PERFORM set_config('app.clerk_org_lookup', 'on', true);
            SELECT t.id INTO found
            FROM public.tenants AS t
            WHERE t.clerk_org_id = tenant_for_clerk_org.clerk_org_id;
            PERFORM set_config('app.clerk_org_lookup', 'off', true);
            RETURN found;
        END
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {SIGNATURE}")
    op.execute("DROP POLICY clerk_org_lookup ON tenants")
    op.execute("ALTER TABLE tenants DROP CONSTRAINT tenants_clerk_org_id_check")
    op.execute("ALTER TABLE tenants DROP CONSTRAINT tenants_clerk_org_id_key")
    op.execute("ALTER TABLE tenants DROP COLUMN clerk_org_id")
