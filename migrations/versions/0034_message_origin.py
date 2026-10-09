"""Where an inbound message came from: the supplier's email, or a quote the buyer uploaded.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-09

A supplier's quote reaches Bido by email: the intake stores the `messages` row
(direction 'in') and its attachments, and extraction turns it into a proposal.
The Copilot's `register_quote` now does the same for a quote the buyer attached
in the chat (a PDF the supplier sent them outside Bido): it stores a `messages`
row for the invite, the file as its attachment, and the extracted proposal in
the same tables, so the comparison and the Quotes tab see it like any reply.

`messages.origin` tells the two apart: 'email' (every row so far; the default)
or 'copilot_upload' (registered by the buyer from the chat). The activity feed
and the comparison read it. Additive: NOT NULL with a server default, so the
intake's inserts, which do not name the column, keep working.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | Sequence[str] | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("origin", sa.Text(), nullable=False, server_default="email"),
    )
    op.create_check_constraint(
        "messages_origin_check", "messages", "origin IN ('email', 'copilot_upload')"
    )


def downgrade() -> None:
    op.drop_constraint("messages_origin_check", "messages")
    op.drop_column("messages", "origin")
