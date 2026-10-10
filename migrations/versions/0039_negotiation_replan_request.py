"""A supplier reply asks for the negotiation to be planned again now.

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-09

Until now a reply moved the negotiation only at the next `negotiation.sweep`
(every 15 minutes). Quote extraction now marks the RFQ in the same transaction
that stores the reply, and enqueues one `negotiation.replan` job for it
(`bido_back.negotiation.jobs`):

* `rfqs.negotiation_replan_requested_at`: when a supplier reply last asked for a
  re-plan that has not run yet; NULL when none is pending. A burst of replies
  bumps it but enqueues one job (only the reply that finds it NULL enqueues). The
  job, and the sweep as the safety net, clear it after planning, unless a newer
  reply bumped it meanwhile (then the job enqueues itself once more).

Nullable, no default: existing rows read "nothing pending" and older code never
reads it. `rfqs` already has a non-null `tenant_id`, forced RLS and the tenant
policy: no new table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str | Sequence[str] | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rfqs",
        sa.Column("negotiation_replan_requested_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("rfqs", "negotiation_replan_requested_at")
