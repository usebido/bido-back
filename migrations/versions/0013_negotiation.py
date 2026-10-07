"""Supplier negotiation: per-RFQ settings, per-line limits, per-invite state, messages.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-07

`rfqs.negotiation_*`: the negotiation of an RFQ. It starts (`open`) when the
buyer sets it up (a deadline and/or line targets); until then nothing is
negotiated (`not_started`). `ready_to_close` means every line already has an
eligible offer within target and every invited supplier answered, so the buyer
may close early; `closed` is final (by the buyer, or at the deadline) and
`negotiation_outcome` keeps the per-line result at that moment.
`negotiation_max_rounds` caps the counter/BAFO emails per supplier.

`rfq_lines.target_unit_price` / `max_unit_price` / `max_lead_time_days`: the
buyer's limits per line, in the RFQ's base currency. Internal: suppliers are
told a target, never the maximum.

`invites.negotiation_status`: where one supplier stands (open, countered,
bafo_requested, accepted, rejected, closed). `negotiation_evaluated_proposal_id`
is the offer (proposal version) last evaluated, so each new version is
evaluated once.

`negotiation_messages`: one row per counter-offer or best-and-final request
Bido proposes. Like follow-ups, every one is a PENDING approval first and is
sent by the worker only after another person approves. `terms` holds the
numbers asked for (ids and prices only); the approved text lives in the
approval's args and, once sent, in `messages`.

Every new table has `tenant_id`, forced RLS and the tenant policy.

This branch was cut from 0012 in parallel with other migrations (0014-0016).
When it merges after them, `down_revision` moves to the head at that time.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"


def upgrade() -> None:
    op.add_column(
        "rfqs",
        sa.Column(
            "negotiation_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'not_started'"),
        ),
    )
    op.add_column("rfqs", sa.Column("negotiation_deadline", sa.DateTime(timezone=True)))
    op.add_column(
        "rfqs",
        sa.Column("negotiation_max_rounds", sa.Integer(), nullable=False, server_default="3"),
    )
    op.add_column(
        "rfqs",
        sa.Column(
            "negotiation_early_close", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )
    op.add_column("rfqs", sa.Column("negotiation_ready_at", sa.DateTime(timezone=True)))
    op.add_column("rfqs", sa.Column("negotiation_closed_at", sa.DateTime(timezone=True)))
    op.add_column("rfqs", sa.Column("negotiation_outcome", postgresql.JSONB()))
    op.create_check_constraint(
        "rfqs_negotiation_status_check",
        "rfqs",
        "negotiation_status IN ('not_started', 'open', 'ready_to_close', 'closed')",
    )
    op.create_check_constraint(
        "rfqs_negotiation_max_rounds_check", "rfqs", "negotiation_max_rounds BETWEEN 1 AND 10"
    )
    op.create_check_constraint(
        "rfqs_negotiation_closed_check",
        "rfqs",
        "(negotiation_status = 'closed') = (negotiation_closed_at IS NOT NULL)",
    )

    op.add_column("rfq_lines", sa.Column("target_unit_price", sa.Numeric(18, 6)))
    op.add_column("rfq_lines", sa.Column("max_unit_price", sa.Numeric(18, 6)))
    op.add_column("rfq_lines", sa.Column("max_lead_time_days", sa.Integer()))
    op.create_check_constraint(
        "rfq_lines_negotiation_limits_check",
        "rfq_lines",
        "(target_unit_price IS NULL OR target_unit_price > 0)"
        " AND (max_unit_price IS NULL OR max_unit_price > 0)"
        " AND (target_unit_price IS NULL OR max_unit_price IS NULL"
        " OR target_unit_price <= max_unit_price)"
        " AND (max_lead_time_days IS NULL OR max_lead_time_days BETWEEN 0 AND 3650)",
    )

    op.add_column(
        "invites",
        sa.Column(
            "negotiation_status", sa.Text(), nullable=False, server_default=sa.text("'open'")
        ),
    )
    op.add_column(
        "invites",
        sa.Column(
            "negotiation_evaluated_proposal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("proposals.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "invites_negotiation_status_check",
        "invites",
        "negotiation_status IN"
        " ('open', 'countered', 'bafo_requested', 'accepted', 'rejected', 'closed')",
    )

    op.create_table(
        "negotiation_messages",
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
        sa.Column(
            "proposal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("proposals.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'proposed'")),
        sa.Column(
            "approval_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("approvals.id"),
            nullable=True,
        ),
        sa.Column("terms", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),
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
            "tenant_id", "invite_id", "round", name="negotiation_messages_tenant_invite_round_key"
        ),
        sa.CheckConstraint(
            "action IN ('counter', 'bafo')", name="negotiation_messages_action_check"
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'sent', 'canceled', 'rejected')",
            name="negotiation_messages_status_check",
        ),
        sa.CheckConstraint("round >= 1", name="negotiation_messages_round_check"),
        sa.CheckConstraint(
            "(status = 'sent') = (sent_at IS NOT NULL)", name="negotiation_messages_sent_check"
        ),
    )
    op.create_index(
        "negotiation_messages_tenant_id_invite_id_idx",
        "negotiation_messages",
        ["tenant_id", "invite_id"],
    )
    op.execute("ALTER TABLE negotiation_messages ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE negotiation_messages FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY tenant_isolation ON negotiation_messages "
        f"USING (tenant_id = {CURRENT_TENANT}) "
        f"WITH CHECK (tenant_id = {CURRENT_TENANT})"
    )


def downgrade() -> None:
    op.drop_table("negotiation_messages")
    op.drop_constraint("invites_negotiation_status_check", "invites")
    op.drop_column("invites", "negotiation_evaluated_proposal_id")
    op.drop_column("invites", "negotiation_status")
    op.drop_constraint("rfq_lines_negotiation_limits_check", "rfq_lines")
    op.drop_column("rfq_lines", "max_lead_time_days")
    op.drop_column("rfq_lines", "max_unit_price")
    op.drop_column("rfq_lines", "target_unit_price")
    op.drop_constraint("rfqs_negotiation_closed_check", "rfqs")
    op.drop_constraint("rfqs_negotiation_max_rounds_check", "rfqs")
    op.drop_constraint("rfqs_negotiation_status_check", "rfqs")
    for column in (
        "negotiation_outcome",
        "negotiation_closed_at",
        "negotiation_ready_at",
        "negotiation_early_close",
        "negotiation_max_rounds",
        "negotiation_deadline",
        "negotiation_status",
    ):
        op.drop_column("rfqs", column)
