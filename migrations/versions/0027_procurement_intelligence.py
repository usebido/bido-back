"""Persist procurement evidence, scoped stock inputs and comparison policies.

Revision ID: 0027
Revises: 0026
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"


def upgrade() -> None:
    for column in ("reserved_qty", "blocked_qty"):
        op.add_column(
            "inventory",
            sa.Column(column, sa.Numeric(18, 6), nullable=False, server_default=sa.text("0")),
        )
    op.add_column("inventory", sa.Column("on_order_due", sa.Date()))
    op.create_check_constraint(
        "inventory_unavailable_check", "inventory", "reserved_qty >= 0 AND blocked_qty >= 0"
    )
    for table, column in (
        ("analysis_lines", "calculation"),
        ("rfqs", "comparison_policy"),
        ("rfq_lines", "source_evidence"),
    ):
        op.add_column(
            table,
            sa.Column(
                column, postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
            ),
        )
    op.create_table(
        "procurement_assessments",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
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
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("upload_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("assessment", postgresql.JSONB(), nullable=False),
        sa.Column(
            "rfq_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("rfqs.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "tenant_id", "user_id", "source_key", name="procurement_assessments_source_key"
        ),
    )
    op.execute("ALTER TABLE procurement_assessments ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE procurement_assessments FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON procurement_assessments "
        f"USING (tenant_id = {CURRENT_TENANT}) WITH CHECK (tenant_id = {CURRENT_TENANT})"
    )
    # Evidence is immutable to the application; only the draft link can change.
    op.execute("REVOKE UPDATE ON procurement_assessments FROM bido_app")
    op.execute("GRANT UPDATE (rfq_id) ON procurement_assessments TO bido_app")


def downgrade() -> None:
    op.drop_table("procurement_assessments")
    op.drop_column("rfq_lines", "source_evidence")
    op.drop_column("rfqs", "comparison_policy")
    op.drop_column("analysis_lines", "calculation")
    op.drop_constraint("inventory_unavailable_check", "inventory", type_="check")
    op.drop_column("inventory", "on_order_due")
    op.drop_column("inventory", "blocked_qty")
    op.drop_column("inventory", "reserved_qty")
