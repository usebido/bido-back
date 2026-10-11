"""Release a funded escrow milestone by milestone: Tool start, T1, PPAP.

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-11

The factory sends each milestone's documents through a link with no login; a
person at the buyer approves the release (`release_po_milestone`); the worker
writes the gate's attestation and releases that milestone on chain, once.

* `po_payment_milestones`: one row per milestone of a payment's escrow (index =
  gate), with the on-chain terms (name, bps, amount in base units) and where its
  release stands: `open` (nothing under review), `submitted` (its release waits
  for a person), `releasing` (approved; the worker releases it), `released`,
  `failed` (the release needs a person). Whether an open milestone may be sent
  yet (the escrow funded, every earlier milestone released) is derived.
* `po_milestone_submissions`: each attempt at a milestone: `draft` (the
  documents being gathered), `submitted`, `approved`, `released`, `rejected`
  (the buyer asked for changes, with a reason code and text) or `cancelled`. At most one draft
  and one live (submitted or approved) submission per milestone. Its
  `evidence_hash` is what the attestation carries on chain.
* `po_milestone_documents`: the files of a submission, by type, with their
  sha256 (the evidence hash is computed from them). A file removed from a draft
  keeps its row (`removed_at`).
* `po_factory_links`: the factory's link to one payment. Only the token's
  sha256 is stored; one active link per payment. `accepted_at`: the factory
  confirmed the order and its milestones, which it does before uploading.
* `payment_chain_transactions`: `release` transactions, one milestone each
  (`milestone_index`); attempts are counted per milestone.
* `po_payments.completed_at`: when the escrow released every milestone. The payment
  stays `funded` (the API's states are unchanged).

Every new table has `tenant_id`, forced RLS and the tenant policy. The app role
may not DELETE or TRUNCATE any of them: they are the payment's record.

Two SECURITY DEFINER functions return tenant ids only, past FORCEd RLS (the
pattern of 0030 and 0040): `factory_link_tenant(token_hash)`, the tenant of an
active, unexpired link (the factory has no session), and
`milestone_retry_tenants(min_age, max_age)`, tenants with a release stuck in
`releasing`, for the scheduled sweep.
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0043"
down_revision: str | Sequence[str] | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "bido_app"
CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
LINK_LOOKUP_FLAG = "(select current_setting('app.factory_link_lookup', true) = 'on')"
RETRY_LOOKUP_FLAG = "(select current_setting('app.milestone_lookup', true) = 'on')"
LINK_FUNCTION = "factory_link_tenant(text)"
RETRY_FUNCTION = "milestone_retry_tenants(interval, interval)"
TABLES = (
    "po_factory_links",
    "po_payment_milestones",
    "po_milestone_submissions",
    "po_milestone_documents",
)
HEX64 = "'^[0-9a-f]{64}$'"
BASE58 = "'^[1-9A-HJ-NP-Za-km-z]{32,88}$'"
CODE = "'^[a-z0-9_]{1,64}$'"


def _id() -> sa.Column[UUID]:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def _tenant() -> sa.Column[UUID]:
    return sa.Column(
        "tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False
    )


def _fk(name: str, target: str, *, nullable: bool = False) -> sa.Column[UUID]:
    return sa.Column(name, postgresql.UUID(as_uuid=True), sa.ForeignKey(target), nullable=nullable)


def _timestamp(name: str, *, now: bool = False) -> sa.Column[datetime]:
    if now:
        return sa.Column(
            name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        )
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    op.create_table(
        "po_factory_links",
        _id(),
        _tenant(),
        _fk("payment_id", "po_payments.id"),
        sa.Column("token_hash", sa.Text(), nullable=False),
        _fk("created_by", "users.id"),
        _timestamp("created_at", now=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        _timestamp("revoked_at"),
        _fk("revoked_by", "users.id", nullable=True),
        # The factory confirmed the order and its milestones (before its first upload).
        _timestamp("accepted_at"),
        _timestamp("last_used_at"),
        sa.UniqueConstraint("token_hash", name="po_factory_links_token_hash_key"),
        sa.CheckConstraint(f"token_hash ~ {HEX64}", name="po_factory_links_token_hash_check"),
        sa.CheckConstraint("expires_at > created_at", name="po_factory_links_expiry_check"),
        sa.CheckConstraint(
            "revoked_by IS NULL OR revoked_at IS NOT NULL",
            name="po_factory_links_revoked_check",
        ),
    )
    op.create_index("po_factory_links_payment_id_idx", "po_factory_links", ["payment_id"])
    op.create_index(
        "po_factory_links_one_active_per_payment_key",
        "po_factory_links",
        ["payment_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "po_payment_milestones",
        _id(),
        _tenant(),
        _fk("payment_id", "po_payments.id"),
        sa.Column("milestone_index", sa.SmallInteger(), nullable=False),
        sa.Column("key", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("bps", sa.Integer(), nullable=False),
        sa.Column("token_amount", sa.Numeric(20, 0), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="open"),
        # The current `release_po_milestone` approval. No FK on purpose (see 0040).
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("submission_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("evidence_hash", sa.Text(), nullable=True),
        sa.Column("release_signature", sa.Text(), nullable=True),
        _timestamp("released_at"),
        sa.Column("failure_code", sa.Text(), nullable=True),
        _timestamp("created_at", now=True),
        _timestamp("updated_at", now=True),
        sa.UniqueConstraint(
            "payment_id", "milestone_index", name="po_payment_milestones_payment_index_key"
        ),
        sa.CheckConstraint(
            "milestone_index BETWEEN 0 AND 3", name="po_payment_milestones_index_check"
        ),
        sa.CheckConstraint(
            "key IS NULL OR key ~ '^[a-z][a-z0-9_]{0,31}$'", name="po_payment_milestones_key_check"
        ),
        sa.CheckConstraint(
            "octet_length(name) BETWEEN 1 AND 24", name="po_payment_milestones_name_check"
        ),
        sa.CheckConstraint("bps BETWEEN 1 AND 10000", name="po_payment_milestones_bps_check"),
        sa.CheckConstraint(
            "token_amount > 0 AND token_amount <= 18446744073709551615",
            name="po_payment_milestones_token_amount_check",
        ),
        sa.CheckConstraint(
            "state IN ('open', 'submitted', 'releasing', 'released', 'failed')",
            name="po_payment_milestones_state_check",
        ),
        sa.CheckConstraint(
            "state NOT IN ('submitted', 'releasing', 'released') "
            "OR (approval_id IS NOT NULL AND submission_id IS NOT NULL "
            "AND evidence_hash IS NOT NULL)",
            name="po_payment_milestones_reviewed_check",
        ),
        sa.CheckConstraint(
            "state <> 'released' OR released_at IS NOT NULL",
            name="po_payment_milestones_released_check",
        ),
        sa.CheckConstraint(
            f"evidence_hash IS NULL OR evidence_hash ~ {HEX64}",
            name="po_payment_milestones_evidence_hash_check",
        ),
        sa.CheckConstraint(
            f"release_signature IS NULL OR release_signature ~ {BASE58}",
            name="po_payment_milestones_release_signature_check",
        ),
        sa.CheckConstraint(
            f"failure_code IS NULL OR failure_code ~ {CODE}",
            name="po_payment_milestones_failure_code_check",
        ),
    )

    op.create_table(
        "po_milestone_submissions",
        _id(),
        _tenant(),
        _fk("payment_id", "po_payments.id"),
        _fk("milestone_id", "po_payment_milestones.id"),
        sa.Column("milestone_index", sa.SmallInteger(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="draft"),
        # Who gathered it: the factory through its link, or a buyer on its behalf.
        sa.Column("source", sa.Text(), nullable=False),
        _fk("factory_link_id", "po_factory_links.id", nullable=True),
        _fk("submitted_by", "users.id", nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("evidence_hash", sa.Text(), nullable=True),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("rejection_code", sa.Text(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("failure_code", sa.Text(), nullable=True),
        _fk("decided_by", "users.id", nullable=True),
        _timestamp("decided_at"),
        _timestamp("submitted_at"),
        _timestamp("created_at", now=True),
        _timestamp("updated_at", now=True),
        sa.UniqueConstraint(
            "milestone_id", "attempt", name="po_milestone_submissions_milestone_attempt_key"
        ),
        sa.CheckConstraint("attempt >= 1", name="po_milestone_submissions_attempt_check"),
        sa.CheckConstraint(
            "milestone_index BETWEEN 0 AND 3", name="po_milestone_submissions_index_check"
        ),
        sa.CheckConstraint(
            "state IN ('draft', 'submitted', 'approved', 'released', 'rejected', 'cancelled')",
            name="po_milestone_submissions_state_check",
        ),
        sa.CheckConstraint(
            "source IN ('factory', 'buyer')", name="po_milestone_submissions_source_check"
        ),
        sa.CheckConstraint(
            "state IN ('draft', 'cancelled') "
            "OR (evidence_hash IS NOT NULL AND approval_id IS NOT NULL "
            "AND submitted_at IS NOT NULL)",
            name="po_milestone_submissions_submitted_check",
        ),
        sa.CheckConstraint(
            f"evidence_hash IS NULL OR evidence_hash ~ {HEX64}",
            name="po_milestone_submissions_evidence_hash_check",
        ),
        sa.CheckConstraint(
            "note IS NULL OR length(note) <= 2000", name="po_milestone_submissions_note_check"
        ),
        sa.CheckConstraint(
            "rejection_reason IS NULL OR length(rejection_reason) <= 2000",
            name="po_milestone_submissions_rejection_reason_check",
        ),
        sa.CheckConstraint(
            f"rejection_code IS NULL OR rejection_code ~ {CODE}",
            name="po_milestone_submissions_rejection_code_check",
        ),
        sa.CheckConstraint(
            f"failure_code IS NULL OR failure_code ~ {CODE}",
            name="po_milestone_submissions_failure_code_check",
        ),
    )
    op.create_index(
        "po_milestone_submissions_milestone_id_idx", "po_milestone_submissions", ["milestone_id"]
    )
    op.create_index(
        "po_milestone_submissions_one_draft_key",
        "po_milestone_submissions",
        ["milestone_id"],
        unique=True,
        postgresql_where=sa.text("state = 'draft'"),
    )
    op.create_index(
        "po_milestone_submissions_one_live_key",
        "po_milestone_submissions",
        ["milestone_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('submitted', 'approved')"),
    )
    op.create_foreign_key(
        "po_payment_milestones_submission_id_fkey",
        "po_payment_milestones",
        "po_milestone_submissions",
        ["submission_id"],
        ["id"],
    )

    op.create_table(
        "po_milestone_documents",
        _id(),
        _tenant(),
        _fk("payment_id", "po_payments.id"),
        _fk("milestone_id", "po_payment_milestones.id"),
        _fk("submission_id", "po_milestone_submissions.id"),
        sa.Column("doc_type", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        _fk("factory_link_id", "po_factory_links.id", nullable=True),
        _fk("uploaded_by", "users.id", nullable=True),
        _timestamp("removed_at"),
        _timestamp("created_at", now=True),
        sa.CheckConstraint(
            "doc_type ~ '^[a-z][a-z0-9_]{0,47}$'", name="po_milestone_documents_doc_type_check"
        ),
        sa.CheckConstraint(
            "length(filename) BETWEEN 1 AND 255", name="po_milestone_documents_filename_check"
        ),
        sa.CheckConstraint(
            "length(content_type) BETWEEN 1 AND 255",
            name="po_milestone_documents_content_type_check",
        ),
        sa.CheckConstraint("size_bytes > 0", name="po_milestone_documents_size_check"),
        sa.CheckConstraint(f"sha256 ~ {HEX64}", name="po_milestone_documents_sha256_check"),
        sa.CheckConstraint(
            "source IN ('factory', 'buyer')", name="po_milestone_documents_source_check"
        ),
        sa.CheckConstraint(
            "(source = 'factory' AND factory_link_id IS NOT NULL) "
            "OR (source = 'buyer' AND uploaded_by IS NOT NULL)",
            name="po_milestone_documents_uploader_check",
        ),
    )
    op.create_index(
        "po_milestone_documents_submission_id_idx", "po_milestone_documents", ["submission_id"]
    )
    op.create_index(
        "po_milestone_documents_one_file_per_submission_key",
        "po_milestone_documents",
        ["submission_id", "sha256"],
        unique=True,
        postgresql_where=sa.text("removed_at IS NULL"),
    )

    # Release transactions: one milestone each, attempts counted per milestone.
    op.add_column(
        "payment_chain_transactions",
        sa.Column("milestone_index", sa.SmallInteger(), nullable=True),
    )
    op.drop_constraint(
        "payment_chain_transactions_step_check", "payment_chain_transactions", type_="check"
    )
    op.create_check_constraint(
        "payment_chain_transactions_step_check",
        "payment_chain_transactions",
        "step IN ('mint', 'create_escrow', 'fund', 'release')",
    )
    op.create_check_constraint(
        "payment_chain_transactions_milestone_check",
        "payment_chain_transactions",
        "(step = 'release') = (milestone_index IS NOT NULL) "
        "AND (milestone_index IS NULL OR milestone_index BETWEEN 0 AND 3)",
    )
    op.drop_constraint(
        "payment_chain_transactions_attempt_key", "payment_chain_transactions", type_="unique"
    )
    op.execute(
        "CREATE UNIQUE INDEX payment_chain_transactions_attempt_key ON payment_chain_transactions "
        "(payment_id, step, COALESCE(milestone_index, -1), attempt)"
    )

    # When the escrow released every milestone; the payment stays `funded`.
    op.add_column(
        "po_payments", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "po_payments_completed_check",
        "po_payments",
        "completed_at IS NULL OR state = 'funded'",
    )

    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (tenant_id = {CURRENT_TENANT}) "
            f"WITH CHECK (tenant_id = {CURRENT_TENANT})"
        )
        # A payment's record is never deleted by the app.
        op.execute(f"REVOKE DELETE, TRUNCATE ON {table} FROM {APP_ROLE}")

    # The factory has no session: the tenant of its link, tenant id only (0030, 0040).
    op.execute(
        "CREATE POLICY factory_link_lookup ON po_factory_links FOR SELECT "
        f"TO CURRENT_USER USING ({LINK_LOOKUP_FLAG})"
    )
    op.execute(
        """
        CREATE FUNCTION factory_link_tenant(token_hash text)
        RETURNS uuid
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        DECLARE
            found uuid;
        BEGIN
            PERFORM set_config('app.factory_link_lookup', 'on', true);
            SELECT l.tenant_id INTO found
                FROM public.po_factory_links AS l
                WHERE l.token_hash = factory_link_tenant.token_hash
                  AND l.revoked_at IS NULL
                  AND l.expires_at > now();
            PERFORM set_config('app.factory_link_lookup', 'off', true);
            RETURN found;
        END
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {LINK_FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {LINK_FUNCTION} TO {APP_ROLE}")

    # The scheduled sweep has no tenant either: tenants with a release stuck in `releasing`.
    op.execute(
        "CREATE POLICY milestone_retry_lookup ON po_payment_milestones FOR SELECT "
        f"TO CURRENT_USER USING ({RETRY_LOOKUP_FLAG})"
    )
    op.execute(
        """
        CREATE FUNCTION milestone_retry_tenants(min_age interval, max_age interval)
        RETURNS TABLE (tenant_id uuid)
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        BEGIN
            PERFORM set_config('app.milestone_lookup', 'on', true);
            RETURN QUERY
                SELECT DISTINCT m.tenant_id
                FROM public.po_payment_milestones AS m
                WHERE m.state = 'releasing'
                  AND m.updated_at < now() - min_age
                  AND m.updated_at > now() - max_age
                ORDER BY m.tenant_id;
            PERFORM set_config('app.milestone_lookup', 'off', true);
        END
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {RETRY_FUNCTION} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {RETRY_FUNCTION} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {RETRY_FUNCTION}")
    op.execute("DROP POLICY milestone_retry_lookup ON po_payment_milestones")
    op.execute(f"DROP FUNCTION {LINK_FUNCTION}")
    op.execute("DROP POLICY factory_link_lookup ON po_factory_links")

    op.drop_constraint("po_payments_completed_check", "po_payments", type_="check")
    op.drop_column("po_payments", "completed_at")

    op.execute("DROP INDEX payment_chain_transactions_attempt_key")
    op.create_unique_constraint(
        "payment_chain_transactions_attempt_key",
        "payment_chain_transactions",
        ["payment_id", "step", "attempt"],
    )
    op.drop_constraint(
        "payment_chain_transactions_milestone_check", "payment_chain_transactions", type_="check"
    )
    op.drop_constraint(
        "payment_chain_transactions_step_check", "payment_chain_transactions", type_="check"
    )
    op.create_check_constraint(
        "payment_chain_transactions_step_check",
        "payment_chain_transactions",
        "step IN ('mint', 'create_escrow', 'fund')",
    )
    op.drop_column("payment_chain_transactions", "milestone_index")

    op.drop_table("po_milestone_documents")
    op.drop_constraint(
        "po_payment_milestones_submission_id_fkey", "po_payment_milestones", type_="foreignkey"
    )
    op.drop_table("po_milestone_submissions")
    op.drop_table("po_payment_milestones")
    op.drop_table("po_factory_links")
