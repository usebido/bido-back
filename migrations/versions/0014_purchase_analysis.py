"""Purchase analysis: imported company data and the analyses built from it.

Revision ID: 0014
Revises: 0012
Create Date: 2026-10-07

The data a buyer needs to answer "what do I buy for the next build, how much,
from whom, at what price", imported from spreadsheets in the prototype (no
ERP connection yet):

* `items`: the company's parts, by internal part number (MPN, manufacturer,
  category, unit).
* `bom_lines`: bill of materials, one row per (product, item) with the
  quantity of that item in one unit of the product (e.g. "Motor EV-120").
* `inventory`: on hand and already on order, per item and location.
* `purchase_orders` + `po_lines`: purchase history (supplier, prices, ordered,
  promised and received dates, received and rejected quantities). Suppliers
  are the existing `suppliers` rows, created by name on import when missing.
* `production_plan`: how many units of a product are needed by when.
* `analyses` + `analysis_lines`: one run of the purchase analysis (computed in
  code, see `bido_back.purchasing.analysis`) and its result per item. An
  analysis line links to the RFQ line created from it (`rfq_line_id`), with
  the target and maximum prices the negotiation can read.

Every table has `tenant_id`, forced RLS and the tenant policy.

Merge note: branched from 0012 in parallel with other feature branches
(0013, 0015, 0016). Whichever merges later moves its `down_revision` to the
head at that time; nothing here depends on their tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
TABLES = (
    "items",
    "bom_lines",
    "inventory",
    "purchase_orders",
    "po_lines",
    "production_plan",
    "analyses",
    "analysis_lines",
)
AMOUNT = sa.Numeric(18, 6)


def _uuid_pk() -> sa.Column[object]:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _tenant() -> sa.Column[object]:
    return sa.Column(
        "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )


def _fk(name: str, target: str, *, ondelete: str, nullable: bool = False) -> sa.Column[object]:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(target, ondelete=ondelete),
        nullable=nullable,
    )


def _created_at() -> sa.Column[object]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def upgrade() -> None:
    op.create_table(
        "items",
        _uuid_pk(),
        _tenant(),
        sa.Column("part_number", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("mpn", sa.Text(), nullable=True),
        sa.Column("manufacturer", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("unit", sa.Text(), nullable=False, server_default=sa.text("'pcs'")),
        _created_at(),
        sa.UniqueConstraint("tenant_id", "part_number", name="items_tenant_part_number_key"),
    )

    op.create_table(
        "bom_lines",
        _uuid_pk(),
        _tenant(),
        sa.Column("product", sa.Text(), nullable=False),
        _fk("item_id", "items.id", ondelete="CASCADE"),
        sa.Column("qty_per_unit", AMOUNT, nullable=False),
        _created_at(),
        sa.UniqueConstraint("tenant_id", "product", "item_id", name="bom_lines_product_item_key"),
        sa.CheckConstraint("qty_per_unit > 0", name="bom_lines_qty_check"),
    )

    op.create_table(
        "inventory",
        _uuid_pk(),
        _tenant(),
        _fk("item_id", "items.id", ondelete="CASCADE"),
        sa.Column("location", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("on_hand", AMOUNT, nullable=False, server_default=sa.text("0")),
        sa.Column("on_order", AMOUNT, nullable=False, server_default=sa.text("0")),
        sa.Column("as_of", sa.Date(), nullable=True),
        _created_at(),
        sa.UniqueConstraint("tenant_id", "item_id", "location", name="inventory_item_location_key"),
        sa.CheckConstraint("on_hand >= 0 AND on_order >= 0", name="inventory_quantities_check"),
    )

    op.create_table(
        "purchase_orders",
        _uuid_pk(),
        _tenant(),
        sa.Column("po_number", sa.Text(), nullable=False),
        _fk("supplier_id", "suppliers.id", ondelete="CASCADE"),
        sa.Column("currency", sa.Text(), nullable=False, server_default=sa.text("'EUR'")),
        sa.Column("ordered_at", sa.Date(), nullable=False),
        _created_at(),
        sa.UniqueConstraint(
            "tenant_id", "supplier_id", "po_number", name="purchase_orders_number_key"
        ),
    )

    op.create_table(
        "po_lines",
        _uuid_pk(),
        _tenant(),
        _fk("purchase_order_id", "purchase_orders.id", ondelete="CASCADE"),
        sa.Column("line_no", sa.Integer(), nullable=False),
        _fk("item_id", "items.id", ondelete="CASCADE"),
        sa.Column("quantity", AMOUNT, nullable=False),
        sa.Column("unit_price", AMOUNT, nullable=False),
        sa.Column("promised_at", sa.Date(), nullable=True),
        sa.Column("received_at", sa.Date(), nullable=True),
        sa.Column("received_qty", AMOUNT, nullable=True),
        sa.Column("rejected_qty", AMOUNT, nullable=False, server_default=sa.text("0")),
        _created_at(),
        sa.UniqueConstraint("tenant_id", "purchase_order_id", "line_no", name="po_lines_line_key"),
        sa.CheckConstraint("quantity > 0", name="po_lines_quantity_check"),
        sa.CheckConstraint("unit_price >= 0", name="po_lines_price_check"),
        sa.CheckConstraint(
            "rejected_qty >= 0 AND (received_qty IS NULL OR received_qty >= 0)",
            name="po_lines_received_check",
        ),
    )
    op.create_index("po_lines_tenant_item_idx", "po_lines", ["tenant_id", "item_id"])

    op.create_table(
        "production_plan",
        _uuid_pk(),
        _tenant(),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("quantity", AMOUNT, nullable=False),
        sa.Column("need_by", sa.Date(), nullable=False),
        _created_at(),
        sa.UniqueConstraint(
            "tenant_id", "product", "need_by", name="production_plan_product_date_key"
        ),
        sa.CheckConstraint("quantity > 0", name="production_plan_quantity_check"),
    )

    op.create_table(
        "analyses",
        _uuid_pk(),
        _tenant(),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("quantity", AMOUNT, nullable=False),
        sa.Column("need_by", sa.Date(), nullable=True),
        sa.Column(
            "params", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "summary", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        _fk("created_by", "users.id", ondelete="SET NULL", nullable=True),
        _fk("rfq_id", "rfqs.id", ondelete="SET NULL", nullable=True),
        _created_at(),
        sa.CheckConstraint("quantity > 0", name="analyses_quantity_check"),
    )

    op.create_table(
        "analysis_lines",
        _uuid_pk(),
        _tenant(),
        _fk("analysis_id", "analyses.id", ondelete="CASCADE"),
        _fk("item_id", "items.id", ondelete="CASCADE"),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("required", AMOUNT, nullable=False),
        sa.Column("on_hand", AMOUNT, nullable=False),
        sa.Column("on_order", AMOUNT, nullable=False),
        sa.Column("to_buy", AMOUNT, nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("need_by", sa.Date(), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("last_price", AMOUNT, nullable=True),
        sa.Column("avg_price", AMOUNT, nullable=True),
        sa.Column("min_price", AMOUNT, nullable=True),
        sa.Column("trend_pct", AMOUNT, nullable=True),
        sa.Column("target_price", AMOUNT, nullable=True),
        sa.Column("max_price", AMOUNT, nullable=True),
        sa.Column(
            "suppliers", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "flags", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        _fk("rfq_line_id", "rfq_lines.id", ondelete="SET NULL", nullable=True),
        sa.UniqueConstraint("tenant_id", "analysis_id", "position", name="analysis_lines_pos_key"),
    )
    op.create_index(
        "analysis_lines_rfq_line_idx",
        "analysis_lines",
        ["tenant_id", "rfq_line_id"],
        postgresql_where=sa.text("rfq_line_id IS NOT NULL"),
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
