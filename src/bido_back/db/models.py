"""SQLAlchemy models for the business schema (`public`).

The models mirror `migrations/versions/`; the migrations are the source of
truth for DDL (RLS, policies, grants are not expressible here). Agno's own
tables live in schema `agno` and are not modelled here.

Tenant-owned tables carry `tenant_id` and are protected by RLS. `users` is
global (one person can belong to several tenants) and is on the explicit RLS
allowlist in `scripts/check_rls.py`.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
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
