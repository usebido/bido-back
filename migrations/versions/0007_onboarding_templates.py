"""Onboarding: RFQ templates, sent-mail scans, uploads, and the per-member done flag.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-07

`rfq_templates`: how the tenant's RFQ emails usually look (greeting, item
columns, footer, the fields they ask suppliers for), learned from the buyer's
old RFQs (`search`), from uploaded files (`upload`), or Bido's standard one
(`none`). At most one default per tenant (partial unique index); the RFQ email
writer follows the default. `source_refs` holds ids only (the scan and its
candidate ids, or upload ids), never content.

`template_scans`: one sent-mail scan the user starts from onboarding (the
only place Bido looks outside RFQ threads). `candidates` holds metadata of the
sent emails that look like RFQs (subject, recipients, date, attachment names
and kinds, the provider message id, a note); never bodies or attachment
content.

`template_uploads`: files the user uploads to learn the template from. The
bytes are in blob storage under `storage_key`.

`started_at` on scans and templates is the worker's claim: a job runs only if
nobody claimed the row, or the claim is stale (a worker died mid-job).

`memberships.onboarded_at`: when the user finished onboarding in this tenant.
On the membership, not the user: the mailbox connection and the template are
per tenant, so a person who joins a second tenant onboards there too.

Every new table has `tenant_id`, forced RLS and the tenant policy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
TABLES = ("rfq_templates", "template_scans", "template_uploads")


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


def _timestamp(name: str) -> sa.Column[object]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _jsonb_list(name: str) -> sa.Column[object]:
    return sa.Column(
        name, postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
    )


def upgrade() -> None:
    op.create_table(
        "rfq_templates",
        _uuid_pk(),
        _tenant(),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'building'")),
        sa.Column("language", sa.Text(), nullable=True),
        sa.Column("greeting", sa.Text(), nullable=True),
        _jsonb_list("columns"),
        sa.Column("footer", sa.Text(), nullable=True),
        _jsonb_list("fields"),
        sa.Column(
            "source_refs", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint(
            "source IN ('search', 'upload', 'none')", name="rfq_templates_source_check"
        ),
        sa.CheckConstraint(
            "status IN ('building', 'ready', 'failed')", name="rfq_templates_status_check"
        ),
        sa.CheckConstraint(
            "status <> 'ready' OR greeting IS NOT NULL", name="rfq_templates_ready_check"
        ),
        sa.CheckConstraint(
            "NOT is_default OR status = 'ready'", name="rfq_templates_default_ready_check"
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX rfq_templates_one_default_idx ON rfq_templates (tenant_id) "
        "WHERE is_default"
    )
    op.create_index(
        "rfq_templates_tenant_id_created_at_idx", "rfq_templates", ["tenant_id", "created_at"]
    )

    op.create_table(
        "template_scans",
        _uuid_pk(),
        _tenant(),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "mailbox_connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("mailbox_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'running'")),
        sa.Column("checked", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("found", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _jsonb_list("candidates"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint(
            "status IN ('running', 'done', 'failed')", name="template_scans_status_check"
        ),
    )
    op.create_index(
        "template_scans_tenant_id_user_id_created_at_idx",
        "template_scans",
        ["tenant_id", "user_id", "created_at"],
    )

    op.create_table(
        "template_uploads",
        _uuid_pk(),
        _tenant(),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        _timestamp("created_at"),
    )

    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = {CURRENT_TENANT}) "
            f"WITH CHECK (tenant_id = {CURRENT_TENANT})"
        )

    op.add_column(
        "memberships", sa.Column("onboarded_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("memberships", "onboarded_at")
    for table in reversed(TABLES):
        op.drop_table(table)
