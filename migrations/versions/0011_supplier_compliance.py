"""Supplier compliance: the tenant's requirement set and the documents on record.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-07

`compliance_requirements`: the documents a tenant expects from its suppliers
(RoHS, REACH SVHC, CMRT, AEC-Q reports, IATF 16949, ISO 9001, PPAP). A tenant
without rows uses Bido's default set (code, not data); the first edit stores
the whole set. `scope` says who a requirement applies to: every supplier,
manufacturers only, or one RFQ line whose qualification text mentions
`line_keyword` (an AEC-Q report is per part, so it is checked per line).
`keywords` are lower-case filename fragments used to spot candidate documents
among the attachments suppliers already sent; no model reads them.

`supplier_compliance_documents`: a document a person confirmed for one
requirement of one supplier (and, for line-scoped requirements, one RFQ line):
either an attachment Bido already stores or an https link. The latest row per
supplier, requirement and line is the one in force; `expires_on` decides
whether it is still valid. Requirements are referenced by code, not by id, so
editing the requirement set never deletes a supplier's documents.

Both tables have `tenant_id`, forced RLS and the tenant policy.

Merge note: branched from 0007 in parallel with other feature branches. When
those merge first, this revision's `down_revision` moves to the head at that
time; nothing here depends on their tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "(select NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
TABLES = ("compliance_requirements", "supplier_compliance_documents")


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


def upgrade() -> None:
    op.create_table(
        "compliance_requirements",
        _uuid_pk(),
        _tenant(),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("line_keyword", sa.Text(), nullable=True),
        sa.Column(
            "keywords", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("validity_months", sa.Integer(), nullable=True),
        sa.Column("blocking", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("tenant_id", "code", name="compliance_requirements_tenant_code_key"),
        sa.CheckConstraint(
            "scope IN ('supplier', 'manufacturer', 'line')",
            name="compliance_requirements_scope_check",
        ),
        sa.CheckConstraint(
            "(scope = 'line') = (line_keyword IS NOT NULL)",
            name="compliance_requirements_line_keyword_check",
        ),
        sa.CheckConstraint(
            "validity_months IS NULL OR validity_months BETWEEN 1 AND 120",
            name="compliance_requirements_validity_check",
        ),
    )

    op.create_table(
        "supplier_compliance_documents",
        _uuid_pk(),
        _tenant(),
        sa.Column(
            "supplier_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("suppliers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("requirement_code", sa.Text(), nullable=False),
        sa.Column(
            "rfq_line_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("rfq_lines.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "attachment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("attachments.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("issued_on", sa.Date(), nullable=True),
        sa.Column("expires_on", sa.Date(), nullable=True),
        sa.Column(
            "recorded_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "attachment_id IS NOT NULL OR url IS NOT NULL",
            name="supplier_compliance_documents_evidence_check",
        ),
        sa.CheckConstraint(
            "url IS NULL OR url ~ '^https://'",
            name="supplier_compliance_documents_url_check",
        ),
        sa.CheckConstraint(
            "expires_on IS NULL OR issued_on IS NULL OR expires_on >= issued_on",
            name="supplier_compliance_documents_dates_check",
        ),
    )
    op.create_index(
        "supplier_compliance_documents_tenant_supplier_idx",
        "supplier_compliance_documents",
        ["tenant_id", "supplier_id", "requirement_code", "created_at"],
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
