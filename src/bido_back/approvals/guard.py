"""The approval guard: the one place an `act` proposal is approved or rejected.

Nothing a client sends counts as approval except a call to `decide_approval`
by an authenticated principal. The decision is made in ONE transaction in the
principal's tenant, checking, in order:

1. the principal is a member of the tenant (read from `memberships` now, never
   from the token; the row is share-locked so a concurrent demotion cannot
   slip in between check and decision) -> 403;
2. the approval exists in that tenant (forced RLS: another tenant's id is
   simply not found) and is locked `FOR UPDATE` -> 404;
3. who may decide:
   - the requester themself, when the tenant allows self-approval (the
     default: the AI prepared the act for this buyer, who reviews it);
   - otherwise a member with role `approver` or `owner` who is not the
     requester (four-eyes tenants: `allow_self_approval = false`) -> 403.
   The database enforces the same rule (CHECK + trigger, migration 0017);
4. it is still `pending`, so a decision is made exactly once and a replayed
   request changes nothing -> 409;
5. its stored args still hash to the proposed `args_hash` -> 409, nothing is
   decided, and an `approval.integrity_failed` audit event is kept;
6. approving an act that sends email: every recipient domain is a reserved
   test domain or confirmed for the tenant (`bido_back.outbound`). The decision
   may confirm domains of this email (`confirmed_domains`); anything still
   unconfirmed -> 409 listing the domains, and nothing is decided.

Then the status, `decided_by`, `decided_at` and `self_approved` are set and an
audit event is written (ids, hash and the self-approval mark only, never the
args), in the same transaction. On a
rejection, the tool's `RejectHook` (if the caller passes one for that tool)
runs in that transaction too, so the feature can release what the proposal
held (e.g. an RFQ invite goes back to `draft`).

Approving does not execute anything. After the commit, an `approval.execute`
job is enqueued (idempotency key `approval:<id>`); the worker runs the tool
(`executor.py`). The enqueue happens after the commit: if it fails, the
approval stays `approved` with no job and the error propagates (the caller
sees a 5xx and it reaches Sentry). The scheduled follow-ups sweep re-enqueues
approvals left like that (`reconcile.py`).
"""

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.approvals.audit import approval_audit
from bido_back.approvals.errors import (
    ApprovalForbidden,
    ApprovalIntegrityError,
    ApprovalNotFound,
    ApprovalNotPending,
    ApprovalRecipientsUnconfirmed,
)
from bido_back.approvals.executor import approval_execute_envelope
from bido_back.approvals.integrity import payload_matches
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import Approval, Membership, Tenant
from bido_back.email.recipients import domain_of
from bido_back.outbound.domains import (
    confirm_domains,
    normalize_domain,
    recipients_in_args,
    unconfirmed_domains,
)
from bido_back.worker.jobs import JobQueue

logger = logging.getLogger(__name__)

Decision = Literal["approve", "reject"]
DecidedStatus = Literal["approved", "rejected"]

APPROVER_ROLES: Final = frozenset({"approver", "owner"})

_STATUS: Final[dict[Decision, DecidedStatus]] = {"approve": "approved", "reject": "rejected"}


# Runs inside the decision's transaction, after the payload was verified.
RejectHook = Callable[[AsyncSession, Approval], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class DecisionOutcome:
    approval_id: UUID
    status: DecidedStatus


async def decide_approval(
    db: AsyncEngine | async_sessionmaker[AsyncSession],
    principal: Principal,
    approval_id: UUID,
    decision: Decision,
    *,
    jobs: JobQueue,
    on_reject: Mapping[str, RejectHook] | None = None,
    confirmed_domains: Sequence[str] = (),
) -> DecisionOutcome:
    """Record `principal`'s decision on one approval; enqueue execution if approved.

    Raises ApprovalForbidden, ApprovalNotFound, ApprovalNotPending or
    ApprovalIntegrityError; on any of them nothing was decided.
    """
    status = _STATUS[decision]
    integrity_failed = False

    async with tenant_session(db, principal.tenant_id) as session:
        role = await session.scalar(
            select(Membership.role)
            .where(
                Membership.tenant_id == principal.tenant_id,
                Membership.user_id == principal.user_id,
            )
            .with_for_update(read=True)
        )
        if role is None:
            raise ApprovalForbidden("only a member of the tenant can decide approvals")

        approval = await session.scalar(
            select(Approval).where(Approval.id == approval_id).with_for_update()
        )
        if approval is None or approval.tenant_id != principal.tenant_id:
            raise ApprovalNotFound(f"approval {approval_id} not found")
        self_approval = approval.requested_by == principal.user_id
        if self_approval:
            allowed = await session.scalar(
                select(Tenant.allow_self_approval).where(Tenant.id == principal.tenant_id)
            )
            if not allowed:
                raise ApprovalForbidden(
                    "this company requires a second person: the requester cannot decide"
                )
        elif role not in APPROVER_ROLES:
            raise ApprovalForbidden("the approver or owner role is required")
        if approval.status != "pending":
            raise ApprovalNotPending(f"approval {approval_id} is {approval.status}")

        if not payload_matches(approval):
            # Keep the evidence (committed), decide nothing, refuse below.
            session.add(approval_audit(approval, "approval.integrity_failed", principal.user_id))
            integrity_failed = True
        else:
            if status == "approved":
                await _check_recipients(session, principal, approval, confirmed_domains)
            approval.status = status
            approval.decided_by = principal.user_id
            approval.decided_at = datetime.now(UTC)
            approval.self_approved = self_approval
            session.add(approval_audit(approval, f"approval.{status}", principal.user_id))
            hook = (on_reject or {}).get(approval.tool) if status == "rejected" else None
            if hook is not None:
                await hook(session, approval)
        await session.flush()

    if integrity_failed:
        logger.error("approval %s refused: stored args do not match args_hash", approval_id)
        raise ApprovalIntegrityError(f"approval {approval_id}: args do not match hash")

    logger.info(
        "approval %s %s tenant=%s by=%s self_approved=%s",
        approval_id,
        status,
        principal.tenant_id,
        principal.user_id,
        self_approval,
    )
    if status == "approved":
        try:
            await jobs.enqueue(approval_execute_envelope(principal.tenant_id, approval_id))
        except Exception:
            logger.exception("approval %s approved but its job was not enqueued", approval_id)
            raise
    return DecisionOutcome(approval_id=approval_id, status=status)


async def _check_recipients(
    session: AsyncSession,
    principal: Principal,
    approval: Approval,
    confirmed: Sequence[str],
) -> None:
    """Outbound guard for acts that send email (step 6 of the module docstring)."""
    addresses = recipients_in_args(approval.args)
    if not addresses:
        return
    in_email = {d for d in (domain_of(a) for a in addresses) if d is not None}
    wanted = {d for d in (normalize_domain(x) for x in confirmed) if d in in_email}
    if wanted:
        await confirm_domains(
            session, principal.tenant_id, principal.user_id, wanted, approval_id=approval.id
        )
    blocked = await unconfirmed_domains(session, principal.tenant_id, addresses)
    if blocked:
        # Domains are the supplier's; count them, never name them.
        logger.warning(
            "recipient guard blocked",
            extra={
                "approval_id": str(approval.id),
                "tool": approval.tool,
                "recipients": len(addresses),
                "unconfirmed_domains": len(blocked),
            },
        )
        raise ApprovalRecipientsUnconfirmed(blocked)
