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
