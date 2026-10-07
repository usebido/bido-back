"""Field corrections: what a person changed (or confirmed) in an extracted quote.

Revision ID: 0010
Revises: 0007
Create Date: 2026-10-07

`field_corrections`: one row per field a user corrected or confirmed on a
proposal header (`proposal_line_id` NULL) or a proposal line. It keeps the
value before and after, and the extraction's provenance of the old value
(source, quote, confidence, issues), so the rows double as the evaluation set
for quote extraction. The proposal tables hold the effective value; the
correction rows are append-only history.

Parallel branches all start from 0007; whichever merges later moves its
`down_revision` onto the current head.

The table has `tenant_id`, forced RLS and the tenant policy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"


def upgrade() -> None:
    op.create_table(
        "field_corrections",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
        ),
        sa.Column(
            "proposal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("proposals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "proposal_line_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("proposal_lines.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("old_value", postgresql.JSONB(), nullable=True),
        sa.Column("new_value", postgresql.JSONB(), nullable=True),
        sa.Column(
            "old_provenance",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "corrected_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "action IN ('corrected', 'confirmed')", name="field_corrections_action_check"
        ),
    )
    op.create_index(
        "field_corrections_proposal_idx", "field_corrections", ["tenant_id", "proposal_id"]
    )
    op.execute("ALTER TABLE field_corrections ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE field_corrections FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON field_corrections "
        f"USING (tenant_id = {CURRENT_TENANT}) "
        f"WITH CHECK (tenant_id = {CURRENT_TENANT})"
    )


def downgrade() -> None:
    op.drop_table("field_corrections")
