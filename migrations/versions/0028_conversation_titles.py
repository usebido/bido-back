"""Copilot conversation titles: where a title came from, and when it settled.

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-08

A conversation's title starts as the first message, truncated (`fallback`).
A small agent then refines it over the first turns until the conversation's
purpose is clear (`ai`, `title_settled`). Creating an RFQ, running an analysis
or preparing an award writes a deterministic title with its reference
(`milestone`, once per kind, recorded in `title_milestones`). A buyer's own
rename (`user`) is final: nothing updates the title after it.

`title_language` is the language the titler detected (ISO 639-1), used to
label milestone titles. All columns are additive with defaults, so existing
rows read as untouched fallback titles.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028"
down_revision: str | Sequence[str] | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "copilot_conversations",
        sa.Column("title_source", sa.Text(), nullable=False, server_default="fallback"),
    )
    op.add_column(
        "copilot_conversations",
        sa.Column("title_settled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "copilot_conversations",
        sa.Column("title_language", sa.Text(), nullable=True),
    )
    op.add_column(
        "copilot_conversations",
        sa.Column(
            "title_milestones",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )
    op.create_check_constraint(
        "copilot_conversations_title_source_check",
        "copilot_conversations",
        "title_source IN ('fallback', 'ai', 'milestone', 'user')",
    )
    op.create_check_constraint(
        "copilot_conversations_title_length_check",
        "copilot_conversations",
        "char_length(title) BETWEEN 1 AND 500",
    )


def downgrade() -> None:
    op.drop_constraint(
        "copilot_conversations_title_length_check", "copilot_conversations", type_="check"
    )
    op.drop_constraint(
        "copilot_conversations_title_source_check", "copilot_conversations", type_="check"
    )
    op.drop_column("copilot_conversations", "title_milestones")
    op.drop_column("copilot_conversations", "title_language")
    op.drop_column("copilot_conversations", "title_settled")
    op.drop_column("copilot_conversations", "title_source")
