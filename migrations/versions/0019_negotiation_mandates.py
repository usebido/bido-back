"""Negotiation mandates: one approval, then automatic sends within its bounds.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-07

`negotiation_mandates`: what a person approved once, for one RFQ, by
approving the `approve_negotiation_mandate` act: the suppliers and the
addresses Bido may write to, per-line limits (target, maximum, lead time),
the deadline, the round cap, a per-supplier daily message cap and the exact
initial RFQ emails (by approval id and args hash). `args_hash` is the hash of
the approved payload; `approval_id` is that approval. While a mandate is
`active`, the worker approves the RFQ's own follow-up, negotiation and initial
emails on the buyer's behalf, but only after code has checked each one is
inside the mandate (`bido_back.mandates`). `paused` stops that immediately;
`ended` is final (closed negotiation, or a new mandate replaced it).

`mandate_sends`: one row per approval the mandate decided on: `auto_approved`
(sent through the normal executor, once) or `held` (out of bounds; left
pending for a person, with the reason). Ids and reasons only.

Both tables have `tenant_id`, forced RLS and the tenant policy.

Follows 0018 (Clerk organizations). Automatic approvals follow the same
self-approval rule as people: when the mandate's approver is the requesting
buyer (tenants that allow self-approval, the default), they are recorded as
self-approved.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (tenant_id = {CURRENT_TENANT}) "
        f"WITH CHECK (tenant_id = {CURRENT_TENANT})"
    )


def upgrade() -> None:
    op.create_table(
        "negotiation_mandates",
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
            "rfq_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("rfqs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # No foreign key: the mandate is written while the executor holds a lock on
        # its approval row (see migration 0015 for the same reason on awards).
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("args_hash", sa.Text(), nullable=False),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sender_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("terms", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "approval_id", name="negotiation_mandates_tenant_approval_key"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'ended')", name="negotiation_mandates_status_check"
        ),
        sa.CheckConstraint(
            "args_hash ~ '^[0-9a-f]{64}$'", name="negotiation_mandates_args_hash_check"
        ),
        sa.CheckConstraint(
            "(status = 'ended') = (ended_at IS NOT NULL)", name="negotiation_mandates_ended_check"
        ),
    )
    # At most one live (active or paused) mandate per RFQ.
    op.create_index(
        "negotiation_mandates_live_rfq_key",
        "negotiation_mandates",
        ["tenant_id", "rfq_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'ended'"),
    )
    _rls("negotiation_mandates")

    op.create_table(
        "mandate_sends",
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
            "mandate_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("negotiation_mandates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("invite_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("tenant_id", "approval_id", name="mandate_sends_tenant_approval_key"),
        sa.CheckConstraint(
            "outcome IN ('auto_approved', 'held')", name="mandate_sends_outcome_check"
        ),
        sa.CheckConstraint(
            "(outcome = 'held') = (reason IS NOT NULL)", name="mandate_sends_reason_check"
        ),
    )
    op.create_index(
        "mandate_sends_tenant_mandate_created_idx",
        "mandate_sends",
        ["tenant_id", "mandate_id", "created_at"],
    )
    _rls("mandate_sends")


def downgrade() -> None:
    op.drop_table("mandate_sends")
    op.drop_table("negotiation_mandates")
