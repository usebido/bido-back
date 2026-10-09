"""Negotiation stages: decisions persisted before writing, rounds apart from email sequence.

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-09

`negotiation_messages` becomes the record of each supplier email the negotiation
planner decides (`bido_back.negotiation.planner`), written BEFORE the email:

* `sequence`: the email's number for its invite (1, 2, ...), counting every email
  ever decided, canceled ones included. It is the row's identity (the row id and
  the approval are derived from it). Existing rows keep their old round number.
* `round`: from now on the negotiation round the email belongs to: the emails
  already SENT to that supplier, plus one. A canceled or rejected email never
  consumes a round. The old unique key on (invite, round) moves to (invite,
  sequence): a replacement email repeats the round of the one it replaces.
* `kind`: what the email asks (`revised_offer`, `delivery_date`, `bafo`);
  existing rows get `bafo` or `revised_offer` from their action.
* `attempts`, `held_reason`: a decided email the send gate refuses (or that
  cannot be addressed) is `held` with the reason: it waits for the buyer and the
  planner never re-decides that offer version.
* `status` gains `decided` (persisted, the email not yet proposed for approval)
  and `held`.

`rfqs.negotiation_cutoff_at`: the one best-and-final cut-off (date and time) for
every supplier of the RFQ, set by the first best-and-final request.

`invites.negotiation_decision`: the planner's last recorded decision for that
supplier (action, buyer-visible phase, reason code, offer version, when): codes
and ids only, never content.

`invites.negotiation_released_proposal_id`: the offer version whose holds the
buyer released ("negotiate this offer as it is", audited); a newer version is
held again if it needs to be.

`rfqs.negotiation_max_rounds` defaults to 2: one revised-offer round, then one
best-and-final request.

Rolling deploys: code from before this revision cannot insert negotiation rows
(it sets no `sequence`). Its sweep fails until the worker is replaced; at worst it
leaves a pending approval without a row, which the executor refuses if approved
(nothing is sent). Deploy the worker and the API together.

All on tables that already have forced RLS; no new table. The backfill of
existing rows (every tenant's) runs as the table owner with RLS briefly not
forced, inside this migration's transaction (the ALTER TABLE holds the table's
exclusive lock until commit, so no session sees the gap); forcing is restored
before the end.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0036"
down_revision: str | Sequence[str] | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES = "('decided', 'proposed', 'sent', 'canceled', 'rejected', 'held')"
_OLD_STATUSES = "('proposed', 'sent', 'canceled', 'rejected')"
_KINDS = "('revised_offer', 'delivery_date', 'bafo')"


def _backfill(*statements: str) -> None:
    """Rewrite every tenant's rows as the owner: RLS unforced only inside this transaction."""
    op.execute("ALTER TABLE negotiation_messages NO FORCE ROW LEVEL SECURITY")
    for statement in statements:
        op.execute(statement)
    op.execute("ALTER TABLE negotiation_messages FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    op.add_column("negotiation_messages", sa.Column("sequence", sa.Integer(), nullable=True))
    op.add_column("negotiation_messages", sa.Column("kind", sa.Text(), nullable=True))
    _backfill(
        "UPDATE negotiation_messages SET sequence = round, kind = CASE WHEN action = 'bafo' "
        "THEN 'bafo' ELSE 'revised_offer' END"
    )
    op.alter_column("negotiation_messages", "sequence", nullable=False)
    op.add_column(
        "negotiation_messages",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("negotiation_messages", sa.Column("held_reason", sa.Text(), nullable=True))
    op.drop_constraint(
        "negotiation_messages_tenant_invite_round_key", "negotiation_messages", type_="unique"
    )
    op.create_unique_constraint(
        "negotiation_messages_tenant_invite_sequence_key",
        "negotiation_messages",
        ["tenant_id", "invite_id", "sequence"],
    )
    op.drop_constraint("negotiation_messages_status_check", "negotiation_messages")
    op.create_check_constraint(
        "negotiation_messages_status_check", "negotiation_messages", f"status IN {_STATUSES}"
    )
    op.create_check_constraint(
        "negotiation_messages_kind_check",
        "negotiation_messages",
        f"kind IS NULL OR kind IN {_KINDS}",
    )
    op.create_check_constraint(
        "negotiation_messages_sequence_check", "negotiation_messages", "sequence >= 1"
    )
    op.create_check_constraint(
        "negotiation_messages_attempts_check", "negotiation_messages", "attempts >= 0"
    )

    op.add_column("rfqs", sa.Column("negotiation_cutoff_at", sa.DateTime(timezone=True)))
    op.alter_column("rfqs", "negotiation_max_rounds", server_default="2")
    op.add_column("invites", sa.Column("negotiation_decision", postgresql.JSONB()))
    op.add_column(
        "invites",
        sa.Column(
            "negotiation_released_proposal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("proposals.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("invites", "negotiation_released_proposal_id")
    op.drop_column("invites", "negotiation_decision")
    op.alter_column("rfqs", "negotiation_max_rounds", server_default="3")
    op.drop_column("rfqs", "negotiation_cutoff_at")
    op.drop_constraint("negotiation_messages_attempts_check", "negotiation_messages")
    op.drop_constraint("negotiation_messages_sequence_check", "negotiation_messages")
    op.drop_constraint("negotiation_messages_kind_check", "negotiation_messages")
    op.drop_constraint("negotiation_messages_status_check", "negotiation_messages")
    op.drop_constraint(
        "negotiation_messages_tenant_invite_sequence_key", "negotiation_messages", type_="unique"
    )
    # Rounds repeat across sequences after the upgrade: restore unique rounds first.
    _backfill(
        "UPDATE negotiation_messages SET status = 'canceled', canceled_reason = 'downgraded' "
        "WHERE status IN ('decided', 'held')",
        "UPDATE negotiation_messages SET round = sequence",
    )
    op.create_check_constraint(
        "negotiation_messages_status_check", "negotiation_messages", f"status IN {_OLD_STATUSES}"
    )
    op.create_unique_constraint(
        "negotiation_messages_tenant_invite_round_key",
        "negotiation_messages",
        ["tenant_id", "invite_id", "round"],
    )
    op.drop_column("negotiation_messages", "held_reason")
    op.drop_column("negotiation_messages", "attempts")
    op.drop_column("negotiation_messages", "kind")
    op.drop_column("negotiation_messages", "sequence")
