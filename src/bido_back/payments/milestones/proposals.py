"""The `release_po_milestone` tool's name, proposal ids, job keys and audit actions.

Shared by the service that proposes a release (the factory or a buyer submits a
milestone), the act tool that claims it and the job that releases it on chain.
"""

from typing import Final
from uuid import UUID, uuid5

RELEASE_PO_MILESTONE: Final = "release_po_milestone"
# payload: {"payment_id": "<uuid>", "milestone_index": <int>}. Releases one approved
# milestone on chain (`payments.milestones.release`).
MILESTONE_RELEASE: Final = "milestone.release"

_NAMESPACE: Final = UUID("8c0f4b1e-6f3a-4d7e-9a51-3b2d7c9e0a44")

# Audit actions (keyed by `rfq_id` through `rfqs.audit.rfq_event`, so the RFQ feed shows them).
FACTORY_LINK_ISSUED: Final = "purchase_order.factory_link_issued"
FACTORY_LINK_REVOKED: Final = "purchase_order.factory_link_revoked"
MILESTONE_DOCUMENT_ADDED: Final = "purchase_order.milestone_document_added"
MILESTONE_DOCUMENT_REMOVED: Final = "purchase_order.milestone_document_removed"
MILESTONE_SUBMITTED: Final = "purchase_order.milestone_submitted"
MILESTONE_CHANGES_REQUESTED: Final = "purchase_order.milestone_changes_requested"
MILESTONE_RELEASING: Final = "purchase_order.milestone_releasing"
MILESTONE_RELEASED: Final = "purchase_order.milestone_released"
MILESTONE_RELEASE_FAILED: Final = "purchase_order.milestone_release_failed"
PAYMENT_COMPLETED: Final = "purchase_order.payment_completed"


def release_proposal_ids(payment_id: UUID, milestone_index: int, attempt: int) -> tuple[UUID, str]:
    """(run_id, tool_call_id) of one submission's release: one proposal per attempt."""
    call_id = f"{RELEASE_PO_MILESTONE}:{payment_id}:{milestone_index}"
    return uuid5(_NAMESPACE, f"{call_id}:{attempt}"), call_id


def release_job_key(payment_id: UUID, milestone_index: int) -> str:
    """The `milestone.release` idempotency key: one release per milestone."""
    return f"milestone:{payment_id}:{milestone_index}:release"
