"""RFQ flow: contacts, suppliers, RFQs, invites, messages, proposals, FX rates.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-06

Every table except `fx_rates` is tenant-owned (forced RLS, tenant_isolation
policy). Personal data (names, emails, phones) lives only in `contacts`;
everything else references contacts by id, so erasing a person means editing
one row. `fx_rates` is global reference data (daily ECB rates) and is on the
RLS allowlist in scripts/check_rls.py.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"

TENANT_TABLES = (
    "contacts",
    "suppliers",
    "supplier_contacts",
    "rfqs",
    "rfq_lines",
    "invites",
    "messages",
    "attachments",
    "proposals",
    "proposal_lines",
)


def _id() -> sa.Column[UUID]:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _tenant() -> sa.Column[UUID]:
    return sa.Column(
        "tenant_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("tenants.id"),
        nullable=False,
    )


def _fk(
    name: str, target: str, *, nullable: bool = False, cascade: bool = False
) -> sa.Column[UUID]:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(target, ondelete="CASCADE" if cascade else None),
        nullable=nullable,
    )


def _created_at() -> sa.Column[datetime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def upgrade() -> None:
    op.create_table(
        "contacts",
        _id(),
        _tenant(),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("pseudonymized_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.CheckConstraint("kind IN ('buyer', 'supplier')", name="contacts_kind_check"),
    )
    op.create_index("contacts_tenant_id_email_idx", "contacts", ["tenant_id", "email"])

    op.create_table(
        "suppliers",
        _id(),
        _tenant(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False, server_default=sa.text("'distributor'")),
        sa.Column("country", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=False, server_default=sa.text("'en'")),
        sa.Column("email_domain", sa.Text(), nullable=True),
        _created_at(),
        sa.CheckConstraint(
            "type IN ('authorized_distributor', 'distributor', 'reseller', 'manufacturer',"
            " 'machining', 'other')",
            name="suppliers_type_check",
        ),
    )

    op.create_table(
        "supplier_contacts",
        _id(),
        _tenant(),
        _fk("supplier_id", "suppliers.id", cascade=True),
        _fk("contact_id", "contacts.id", cascade=True),
        sa.UniqueConstraint("supplier_id", "contact_id", name="supplier_contacts_key"),
    )

    op.create_table(
        "rfqs",
        _id(),
        _tenant(),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("base_currency", sa.Text(), nullable=False, server_default=sa.text("'EUR'")),
        sa.Column("response_due", sa.Date(), nullable=True),
        sa.Column("source_text", sa.Text(), nullable=True),
        _fk("created_by", "users.id"),
        _created_at(),
        sa.UniqueConstraint("tenant_id", "ref", name="rfqs_tenant_id_ref_key"),
        sa.CheckConstraint(
            "status IN ('draft', 'open', 'closed', 'cancelled')", name="rfqs_status_check"
        ),
    )

    op.create_table(
        "rfq_lines",
        _id(),
        _tenant(),
        _fk("rfq_id", "rfqs.id", cascade=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("mpn", sa.Text(), nullable=True),
        sa.Column("manufacturer", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=False, server_default=sa.text("'catalog'")),
        sa.Column("quantities", postgresql.JSONB(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False, server_default=sa.text("'pcs'")),
        sa.Column("need_by", sa.Date(), nullable=True),
        sa.Column("incoterm", sa.Text(), nullable=True),
        sa.Column("delivery_location", sa.Text(), nullable=True),
        sa.Column("qualification", sa.Text(), nullable=True),
        sa.UniqueConstraint("rfq_id", "position", name="rfq_lines_rfq_id_position_key"),
        sa.CheckConstraint(
            "category IN ('catalog', 'drawing', 'cell', 'material')",
            name="rfq_lines_category_check",
        ),
    )

    op.create_table(
        "invites",
        _id(),
        _tenant(),
        _fk("rfq_id", "rfqs.id", cascade=True),
        _fk("supplier_id", "suppliers.id"),
        _fk("contact_id", "contacts.id"),
        sa.Column("reply_token", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("thread_message_id", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.UniqueConstraint("reply_token", name="invites_reply_token_key"),
        sa.UniqueConstraint("rfq_id", "supplier_id", name="invites_rfq_id_supplier_id_key"),
        sa.CheckConstraint(
            "status IN ('draft', 'pending_approval', 'sent', 'responded', 'declined')",
            name="invites_status_check",
        ),
    )

    op.create_table(
        "messages",
        _id(),
        _tenant(),
        _fk("invite_id", "invites.id", nullable=True),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("message_id_header", sa.Text(), nullable=True),
        sa.Column("in_reply_to", sa.Text(), nullable=True),
        _fk("from_contact_id", "contacts.id", nullable=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("raw_key", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'stored'")),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        _created_at(),
        sa.UniqueConstraint(
            "tenant_id", "message_id_header", name="messages_tenant_id_message_id_header_key"
        ),
        sa.CheckConstraint("direction IN ('in', 'out')", name="messages_direction_check"),
        sa.CheckConstraint(
            "status IN ('stored', 'unmatched', 'quarantined', 'extracted', 'failed')",
            name="messages_status_check",
        ),
    )

    op.create_table(
        "attachments",
        _id(),
        _tenant(),
        _fk("message_id", "messages.id", cascade=True),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        _created_at(),
    )

    op.create_table(
        "proposals",
        _id(),
        _tenant(),
        _fk("invite_id", "invites.id", cascade=True),
        _fk("message_id", "messages.id"),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("valid_until", sa.Date(), nullable=True),
        sa.Column("incoterm", sa.Text(), nullable=True),
        sa.Column("payment_terms", sa.Text(), nullable=True),
        sa.Column(
            "fields", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        _created_at(),
        sa.UniqueConstraint("invite_id", "version", name="proposals_invite_id_version_key"),
    )

    op.create_table(
        "proposal_lines",
        _id(),
        _tenant(),
        _fk("proposal_id", "proposals.id", cascade=True),
        _fk("rfq_line_id", "rfq_lines.id", nullable=True),
        sa.Column("offered_mpn", sa.Text(), nullable=True),
        sa.Column("is_alternative", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("min_quantity", sa.Numeric(18, 4), nullable=True),
        sa.Column("unit_price", sa.Numeric(18, 6), nullable=True),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("tooling_cost", sa.Numeric(18, 2), nullable=True),
        sa.Column("moq", sa.Numeric(18, 4), nullable=True),
        sa.Column("lead_time_days", sa.Integer(), nullable=True),
        sa.Column("origin_country", sa.Text(), nullable=True),
        sa.Column(
            "fields", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.false()),
        _created_at(),
    )

    op.create_table(
        "fx_rates",
        sa.Column("rate_date", sa.Date(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("per_eur", sa.Numeric(18, 8), nullable=False),
        sa.PrimaryKeyConstraint("rate_date", "currency", name="fx_rates_pkey"),
    )

    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = {CURRENT_TENANT}) "
            f"WITH CHECK (tenant_id = {CURRENT_TENANT})"
        )


def downgrade() -> None:
    op.drop_table("fx_rates")
    for table in reversed(TENANT_TABLES):
        op.drop_table(table)
