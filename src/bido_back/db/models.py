"""SQLAlchemy models for the business schema (`public`).

The models mirror `migrations/versions/`; the migrations are the source of
truth for DDL (RLS, policies, grants are not expressible here). Agno's own
tables live in schema `agno` and are not modelled here.

Tenant-owned tables carry `tenant_id` and are protected by RLS. `users` is
global (one person can belong to several tenants) and is on the explicit RLS
allowlist in `scripts/check_rls.py`.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

MEMBERSHIP_ROLES = ("member", "approver", "owner")
APPROVAL_STATUSES = ("pending", "approved", "rejected", "executed", "expired")


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    """A person, identified by Clerk. Global: not tenant-owned."""

    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    clerk_user_id: Mapped[str] = mapped_column(Text, unique=True)
    email: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="memberships_tenant_id_user_id_key"),
        CheckConstraint("role IN ('member', 'approver', 'owner')", name="memberships_role_check"),
        Index("memberships_user_id_idx", "user_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # When the user finished onboarding in this tenant (migration 0007).
    onboarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    """Append-only: the app role has INSERT/SELECT only (UPDATE/DELETE revoked)."""

    __tablename__ = "audit_events"
    __table_args__ = (Index("audit_events_tenant_id_created_at_idx", "tenant_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Approval(Base):
    """A person's decision on one exact act-tool payload (migration 0002).

    `args_hash` is the SHA-256 hex of the canonical-JSON `args`; the executor
    recomputes it and refuses on mismatch. The app role may INSERT and SELECT,
    may UPDATE only status/decided_by/decided_at/executed_at, and may never
    DELETE. The requester can never be the approver (database CHECK).
    """

    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "tool_call_id",
            name="approvals_tenant_id_run_id_tool_call_id_key",
        ),
        CheckConstraint("args_hash ~ '^[0-9a-f]{64}$'", name="approvals_args_hash_check"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'executed', 'expired')",
            name="approvals_status_check",
        ),
        CheckConstraint(
            "decided_by IS NULL OR decided_by <> requested_by",
            name="approvals_not_self_approved_check",
        ),
        CheckConstraint(
            "(status IN ('approved', 'rejected', 'executed')"
            " AND decided_by IS NOT NULL AND decided_at IS NOT NULL)"
            " OR (status IN ('pending', 'expired')"
            " AND decided_by IS NULL AND decided_at IS NULL)",
            name="approvals_decision_check",
        ),
        CheckConstraint(
            "(status = 'executed') = (executed_at IS NOT NULL)",
            name="approvals_executed_check",
        ),
        Index("approvals_tenant_id_status_created_at_idx", "tenant_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    run_id: Mapped[str] = mapped_column(Text)
    session_id: Mapped[str] = mapped_column(Text)
    tool_call_id: Mapped[str] = mapped_column(Text)
    tool: Mapped[str] = mapped_column(Text)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB)
    args_hash: Mapped[str] = mapped_column(Text)
    requested_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    decided_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- RFQ flow (migration 0003) ----------------------------------------------
#
# Personal data lives only in `contacts`; every other table references a
# contact by id, so erasing a person is a change to one row.

CONTACT_KINDS = ("buyer", "supplier")
SUPPLIER_TYPES = (
    "authorized_distributor",
    "distributor",
    "reseller",
    "manufacturer",
    "machining",
    "other",
)
RFQ_STATUSES = ("draft", "open", "closed", "cancelled")
LINE_CATEGORIES = ("catalog", "drawing", "cell", "material")
INVITE_STATUSES = ("draft", "pending_approval", "sent", "responded", "declined")
MESSAGE_STATUSES = ("stored", "unmatched", "quarantined", "extracted", "failed")


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    kind: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    pseudonymized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text, server_default=text("'distributor'"))
    country: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text, server_default=text("'en'"))
    email_domain: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SupplierContact(Base):
    __tablename__ = "supplier_contacts"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    supplier_id: Mapped[UUID] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"))
    contact_id: Mapped[UUID] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))


class Rfq(Base):
    __tablename__ = "rfqs"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    ref: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'draft'"))
    base_currency: Mapped[str] = mapped_column(Text, server_default=text("'EUR'"))
    response_due: Mapped[date | None] = mapped_column(Date)
    source_text: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # The template version its emails were rendered from, set at the first send.
    # template_version 0 without a template_id: Bido's standard template.
    template_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("rfq_templates.id", ondelete="SET NULL")
    )
    template_version: Mapped[int | None] = mapped_column(Integer)
    # The chase schedule, in business days (migration 0008).
    followups_enabled: Mapped[bool] = mapped_column(Boolean, server_default=true())
    followup_first_after_days: Mapped[int] = mapped_column(Integer, server_default=text("2"))
    followup_every_days: Mapped[int] = mapped_column(Integer, server_default=text("2"))
    followup_max_chases: Mapped[int] = mapped_column(Integer, server_default=text("3"))
    # The supplier negotiation (migration 0013): not_started, open,
    # ready_to_close, closed.
    negotiation_status: Mapped[str] = mapped_column(Text, server_default=text("'not_started'"))
    negotiation_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    negotiation_max_rounds: Mapped[int] = mapped_column(Integer, server_default=text("3"))
    negotiation_early_close: Mapped[bool] = mapped_column(Boolean, server_default=true())
    negotiation_ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    negotiation_closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    negotiation_outcome: Mapped[list[Any] | None] = mapped_column(JSONB)


class RfqLine(Base):
    __tablename__ = "rfq_lines"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    rfq_id: Mapped[UUID] = mapped_column(ForeignKey("rfqs.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    mpn: Mapped[str | None] = mapped_column(Text)
    manufacturer: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text, server_default=text("'catalog'"))
    # Requested quantity tiers, e.g. [1000, 2000, 5000].
    quantities: Mapped[list[Any]] = mapped_column(JSONB)
    unit: Mapped[str] = mapped_column(Text, server_default=text("'pcs'"))
    need_by: Mapped[date | None] = mapped_column(Date)
    incoterm: Mapped[str | None] = mapped_column(Text)
    delivery_location: Mapped[str | None] = mapped_column(Text)
    qualification: Mapped[str | None] = mapped_column(Text)
    # The buyer's negotiation limits, in the RFQ's base currency (migration 0013).
    target_unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    max_unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    max_lead_time_days: Mapped[int | None] = mapped_column(Integer)


class Invite(Base):
    """One RFQ sent to one supplier. `reply_token` is the unguessable part of the
    per-invite reply address; replies are matched by it first."""

    __tablename__ = "invites"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    rfq_id: Mapped[UUID] = mapped_column(ForeignKey("rfqs.id", ondelete="CASCADE"))
    supplier_id: Mapped[UUID] = mapped_column(ForeignKey("suppliers.id"))
    contact_id: Mapped[UUID] = mapped_column(ForeignKey("contacts.id"))
    reply_token: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, server_default=text("'draft'"))
    thread_message_id: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # The thread in the buyer's own mailbox (Gmail threadId / Graph
    # conversationId) and the connection it lives in (migration 0006).
    provider_thread_id: Mapped[str | None] = mapped_column(Text)
    mailbox_connection_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("mailbox_connections.id", ondelete="SET NULL")
    )
    # No chase before this (an auto-reply / out of office; migration 0008).
    followups_paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Where this supplier stands in the negotiation, and the offer (proposal
    # version) last evaluated (migration 0013).
    negotiation_status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    negotiation_evaluated_proposal_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("proposals.id", ondelete="SET NULL")
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    invite_id: Mapped[UUID | None] = mapped_column(ForeignKey("invites.id"))
    direction: Mapped[str] = mapped_column(Text)
    message_id_header: Mapped[str | None] = mapped_column(Text)
    in_reply_to: Mapped[str | None] = mapped_column(Text)
    from_contact_id: Mapped[UUID | None] = mapped_column(ForeignKey("contacts.id"))
    subject: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    raw_key: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'stored'"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    message_id: Mapped[UUID] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Proposal(Base):
    """One supplier answer, versioned: a revised quote is a new version."""

    __tablename__ = "proposals"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    invite_id: Mapped[UUID] = mapped_column(ForeignKey("invites.id", ondelete="CASCADE"))
    message_id: Mapped[UUID] = mapped_column(ForeignKey("messages.id"))
    version: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str | None] = mapped_column(Text)
    valid_until: Mapped[date | None] = mapped_column(Date)
    incoterm: Mapped[str | None] = mapped_column(Text)
    payment_terms: Mapped[str | None] = mapped_column(Text)
    # Per-field provenance: {"currency": {"source": {...}, "confidence": 0.93}, ...}
    fields: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProposalLine(Base):
    __tablename__ = "proposal_lines"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    proposal_id: Mapped[UUID] = mapped_column(ForeignKey("proposals.id", ondelete="CASCADE"))
    rfq_line_id: Mapped[UUID | None] = mapped_column(ForeignKey("rfq_lines.id"))
    offered_mpn: Mapped[str | None] = mapped_column(Text)
    is_alternative: Mapped[bool] = mapped_column(Boolean, server_default=false())
    min_quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    unit: Mapped[str | None] = mapped_column(Text)
    tooling_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    moq: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    lead_time_days: Mapped[int | None] = mapped_column(Integer)
    origin_country: Mapped[str | None] = mapped_column(Text)
    # Per-field provenance, same shape as Proposal.fields.
    fields: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    needs_review: Mapped[bool] = mapped_column(Boolean, server_default=false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


CORRECTION_ACTIONS = ("corrected", "confirmed")


class FieldCorrection(Base):
    """A user's correction (or confirmation) of one extracted value (migration 0010).

    `proposal_line_id` is NULL for a proposal header field. `old_provenance` is
    the extraction's record of the old value; the proposal tables hold the
    effective value. Append-only: these rows are the extraction's eval set.
    """

    __tablename__ = "field_corrections"
    __table_args__ = (
        CheckConstraint(
            "action IN ('corrected', 'confirmed')", name="field_corrections_action_check"
        ),
        Index("field_corrections_proposal_idx", "tenant_id", "proposal_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    proposal_id: Mapped[UUID] = mapped_column(ForeignKey("proposals.id", ondelete="CASCADE"))
    proposal_line_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("proposal_lines.id", ondelete="CASCADE")
    )
    field: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    # JSON of the column value (decimals and dates as strings); SQL NULL when empty.
    old_value: Mapped[Any] = mapped_column(JSONB(none_as_null=True), nullable=True)
    new_value: Mapped[Any] = mapped_column(JSONB(none_as_null=True), nullable=True)
    old_provenance: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    reason: Mapped[str | None] = mapped_column(Text)
    corrected_by: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FxRate(Base):
    """Daily ECB reference rates: how many units of `currency` per 1 EUR. Global."""

    __tablename__ = "fx_rates"

    rate_date: Mapped[date] = mapped_column(Date, primary_key=True)
    currency: Mapped[str] = mapped_column(Text, primary_key=True)
    per_eur: Mapped[Decimal] = mapped_column(Numeric(18, 8))


# --- Mailbox connections (migration 0006) -----------------------------------

MAILBOX_PROVIDERS = ("google", "microsoft")
MAILBOX_STATUSES = ("active", "revoked", "error")


class MailboxConnection(Base):
    """A buyer's own mailbox (Gmail / Microsoft 365), connected over OAuth.

    `token_ciphertext` is the encrypted token set (`bido_back.mailbox.crypto`),
    NULL once revoked. `email_address` is the buyer's own work address.
    `sync_state` holds, per provider thread, the provider message ids already
    handled; nothing else from the mailbox is stored.
    """

    __tablename__ = "mailbox_connections"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", "provider", name="mailbox_connections_tenant_user_provider_key"
        ),
        CheckConstraint(
            "provider IN ('google', 'microsoft')", name="mailbox_connections_provider_check"
        ),
        CheckConstraint(
            "status IN ('active', 'revoked', 'error')", name="mailbox_connections_status_check"
        ),
        CheckConstraint(
            "status <> 'active' OR token_ciphertext IS NOT NULL",
            name="mailbox_connections_active_has_tokens_check",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(Text)
    email_address: Mapped[str] = mapped_column(Text)
    token_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    sync_state: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- Onboarding (migration 0007) --------------------------------------------

TEMPLATE_SOURCES = ("search", "upload", "none")
TEMPLATE_STATUSES = ("building", "ready", "failed")
SCAN_STATUSES = ("running", "done", "failed")


class RfqTemplate(Base):
    """How the tenant's RFQ emails usually look. At most one default per tenant."""

    __tablename__ = "rfq_templates"
    __table_args__ = (
        CheckConstraint(
            "source IN ('search', 'upload', 'none')", name="rfq_templates_source_check"
        ),
        CheckConstraint(
            "status IN ('building', 'ready', 'failed')", name="rfq_templates_status_check"
        ),
        CheckConstraint(
            "status <> 'ready' OR greeting IS NOT NULL", name="rfq_templates_ready_check"
        ),
        CheckConstraint(
            "NOT is_default OR status = 'ready'", name="rfq_templates_default_ready_check"
        ),
        CheckConstraint(
            "category IS NULL OR category IN ('catalog', 'drawing', 'cell', 'material')",
            name="rfq_templates_category_check",
        ),
        CheckConstraint("version >= 1", name="rfq_templates_version_check"),
        # One default per tenant and item category (NULL = the general template).
        Index(
            "rfq_templates_one_default_idx",
            "tenant_id",
            text("(coalesce(category, ''))"),
            unique=True,
            postgresql_where=text("is_default"),
        ),
        Index("rfq_templates_tenant_id_created_at_idx", "tenant_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'building'"))
    # ISO 639-1 of the buyer's RFQs, e.g. "pt".
    language: Mapped[str | None] = mapped_column(Text)
    greeting: Mapped[str | None] = mapped_column(Text)
    columns: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    footer: Mapped[str | None] = mapped_column(Text)
    fields: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    # Ids only: {"scan_id": ..., "candidate_ids": [...]} or {"upload_ids": [...]}.
    source_refs: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    is_default: Mapped[bool] = mapped_column(Boolean, server_default=false())
    # A variant for one item category; None is the general template.
    category: Mapped[str | None] = mapped_column(Text)
    # The current version; its content is the row's own columns.
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RfqTemplateVersion(Base):
    """An immutable snapshot of one version of an RFQ template (the app cannot UPDATE it)."""

    __tablename__ = "rfq_template_versions"
    __table_args__ = (
        UniqueConstraint("template_id", "version", name="rfq_template_versions_version_key"),
        CheckConstraint("version >= 1", name="rfq_template_versions_version_check"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    template_id: Mapped[UUID] = mapped_column(ForeignKey("rfq_templates.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    language: Mapped[str] = mapped_column(Text)
    greeting: Mapped[str] = mapped_column(Text)
    columns: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    footer_fields: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    fields: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TemplateScan(Base):
    """A one-off scan of the user's sent mail for old RFQs. Metadata only, never bodies."""

    __tablename__ = "template_scans"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'done', 'failed')", name="template_scans_status_check"
        ),
        Index(
            "template_scans_tenant_id_user_id_created_at_idx", "tenant_id", "user_id", "created_at"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    mailbox_connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("mailbox_connections.id", ondelete="CASCADE")
    )
    status: Mapped[str] = mapped_column(Text, server_default=text("'running'"))
    checked: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    found: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    candidates: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TemplateUpload(Base):
    """A file uploaded to learn the RFQ template from; the bytes are in blob storage."""

    __tablename__ = "template_uploads"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FollowupMessage(Base):
    """A chase or clarification email Bido proposed for an invite (migration 0008).

    The id is derived from (invite, kind, sequence). The approved text lives in
    the approval's args; once sent, in `messages`.
    """

    __tablename__ = "followup_messages"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    invite_id: Mapped[UUID] = mapped_column(ForeignKey("invites.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    sequence: Mapped[int] = mapped_column(Integer)
    proposal_id: Mapped[UUID | None] = mapped_column(ForeignKey("proposals.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'proposed'"))
    approval_id: Mapped[UUID | None] = mapped_column(ForeignKey("approvals.id"))
    message_id_header: Mapped[str | None] = mapped_column(Text)
    canceled_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NegotiationMessage(Base):
    """A counter-offer or best-and-final request Bido proposed (migration 0013).

    The id is derived from (invite, round). `terms` holds the numbers asked
    for; the approved text lives in the approval's args and, once sent, in
    `messages`.
    """

    __tablename__ = "negotiation_messages"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    invite_id: Mapped[UUID] = mapped_column(ForeignKey("invites.id", ondelete="CASCADE"))
    proposal_id: Mapped[UUID] = mapped_column(ForeignKey("proposals.id", ondelete="CASCADE"))
    round: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'proposed'"))
    approval_id: Mapped[UUID | None] = mapped_column(ForeignKey("approvals.id"))
    terms: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    message_id_header: Mapped[str | None] = mapped_column(Text)
    canceled_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --- Supplier compliance (migration 0011) -----------------------------------

COMPLIANCE_SCOPES = ("supplier", "manufacturer", "line")


class ComplianceRequirement(Base):
    """One document the tenant expects from suppliers. No rows = Bido's default set."""

    __tablename__ = "compliance_requirements"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="compliance_requirements_tenant_code_key"),
        CheckConstraint(
            "scope IN ('supplier', 'manufacturer', 'line')",
            name="compliance_requirements_scope_check",
        ),
        CheckConstraint(
            "(scope = 'line') = (line_keyword IS NOT NULL)",
            name="compliance_requirements_line_keyword_check",
        ),
        CheckConstraint(
            "validity_months IS NULL OR validity_months BETWEEN 1 AND 120",
            name="compliance_requirements_validity_check",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    code: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(Text)
    # Required for scope "line": the RFQ line's qualification must mention it.
    line_keyword: Mapped[str | None] = mapped_column(Text)
    # Lower-case filename fragments that mark a candidate attachment.
    keywords: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    validity_months: Mapped[int | None] = mapped_column(Integer)
    blocking: Mapped[bool] = mapped_column(Boolean, server_default=false())
    position: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SupplierComplianceDocument(Base):
    """A document a person confirmed for one requirement of one supplier (and line)."""

    __tablename__ = "supplier_compliance_documents"
    __table_args__ = (
        CheckConstraint(
            "attachment_id IS NOT NULL OR url IS NOT NULL",
            name="supplier_compliance_documents_evidence_check",
        ),
        CheckConstraint(
            "url IS NULL OR url ~ '^https://'", name="supplier_compliance_documents_url_check"
        ),
        CheckConstraint(
            "expires_on IS NULL OR issued_on IS NULL OR expires_on >= issued_on",
            name="supplier_compliance_documents_dates_check",
        ),
        Index(
            "supplier_compliance_documents_tenant_supplier_idx",
            "tenant_id",
            "supplier_id",
            "requirement_code",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    supplier_id: Mapped[UUID] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"))
    requirement_code: Mapped[str] = mapped_column(Text)
    rfq_line_id: Mapped[UUID | None] = mapped_column(ForeignKey("rfq_lines.id", ondelete="CASCADE"))
    attachment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("attachments.id", ondelete="CASCADE")
    )
    url: Mapped[str | None] = mapped_column(Text)
    issued_on: Mapped[date | None] = mapped_column(Date)
    expires_on: Mapped[date | None] = mapped_column(Date)
    recorded_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- Purchase analysis (migration 0014) ----------------------------------------------

AMOUNT = Numeric(18, 6)


class Item(Base):
    """A part the company buys, by its internal part number."""

    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("tenant_id", "part_number", name="items_tenant_part_number_key"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    part_number: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text)
    mpn: Mapped[str | None] = mapped_column(Text)
    manufacturer: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    unit: Mapped[str] = mapped_column(Text, server_default=text("'pcs'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BomLine(Base):
    """How much of one item goes into one unit of a product."""

    __tablename__ = "bom_lines"
    __table_args__ = (
        UniqueConstraint("tenant_id", "product", "item_id", name="bom_lines_product_item_key"),
        CheckConstraint("qty_per_unit > 0", name="bom_lines_qty_check"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    product: Mapped[str] = mapped_column(Text)
    item_id: Mapped[UUID] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))
    qty_per_unit: Mapped[Decimal] = mapped_column(AMOUNT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InventoryRow(Base):
    """On hand and on order for one item at one location."""

    __tablename__ = "inventory"
    __table_args__ = (
        UniqueConstraint("tenant_id", "item_id", "location", name="inventory_item_location_key"),
        CheckConstraint("on_hand >= 0 AND on_order >= 0", name="inventory_quantities_check"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    item_id: Mapped[UUID] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))
    location: Mapped[str] = mapped_column(Text, server_default=text("''"))
    on_hand: Mapped[Decimal] = mapped_column(AMOUNT, server_default=text("0"))
    on_order: Mapped[Decimal] = mapped_column(AMOUNT, server_default=text("0"))
    as_of: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PurchaseOrder(Base):
    """A past purchase order (imported history), one supplier and currency."""

    __tablename__ = "purchase_orders"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "supplier_id", "po_number", name="purchase_orders_number_key"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    po_number: Mapped[str] = mapped_column(Text)
    supplier_id: Mapped[UUID] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"))
    currency: Mapped[str] = mapped_column(Text, server_default=text("'EUR'"))
    ordered_at: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PoLine(Base):
    """One item of a purchase order, with what was promised and what arrived."""

    __tablename__ = "po_lines"
    __table_args__ = (
        UniqueConstraint("tenant_id", "purchase_order_id", "line_no", name="po_lines_line_key"),
        CheckConstraint("quantity > 0", name="po_lines_quantity_check"),
        CheckConstraint("unit_price >= 0", name="po_lines_price_check"),
        CheckConstraint(
            "rejected_qty >= 0 AND (received_qty IS NULL OR received_qty >= 0)",
            name="po_lines_received_check",
        ),
        Index("po_lines_tenant_item_idx", "tenant_id", "item_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    purchase_order_id: Mapped[UUID] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="CASCADE")
    )
    line_no: Mapped[int] = mapped_column(Integer)
    item_id: Mapped[UUID] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))
    quantity: Mapped[Decimal] = mapped_column(AMOUNT)
    unit_price: Mapped[Decimal] = mapped_column(AMOUNT)
    promised_at: Mapped[date | None] = mapped_column(Date)
    received_at: Mapped[date | None] = mapped_column(Date)
    received_qty: Mapped[Decimal | None] = mapped_column(AMOUNT)
    rejected_qty: Mapped[Decimal] = mapped_column(AMOUNT, server_default=text("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProductionPlanRow(Base):
    """Units of a product needed by a date."""

    __tablename__ = "production_plan"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "product", "need_by", name="production_plan_product_date_key"
        ),
        CheckConstraint("quantity > 0", name="production_plan_quantity_check"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    product: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(AMOUNT)
    need_by: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Analysis(Base):
    """One run of the purchase analysis for a product and quantity."""

    __tablename__ = "analyses"
    __table_args__ = (CheckConstraint("quantity > 0", name="analyses_quantity_check"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    product: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(AMOUNT)
    need_by: Mapped[date | None] = mapped_column(Date)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    rfq_id: Mapped[UUID | None] = mapped_column(ForeignKey("rfqs.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AnalysisLine(Base):
    """The analysis result for one item: how much to buy, from whom, at what price.

    `rfq_line_id` links the RFQ line created from it; `target_price` and
    `max_price` (in `currency`) are what the negotiation aims at and accepts.
    """

    __tablename__ = "analysis_lines"
    __table_args__ = (
        UniqueConstraint("tenant_id", "analysis_id", "position", name="analysis_lines_pos_key"),
        Index(
            "analysis_lines_rfq_line_idx",
            "tenant_id",
            "rfq_line_id",
            postgresql_where=text("rfq_line_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))
    analysis_id: Mapped[UUID] = mapped_column(ForeignKey("analyses.id", ondelete="CASCADE"))
    item_id: Mapped[UUID] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    required: Mapped[Decimal] = mapped_column(AMOUNT)
    on_hand: Mapped[Decimal] = mapped_column(AMOUNT)
    on_order: Mapped[Decimal] = mapped_column(AMOUNT)
    to_buy: Mapped[Decimal] = mapped_column(AMOUNT)
    unit: Mapped[str] = mapped_column(Text)
    need_by: Mapped[date | None] = mapped_column(Date)
    currency: Mapped[str | None] = mapped_column(Text)
    last_price: Mapped[Decimal | None] = mapped_column(AMOUNT)
    avg_price: Mapped[Decimal | None] = mapped_column(AMOUNT)
    min_price: Mapped[Decimal | None] = mapped_column(AMOUNT)
    trend_pct: Mapped[Decimal | None] = mapped_column(AMOUNT)
    target_price: Mapped[Decimal | None] = mapped_column(AMOUNT)
    max_price: Mapped[Decimal | None] = mapped_column(AMOUNT)
    suppliers: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    flags: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    rfq_line_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("rfq_lines.id", ondelete="SET NULL")
    )
