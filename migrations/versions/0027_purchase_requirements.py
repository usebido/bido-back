"""Purchase requirements as a demand source; stock buffers and units; supplier groups.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-08

Buyers usually come with what they must buy already worked out: an MRP export of
purchase requisitions or net requirements (part, quantity, need-by date), or a
list typed in the chat. Until now the analysis only knew demand from a bill of
materials times a production plan, and none of the existing tables holds "this
part, this much, by then":

* `requirements`: one need per row, upserted per (part, need-by date,
  reference); `rfq_id` is set once an RFQ covers it, so it is not quoted twice.
* `inventory.safety_stock`: the buffer the net need must cover;
  `inventory.unit` / `inventory.order_unit`: the unit the stock is counted in
  and the unit the part is bought and priced in, so a requirement in KG is
  never netted against stock counted in another unit by accident.
* `suppliers.category` (the buyer's material group) and the buyer's own
  evaluation (`otif_pct`, `quality_pct`, `rating`, `lead_time_days`): who to
  ask for a part nobody sold before, and how to rank them.
* `analyses.source` ('bom' | 'requirements'), `analysis_lines.safety_stock`
  and `analysis_lines.candidates` (suppliers of the item's group without a
  purchase of it yet).

Additive only: new nullable columns or columns with defaults, one new table with
the same forced RLS policy as every tenant table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
AMOUNT = sa.Numeric(18, 6)


def upgrade() -> None:
    op.create_table(
        "requirements",
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
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("quantity", AMOUNT, nullable=False),
        sa.Column("unit", sa.Text()),
        sa.Column("need_by", sa.Date(), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("location", sa.Text()),
        sa.Column("project", sa.Text()),
        sa.Column("kind", sa.Text()),
        sa.Column("source", sa.Text(), nullable=False, server_default=sa.text("'import'")),
        sa.Column(
            "rfq_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("rfqs.id", ondelete="SET NULL")
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "tenant_id", "item_id", "need_by", "reference", name="requirements_item_date_ref_key"
        ),
        sa.CheckConstraint("quantity > 0", name="requirements_quantity_check"),
        sa.CheckConstraint("source IN ('import', 'chat')", name="requirements_source_check"),
    )
    op.create_index(
        "requirements_open_idx",
        "requirements",
        ["tenant_id", "need_by"],
        postgresql_where=sa.text("rfq_id IS NULL"),
    )
    op.execute("ALTER TABLE requirements ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE requirements FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON requirements "
        f"USING (tenant_id = {CURRENT_TENANT}) WITH CHECK (tenant_id = {CURRENT_TENANT})"
    )

    op.add_column(
        "inventory", sa.Column("safety_stock", AMOUNT, nullable=False, server_default="0")
    )
    op.add_column("inventory", sa.Column("unit", sa.Text()))
    op.add_column("inventory", sa.Column("order_unit", sa.Text()))
    op.create_check_constraint("inventory_safety_stock_check", "inventory", "safety_stock >= 0")

    op.add_column("suppliers", sa.Column("category", sa.Text()))
    op.add_column("suppliers", sa.Column("otif_pct", AMOUNT))
    op.add_column("suppliers", sa.Column("quality_pct", AMOUNT))
    op.add_column("suppliers", sa.Column("rating", AMOUNT))
    op.add_column("suppliers", sa.Column("lead_time_days", AMOUNT))

    op.add_column("analyses", sa.Column("source", sa.Text(), nullable=False, server_default="bom"))
    op.create_check_constraint(
        "analyses_source_check", "analyses", "source IN ('bom', 'requirements')"
    )
    op.add_column(
        "analysis_lines", sa.Column("safety_stock", AMOUNT, nullable=False, server_default="0")
    )
    op.add_column(
        "analysis_lines",
        sa.Column(
            "candidates",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("analysis_lines", "candidates")
    op.drop_column("analysis_lines", "safety_stock")
    op.drop_constraint("analyses_source_check", "analyses", type_="check")
    op.drop_column("analyses", "source")
    for column in ("lead_time_days", "rating", "quality_pct", "otif_pct", "category"):
        op.drop_column("suppliers", column)
    op.drop_constraint("inventory_safety_stock_check", "inventory", type_="check")
    for column in ("order_unit", "unit", "safety_stock"):
        op.drop_column("inventory", column)
    op.drop_index("requirements_open_idx", table_name="requirements")
    op.drop_table("requirements")
