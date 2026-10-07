"""Sandbox tenants and the supplier simulator.

Revision ID: 0016
Revises: 0012
Create Date: 2026-10-07

`tenants.sandbox`: a tenant whose outbound RFQ email never leaves Bido. Its
RFQ, follow-up and negotiation emails go to the supplier simulator instead of
a mailbox, and simulated suppliers answer them (see `bido_back.simulator`).
Only a seed script sets it, at creation: the app role loses UPDATE on
`tenants` except `name`, so no request can turn a real tenant into a sandbox
(or back).

`supplier_personas`: how a simulated supplier behaves (language, behaviors,
price anchor, what it sells, lead time, MOQ, response delay, quote format). One
per supplier.

`sandbox_outbound`: every email a sandbox tenant "sent", with what the
simulator needs to answer it in the same thread (Message-ID, In-Reply-To,
References), when the answer is due, and the offer the simulated supplier
made (the next round concedes from it).

Both tables have `tenant_id`, forced RLS and the tenant policy.

Merge note: branched from 0012 in parallel with other feature branches
(0013-0015). When those merge first, `down_revision` moves to the head at that
time; nothing here depends on their tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "bido_app"
CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
TABLES = ("supplier_personas", "sandbox_outbound")


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


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("sandbox", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # The app may rename a tenant, nothing else: `sandbox` is set at creation only.
    op.execute(f"REVOKE UPDATE ON tenants FROM {APP_ROLE}")
    op.execute(f"GRANT UPDATE (name) ON tenants TO {APP_ROLE}")

    op.create_table(
        "supplier_personas",
        _uuid_pk(),
        _tenant(),
        sa.Column(
            "supplier_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("suppliers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("language", sa.Text(), nullable=False, server_default=sa.text("'en'")),
        sa.Column(
            "behaviors", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("price_anchor", sa.Numeric(6, 3), nullable=False, server_default=sa.text("1.0")),
        sa.Column(
            "reference_prices",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "portfolio", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("lead_time_days", sa.Integer(), nullable=False, server_default=sa.text("28")),
        sa.Column("moq", sa.Numeric(18, 4), nullable=True),
        sa.Column(
            "response_delay_minutes", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("quote_format", sa.Text(), nullable=False, server_default=sa.text("'xlsx'")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("tenant_id", "supplier_id", name="supplier_personas_supplier_key"),
        sa.CheckConstraint(
            "language IN ('it', 'de', 'en', 'fr', 'pt')", name="supplier_personas_language_check"
        ),
        sa.CheckConstraint(
            "quote_format IN ('xlsx', 'pdf', 'body')", name="supplier_personas_format_check"
        ),
        sa.CheckConstraint(
            "price_anchor > 0 AND price_anchor <= 10", name="supplier_personas_anchor_check"
        ),
        sa.CheckConstraint(
            "lead_time_days BETWEEN 1 AND 730", name="supplier_personas_lead_time_check"
        ),
        sa.CheckConstraint(
            "response_delay_minutes BETWEEN 0 AND 1440", name="supplier_personas_delay_check"
        ),
    )

    op.create_table(
        "sandbox_outbound",
        _uuid_pk(),
        _tenant(),
        sa.Column(
            "invite_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invites.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("message_id", sa.Text(), nullable=False),
        sa.Column("in_reply_to", sa.Text(), nullable=True),
        sa.Column(
            "references", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("from_name", sa.Text(), nullable=False),
        sa.Column("from_address", sa.Text(), nullable=False),
        sa.Column("to_addresses", postgresql.JSONB(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("offer", postgresql.JSONB(), nullable=True),
        sa.Column("reply_message_id", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "message_id", name="sandbox_outbound_message_key"),
        sa.CheckConstraint(
            "status IN ('pending', 'replied', 'ignored', 'failed', 'cancelled')",
            name="sandbox_outbound_status_check",
        ),
    )
    op.create_index(
        "sandbox_outbound_invite_idx", "sandbox_outbound", ["tenant_id", "invite_id", "created_at"]
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
    op.execute(f"GRANT UPDATE ON tenants TO {APP_ROLE}")
    op.drop_column("tenants", "sandbox")
