"""Copilot conversations: a list of each user's conversations.

Revision ID: 0022
Revises: 0019
Create Date: 2026-10-07

The turns of a conversation live in Agno's run storage (schema `agno`), keyed
by tenant and user. Agno can't list one user's copilot sessions efficiently or
safely: it filters by user only, mixes in the user's other agent runs, and
paginates by creation time. So `copilot_conversations` keeps one small row per
conversation (id = the conversation id the API returns): its owner, a title
(the first message, truncated), the turn count and the last activity. It has
no message content beyond the title, `tenant_id`, forced RLS and the tenant
policy; queries also filter by the owner.

Parallel branches add 0020/0021 from 0019: whichever merges later must move
this revision's `down_revision` onto the head at that time.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"


def upgrade() -> None:
    op.create_table(
        "copilot_conversations",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        # The conversation id the API hands out. It is client-supplied when a
        # conversation continues, so the key includes the owner: an id someone
        # else uses never collides with (or updates) this user's row.
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("turns", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("turns >= 0", name="copilot_conversations_turns_check"),
        sa.PrimaryKeyConstraint("tenant_id", "user_id", "id", name="copilot_conversations_pkey"),
    )
    op.create_index(
        "copilot_conversations_owner_activity_idx",
        "copilot_conversations",
        ["tenant_id", "user_id", sa.text("last_activity_at DESC"), sa.text("id DESC")],
    )
    op.execute("ALTER TABLE copilot_conversations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE copilot_conversations FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON copilot_conversations "
        f"USING (tenant_id = {CURRENT_TENANT}) "
        f"WITH CHECK (tenant_id = {CURRENT_TENANT})"
    )


def downgrade() -> None:
    op.drop_table("copilot_conversations")
