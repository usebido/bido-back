"""Logo preparation (trimmed, background removable) and logo placement on RFQ documents.

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-08

* `company_logos.original_storage_key` / `original_content_type`: the file exactly as
  uploaded. `storage_key` now points at the prepared image the documents use: margins
  trimmed and, when the buyer asks, the background removed (always a PNG). Rows from
  0031 keep `original_storage_key` NULL: their stored file is the original.
* `company_logos.background_removable` / `background_removed`: an opaque, near-uniform
  background (plain or a baked-in checkerboard) was detected / has been removed.
* `company_branding.logo_scale` / `logo_align` / `logo_offset_y`: where the logo sits
  in its reserved box on the document (defaults 1.0, left, 0).
* `rfqs.logo_scale` / `logo_align` / `logo_offset_y`: this RFQ's own placement
  (NULL = the company default). Editable while the RFQ is a draft, like its logo.

All additive; both tables already have forced RLS.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | Sequence[str] | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALIGN = "('left', 'center', 'right')"


def upgrade() -> None:
    op.add_column("company_logos", sa.Column("original_storage_key", sa.Text()))
    op.add_column("company_logos", sa.Column("original_content_type", sa.Text()))
    op.add_column(
        "company_logos",
        sa.Column("background_removable", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column(
        "company_logos",
        sa.Column("background_removed", sa.Boolean(), nullable=False, server_default="false"),
    )
    for table, nullable in (("company_branding", False), ("rfqs", True)):
        op.add_column(
            table,
            sa.Column(
                "logo_scale",
                sa.Numeric(3, 2),
                nullable=nullable,
                server_default=None if nullable else "1.00",
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "logo_align",
                sa.Text(),
                nullable=nullable,
                server_default=None if nullable else "left",
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "logo_offset_y",
                sa.SmallInteger(),
                nullable=nullable,
                server_default=None if nullable else "0",
            ),
        )
        op.create_check_constraint(
            f"{table}_logo_placement_check",
            table,
            "(logo_scale IS NULL OR logo_scale BETWEEN 0.5 AND 2.0)"
            f" AND (logo_align IS NULL OR logo_align IN {_ALIGN})"
            " AND (logo_offset_y IS NULL OR logo_offset_y BETWEEN -20 AND 20)",
        )


def downgrade() -> None:
    for table in ("rfqs", "company_branding"):
        op.drop_constraint(f"{table}_logo_placement_check", table)
        op.drop_column(table, "logo_offset_y")
        op.drop_column(table, "logo_align")
        op.drop_column(table, "logo_scale")
    op.drop_column("company_logos", "background_removed")
    op.drop_column("company_logos", "background_removable")
    op.drop_column("company_logos", "original_content_type")
    op.drop_column("company_logos", "original_storage_key")
