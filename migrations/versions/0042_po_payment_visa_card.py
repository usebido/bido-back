"""A payment's card can come from Visa Intelligent Commerce: approved with a passkey,
issued to the agent as a single-use virtual card.

Revision ID: 0042
Revises: 0041
Create Date: 2026-10-10

* `po_payments.card_rail`: where the payment's card credential comes from, as
  approved: `spt` (a Stripe Shared Payment Token, every payment so far) or `vic`
  (Visa Intelligent Commerce through Basis Theory).
* `po_payments.card_verified_at`: when Bido read the `vic` allowance as approved
  by the cardholder's passkey. NULL until then, and for `spt`.
* `po_payments.issued_card_ref`, `issued_card_last4`, `issued_card_exp_month`,
  `issued_card_exp_year`: the single-use virtual card minted for the agent
  (`vic`): Basis Theory's credential id, the last four digits and the expiry.
  Never its number or security code.

`po_payments` already has a non-null `tenant_id`, forced RLS and the tenant
policy: no new table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: str | Sequence[str] | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECKS: tuple[tuple[str, str], ...] = (
    ("po_payments_card_rail_check", "card_rail IN ('spt', 'vic')"),
    (
        "po_payments_card_verified_check",
        "card_verified_at IS NULL OR card_rail = 'vic'",
    ),
    (
        "po_payments_issued_card_check",
        "(issued_card_last4 IS NULL OR issued_card_last4 ~ '^[0-9]{4}$') "
        "AND (issued_card_exp_month IS NULL OR issued_card_exp_month BETWEEN 1 AND 12) "
        "AND (issued_card_exp_year IS NULL OR issued_card_exp_year BETWEEN 2000 AND 2999) "
        "AND (card_rail = 'vic' OR (issued_card_ref IS NULL AND issued_card_last4 IS NULL))",
    ),
)


def upgrade() -> None:
    op.add_column(
        "po_payments",
        sa.Column("card_rail", sa.Text(), nullable=False, server_default=sa.text("'spt'")),
    )
    op.add_column(
        "po_payments", sa.Column("card_verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("po_payments", sa.Column("issued_card_ref", sa.Text(), nullable=True))
    op.add_column("po_payments", sa.Column("issued_card_last4", sa.Text(), nullable=True))
    op.add_column(
        "po_payments", sa.Column("issued_card_exp_month", sa.SmallInteger(), nullable=True)
    )
    op.add_column(
        "po_payments", sa.Column("issued_card_exp_year", sa.SmallInteger(), nullable=True)
    )
    for name, condition in _CHECKS:
        op.create_check_constraint(name, "po_payments", condition)


def downgrade() -> None:
    for name, _ in reversed(_CHECKS):
        op.drop_constraint(name, "po_payments", type_="check")
    for column in (
        "issued_card_exp_year",
        "issued_card_exp_month",
        "issued_card_last4",
        "issued_card_ref",
        "card_verified_at",
        "card_rail",
    ):
        op.drop_column("po_payments", column)
