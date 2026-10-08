"""Outbound recipient guard: emails only to test domains or domains the buyer confirmed.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-07

A buyer's spreadsheet can hold made-up supplier addresses whose domains belong
to real companies. With `tenants.outbound_requires_confirmed_domains` (default
true), Bido sends from the buyer's mailbox only to reserved test domains
(`example.com`, `.test`, ...) or to domains a person of the tenant confirmed.

- `tenants.outbound_requires_confirmed_domains boolean NOT NULL DEFAULT true`.
  Set by seeds or the migration role: the app role has no UPDATE on `tenants`
  (migration 0017), so no request can turn the guard off.
- `outbound_domain_confirmations`: one row per (tenant, domain) a person
  confirmed, with who, when and the approval it came with (if any). Tenant
  owned: forced RLS and the tenant policy. Rows are only inserted by the app;
  removing one is an operator action (the app role gets no UPDATE or DELETE).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "bido_app"
CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
TABLE = "outbound_domain_confirmations"


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "outbound_requires_confirmed_domains",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )

    op.create_table(
        TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column(
            "confirmed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # No foreign key: confirmations are written in the approval decision's
        # transaction, which holds the approval row locked.
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "confirmed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tenant_id", "domain", name=f"{TABLE}_tenant_domain_key"),
        sa.CheckConstraint(
            "domain = lower(domain) AND length(domain) BETWEEN 3 AND 253 AND domain LIKE '%.%'",
            name=f"{TABLE}_domain_check",
        ),
    )
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {TABLE} "
        f"USING (tenant_id = {CURRENT_TENANT}) "
        f"WITH CHECK (tenant_id = {CURRENT_TENANT})"
    )
    # Default privileges grant the app SELECT/INSERT/UPDATE/DELETE; keep only
    # what it needs: confirmations are added, never edited or removed by a request.
    op.execute(f"REVOKE UPDATE, DELETE ON {TABLE} FROM {APP_ROLE}")


def downgrade() -> None:
    op.drop_table(TABLE)
    op.drop_column("tenants", "outbound_requires_confirmed_domains")
