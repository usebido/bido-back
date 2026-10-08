"""Copilot proposals: what the Copilot offered to do, waiting for the buyer's go-ahead.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-08

Creating an RFQ and inviting suppliers are draft actions (nothing is sent),
but the Copilot must not take them on its own: the buyer confirms first, in a
later message. A gated tool called without a proposal stores what it would do
here (`args`, and `preview`: what the buyer is shown, including the intake
draft for an RFQ written from text) and does nothing else. Called again with
the proposal's id from a LATER run of the same conversation, it executes the
stored args, so what runs is what the buyer saw, and marks the row `used_at`
(once).

Tenant owned: forced RLS and the tenant policy; queries also filter by the
owner and the conversation. Rows hold the buyer's request text for an RFQ
proposal, like `rfqs.source_text`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028"
down_revision: str | Sequence[str] | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"


def upgrade() -> None:
    op.create_table(
        "copilot_proposals",
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
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        # The run that proposed: the confirmation must come from another (later) run.
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("args", postgresql.JSONB(), nullable=False),
        sa.Column("preview", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("used_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "copilot_proposals_conversation_idx",
        "copilot_proposals",
        ["tenant_id", "user_id", "conversation_id"],
    )
    op.execute("ALTER TABLE copilot_proposals ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE copilot_proposals FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON copilot_proposals "
        f"USING (tenant_id = {CURRENT_TENANT}) "
        f"WITH CHECK (tenant_id = {CURRENT_TENANT})"
    )


def downgrade() -> None:
    op.drop_table("copilot_proposals")
