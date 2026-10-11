"""A payment's milestones as rows, and where each one stands. Shared by every module here.

* `ensure_milestones`: the rows are created on first use from the payment's approved
  terms (`po_payments.milestones`, what `create_po` wrote on chain), with the amounts
  split exactly like the program (`catalog.milestone_amounts`).
* Stored states: `open` (nothing under review), `submitted`, `releasing`, `released`,
  `failed`. What a person sees (`MilestoneStatus`) is derived: an open milestone is
  `locked` until the escrow is funded and every earlier milestone is released, and
  `changes_requested` when its latest submission was rejected.
* `accepts_documents`: the factory may gather documents for any open milestone of a
  funded escrow (T1's report may come with the tool's photos); `submittable`: only
  the first unreleased milestone can be sent for approval.
* `release_terms_match` and `submission_evidence`: what the approver approved still
  holds (the act tool and the release job check both every time).
"""

from collections.abc import Sequence
from decimal import Decimal
from typing import Final, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.db.models import (
    PoMilestoneDocument,
    PoMilestoneSubmission,
    PoPayment,
    PoPaymentMilestone,
)
from bido_back.payments.chain.hashes import compute_evidence_hash
from bido_back.payments.errors import PaymentNotFound
from bido_back.payments.milestones.catalog import milestone_amounts
from bido_back.payments.milestones.schemas import ReleaseMilestoneArgs
from bido_back.payments.milestones.views import FactoryMilestoneStatus, MilestoneStatus

# The escrow holds the money and may release it: milestones can move.
FUNDED: Final = "funded"
_FACTORY_STATUS: Final[dict[MilestoneStatus, FactoryMilestoneStatus]] = {
    "locked": "locked",
    "awaiting_documents": "awaiting_documents",
    "changes_requested": "changes_requested",
    "in_review": "in_review",
    "releasing": "processing",
    # A person settles a failed release; the factory sees it on its way.
    "failed": "processing",
    "released": "released",
}


async def ensure_milestones(session: AsyncSession, payment: PoPayment) -> list[PoPaymentMilestone]:
    """The payment's milestones by index, created from its approved terms on first use."""
    rows = await _milestones(session, payment.id)
    terms = payment.milestones
    if len(rows) == len(terms):
        return rows
    amounts = milestone_amounts(int(payment.token_amount), [int(t["bps"]) for t in terms])
    for index, (term, amount) in enumerate(zip(terms, amounts, strict=True)):
        key = term.get("key")
        await session.execute(
            insert(PoPaymentMilestone)
            .values(
                tenant_id=payment.tenant_id,
                payment_id=payment.id,
                milestone_index=index,
                key=key if isinstance(key, str) else None,
                name=str(term["name"]),
                bps=int(term["bps"]),
                token_amount=Decimal(amount),
            )
            .on_conflict_do_nothing(index_elements=["payment_id", "milestone_index"])
        )
    return await _milestones(session, payment.id)


async def _milestones(session: AsyncSession, payment_id: UUID) -> list[PoPaymentMilestone]:
    return list(
        await session.scalars(
            select(PoPaymentMilestone)
            .where(PoPaymentMilestone.payment_id == payment_id)
            .order_by(PoPaymentMilestone.milestone_index)
        )
    )


async def locked_milestone(
    session: AsyncSession, payment_id: UUID, milestone_index: int
) -> PoPaymentMilestone:
    """The milestone row, locked `FOR UPDATE` (lock the payment first when both are needed)."""
    row = await session.scalar(
        select(PoPaymentMilestone)
        .where(
            PoPaymentMilestone.payment_id == payment_id,
            PoPaymentMilestone.milestone_index == milestone_index,
        )
        .with_for_update()
    )
    if row is None:
        raise PaymentNotFound(f"payment {payment_id} has no milestone {milestone_index}")
    return row


def earlier_released(milestones: Sequence[PoPaymentMilestone], index: int) -> bool:
    return all(m.state == "released" for m in milestones[:index])


def accepts_documents(payment: PoPayment, milestone: PoPaymentMilestone) -> bool:
    """Documents may be gathered for an open milestone of a funded escrow."""
    return payment.state == FUNDED and milestone.state == "open"


def submittable(payment: PoPayment, milestones: Sequence[PoPaymentMilestone], index: int) -> bool:
    """Only the first unreleased milestone of a funded escrow is sent for approval."""
    return accepts_documents(payment, milestones[index]) and earlier_released(milestones, index)


def milestone_status(
    payment: PoPayment,
    milestones: Sequence[PoPaymentMilestone],
    index: int,
    latest: PoMilestoneSubmission | None,
) -> MilestoneStatus:
    """What a person sees; `latest`: the milestone's newest submission that is not a draft."""
    row = milestones[index]
    match row.state:
        case "submitted":
            return "in_review"
        case "releasing" | "released" | "failed":
            return cast(MilestoneStatus, row.state)
        case _:
            pass
    if payment.state != FUNDED or not earlier_released(milestones, index):
        return "locked"
    if latest is not None and latest.state == "rejected":
        return "changes_requested"
    return "awaiting_documents"


def factory_status(status: MilestoneStatus) -> FactoryMilestoneStatus:
    return _FACTORY_STATUS[status]


async def active_documents(session: AsyncSession, submission_id: UUID) -> list[PoMilestoneDocument]:
    """The submission's files not removed, oldest first."""
    return list(
        await session.scalars(
            select(PoMilestoneDocument)
            .where(
                PoMilestoneDocument.submission_id == submission_id,
                PoMilestoneDocument.removed_at.is_(None),
            )
            .order_by(PoMilestoneDocument.created_at, PoMilestoneDocument.id)
        )
    )


def evidence_hash(
    chain_po_id: str, milestone_index: int, documents: Sequence[PoMilestoneDocument]
) -> str:
    """The evidence hash of these files for this escrow and gate (gate = milestone index)."""
    return compute_evidence_hash(chain_po_id, milestone_index, (d.sha256 for d in documents))


def release_terms_match(
    payment: PoPayment,
    milestone: PoPaymentMilestone,
    submission: PoMilestoneSubmission,
    args: ReleaseMilestoneArgs,
) -> bool:
    """The rows still hold exactly what was approved: which escrow, which milestone, how
    much, against which submission and evidence."""
    return (
        args.payment_id == payment.id
        and milestone.payment_id == payment.id
        and submission.milestone_id == milestone.id
        and args.purchase_order_id == payment.purchase_order_id
        and args.requested_by == payment.requested_by
        and args.chain_po_id == payment.chain_po_id
        and args.escrow_address == payment.escrow_address
        and args.content_hash == payment.po_hash
        and args.milestone_id == milestone.id
        and args.milestone_index == milestone.milestone_index
        and args.milestone_key == milestone.key
        and args.milestone_name == milestone.name
        and args.bps == milestone.bps
        and Decimal(args.token_amount) == milestone.token_amount
        and args.submission_id == submission.id
        and args.attempt == submission.attempt
        and args.evidence_hash == submission.evidence_hash
    )


def documents_match(args: ReleaseMilestoneArgs, documents: Sequence[PoMilestoneDocument]) -> bool:
    """The submission's files are exactly the approved ones, and hash to the approved
    evidence (recomputed: a changed row cannot slip through)."""
    approved = sorted((d.document_id, d.sha256) for d in args.documents)
    current = sorted((d.id, d.sha256) for d in documents)
    return (
        approved == current
        and bool(documents)
        and evidence_hash(args.chain_po_id, args.milestone_index, documents) == args.evidence_hash
    )
