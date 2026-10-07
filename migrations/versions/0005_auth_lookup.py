"""Auth lookup: a Clerk user's memberships, before any tenant is known.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-06

Authentication maps a verified Clerk user id to our user and memberships.
`memberships` is under forced RLS and no tenant is set yet at that point, so
the app role cannot read it directly (and must never read it wholesale).
`memberships_for_clerk_user(clerk_user_id)` returns exactly that one user's
memberships, `(tenant_id, user_id, role)`, for an exact match on a Clerk id
the caller has already verified.

How it gets past RLS (same pattern as 0004's inbound lookups): the function is
SECURITY DEFINER, so it runs as its owner, the migration role. FORCE ROW LEVEL
SECURITY binds the owner too and it has no BYPASSRLS, so a SELECT-only policy
`auth_lookup` on `memberships` admits rows to the owner role only (`TO` that
role, never the app role) and only while the transaction-local flag
`app.auth_lookup` is 'on'. The function turns the flag on with
`set_config(..., true)` right before its one query and off right after. The
app role gains nothing by setting the flag itself, and the migration role
stays tenant-bound during migrations and test cleanup. (A function-level
`SET app.auth_lookup = 'on'` clause would be simpler, but Postgres 15+ lets
only a superuser attach an unregistered custom parameter to a function.)

`search_path` is pinned and every table is schema-qualified, so a caller
cannot shadow `users` or `memberships`. EXECUTE is revoked from PUBLIC and
granted to the app role only.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "bido_app"
SIGNATURE = "memberships_for_clerk_user(text)"
LOOKUP_FLAG = "(select current_setting('app.auth_lookup', true) = 'on')"


def upgrade() -> None:
    # Owned by whoever runs the migration (bido_migrator locally/CI, the
    # migration role in AWS).
    op.execute(
        f"CREATE POLICY auth_lookup ON memberships FOR SELECT TO CURRENT_USER USING ({LOOKUP_FLAG})"
    )
    op.execute(
        """
        CREATE FUNCTION memberships_for_clerk_user(clerk_user_id text)
        RETURNS TABLE (tenant_id uuid, user_id uuid, role text)
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        BEGIN
            IF clerk_user_id IS NULL OR length(clerk_user_id) = 0
               OR length(clerk_user_id) > 255 THEN
                RETURN;
            END IF;
            PERFORM set_config('app.auth_lookup', 'on', true);
            RETURN QUERY
                SELECT m.tenant_id, m.user_id, m.role
                FROM public.users AS u
                JOIN public.memberships AS m ON m.user_id = u.id
                WHERE u.clerk_user_id = memberships_for_clerk_user.clerk_user_id
                ORDER BY m.created_at, m.tenant_id;
            PERFORM set_config('app.auth_lookup', 'off', true);
        END
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {SIGNATURE}")
    op.execute("DROP POLICY auth_lookup ON memberships")
