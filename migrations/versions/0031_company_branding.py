"""Company branding for RFQ documents: a logo library, company details, per-RFQ choice.

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-08

An RFQ goes out in the buyer company's name, the way large buyers send them on
letterhead: their logo, legal name, tax id and address, and their terms.

* `company_logos`: the company's images. The bytes live in blob storage under
  `branding/<tenant>/<id>.<ext>`; the row keeps the name, type, size and pixel
  size. An SVG is stored sanitized.
* `company_branding`: one row per tenant, every field optional, including the
  default logo and the default footer (terms, confidentiality note).
* `rfqs.logo_asset_id` / `rfqs.footer_override`: what this RFQ uses instead of
  the company defaults (NULL = the default). Editable while the RFQ is a draft.

Both tables are tenant owned: forced RLS and the tenant policy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0031"
down_revision: str | Sequence[str] | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"


def _tenant_owned(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (tenant_id = {CURRENT_TENANT}) "
        f"WITH CHECK (tenant_id = {CURRENT_TENANT})"
    )


def upgrade() -> None:
    op.create_table(
        "company_logos",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "content_type IN ('image/png', 'image/jpeg', 'image/webp', 'image/svg+xml')",
            name="company_logos_content_type_check",
        ),
    )
    op.create_index("company_logos_tenant_idx", "company_logos", ["tenant_id", "created_at"])
    _tenant_owned("company_logos")

    op.create_table(
        "company_branding",
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            primary_key=True,
        ),
        sa.Column("legal_name", sa.Text()),
        sa.Column("trade_name", sa.Text()),
        sa.Column("tax_id", sa.Text()),
        sa.Column("address", sa.Text()),
        sa.Column("phone", sa.Text()),
        sa.Column("website", sa.Text()),
        sa.Column("default_footer", sa.Text()),
        sa.Column("accent_color", sa.Text()),
        sa.Column(
            "default_logo_asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("company_logos.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "updated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "accent_color IS NULL OR accent_color ~ '^#[0-9A-Fa-f]{6}$'",
            name="company_branding_accent_color_check",
        ),
    )
    _tenant_owned("company_branding")

    op.add_column(
        "rfqs",
        sa.Column(
            "logo_asset_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("company_logos.id", ondelete="SET NULL"),
        ),
    )
    op.add_column("rfqs", sa.Column("footer_override", sa.Text()))


def downgrade() -> None:
    op.drop_column("rfqs", "footer_override")
    op.drop_column("rfqs", "logo_asset_id")
    op.drop_table("company_branding")
    op.drop_table("company_logos")
