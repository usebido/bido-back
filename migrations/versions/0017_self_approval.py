"""Self-approval by default; four-eyes becomes an opt-in tenant policy.

Revision ID: 0017
Revises: 0015
Create Date: 2026-10-07

Until now the requester could never decide their own approval (database CHECK
`approvals_not_self_approved_check`). But the "requester" of an act is the
buyer whose task the AI is working on: the AI prepares the email, the buyer
reviews and approves it. Requiring a second person blocked every buyer working
alone. Human-in-the-loop stays: every act still needs an explicit decision by
a person, on the hashed payload, executed once. Only the four-eyes rule
(requester != decider) becomes a per-tenant policy, off by default.

- `tenants.allow_self_approval` (default TRUE; existing tenants get true).
  A tenant that wants four-eyes has it set to false. The app role can no
  longer UPDATE `tenants` (it never did): the flag is set when a tenant is
  created (seed / provisioning) or by the migration role, never flipped by a
  request.
- `approvals.self_approved` (default false), set by the guard when the
  requester decides their own approval.
- The old CHECK is replaced by `approvals_self_approval_check`: a decision by
  the requester must be marked `self_approved`, and `self_approved` is only
  allowed when the decider is the requester.
- A trigger refuses `self_approved = true` unless the approval's tenant has
  `allow_self_approval`, so the guarantee stays in the database.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "bido_app"

_APP_UPDATABLE = ("status", "decided_by", "decided_at", "executed_at", "self_approved")


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("allow_self_approval", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.execute(f"REVOKE UPDATE ON tenants FROM {APP_ROLE}")

    op.add_column(
        "approvals",
        sa.Column("self_approved", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.drop_constraint("approvals_not_self_approved_check", "approvals")
    op.create_check_constraint(
        "approvals_self_approval_check",
        "approvals",
        "decided_by IS NULL"
        " OR (self_approved AND decided_by = requested_by)"
        " OR (NOT self_approved AND decided_by <> requested_by)",
    )
    op.execute(f"GRANT UPDATE ({', '.join(_APP_UPDATABLE)}) ON approvals TO {APP_ROLE}")

    # SECURITY INVOKER: the caller's tenant session sees its own tenant row
    # under RLS; another tenant's row (or none) means "not allowed".
    op.execute(
        """
        CREATE FUNCTION approvals_check_self_approval() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.self_approved AND NOT COALESCE(
                (SELECT allow_self_approval FROM tenants WHERE id = NEW.tenant_id), false
            ) THEN
                RAISE EXCEPTION 'self-approval is not allowed in this tenant'
                    USING ERRCODE = 'check_violation',
                          CONSTRAINT = 'approvals_self_approval_tenant_check';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER approvals_self_approval_tenant_check "
        "BEFORE INSERT OR UPDATE OF self_approved ON approvals "
        "FOR EACH ROW WHEN (NEW.self_approved) "
        "EXECUTE FUNCTION approvals_check_self_approval()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER approvals_self_approval_tenant_check ON approvals")
    op.execute("DROP FUNCTION approvals_check_self_approval()")
    op.execute(f"REVOKE UPDATE (self_approved) ON approvals FROM {APP_ROLE}")
    op.drop_constraint("approvals_self_approval_check", "approvals")
    # Self-approved rows would violate the old CHECK; they cannot survive it.
    # RLS is forced on approvals, so lift it for the owner just for the delete.
    op.execute("ALTER TABLE approvals NO FORCE ROW LEVEL SECURITY")
    op.execute("DELETE FROM approvals WHERE self_approved")
    op.execute("ALTER TABLE approvals FORCE ROW LEVEL SECURITY")
    op.create_check_constraint(
        "approvals_not_self_approved_check",
        "approvals",
        "decided_by IS NULL OR decided_by <> requested_by",
    )
    op.drop_column("approvals", "self_approved")
    op.execute(f"GRANT UPDATE ON tenants TO {APP_ROLE}")
    op.drop_column("tenants", "allow_self_approval")
