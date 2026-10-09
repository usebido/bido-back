"""Clarification asks carried by a negotiation email; supplier questions a later offer superseded.

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-09

One email per supplier answer: when the answer leaves open clarification items
AND something to negotiate (a price above target, a late delivery), the
negotiation planner decides ONE email that asks both (`bido_back.negotiation`).
The clarification ledger records what that email asked:

* `clarification_asks.negotiation_message_id`: the negotiation email that asked
  the item (`followup_id` stays the clarification email for the items a
  follow-up asked). Exactly one of the two is set. A negotiation email's items
  are recorded when it goes out (`open`, `asked_at`), so a canceled or
  rejected email never asked anything.

A supplier's question is closed without an answer when the supplier's later
offer makes it moot:

* `supplier_questions.status` gains `superseded_by_offer`: a later offer version
  from the same supplier, complete (every requested line priced or declined)
  and asking nothing, was received after the question. Never "answered": no
  answer was sent. `superseded_by_proposal_id` is that offer (its date is shown
  to the buyer, who may reopen the question). `decided_at` is when it was
  superseded; `decided_by` stays NULL (Bido decided, no person).

Both tables already have a non-null `tenant_id`, forced RLS and the tenant
policy (0036): no new table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0038"
down_revision: str | Sequence[str] | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES = "('open', 'answered', 'dismissed', 'superseded_by_offer')"
_OLD_STATUSES = "('open', 'answered', 'dismissed')"


def upgrade() -> None:
    # --- clarification_asks: the negotiation email that asked an item ---------------------
    op.alter_column("clarification_asks", "followup_id", nullable=True)
    op.add_column(
        "clarification_asks",
        sa.Column(
            "negotiation_message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("negotiation_messages.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "clarification_asks_email_check",
        "clarification_asks",
        "(followup_id IS NULL) <> (negotiation_message_id IS NULL)",
    )
    op.create_index(
        "clarification_asks_negotiation_message_idx",
        "clarification_asks",
        ["negotiation_message_id"],
        postgresql_where=sa.text("negotiation_message_id IS NOT NULL"),
    )

    # --- supplier_questions: superseded by a later offer ----------------------------------
    op.drop_constraint("supplier_questions_status_check", "supplier_questions", type_="check")
    op.create_check_constraint(
        "supplier_questions_status_check", "supplier_questions", f"status IN {_STATUSES}"
    )
    op.add_column(
        "supplier_questions",
        sa.Column(
            "superseded_by_proposal_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("proposals.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    # Forced RLS hides every row without a tenant, even from the owner: lift it to rewrite
    # every tenant's rows inside this transaction, then force it again.
    op.execute("ALTER TABLE supplier_questions NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE supplier_questions SET status = 'open', decided_by = NULL, decided_at = NULL "
        "WHERE status = 'superseded_by_offer'"
    )
    op.execute("ALTER TABLE supplier_questions FORCE ROW LEVEL SECURITY")
    op.drop_column("supplier_questions", "superseded_by_proposal_id")
    op.drop_constraint("supplier_questions_status_check", "supplier_questions", type_="check")
    op.create_check_constraint(
        "supplier_questions_status_check", "supplier_questions", f"status IN {_OLD_STATUSES}"
    )

    op.execute("ALTER TABLE clarification_asks NO FORCE ROW LEVEL SECURITY")
    op.execute("DELETE FROM clarification_asks WHERE negotiation_message_id IS NOT NULL")
    op.execute("ALTER TABLE clarification_asks FORCE ROW LEVEL SECURITY")
    op.drop_index("clarification_asks_negotiation_message_idx", table_name="clarification_asks")
    op.drop_constraint("clarification_asks_email_check", "clarification_asks", type_="check")
    op.drop_column("clarification_asks", "negotiation_message_id")
    op.alter_column("clarification_asks", "followup_id", nullable=False)
