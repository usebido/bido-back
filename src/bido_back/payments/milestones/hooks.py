"""What a rejected `release_po_milestone` approval does: the milestone goes back to the factory.

Runs inside the guard's decision transaction (`approvals.RejectHook`), after the payload
was verified. The milestone under review with this approval becomes `open` again and its
submission `rejected`; nothing reached the chain. The reason, when a person gives one, is
stored by the caller after the decision (`service.request_milestone_changes`).
"""

from typing import Final

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.approvals.guard import RejectHook
from bido_back.db.models import Approval, PoMilestoneSubmission, PoPaymentMilestone
from bido_back.payments.milestones.proposals import (
    MILESTONE_CHANGES_REQUESTED,
    RELEASE_PO_MILESTONE,
)
from bido_back.payments.milestones.schemas import ReleaseMilestoneArgs
from bido_back.rfqs.audit import rfq_event


async def release_rejected_milestone(session: AsyncSession, approval: Approval) -> None:
    """The milestone `submitted` with this approval: `open` again, its submission `rejected`."""
    milestone = await session.scalar(
        select(PoPaymentMilestone)
        .where(
            PoPaymentMilestone.approval_id == approval.id,
            PoPaymentMilestone.state == "submitted",
        )
        .with_for_update()
    )
    if milestone is None:
        return  # not recorded yet, or already decided: the recorder applies the decision
    submission = (
        await session.get(PoMilestoneSubmission, milestone.submission_id)
        if milestone.submission_id is not None
        else None
    )
    if submission is not None and submission.state == "submitted":
        submission.state = "rejected"
        submission.decided_by = approval.decided_by
        submission.decided_at = approval.decided_at
        submission.updated_at = func.now()
    milestone.state = "open"
    milestone.approval_id = None
    milestone.submission_id = None
    milestone.evidence_hash = None
    milestone.updated_at = func.now()
    try:
        args = ReleaseMilestoneArgs.model_validate(approval.args)
    except ValidationError:
        args = None  # never chained: its text holds the values
    if args is not None:
        session.add(
            rfq_event(
                approval.tenant_id,
                approval.decided_by,
                MILESTONE_CHANGES_REQUESTED,
                args.rfq_id,
                payment_id=milestone.payment_id,
                milestone_index=milestone.milestone_index,
                submission_id=submission.id if submission is not None else None,
                approval_id=approval.id,
                purchase_order_id=args.purchase_order_id,
                purchase_order_ref=args.purchase_order_ref,
            )
        )
    await session.flush()


MILESTONE_REJECT_HOOKS: Final[dict[str, RejectHook]] = {
    RELEASE_PO_MILESTONE: release_rejected_milestone,
}
