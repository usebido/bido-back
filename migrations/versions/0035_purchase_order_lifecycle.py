"""The purchase order after "sent": acknowledged, received (per line), closed, cancelled.

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-09

`issued_purchase_orders.status` keeps its three send values (`draft`,
`pending_approval`, `sent`): the API exposes that enum and widening it would
break clients. The order's life after the send is a separate column:

`fulfilment_state`: `not_sent` until the send, then `sent` -> `acknowledged`
-> `partially_received` -> `received`, and the terminal `closed` or
`cancelled`. Rows that were already `sent` are backfilled to `sent`; the rest
stay `not_sent` (they move to `sent` when the worker sends them).

* `acknowledged_at` / `acknowledged_by` / `supplier_order_ref` /
  `acknowledgement_note`: the supplier confirmed the order (recorded by the
  buyer; a mailbox reply may set it later through the same service).
* `closed_at` / `closed_by` / `closed_short` / `close_reason`: closed after
  everything arrived, or short (then a reason is required).
* `cancelled_at` / `cancelled_by` / `cancel_reason`: cancelled before receipt.

`purchase_order_receipts`: one delivery against a PO (date, invoice number
and date, note, who recorded it); `purchase_order_receipt_lines`: the
quantity received per PO line in that receipt. The received total per line
never exceeds the ordered quantity (the service checks it under the PO's row
lock). Many receipts per PO; both tables cascade with the PO.

`rfqs.completed_at`: set once every PO of the RFQ is closed or cancelled
(with at least one closed). The RFQ `status` stays `closed` (same reason as
above): the list derives its `completed` stage from the POs.

Every new table has `tenant_id`, forced RLS and the tenant policy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0035"
down_revision: str | Sequence[str] | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
TABLES = ("purchase_order_receipts", "purchase_order_receipt_lines")
STATES = (
    "'not_sent', 'sent', 'acknowledged', 'partially_received', 'received', 'closed', 'cancelled'"
)


def _user_fk(name: str) -> sa.Column[object]:
    return sa.Column(name, postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True)


def _timestamp(name: str) -> sa.Column[object]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    po = "issued_purchase_orders"
    op.add_column(
        po,
        sa.Column(
            "fulfilment_state", sa.Text(), nullable=False, server_default=sa.text("'not_sent'")
        ),
    )
    op.execute("UPDATE issued_purchase_orders SET fulfilment_state = 'sent' WHERE status = 'sent'")
    op.add_column(po, _timestamp("acknowledged_at"))
    op.add_column(po, _user_fk("acknowledged_by"))
    op.add_column(po, sa.Column("supplier_order_ref", sa.Text(), nullable=True))
    op.add_column(po, sa.Column("acknowledgement_note", sa.Text(), nullable=True))
    op.add_column(po, _timestamp("closed_at"))
    op.add_column(po, _user_fk("closed_by"))
    op.add_column(
        po,
        sa.Column("closed_short", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(po, sa.Column("close_reason", sa.Text(), nullable=True))
    op.add_column(po, _timestamp("cancelled_at"))
    op.add_column(po, _user_fk("cancelled_by"))
    op.add_column(po, sa.Column("cancel_reason", sa.Text(), nullable=True))
    op.create_check_constraint(
        f"{po}_fulfilment_state_check", po, f"fulfilment_state IN ({STATES})"
    )
    op.create_check_constraint(
        f"{po}_fulfilment_sent_check",
        po,
        "fulfilment_state IN ('not_sent', 'cancelled') OR sent_at IS NOT NULL",
    )
    op.create_check_constraint(
        f"{po}_fulfilment_closed_check",
        po,
        "(fulfilment_state = 'closed') = (closed_at IS NOT NULL)"
        " AND (NOT closed_short OR close_reason IS NOT NULL)",
    )
    op.create_check_constraint(
        f"{po}_fulfilment_cancelled_check",
        po,
        "(fulfilment_state = 'cancelled') = (cancelled_at IS NOT NULL)"
        " AND (cancelled_at IS NULL OR cancel_reason IS NOT NULL)",
    )
    op.create_index(f"{po}_tenant_id_fulfilment_state_idx", po, ["tenant_id", "fulfilment_state"])

    op.add_column("rfqs", _timestamp("completed_at"))

    op.create_table(
        "purchase_order_receipts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "purchase_order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("issued_purchase_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("received_on", sa.Date(), nullable=False),
        sa.Column("invoice_number", sa.Text(), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "recorded_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "purchase_order_receipts_purchase_order_id_idx",
        "purchase_order_receipts",
        ["purchase_order_id"],
    )

    op.create_table(
        "purchase_order_receipt_lines",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "receipt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("purchase_order_receipts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "purchase_order_line_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("issued_purchase_order_lines.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False),
        sa.UniqueConstraint(
            "receipt_id",
            "purchase_order_line_id",
            name="purchase_order_receipt_lines_receipt_id_line_id_key",
        ),
        sa.CheckConstraint("quantity > 0", name="purchase_order_receipt_lines_quantity_check"),
    )
    op.create_index(
        "purchase_order_receipt_lines_purchase_order_line_id_idx",
        "purchase_order_receipt_lines",
        ["purchase_order_line_id"],
    )

    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = {CURRENT_TENANT}) "
            f"WITH CHECK (tenant_id = {CURRENT_TENANT})"
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
    op.drop_column("rfqs", "completed_at")
    po = "issued_purchase_orders"
    op.drop_index(f"{po}_tenant_id_fulfilment_state_idx", table_name=po)
    for name in ("cancelled", "closed", "sent", "state"):
        op.drop_constraint(f"{po}_fulfilment_{name}_check", po)
    for column in (
        "cancel_reason",
        "cancelled_by",
        "cancelled_at",
        "close_reason",
        "closed_short",
        "closed_by",
        "closed_at",
        "acknowledgement_note",
        "supplier_order_ref",
        "acknowledged_by",
        "acknowledged_at",
        "fulfilment_state",
    ):
        op.drop_column(po, column)
