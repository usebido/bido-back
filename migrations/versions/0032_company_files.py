"""The company file library: searchable text per file, and library references.

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-08

Buyers' uploads (`template_uploads`) and suppliers' reply attachments
(`attachments`) form one library per company (`bido_back.files`).

* `file_texts`: the text read from each file once (cells, pages, paragraphs,
  with their locator markers), for content search. `tsv` is a stored
  `to_tsvector('simple', ...)`: no language stemming, so Portuguese, English and
  Italian words match as typed. A GIN index serves `@@` queries. `status` says
  whether the file was read (`indexed`), could not be (`skipped`, `failed`).
  Tenant owned: forced RLS and the tenant policy.
* `template_uploads.source_kind` / `source_id`: an upload row that only points
  at a library file (same storage key, no copy), made when a buyer attaches a
  colleague's upload or a supplier attachment to a Copilot message. The library
  lists the original only.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0032"
down_revision: str | Sequence[str] | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"


def upgrade() -> None:
    op.create_table(
        "file_texts",
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
        ),
        sa.Column("file_kind", sa.Text(), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple'::regconfig, text)", persisted=True),
        ),
        sa.Column(
            "indexed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("tenant_id", "file_kind", "file_id", name="file_texts_pkey"),
        sa.CheckConstraint(
            "file_kind IN ('upload', 'supplier_attachment')", name="file_texts_kind_check"
        ),
        sa.CheckConstraint(
            "status IN ('indexed', 'skipped', 'failed')", name="file_texts_status_check"
        ),
    )
    op.create_index("file_texts_tsv_idx", "file_texts", ["tsv"], postgresql_using="gin")
    op.execute("ALTER TABLE file_texts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE file_texts FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON file_texts "
        f"USING (tenant_id = {CURRENT_TENANT}) "
        f"WITH CHECK (tenant_id = {CURRENT_TENANT})"
    )

    op.add_column("template_uploads", sa.Column("source_kind", sa.Text()))
    op.add_column("template_uploads", sa.Column("source_id", postgresql.UUID(as_uuid=True)))
    op.create_check_constraint(
        "template_uploads_source_check",
        "template_uploads",
        "(source_kind IS NULL AND source_id IS NULL) OR "
        "(source_kind IN ('upload', 'supplier_attachment') AND source_id IS NOT NULL)",
    )
    op.create_index(
        "template_uploads_tenant_created_idx", "template_uploads", ["tenant_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("template_uploads_tenant_created_idx", table_name="template_uploads")
    op.drop_constraint("template_uploads_source_check", "template_uploads", type_="check")
    op.drop_column("template_uploads", "source_id")
    op.drop_column("template_uploads", "source_kind")
    op.drop_table("file_texts")
