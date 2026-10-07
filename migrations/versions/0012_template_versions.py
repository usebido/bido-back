"""RFQ templates: versions, variants per item category, and the version each RFQ used.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-07

`rfq_templates.version`: the template's current version. Its content (greeting,
columns, footer, fields, language) stays on the template row, as before.
Editing a template writes the new content as a new version and moves the row
to it.

`rfq_template_versions`: immutable snapshots of every version once a template
has been edited (the first edit also snapshots version 1). The app role may
insert and read them but not update them; they go away only with their
template.

`rfq_templates.category`: a variant for one item category (`catalog`,
`drawing`, `cell`, `material`); NULL is the general template. At most one
default per tenant and category (the partial unique index now includes the
category; NULL counts as its own category).

`rfqs.template_id` / `rfqs.template_version`: the template version an RFQ's
emails were rendered from, recorded at the first send. `template_version = 0`
with no `template_id` means Bido's standard template.

Every new table has `tenant_id`, forced RLS and the tenant policy.

This branch was cut from 0007 in parallel with other migrations (0008-0011).
When it merges after them, `down_revision` moves to the head at that time.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
APP_ROLE = "bido_app"
CATEGORIES = "('catalog', 'drawing', 'cell', 'material')"


def upgrade() -> None:
    op.add_column("rfq_templates", sa.Column("category", sa.Text(), nullable=True))
    op.add_column(
        "rfq_templates",
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.create_check_constraint(
        "rfq_templates_category_check",
        "rfq_templates",
        f"category IS NULL OR category IN {CATEGORIES}",
    )
    op.create_check_constraint("rfq_templates_version_check", "rfq_templates", "version >= 1")
    op.execute("DROP INDEX rfq_templates_one_default_idx")
    op.execute(
        "CREATE UNIQUE INDEX rfq_templates_one_default_idx "
        "ON rfq_templates (tenant_id, (coalesce(category, ''))) WHERE is_default"
    )

    op.create_table(
        "rfq_template_versions",
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
            "template_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("rfq_templates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("greeting", sa.Text(), nullable=False),
        sa.Column(
            "columns", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "footer_fields",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "fields", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("template_id", "version", name="rfq_template_versions_version_key"),
        sa.CheckConstraint("version >= 1", name="rfq_template_versions_version_check"),
    )
    op.execute("ALTER TABLE rfq_template_versions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE rfq_template_versions FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON rfq_template_versions "
        f"USING (tenant_id = {CURRENT_TENANT}) "
        f"WITH CHECK (tenant_id = {CURRENT_TENANT})"
    )
    # Versions are immutable: the app can add them, never change them.
    op.execute(f"REVOKE UPDATE ON rfq_template_versions FROM {APP_ROLE}")

    op.add_column(
        "rfqs",
        sa.Column(
            "template_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("rfq_templates.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("rfqs", sa.Column("template_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("rfqs", "template_version")
    op.drop_column("rfqs", "template_id")
    op.drop_table("rfq_template_versions")
    op.execute("DROP INDEX rfq_templates_one_default_idx")
    op.execute(
        "CREATE UNIQUE INDEX rfq_templates_one_default_idx ON rfq_templates (tenant_id) "
        "WHERE is_default"
    )
    op.drop_constraint("rfq_templates_version_check", "rfq_templates")
    op.drop_constraint("rfq_templates_category_check", "rfq_templates")
    op.drop_column("rfq_templates", "version")
    op.drop_column("rfq_templates", "category")
