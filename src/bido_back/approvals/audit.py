"""Audit events for approvals: ids, the hash and the self-approval mark only.

Never the arguments. `self_approved` is true when the requester decided their
own approval (tenants that allow self-approval, the default).
"""

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
            # None until flushed (server default): not a self-approval.
            "self_approved": bool(approval.self_approved),
        },
    )
