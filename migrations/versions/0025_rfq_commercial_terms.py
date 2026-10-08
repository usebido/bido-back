"""RFQs carry the buyer's payment terms and the quote validity they ask for.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-08

Buyers state payment terms ("28 DDL", "30 days net") and how long a quote must
stay valid on the RFQ itself. Nothing could store either, so the Copilot could
only claim it had. `rfqs.payment_terms` keeps the buyer's words verbatim;
`rfqs.quote_validity_days` is the minimum validity asked of suppliers, between
1 and 365 days.

Nullable, no default: existing RFQs state neither. Same table grants and RLS
as the rest of `rfqs`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | Sequence[str] | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("rfqs", sa.Column("payment_terms", sa.Text()))
    op.add_column("rfqs", sa.Column("quote_validity_days", sa.Integer()))
    op.create_check_constraint(
        "rfqs_quote_validity_days_check", "rfqs", "quote_validity_days BETWEEN 1 AND 365"
    )


def downgrade() -> None:
    op.drop_constraint("rfqs_quote_validity_days_check", "rfqs", type_="check")
    op.drop_column("rfqs", "quote_validity_days")
    op.drop_column("rfqs", "payment_terms")
