"""A saved card keeps the issuer's branding, so the buyer sees their own card.

Revision ID: 0041
Revises: 0040
Create Date: 2026-10-10

* `payment_methods.display`: Basis Theory's `card.display` for the payment
  method (card art URL, background and foreground colours, product
  description, issuer name), cleaned by `bido_back.payments.card_display`:
  `{"art_url", "background_color", "foreground_color", "description",
  "issuer_name"}`, each a string or null. NULL when the provider gave none.
  Public branding, never card data.

Nullable, no default: cards saved before read "no display" until
`scripts/backfill_payment_method_display.py` fills them. `payment_methods`
already has a non-null `tenant_id`, forced RLS and the tenant policy: no new
table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041"
down_revision: str | Sequence[str] | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payment_methods",
        sa.Column("display", postgresql.JSONB(), nullable=True),
    )
    op.create_check_constraint(
        "payment_methods_display_check",
        "payment_methods",
        "display IS NULL OR jsonb_typeof(display) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint("payment_methods_display_check", "payment_methods", type_="check")
    op.drop_column("payment_methods", "display")
