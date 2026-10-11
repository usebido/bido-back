"""The `pay_purchase_order` tool's name, proposal ids and the payment job keys.

Shared by the service that proposes a payment and the act tool that runs it.
"""

from typing import Final
from uuid import UUID, uuid5

PAY_PURCHASE_ORDER: Final = "pay_purchase_order"
# payload: {"payment_id": "<uuid>"}. One step of a charged payment (mint, escrow, fund).
PAYMENT_ADVANCE: Final = "payment.advance"

_NAMESPACE: Final = UUID("52a6ef1b-cf15-43f9-b7ac-4d2737b5cecf")

# Audit actions (keyed by `rfq_id` through `rfqs.audit.rfq_event`, so the RFQ feed shows them).
PAYMENT_REQUESTED: Final = "purchase_order.payment_requested"
PAYMENT_CARD_VERIFIED: Final = "purchase_order.payment_card_verified"
# A Visa payment's failure code when its allowance was never approved with the passkey.
CARD_NOT_VERIFIED: Final = "card_not_verified"
PAYMENT_CHARGED: Final = "purchase_order.payment_charged"
PAYMENT_DECLINED: Final = "purchase_order.payment_declined"
PAYMENT_FAILED: Final = "purchase_order.payment_failed"
PAYMENT_MINTED: Final = "purchase_order.payment_minted"
ESCROW_CREATED: Final = "purchase_order.escrow_created"
ESCROW_FUNDED: Final = "purchase_order.escrow_funded"


def payment_proposal_ids(po_id: UUID, attempt: int = 0) -> tuple[UUID, str]:
    """(run_id, tool_call_id) of a PO's payment; `attempt` = earlier decided payment proposals."""
    call_id = f"{PAY_PURCHASE_ORDER}:{po_id}"
    return uuid5(_NAMESPACE, f"{call_id}:{attempt}"), call_id


def payment_job_key(payment_id: UUID, state: str) -> str:
    """The `payment.advance` idempotency key: one job per payment per state it advances from."""
    return f"payment:{payment_id}:{state}"
