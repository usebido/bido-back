"""Audit events for approvals: ids and the hash only, never the arguments."""

from uuid import UUID

from bido_back.db.models import Approval, AuditEvent


def approval_audit(approval: Approval, action: str, actor: UUID | None) -> AuditEvent:
    return AuditEvent(
        tenant_id=approval.tenant_id,
        actor_user_id=actor,
        action=action,
        payload={
            "approval_id": str(approval.id),
            "tool": approval.tool,
            "args_hash": approval.args_hash,
        },
    )
