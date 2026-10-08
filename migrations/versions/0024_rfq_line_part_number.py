"""RFQ lines carry the buyer's own part number.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-08

A line only had `mpn` (the manufacturer's number), so the buyer's internal code
(`RL-6206-2RS`) ended up there and could be "completed" into a manufacturer
number it never was. `rfq_lines.part_number` keeps the buyer's code verbatim as
the line's main reference; `mpn` stays a separate, optional field.

Nullable, no default: existing lines have no part number. Same table grants
and RLS as the rest of `rfq_lines`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | Sequence[str] | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("rfq_lines", sa.Column("part_number", sa.Text()))


def downgrade() -> None:
    op.drop_column("rfq_lines", "part_number")
