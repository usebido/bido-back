"""Award and purchase orders: the end of the RFQ journey.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-07

Parallel branches (negotiation 0013, purchase analysis 0014, supplier
simulator 0016) all start from 0012. Whichever merges later moves its
`down_revision` onto the head that exists then.

Awarding is an `act`: the buyer asks (`POST /v1/rfqs/{id}/award`), an approver
who is not the requester approves, and the worker executes it once
(`award_rfq`). Only then are these rows written:

`awards`: one per RFQ (unique `rfq_id`). `lines` is the approved selection
(per RFQ line: the tier, the invite and the exact proposal line and price),
copied from the approval's hashed args. `differs_from_recommendation` and
`reason` record why the buyer did not take the recommended offer.

`issued_purchase_orders`: one per awarded supplier, numbered per tenant (`number`,
`ref` = `PO-<number>`). `status`: `draft` until a send is requested,
`pending_approval` while it waits, `sent` once the worker sent it in the
invite's thread (`message_id_header` is claimed before sending and reused on
a retry). A rejected send goes back to `draft`.

`issued_purchase_order_lines`: the ordered quantity and price per line, with
references to the RFQ line and the proposal line it came from.

`award_notices`: the polite "not selected" email to a supplier that did not
win, one per invite, proposed for approval and sent in the invite's thread.
No email content is stored here: the approved text lives in the approval's
args and, once sent, in `messages`.

Every new table has `tenant_id`, forced RLS and the tenant policy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
TABLES = ("awards", "issued_purchase_orders", "issued_purchase_order_lines", "award_notices")


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


def _fk(
    name: str, target: str, *, nullable: bool = False, cascade: bool = False
) -> sa.Column[object]:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(target, ondelete="CASCADE" if cascade else None),
        nullable=nullable,
    )


def _created_at() -> sa.Column[object]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def upgrade() -> None:
    op.create_table(
        "awards",
        _uuid_pk(),
        _tenant(),
        _fk("rfq_id", "rfqs.id", cascade=True),
        # No foreign key: the award is written by the approval's own execution,
        # while the executor holds that approval row FOR UPDATE; the FK check's
        # KEY SHARE lock from this other transaction would wait on it forever.
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=True),
        _fk("requested_by", "users.id"),
        _fk("approved_by", "users.id"),
        sa.Column("differs_from_recommendation", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("lines", postgresql.JSONB(), nullable=False),
        _created_at(),
        sa.UniqueConstraint("rfq_id", name="awards_rfq_id_key"),
        sa.CheckConstraint(
            "NOT differs_from_recommendation OR reason IS NOT NULL",
            name="awards_reason_check",
        ),
    )

    op.create_table(
        "issued_purchase_orders",
        _uuid_pk(),
        _tenant(),
        _fk("award_id", "awards.id", cascade=True),
        _fk("rfq_id", "rfqs.id", cascade=True),
        _fk("invite_id", "invites.id", cascade=True),
        _fk("supplier_id", "suppliers.id"),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("incoterm", sa.Text(), nullable=True),
        sa.Column("payment_terms", sa.Text(), nullable=True),
        sa.Column("total", sa.Numeric(18, 2), nullable=False),
        sa.Column("message_id_header", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        _created_at(),
        sa.UniqueConstraint(
            "tenant_id", "number", name="issued_purchase_orders_tenant_id_number_key"
        ),
        sa.UniqueConstraint("tenant_id", "ref", name="issued_purchase_orders_tenant_id_ref_key"),
        sa.UniqueConstraint(
            "award_id", "invite_id", name="issued_purchase_orders_award_id_invite_id_key"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'pending_approval', 'sent')",
            name="issued_purchase_orders_status_check",
        ),
        sa.CheckConstraint("number > 0", name="issued_purchase_orders_number_check"),
        sa.CheckConstraint(
            "(status = 'sent') = (sent_at IS NOT NULL)", name="issued_purchase_orders_sent_check"
        ),
    )

    op.create_table(
        "issued_purchase_order_lines",
        _uuid_pk(),
        _tenant(),
        _fk("purchase_order_id", "issued_purchase_orders.id", cascade=True),
        sa.Column("position", sa.Integer(), nullable=False),
        _fk("rfq_line_id", "rfq_lines.id", nullable=True),
        _fk("proposal_line_id", "proposal_lines.id", nullable=True),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("mpn", sa.Text(), nullable=True),
        sa.Column("quantity", sa.Numeric(18, 4), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("unit_price", sa.Numeric(18, 6), nullable=False),
        sa.Column("line_total", sa.Numeric(18, 2), nullable=False),
        sa.Column("need_by", sa.Date(), nullable=True),
        sa.Column("lead_time_days", sa.Integer(), nullable=True),
        sa.UniqueConstraint(
            "purchase_order_id", "position", name="issued_purchase_order_lines_po_position_key"
        ),
        sa.CheckConstraint("quantity > 0", name="issued_purchase_order_lines_quantity_check"),
        sa.CheckConstraint("unit_price >= 0", name="issued_purchase_order_lines_price_check"),
    )

    op.create_table(
        "award_notices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _tenant(),
        _fk("award_id", "awards.id", cascade=True),
        _fk("invite_id", "invites.id", cascade=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'proposed'")),
        _fk("approval_id", "approvals.id", nullable=True),
        sa.Column("message_id_header", sa.Text(), nullable=True),
        _created_at(),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("invite_id", name="award_notices_invite_id_key"),
        sa.CheckConstraint(
            "status IN ('proposed', 'sent', 'rejected', 'canceled')",
            name="award_notices_status_check",
        ),
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
