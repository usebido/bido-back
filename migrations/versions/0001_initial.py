"""Initial schema: tenants, users, memberships, audit_events, with RLS.

Revision ID: 0001
Revises:
Create Date: 2026-09-30

Pooled multi-tenancy: tenant-owned rows carry tenant_id and are isolated by
RLS (ENABLE + FORCE, so even the owning migration role is subject to it).
The current tenant comes from the transaction-local `app.tenant_id` setting
(see bido_back.db.session.tenant_session). An unset or empty setting maps to
NULL, which matches no row: fail closed.

`users` is global (no tenant_id, no RLS) and is allowlisted in
scripts/check_rls.py.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "bido_app"

# InitPlan form: the subselect is evaluated once per statement, not per row.
CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"


def _uuid_pk() -> sa.Column[UUID]:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _created_at() -> sa.Column[datetime]:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


def _enable_rls(table: str, column: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING ({column} = {CURRENT_TENANT}) "
        f"WITH CHECK ({column} = {CURRENT_TENANT})"
    )


def upgrade() -> None:
    # The app role has no business touching migration bookkeeping, but the
    # default privileges from init.sql granted it DML on alembic_version.
    op.execute(f"REVOKE ALL ON alembic_version FROM {APP_ROLE}")

    op.create_table(
        "tenants",
        _uuid_pk(),
        sa.Column("name", sa.Text(), nullable=False),
        _created_at(),
    )

    op.create_table(
        "users",
        _uuid_pk(),
        sa.Column("clerk_user_id", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        _created_at(),
        sa.UniqueConstraint("clerk_user_id", name="users_clerk_user_id_key"),
    )

    op.create_table(
        "memberships",
        _uuid_pk(),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.Text(), nullable=False),
        _created_at(),
        sa.UniqueConstraint("tenant_id", "user_id", name="memberships_tenant_id_user_id_key"),
        sa.CheckConstraint(
            "role IN ('member', 'approver', 'owner')", name="memberships_role_check"
        ),
    )
    op.create_index("memberships_user_id_idx", "memberships", ["user_id"])

    op.create_table(
        "audit_events",
        _uuid_pk(),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        _created_at(),
    )
    op.create_index(
        "audit_events_tenant_id_created_at_idx", "audit_events", ["tenant_id", "created_at"]
    )

    # Append-only for the app role. The migration role (owner) keeps full
    # rights for retention jobs and test cleanup.
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON audit_events FROM {APP_ROLE}")

    _enable_rls("tenants", "id")
    _enable_rls("memberships", "tenant_id")
    _enable_rls("audit_events", "tenant_id")


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("memberships")
    op.drop_table("users")
    op.drop_table("tenants")
