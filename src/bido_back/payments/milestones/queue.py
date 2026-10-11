"""What the approval queue shows for `release_po_milestone`: exactly what is approved.

The approval's arguments are the record: the milestone, its share of the PO and of the
card charge, the files and the evidence hash that binds them. The rows add what a person
needs to judge them: the supplier's name, who gathered the documents (the factory through
its link, or a buyer on its behalf) and their note, and the catalog's labels and the
checks the approver makes before releasing the milestone.
"""

import logging
from collections.abc import Sequence
from decimal import Decimal
from typing import cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.db.models import Approval, PoMilestoneSubmission, Supplier
from bido_back.payments.milestones.catalog import MilestoneDefinition, definition_for
from bido_back.payments.milestones.proposals import RELEASE_PO_MILESTONE
from bido_back.payments.milestones.schemas import MilestoneDocumentRef, ReleaseMilestoneArgs
from bido_back.payments.milestones.views import (
    DocumentSource,
    MilestoneDocumentRefView,
    MilestoneReleaseApprovalView,
)

logger = logging.getLogger(__name__)


async def queue_details(
    session: AsyncSession, approvals: Sequence[Approval]
) -> dict[UUID, MilestoneReleaseApprovalView]:
    """What the queue shows for each `release_po_milestone` approval, by approval id.

    An approval whose arguments no longer validate, or whose submission is not this
    milestone's, shows none: the act tool refuses to release it anyway."""
    parsed: dict[UUID, ReleaseMilestoneArgs] = {}
    for approval in approvals:
        if approval.tool != RELEASE_PO_MILESTONE:
            continue
        try:
            parsed[approval.id] = ReleaseMilestoneArgs.model_validate(approval.args)
        except ValidationError:
            # Never log the error: its text holds the values.
            logger.error("approval %s has args that do not validate", approval.id)
    if not parsed:
        return {}
    rows = await session.execute(
        select(Supplier.id, Supplier.name).where(
            Supplier.id.in_({args.supplier_id for args in parsed.values()})
        )
    )
    names: dict[UUID, str] = {supplier_id: name for supplier_id, name in rows}
    submissions: dict[UUID, PoMilestoneSubmission] = {
        row.id: row
        for row in await session.scalars(
            select(PoMilestoneSubmission).where(
                PoMilestoneSubmission.id.in_({args.submission_id for args in parsed.values()})
            )
        )
    }
    views: dict[UUID, MilestoneReleaseApprovalView] = {}
    for approval_id, args in parsed.items():
        submission = submissions.get(args.submission_id)
        if (
            submission is None
            or submission.payment_id != args.payment_id
            or submission.milestone_id != args.milestone_id
        ):
            logger.error(
                "approval %s: submission %s of payment %s milestone %d not found",
                approval_id,
                args.submission_id,
                args.payment_id,
                args.milestone_index,
            )
            continue
        definition = definition_for(args.milestone_key, args.milestone_name, args.bps)
        views[approval_id] = MilestoneReleaseApprovalView(
            payment_id=args.payment_id,
            purchase_order_id=args.purchase_order_id,
            purchase_order_ref=args.purchase_order_ref,
            supplier_name=names.get(args.supplier_id),
            milestone_index=args.milestone_index,
            milestone_key=args.milestone_key,
            milestone_name=args.milestone_name,
            label=definition.label,
            bps=args.bps,
            po_currency=args.po_currency,
            po_amount=Decimal(args.po_amount),
            charge_currency=args.charge_currency,
            charge_amount=Decimal(args.charge_amount),
            attempt=args.attempt,
            submission_id=args.submission_id,
            source=cast(DocumentSource, submission.source),
            note=submission.note,
            evidence_hash=args.evidence_hash,
            documents=[_document(definition, document) for document in args.documents],
            checks=list(definition.checks),
            simulated=args.simulated,
        )
    return views


def _document(
    definition: MilestoneDefinition, document: MilestoneDocumentRef
) -> MilestoneDocumentRefView:
    """An approved file with its type's label (its code when the catalog no longer has it)."""
    kind = definition.document(document.doc_type)
    return MilestoneDocumentRefView(
        document_id=document.document_id,
        doc_type=document.doc_type,
        label=kind.label if kind is not None else document.doc_type,
        filename=document.filename,
        size_bytes=document.size_bytes,
        sha256=document.sha256,
    )
