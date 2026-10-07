"""Mailbox connections: a buyer's own Gmail / Microsoft 365 mailbox, via OAuth.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-06

Bido sends RFQs from, and reads replies in, the buyer's own mailbox, but only
in the threads Bido started. `mailbox_connections` holds one connection per
(tenant, user, provider): the encrypted OAuth tokens (`token_ciphertext`, see
`bido_back.mailbox.crypto`), the granted scopes, a status, and the sync
cursor (`sync_state`, per provider thread: the provider message ids already
handled). Nothing from the rest of the mailbox is ever stored.

Personal data: `email_address` is the buyer's own work address (the account
the tokens belong to), kept to tell the buyer's own messages from the
supplier's and to show which mailbox is connected. Erasing a user means
deleting their rows here (tokens included).

`token_ciphertext` is NULL once a connection is revoked: the tokens are
erased, the row stays so invites keep pointing at the mailbox their thread
lives in. A CHECK makes an active connection without tokens impossible.

`invites` gains the provider's thread id (Gmail threadId / Graph
conversationId) and the connection it lives in, both set by the mailbox
sender on the first send.

`active_mailbox_tenants()` (for the scheduled sync)
---------------------------------------------------
The periodic sync job has no tenant: it must find the tenants that have an
active connection, and RLS hides every row without `app.tenant_id`. Same
pattern as 0004/0005: a SECURITY DEFINER function, STABLE, `search_path`
pinned, returning ONLY tenant ids (no tokens, no addresses, no user ids). It
gets past FORCEd RLS through a SELECT-only policy `mailbox_tenant_lookup`
granted to the owner role only and gated by the transaction-local flag
`app.mailbox_lookup`, which the function turns on right before its one query
and off right after. EXECUTE is revoked from PUBLIC and granted to the app
role only. The worker then does all real work per tenant through
`tenant_session`.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "bido_app"
CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
LOOKUP_FLAG = "(select current_setting('app.mailbox_lookup', true) = 'on')"
SIGNATURE = "active_mailbox_tenants()"


def upgrade() -> None:
    op.create_table(
        "mailbox_connections",
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
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("email_address", sa.Text(), nullable=False),
        sa.Column("token_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column(
            "scopes",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column(
            "sync_state", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "tenant_id", "user_id", "provider", name="mailbox_connections_tenant_user_provider_key"
        ),
        sa.CheckConstraint(
            "provider IN ('google', 'microsoft')", name="mailbox_connections_provider_check"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'revoked', 'error')", name="mailbox_connections_status_check"
        ),
        sa.CheckConstraint(
            "status <> 'active' OR token_ciphertext IS NOT NULL",
            name="mailbox_connections_active_has_tokens_check",
        ),
    )
    op.execute("ALTER TABLE mailbox_connections ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE mailbox_connections FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON mailbox_connections "
        f"USING (tenant_id = {CURRENT_TENANT}) "
        f"WITH CHECK (tenant_id = {CURRENT_TENANT})"
    )

    op.add_column("invites", sa.Column("provider_thread_id", sa.Text(), nullable=True))
    op.add_column(
        "invites",
        sa.Column(
            "mailbox_connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "mailbox_connections.id",
                name="invites_mailbox_connection_id_fkey",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
    )
    op.execute(
        "CREATE INDEX invites_mailbox_connection_id_idx ON invites (mailbox_connection_id) "
        "WHERE provider_thread_id IS NOT NULL"
    )

    # Owned by whoever runs the migration (bido_migrator locally/CI, the
    # migration role in AWS).
    op.execute(
        "CREATE POLICY mailbox_tenant_lookup ON mailbox_connections FOR SELECT "
        f"TO CURRENT_USER USING ({LOOKUP_FLAG})"
    )
    op.execute(
        """
        CREATE FUNCTION active_mailbox_tenants()
        RETURNS TABLE (tenant_id uuid)
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        BEGIN
            PERFORM set_config('app.mailbox_lookup', 'on', true);
            RETURN QUERY
                SELECT DISTINCT c.tenant_id
                FROM public.mailbox_connections AS c
                WHERE c.status = 'active'
                ORDER BY c.tenant_id;
            PERFORM set_config('app.mailbox_lookup', 'off', true);
        END
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {SIGNATURE}")
    op.execute("DROP INDEX invites_mailbox_connection_id_idx")
    op.drop_column("invites", "mailbox_connection_id")
    op.drop_column("invites", "provider_thread_id")
    op.drop_table("mailbox_connections")
