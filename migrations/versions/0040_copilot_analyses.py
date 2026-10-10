"""Copilot analyses: the source analyses a conversation already ran, reused and explained.

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-09

The buyer asks several questions about one analysis. Until now its result lived only
in the conversation's history, as the bounded view the model read, so a follow-up
("e o item 100000152, por quê?") was answered by running the analysis again, often
with a narrower scope that found nothing and contradicted the first answer.

Each `analyze_procurement_sources` result of a conversation is kept here whole, keyed
by its inputs (`input_key`: the files and the buyer's normalized scope). The same
inputs again return the stored result instead of recomputing (`last_used_at` moves);
`explain_analysis_item` reads an item of the latest one. A narrower re-run (one item
or plant of the same files) is never stored, so it never replaces the analysis the
buyer is asking about.

Tenant owned: forced RLS and the tenant policy; queries also filter by the owner and
the conversation. Rows hold the assessment computed from the buyer's files, like
`procurement_assessments.assessment`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040"
down_revision: str | Sequence[str] | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"


def upgrade() -> None:
    op.create_table(
        "copilot_analyses",
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
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        # The run that computed it.
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        # sha256 of the sorted upload ids and the normalized scope request.
        sa.Column("input_key", sa.Text(), nullable=False),
        sa.Column("upload_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False),
        # The buyer's scope request as the call gave it; null = none.
        sa.Column("request", postgresql.JSONB()),
        sa.Column("assessment", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "user_id", "conversation_id", "input_key", name="copilot_analyses_input"
        ),
    )
    op.create_index(
        "copilot_analyses_conversation_idx",
        "copilot_analyses",
        ["tenant_id", "user_id", "conversation_id", sa.text("last_used_at DESC")],
    )
    op.execute("ALTER TABLE copilot_analyses ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE copilot_analyses FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON copilot_analyses "
        f"USING (tenant_id = {CURRENT_TENANT}) "
        f"WITH CHECK (tenant_id = {CURRENT_TENANT})"
    )


def downgrade() -> None:
    op.drop_table("copilot_analyses")
