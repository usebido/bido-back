"""Request models and the `release_po_milestone` act tool's arguments and result.

Decimals travel as strings inside approval args (jsonb rewrites floats, and an
approval's args must hash the same after storage); token amounts, u64, travel as
strings of digits. No `to`/`cc`/`suppliers` keys: the outbound recipient guard
reads those as email recipients.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from bido_back.awards.views import DecimalText
from bido_back.payments.schemas import ChainPoId, CurrencyCode, Digits, Hex64

MilestoneIndex = int  # 0..3, also the gate


class SubmitMilestoneRequest(BaseModel):
    """Send a milestone's gathered documents for approval."""

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(
        default=None, max_length=2000, description="A message for the person who approves it"
    )


# Why a milestone is sent back (reporting without reading the text, e.g. "T1 needed 3 tries").
ChangeReason = Literal[
    "missing_document",
    "wrong_part_or_revision",
    "dimensional_nonconformance",
    "defect",
    "material_certificate_issue",
    "incomplete_ppap_package",
    "ppap_interim_only",
    "other",
]


class RequestMilestoneChangesRequest(BaseModel):
    """Refuse a milestone under review and tell the factory what to change."""

    model_config = ConfigDict(extra="forbid")

    reason_code: ChangeReason = Field(
        default="other", description="Why, as a code the factory and reports understand"
    )
    reason: str = Field(min_length=3, max_length=2000, description="What to change")


# --- The act tool ---------------------------------------------------------------------------


class MilestoneDocumentRef(BaseModel):
    """One file the approver sees and the evidence hash binds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: UUID
    doc_type: str
    filename: str
    size_bytes: int
    sha256: Hex64


class ReleaseMilestoneArgs(BaseModel):
    """Exactly what the approver approves: release this milestone of this escrow, against
    these files, to the payout fixed when the escrow was created."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    payment_id: UUID
    purchase_order_id: UUID
    purchase_order_ref: str
    rfq_id: UUID
    invite_id: UUID
    supplier_id: UUID
    # The payment's requester: the person on whose behalf the release is proposed.
    requested_by: UUID
    milestone_id: UUID
    milestone_index: int = Field(ge=0, le=3)
    milestone_key: str | None
    milestone_name: str
    bps: int = Field(ge=1, le=10_000)
    # Base units the escrow pays for this milestone (its own amount on chain).
    token_amount: Digits
    po_currency: CurrencyCode
    po_amount: DecimalText
    charge_currency: Literal["USD"]
    charge_amount: DecimalText
    chain_po_id: ChainPoId
    escrow_address: str
    # The PO document's hash the escrow holds (`po_payments.po_hash`).
    content_hash: Hex64
    submission_id: UUID
    attempt: int = Field(ge=1)
    evidence_hash: Hex64
    documents: list[MilestoneDocumentRef]
    simulated: bool


ReleaseOutcome = Literal["releasing", "already_released", "skipped", "failed"]


class ReleaseMilestoneResult(BaseModel):
    outcome: ReleaseOutcome
    payment_id: UUID
    milestone_index: int
    failure_code: str | None = None
