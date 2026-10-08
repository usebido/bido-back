"""Find tenants with approved actions that never ran: `approval_retry_tenants()`.

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-08

Approving commits first and enqueues `approval.execute` after the commit
(`approvals/guard.py`). A crash or an SQS error between the two leaves the
approval `approved` with no job. The scheduled follow-ups sweep re-enqueues
such approvals (`approvals/reconcile.py`); the job is idempotent, so a job that
was only late runs once.

The sweep has no tenant and RLS hides every row without `app.tenant_id`.
Same pattern as `followup_tenants()` (0008): a SECURITY DEFINER function
returning ONLY tenant ids, past FORCEd RLS through a SELECT-only policy granted
to the owner role and gated by the transaction-local flag
`app.approval_lookup`. EXECUTE is granted to the app role only.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0030"
down_revision: str | Sequence[str] | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "bido_app"
LOOKUP_FLAG = "(select current_setting('app.approval_lookup', true) = 'on')"
SIGNATURE = "approval_retry_tenants(interval, interval)"


def upgrade() -> None:
    # Owned by whoever runs the migration (bido_migrator locally/CI, the
    # migration role in AWS).
    op.execute(
        "CREATE POLICY approval_retry_lookup ON approvals FOR SELECT "
        f"TO CURRENT_USER USING ({LOOKUP_FLAG})"
    )
    op.execute(
        """
        CREATE FUNCTION approval_retry_tenants(min_age interval, max_age interval)
        RETURNS TABLE (tenant_id uuid)
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        BEGIN
            PERFORM set_config('app.approval_lookup', 'on', true);
            RETURN QUERY
                SELECT DISTINCT a.tenant_id
                FROM public.approvals AS a
                WHERE a.status = 'approved'
                  AND a.decided_at < now() - min_age
                  AND a.decided_at > now() - max_age
                ORDER BY a.tenant_id;
            PERFORM set_config('app.approval_lookup', 'off', true);
        END
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {SIGNATURE}")
    op.execute("DROP POLICY approval_retry_lookup ON approvals")
