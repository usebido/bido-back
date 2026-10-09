"""The clarification ledger: what Bido asked each supplier, and what each supplier asked.

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-09

`clarification_asks`: one row per thing a clarification email asks one supplier
(invite): a requested line (`rfq_line_id`, `position`) or the whole offer (both
NULL), a `field` (currency, valid_until, unit_price, quantity, lead_time,
delivery_date), the `question` kind (value, confirm, deliver_by,
price_or_decline) and, for delivery questions, the line's `need_by` date. The
`followup_id` is the clarification email that carries it. Its `status`:

* `proposed`: the email awaits approval (nothing asked yet);
* `open`: the email went out at `asked_at`; the supplier is asked to answer by
  `reply_by`;
* `answered` / `unusable`: the supplier's next reply with an offer
  (`answered_by_message_id`, at `settled_at`) gave a usable value, or did not.
  An unusable answer is never asked again: it needs the buyer's decision;
* `withdrawn`: the email was canceled or rejected before it went out, so it was
  never asked and may be asked again;
* `resolved`: the buyer decided (`resolved_by`, `resolved_at`).

A unique index keeps one live row per (invite, line, field): Bido asks each
item at most once, automatically.

`supplier_questions`: the questions a supplier's reply asked the buyer
(`message_id`, in order), `open` until the buyer answers (the answer email is
`answer_followup_id`, always held for a person's approval) or dismisses it.
While a supplier has an open question, no automatic email goes to it.

`followup_messages`: two more kinds, `reminder` (once per email we sent and the
supplier left unanswered past its reply-by date: `regarding_message_id` is our
outbound `messages` row) and `answer` (the buyer's answer to a supplier
question); `reply_by`, the date the email asks for an answer by. Clarifications
are now numbered per invite: a canceled one (never sent) no longer blocks a new
one for the same offer version.

The question and answer texts are the supplier's and the buyer's words: like
`messages.body_text`, they are customer content and never logged.

Both new tables have a non-null `tenant_id`, forced RLS and the tenant policy.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0036"
down_revision: str | Sequence[str] | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
NO_LINE = "'00000000-0000-0000-0000-000000000000'::uuid"


def _rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (tenant_id = {CURRENT_TENANT}) "
        f"WITH CHECK (tenant_id = {CURRENT_TENANT})"
    )


def _reference(name: str, target: str, *, ondelete: str | None = None) -> sa.Column[object]:
    """A nullable foreign key column."""
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(target, ondelete=ondelete),
        nullable=True,
    )


def upgrade() -> None:
    # --- followup_messages: reminders, answers, reply-by dates ---------------------------
    op.drop_constraint("followup_messages_kind_check", "followup_messages", type_="check")
    op.create_check_constraint(
        "followup_messages_kind_check",
        "followup_messages",
        "kind IN ('chase', 'clarification', 'reminder', 'answer')",
    )
    op.add_column("followup_messages", sa.Column("reply_by", sa.Date(), nullable=True))
    op.add_column(
        "followup_messages",
        _reference("regarding_message_id", "messages.id", ondelete="SET NULL"),
    )
    op.drop_index("followup_messages_proposal_id_key", table_name="followup_messages")
    op.create_index(
        "followup_messages_proposal_id_key",
        "followup_messages",
        ["proposal_id"],
        unique=True,
        postgresql_where=sa.text("proposal_id IS NOT NULL AND status <> 'canceled'"),
    )
    op.create_index(
        "followup_messages_reminder_key",
        "followup_messages",
        ["regarding_message_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'reminder' AND status <> 'canceled'"),
    )

    # --- clarification_asks -----------------------------------------------------------------
    op.create_table(
        "clarification_asks",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
        ),
        sa.Column(
            "invite_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invites.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "followup_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("followup_messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        _reference("rfq_line_id", "rfq_lines.id", ondelete="CASCADE"),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("need_by", sa.Date(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'proposed'")),
        sa.Column("asked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reply_by", sa.Date(), nullable=True),
        _reference("answered_by_message_id", "messages.id", ondelete="SET NULL"),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        _reference("resolved_by", "users.id"),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "field IN ('currency', 'valid_until', 'unit_price', 'quantity', 'lead_time',"
            " 'delivery_date')",
            name="clarification_asks_field_check",
        ),
        sa.CheckConstraint(
            "question IN ('value', 'confirm', 'deliver_by', 'price_or_decline')",
            name="clarification_asks_question_check",
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'open', 'answered', 'unusable', 'withdrawn', 'resolved')",
            name="clarification_asks_status_check",
        ),
        sa.CheckConstraint(
            "(rfq_line_id IS NULL) = (position IS NULL)", name="clarification_asks_line_check"
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'withdrawn') OR asked_at IS NOT NULL",
            name="clarification_asks_asked_check",
        ),
        sa.CheckConstraint(
            "status <> 'resolved' OR (resolved_by IS NOT NULL AND resolved_at IS NOT NULL)",
            name="clarification_asks_resolved_check",
        ),
    )
    # Each item is asked at most once: one live row per (invite, line or offer, field).
    op.execute(
        "CREATE UNIQUE INDEX clarification_asks_item_key ON clarification_asks "
        f"(tenant_id, invite_id, coalesce(rfq_line_id, {NO_LINE}), field) "
        "WHERE status <> 'withdrawn'"
    )
    op.create_index(
        "clarification_asks_tenant_invite_idx", "clarification_asks", ["tenant_id", "invite_id"]
    )
    op.create_index("clarification_asks_followup_idx", "clarification_asks", ["followup_id"])
    _rls("clarification_asks")

    # --- supplier_questions -----------------------------------------------------------------
    op.create_table(
        "supplier_questions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
        ),
        sa.Column(
            "invite_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invites.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.Column("answer", sa.Text(), nullable=True),
        _reference("answer_followup_id", "followup_messages.id", ondelete="SET NULL"),
        _reference("decided_by", "users.id"),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "tenant_id", "message_id", "ordinal", name="supplier_questions_message_ordinal_key"
        ),
        sa.CheckConstraint(
            "status IN ('open', 'answered', 'dismissed')", name="supplier_questions_status_check"
        ),
        sa.CheckConstraint("ordinal >= 1", name="supplier_questions_ordinal_check"),
        sa.CheckConstraint(
            "(status = 'open') = (decided_by IS NULL AND decided_at IS NULL)",
            name="supplier_questions_decided_check",
        ),
        sa.CheckConstraint(
            "status <> 'answered' OR answer IS NOT NULL", name="supplier_questions_answer_check"
        ),
    )
    op.create_index(
        "supplier_questions_tenant_invite_idx", "supplier_questions", ["tenant_id", "invite_id"]
    )
    _rls("supplier_questions")


def downgrade() -> None:
    op.drop_table("supplier_questions")
    op.drop_table("clarification_asks")
    op.drop_index("followup_messages_reminder_key", table_name="followup_messages")
    op.drop_index("followup_messages_proposal_id_key", table_name="followup_messages")
    # Forced RLS hides every row without a tenant, even from the owner: lift it to delete.
    op.execute("ALTER TABLE followup_messages NO FORCE ROW LEVEL SECURITY")
    op.execute("DELETE FROM followup_messages WHERE kind IN ('reminder', 'answer')")
    op.execute("ALTER TABLE followup_messages FORCE ROW LEVEL SECURITY")
    op.create_index(
        "followup_messages_proposal_id_key",
        "followup_messages",
        ["proposal_id"],
        unique=True,
        postgresql_where=sa.text("proposal_id IS NOT NULL"),
    )
    op.drop_column("followup_messages", "regarding_message_id")
    op.drop_column("followup_messages", "reply_by")
    op.drop_constraint("followup_messages_kind_check", "followup_messages", type_="check")
    op.create_check_constraint(
        "followup_messages_kind_check",
        "followup_messages",
        "kind IN ('chase', 'clarification')",
    )
