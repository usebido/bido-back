"""Approvals: a person's decision on one exact act-tool payload.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-01

An `act` tool (send, accept, pay, ...) is never executed by the agent runner.
The runner records a pending approval here and stops; a person approves or
rejects it; execution happens elsewhere and only for the exact payload that
was approved. Approvals live in our database, not in Agno's storage, so they
get the same tenant isolation (forced RLS) as every other business table.

Integrity rules enforced by the database:
  * `args_hash` is the SHA-256 (lowercase hex) of the canonical-JSON `args`
    the person saw. The executor must recompute it from `args` and refuse on
    mismatch. The app role cannot UPDATE `args`/`args_hash` (or any column
    other than the decision and execution ones), so an approved payload
    cannot be swapped after the fact.
  * Nobody approves their own request: `decided_by <> requested_by`.
  * `decided_by`/`decided_at` are set exactly when the status is a decision
    (approved, rejected, executed); `executed_at` exactly when executed.
  * One approval per tool call per run per tenant.
  * The app role can never DELETE or TRUNCATE approvals.

Who may approve (membership role) and which status transitions are legal are
application rules, not constraints here.
"""

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "bido_app"

# Same InitPlan form as 0001: evaluated once per statement, NULL when unset.
CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"

# Columns the app role may change after insert: the decision and execution.
_APP_UPDATABLE = ("status", "decided_by", "decided_at", "executed_at")


def _uuid_fk(name: str, target: str, *, nullable: bool) -> sa.Column[UUID]:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(target),
        nullable=nullable,
    )


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        _uuid_fk("tenant_id", "tenants.id", nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("tool_call_id", sa.Text(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("args", postgresql.JSONB(), nullable=False),
        sa.Column("args_hash", sa.Text(), nullable=False),
        _uuid_fk("requested_by", "users.id", nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        _uuid_fk("decided_by", "users.id", nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "tool_call_id",
            name="approvals_tenant_id_run_id_tool_call_id_key",
        ),
        sa.CheckConstraint("args_hash ~ '^[0-9a-f]{64}$'", name="approvals_args_hash_check"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'executed', 'expired')",
            name="approvals_status_check",
        ),
        sa.CheckConstraint(
            "decided_by IS NULL OR decided_by <> requested_by",
            name="approvals_not_self_approved_check",
        ),
        sa.CheckConstraint(
            "(status IN ('approved', 'rejected', 'executed')"
            " AND decided_by IS NOT NULL AND decided_at IS NOT NULL)"
            " OR (status IN ('pending', 'expired')"
            " AND decided_by IS NULL AND decided_at IS NULL)",
            name="approvals_decision_check",
        ),
        sa.CheckConstraint(
            "(status = 'executed') = (executed_at IS NOT NULL)",
            name="approvals_executed_check",
        ),
    )
    op.create_index(
        "approvals_tenant_id_status_created_at_idx",
        "approvals",
        ["tenant_id", "status", "created_at"],
    )

    # Default privileges (init.sql) gave the app role full DML. Narrow it:
    # no DELETE/TRUNCATE ever, and UPDATE only on the decision columns.
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON approvals FROM {APP_ROLE}")
    op.execute(f"GRANT UPDATE ({', '.join(_APP_UPDATABLE)}) ON approvals TO {APP_ROLE}")

    op.execute("ALTER TABLE approvals ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE approvals FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON approvals "
        f"USING (tenant_id = {CURRENT_TENANT}) "
        f"WITH CHECK (tenant_id = {CURRENT_TENANT})"
    )


def downgrade() -> None:
    op.drop_table("approvals")
