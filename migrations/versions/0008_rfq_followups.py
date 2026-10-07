"""RFQ follow-ups: chase schedule settings, auto-reply pauses, chase/clarification emails.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-07

Long waits are rows, never sleeping workers. The `followups.sweep` job (every
15 minutes, EventBridge Scheduler) reads these rows and decides what is due.

`rfqs.followups_*`: the per-RFQ chase schedule. The first chase is due
`followup_first_after_days` business days after the RFQ email was sent, the
next ones every `followup_every_days` business days after the previous one,
at most `followup_max_chases`. Business days skip weekends and the supplier
country's public holidays. The response deadline stays `rfqs.response_due`.

`invites.followups_paused_until`: set when the supplier answers with an
auto-reply (out of office); no chase is proposed before it.

`followup_messages`: one row per chase or clarification email Bido proposes.
Its id is derived from (invite, kind, sequence), so a retried sweep proposes
it once. Every one goes through an approval (`approval_id`) and, once
approved, is sent by the worker in the invite's thread. `message_id_header`
is the Message-ID claimed before sending, reused on a retry. A clarification
names the proposal whose missing fields it asks for (`proposal_id`, one
clarification per proposal). No email content is stored here: the approved
text lives in the approval's args and, once sent, in `messages`.

`followup_tenants()` (for the scheduled sweep): the job has no tenant and RLS
hides every row without `app.tenant_id`. Same pattern as 0006: a SECURITY
DEFINER function returning ONLY tenant ids, past FORCEd RLS through a
SELECT-only policy granted to the owner role and gated by the transaction-local
flag `app.followup_lookup`. EXECUTE is granted to the app role only.

Every new table has `tenant_id`, forced RLS and the tenant policy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "bido_app"
CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
LOOKUP_FLAG = "(select current_setting('app.followup_lookup', true) = 'on')"
SIGNATURE = "followup_tenants()"


def upgrade() -> None:
    op.add_column(
        "rfqs",
        sa.Column("followups_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "rfqs",
        sa.Column("followup_first_after_days", sa.Integer(), nullable=False, server_default="2"),
    )
    op.add_column(
        "rfqs",
        sa.Column("followup_every_days", sa.Integer(), nullable=False, server_default="2"),
    )
    op.add_column(
        "rfqs",
        sa.Column("followup_max_chases", sa.Integer(), nullable=False, server_default="3"),
    )
    op.create_check_constraint(
        "rfqs_followup_schedule_check",
        "rfqs",
        "followup_first_after_days BETWEEN 0 AND 30"
        " AND followup_every_days BETWEEN 1 AND 30"
        " AND followup_max_chases BETWEEN 0 AND 10",
    )

    op.add_column(
        "invites",
        sa.Column("followups_paused_until", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "followup_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column(
            "invite_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invites.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column(
            "proposal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("proposals.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'proposed'")),
        sa.Column(
            "approval_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("approvals.id"),
            nullable=True,
        ),
        sa.Column("message_id_header", sa.Text(), nullable=True),
        sa.Column("canceled_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "invite_id",
            "kind",
            "sequence",
            name="followup_messages_tenant_invite_kind_sequence_key",
        ),
        sa.CheckConstraint(
            "kind IN ('chase', 'clarification')", name="followup_messages_kind_check"
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'sent', 'canceled', 'rejected')",
            name="followup_messages_status_check",
        ),
        sa.CheckConstraint("sequence >= 1", name="followup_messages_sequence_check"),
        sa.CheckConstraint(
            "(kind = 'clarification') = (proposal_id IS NOT NULL)",
            name="followup_messages_proposal_check",
        ),
        sa.CheckConstraint(
            "(status = 'sent') = (sent_at IS NOT NULL)", name="followup_messages_sent_check"
        ),
    )
    op.create_index(
        "followup_messages_proposal_id_key",
        "followup_messages",
        ["proposal_id"],
        unique=True,
        postgresql_where=sa.text("proposal_id IS NOT NULL"),
    )
    op.create_index(
        "followup_messages_tenant_id_invite_id_idx",
        "followup_messages",
        ["tenant_id", "invite_id"],
    )
    op.execute("ALTER TABLE followup_messages ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE followup_messages FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON followup_messages "
        f"USING (tenant_id = {CURRENT_TENANT}) "
        f"WITH CHECK (tenant_id = {CURRENT_TENANT})"
    )

    # Owned by whoever runs the migration (bido_migrator locally/CI, the
    # migration role in AWS).
    op.execute(
        "CREATE POLICY followup_tenant_lookup ON invites FOR SELECT "
        f"TO CURRENT_USER USING ({LOOKUP_FLAG})"
    )
    op.execute(
        """
        CREATE FUNCTION followup_tenants()
        RETURNS TABLE (tenant_id uuid)
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        BEGIN
            PERFORM set_config('app.followup_lookup', 'on', true);
            RETURN QUERY
                SELECT DISTINCT i.tenant_id
                FROM public.invites AS i
                WHERE i.status IN ('sent', 'responded')
                ORDER BY i.tenant_id;
            PERFORM set_config('app.followup_lookup', 'off', true);
        END
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {SIGNATURE}")
    op.execute("DROP POLICY followup_tenant_lookup ON invites")
    op.drop_table("followup_messages")
    op.drop_column("invites", "followups_paused_until")
    op.drop_constraint("rfqs_followup_schedule_check", "rfqs", type_="check")
    op.drop_column("rfqs", "followup_max_chases")
    op.drop_column("rfqs", "followup_every_days")
    op.drop_column("rfqs", "followup_first_after_days")
    op.drop_column("rfqs", "followups_enabled")
