"""A PO payment's milestones: what the buyer and the factory see, and sending one for approval.

* `list_milestones` / `milestone_view`: each milestone of the PO's payment with its share of
  the PO total, of the card charge and of the escrow's tokens (the program's split), what it
  asks for and what the approver checks, its submissions (newest first, with their files)
  and where its release stands.
* `factory_order`: the factory's page. Its order, each milestone's amount in the order's
  currency, what to send and where each milestone stands; no chain words, addresses,
  hashes or signatures. `accept_order` (from `links`): the factory confirms the order.
* `submit_milestone`: the draft's files become a `release_po_milestone` proposal that a
  person decides at POST /v1/approvals/{id}/decision. Checked under the payment and
  milestone locks: releases are set up on this server, the milestone is the next one to
  release (`state.submittable`), every required document type is there. The approval's
  arguments carry the files and their evidence hash; its requester is the payment's
  requester, so the company's self-approval or four-eyes rule applies unchanged. Then,
  as `payments.service.request_po_payment` does, a second transaction records the
  submission against the approval (the approval first, then payment and milestone: the
  guard's and the executor's order); a decision made in between is applied, and any
  failure expires the approval.
* `request_milestone_changes`: a person refuses the release under review through the
  approval guard, with a reason code and text the factory sees, in the decision's
  transaction (`hooks` sends the milestone back to the factory).

Logs and audit events carry ids, indexes and codes only: never file names, notes or reasons.
"""

import logging
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Final, cast
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.agents.approvals import ApprovalProposal
from bido_back.agents.events import args_hash
from bido_back.approvals import decide_approval
from bido_back.approvals.audit import approval_audit
from bido_back.approvals.errors import (
    ApprovalConflict,
    ApprovalForbidden,
    ApprovalIntegrityError,
    ApprovalNotFound,
    ApprovalNotPending,
)
from bido_back.approvals.guard import RejectHook
from bido_back.approvals.sink import PostgresApprovalSink
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    CompanyBranding,
    IssuedPurchaseOrder,
    PoMilestoneDocument,
    PoMilestoneSubmission,
    PoPayment,
    PoPaymentMilestone,
    Supplier,
    Tenant,
)
from bido_back.decimals import format_decimal
from bido_back.payments.config import explorer_tx_url, releases_enabled
from bido_back.payments.errors import (
    FactoryLinkInvalid,
    PaymentForbidden,
    PaymentInvalidState,
    PaymentNotFound,
    PaymentRefused,
    PaymentsNotConfigured,
)
from bido_back.payments.milestones.catalog import (
    MilestoneDefinition,
    decimal_shares,
    definition_for,
)
from bido_back.payments.milestones.documents import (
    Db,
    Uploader,
    actor_of,
    document_view,
    draft_submission,
    locked_milestones,
    source_of,
    uploader_link,
)
from bido_back.payments.milestones.hooks import MILESTONE_REJECT_HOOKS
from bido_back.payments.milestones.links import (
    INVALID_LINK,
    FactoryAccess,
    active_link,
    purchase_order_payment,
)
from bido_back.payments.milestones.links import accept_order as accept_order
from bido_back.payments.milestones.proposals import (
    MILESTONE_CHANGES_REQUESTED,
    MILESTONE_SUBMITTED,
    RELEASE_PO_MILESTONE,
    release_proposal_ids,
)
from bido_back.payments.milestones.schemas import (
    ChangeReason,
    MilestoneDocumentRef,
    ReleaseMilestoneArgs,
    SubmitMilestoneRequest,
)
from bido_back.payments.milestones.state import (
    FUNDED,
    active_documents,
    documents_match,
    earlier_released,
    ensure_milestones,
    evidence_hash,
    factory_status,
    milestone_status,
    submittable,
)
from bido_back.payments.milestones.views import (
    DocumentRequirementView,
    FactoryDocumentView,
    FactoryMilestoneView,
    FactoryOrderView,
    FactoryRequirementView,
    MilestoneSubmissionView,
    MilestoneSubmitted,
    PaymentMilestoneView,
    SubmissionState,
)
from bido_back.rfqs.audit import rfq_event
from bido_back.settings import Settings
from bido_back.worker.jobs import JobQueue

logger = logging.getLogger(__name__)

CHARGE_CURRENCY: Final = "USD"
# What a release approval may be when its submission is recorded: still waiting, or
# decided in the instant between the proposal and the record.
_RECORDABLE: Final = ("pending", "approved", "rejected")
PAYMENT_STATES_WITH_RESERVATION: Final = ("funded",)


# --- Reading -----------------------------------------------------------------------------------


async def payment_id_for(db: Db, principal: Principal, po_id: UUID) -> UUID:
    """The PO's current payment (live, else latest) in the caller's tenant; `PaymentNotFound`."""
    async with tenant_session(db, principal.tenant_id) as session:
        return (await purchase_order_payment(session, po_id)).id


async def _submissions_by_milestone(
    session: AsyncSession, milestone_ids: Sequence[UUID]
) -> dict[UUID, list[PoMilestoneSubmission]]:
    """Each milestone's submissions, newest first."""
    found: dict[UUID, list[PoMilestoneSubmission]] = {m: [] for m in milestone_ids}
    if not milestone_ids:
        return found
    rows = await session.scalars(
        select(PoMilestoneSubmission)
        .where(PoMilestoneSubmission.milestone_id.in_(list(milestone_ids)))
        .order_by(PoMilestoneSubmission.attempt.desc())
    )
    for row in rows:
        found.setdefault(row.milestone_id, []).append(row)
    return found


async def _documents_by_submission(
    session: AsyncSession, submission_ids: Iterable[UUID]
) -> dict[UUID, list[PoMilestoneDocument]]:
    """Each submission's files not removed, oldest first."""
    wanted = list(submission_ids)
    found: dict[UUID, list[PoMilestoneDocument]] = {s: [] for s in wanted}
    if not wanted:
        return found
    rows = await session.scalars(
        select(PoMilestoneDocument)
        .where(
            PoMilestoneDocument.submission_id.in_(wanted),
            PoMilestoneDocument.removed_at.is_(None),
        )
        .order_by(PoMilestoneDocument.created_at, PoMilestoneDocument.id)
    )
    for row in rows:
        found.setdefault(row.submission_id, []).append(row)
    return found


def _latest_sent(submissions: Sequence[PoMilestoneSubmission]) -> PoMilestoneSubmission | None:
    """The newest submission that is not a draft (submissions come newest first)."""
    return next((s for s in submissions if s.state != "draft"), None)


def _definition(milestone: PoPaymentMilestone) -> MilestoneDefinition:
    return definition_for(milestone.key, milestone.name, milestone.bps)


def _requirements(definition: MilestoneDefinition) -> list[DocumentRequirementView]:
    return [
        DocumentRequirementView(
            doc_type=d.code,
            label=d.label,
            label_zh=d.label_zh,
            required=d.required,
            accepts=list(d.accepts),
            description=d.description,
        )
        for d in definition.documents
    ]


def _submission_view(
    row: PoMilestoneSubmission, documents: Sequence[PoMilestoneDocument]
) -> MilestoneSubmissionView:
    return MilestoneSubmissionView(
        id=row.id,
        attempt=row.attempt,
        state=cast(SubmissionState, row.state),
        source="factory" if row.source == "factory" else "buyer",
        note=row.note,
        evidence_hash=row.evidence_hash,
        approval_id=row.approval_id,
        rejection_code=row.rejection_code,
        rejection_reason=row.rejection_reason,
        submitted_at=row.submitted_at,
        decided_at=row.decided_at,
        documents=[document_view(d) for d in documents],
    )


async def _milestone_views(
    session: AsyncSession,
    payment: PoPayment,
    milestones: Sequence[PoPaymentMilestone],
    indexes: Iterable[int],
    *,
    settings: Settings,
) -> list[PaymentMilestoneView]:
    chosen = [milestones[i] for i in indexes]
    submissions = await _submissions_by_milestone(session, [m.id for m in chosen])
    documents = await _documents_by_submission(
        session, (s.id for found in submissions.values() for s in found)
    )
    shares = [m.bps for m in milestones]
    po_amounts = decimal_shares(payment.po_total, shares)
    charge_amounts = decimal_shares(payment.charge_amount, shares)
    decimals = settings.solana_stablecoin_decimals
    views: list[PaymentMilestoneView] = []
    for row in chosen:
        index = row.milestone_index
        definition = _definition(row)
        found = submissions.get(row.id, [])
        signature = row.release_signature
        views.append(
            PaymentMilestoneView(
                index=index,
                key=row.key,
                name=row.name,
                label=definition.label,
                bps=row.bps,
                po_currency=payment.po_currency,
                po_amount=po_amounts[index],
                charge_currency=payment.charge_currency,
                charge_amount=charge_amounts[index],
                token_amount=row.token_amount.scaleb(-decimals),
                status=milestone_status(payment, milestones, index, _latest_sent(found)),
                requirements=_requirements(definition),
                checks=list(definition.checks),
                submissions=[_submission_view(s, documents.get(s.id, [])) for s in found],
                approval_id=row.approval_id,
                evidence_hash=row.evidence_hash,
                release_signature=signature,
                release_explorer_url=(
                    explorer_tx_url(settings.solana_cluster, signature) if signature else None
                ),
                released_at=row.released_at,
                failure_code=row.failure_code,
            )
        )
    return views


async def milestone_view(
    session: AsyncSession,
    payment: PoPayment,
    milestones: Sequence[PoPaymentMilestone],
    index: int,
    *,
    settings: Settings,
) -> PaymentMilestoneView:
    """One milestone as the buyer sees it (`milestones`: the payment's, by index)."""
    [view] = await _milestone_views(session, payment, milestones, [index], settings=settings)
    return view


async def list_milestones(
    db: Db, principal: Principal, po_id: UUID, *, settings: Settings
) -> list[PaymentMilestoneView]:
    """The milestones of the PO's current payment, in order (any member)."""
    async with tenant_session(db, principal.tenant_id) as session:
        payment = await purchase_order_payment(session, po_id)
        milestones = await ensure_milestones(session, payment)
        return await _milestone_views(
            session, payment, milestones, range(len(milestones)), settings=settings
        )


# --- The factory's page ------------------------------------------------------------------------


async def _buyer_name(session: AsyncSession, tenant_id: UUID) -> str:
    """The buyer as its documents name it: the trade or legal name, else the company's."""
    branding = await session.get(CompanyBranding, tenant_id)
    if branding is not None and (branding.trade_name or branding.legal_name):
        return branding.trade_name or branding.legal_name or ""
    tenant = await session.get(Tenant, tenant_id)
    return tenant.name if tenant is not None else ""


def _shown_to_factory(
    milestone: PoPaymentMilestone, submissions: Sequence[PoMilestoneSubmission]
) -> PoMilestoneSubmission | None:
    """Whose files the factory sees: the submission under review or released, else the draft."""
    if milestone.submission_id is not None:
        return next((s for s in submissions if s.id == milestone.submission_id), None)
    return next((s for s in submissions if s.state == "draft"), None)


def _factory_document(row: PoMilestoneDocument) -> FactoryDocumentView:
    return FactoryDocumentView(
        id=row.id,
        doc_type=row.doc_type,
        filename=row.filename,
        size_bytes=row.size_bytes,
        uploaded_at=row.created_at,
    )


async def _factory_milestones(
    session: AsyncSession, payment: PoPayment, milestones: Sequence[PoPaymentMilestone]
) -> list[FactoryMilestoneView]:
    submissions = await _submissions_by_milestone(session, [m.id for m in milestones])
    shown = {m.id: _shown_to_factory(m, submissions.get(m.id, [])) for m in milestones}
    documents = await _documents_by_submission(
        session, (s.id for s in shown.values() if s is not None)
    )
    amounts = decimal_shares(payment.po_total, [m.bps for m in milestones])
    views: list[FactoryMilestoneView] = []
    for row in milestones:
        index = row.milestone_index
        definition = _definition(row)
        latest = _latest_sent(submissions.get(row.id, []))
        status = factory_status(milestone_status(payment, milestones, index, latest))
        refused = latest if status == "changes_requested" else None
        submission = shown[row.id]
        files = documents.get(submission.id, []) if submission is not None else []
        views.append(
            FactoryMilestoneView(
                index=index,
                key=row.key,
                label=definition.label,
                label_zh=definition.label_zh,
                bps=row.bps,
                amount=amounts[index],
                status=status,
                rejection_code=refused.rejection_code if refused is not None else None,
                rejection_reason=refused.rejection_reason if refused is not None else None,
                requirements=[
                    FactoryRequirementView(
                        doc_type=d.code,
                        label=d.label,
                        label_zh=d.label_zh,
                        required=d.required,
                        accepts=list(d.accepts),
                        uploaded=[_factory_document(f) for f in files if f.doc_type == d.code],
                    )
                    for d in definition.documents
                ],
                submitted_at=submission.submitted_at if submission is not None else None,
                released_at=row.released_at,
            )
        )
    return views


async def factory_order(db: Db, access: FactoryAccess, *, settings: Settings) -> FactoryOrderView:
    """The factory's order as its page shows it. `FactoryLinkInvalid` once the link stops."""
    async with tenant_session(db, access.tenant_id) as session:
        link = await active_link(session, access)
        payment = await session.get(PoPayment, access.payment_id)
        po = (
            await session.get(IssuedPurchaseOrder, payment.purchase_order_id)
            if payment is not None
            else None
        )
        if payment is None or po is None:
            raise FactoryLinkInvalid(INVALID_LINK)
        supplier_name = await session.scalar(
            select(Supplier.name).where(Supplier.id == po.supplier_id)
        )
        milestones = await ensure_milestones(session, payment)
        return FactoryOrderView(
            purchase_order_ref=po.ref,
            buyer_name=await _buyer_name(session, access.tenant_id),
            supplier_name=supplier_name,
            currency=payment.po_currency,
            total=payment.po_total,
            reserved=payment.state in PAYMENT_STATES_WITH_RESERVATION,
            accepted_at=link.accepted_at,
            milestones=await _factory_milestones(session, payment, milestones),
            link_expires_at=link.expires_at,
        )


async def factory_milestone(
    db: Db, access: FactoryAccess, index: int, *, settings: Settings
) -> FactoryMilestoneView:
    """One milestone of the factory's order (`PaymentNotFound` for an unknown index)."""
    order = await factory_order(db, access, settings=settings)
    found = next((m for m in order.milestones if m.index == index), None)
    if found is None:
        raise PaymentNotFound(f"payment {access.payment_id} has no milestone {index}")
    return found


# --- Sending a milestone for approval ----------------------------------------------------------


def _check_submittable(
    payment: PoPayment, milestones: Sequence[PoPaymentMilestone], index: int
) -> None:
    """Only the first unreleased milestone of a funded escrow is sent (`state.submittable`)."""
    milestone = milestones[index]
    if payment.state != FUNDED:
        raise PaymentInvalidState(
            f"payment {payment.id} is {payment.state}: milestones are sent while it is funded"
        )
    if milestone.state == "submitted":
        raise PaymentInvalidState(f"milestone {index} is already waiting for approval")
    if milestone.state != "open":
        raise PaymentInvalidState(f"milestone {index} is {milestone.state}")
    if not earlier_released(milestones, index):
        raise PaymentInvalidState(
            f"milestone {index} is sent once milestone {index - 1} is released"
        )
    if not submittable(payment, milestones, index):
        raise PaymentInvalidState(f"milestone {index} cannot be sent now")


def _release_args(
    payment: PoPayment,
    po: IssuedPurchaseOrder,
    milestones: Sequence[PoPaymentMilestone],
    index: int,
    draft: PoMilestoneSubmission,
    documents: Sequence[PoMilestoneDocument],
) -> ReleaseMilestoneArgs:
    """Exactly what the approver approves: this milestone of this escrow, these files."""
    milestone = milestones[index]
    if payment.escrow_address is None or payment.charge_currency != CHARGE_CURRENCY:
        raise PaymentInvalidState(f"payment {payment.id} has no escrow to release from")
    shares = [m.bps for m in milestones]
    return ReleaseMilestoneArgs(
        payment_id=payment.id,
        purchase_order_id=po.id,
        purchase_order_ref=po.ref,
        rfq_id=po.rfq_id,
        invite_id=po.invite_id,
        supplier_id=po.supplier_id,
        requested_by=payment.requested_by,
        milestone_id=milestone.id,
        milestone_index=index,
        milestone_key=milestone.key,
        milestone_name=milestone.name,
        bps=milestone.bps,
        token_amount=str(int(milestone.token_amount)),
        po_currency=payment.po_currency,
        po_amount=format_decimal(decimal_shares(payment.po_total, shares)[index], 2),
        charge_currency=CHARGE_CURRENCY,
        charge_amount=format_decimal(decimal_shares(payment.charge_amount, shares)[index], 2),
        chain_po_id=payment.chain_po_id,
        escrow_address=payment.escrow_address,
        content_hash=payment.po_hash,
        submission_id=draft.id,
        attempt=draft.attempt,
        evidence_hash=evidence_hash(payment.chain_po_id, index, documents),
        documents=[
            MilestoneDocumentRef(
                document_id=d.id,
                doc_type=d.doc_type,
                filename=d.filename,
                size_bytes=d.size_bytes,
                sha256=d.sha256,
            )
            for d in documents
        ],
        simulated=payment.charge_mode == "simulated",
    )


async def _proposed_before(session: AsyncSession, call_id: str) -> int:
    """Release proposals of this milestone so far, whatever became of them: the next one's
    attempt, so a withdrawn proposal never blocks the next."""
    count = await session.scalar(
        select(func.count())
        .select_from(Approval)
        .where(Approval.tool == RELEASE_PO_MILESTONE, Approval.tool_call_id == call_id)
    )
    return count or 0


async def _propose(
    db: Db,
    tenant_id: UUID,
    args: ReleaseMilestoneArgs,
    *,
    ids: tuple[UUID, str],
    session_id: str,
) -> UUID:
    run_id, call_id = ids
    payload = cast(dict[str, JsonValue], args.model_dump(mode="json"))
    try:
        await PostgresApprovalSink(db).request_approval(
            ApprovalProposal(
                tenant_id=tenant_id,
                run_id=run_id,
                session_id=session_id,
                tool_call_id=call_id,
                tool=RELEASE_PO_MILESTONE,
                args=payload,
                args_hash=args_hash(payload),
                requested_by=args.requested_by,
            )
        )
    except ApprovalConflict:
        raise PaymentInvalidState(
            f"milestone {args.milestone_index} is being sent already; try again"
        ) from None
    async with tenant_session(db, tenant_id) as session:
        approval_id = await session.scalar(
            select(Approval.id).where(
                Approval.run_id == str(run_id), Approval.tool_call_id == call_id
            )
        )
    if approval_id is None:
        raise PaymentInvalidState(f"{call_id}: the approval was not recorded")
    return approval_id


async def _record_submission(
    session: AsyncSession,
    submitter: Uploader,
    args: ReleaseMilestoneArgs,
    approval_id: UUID,
    note: str | None,
    *,
    settings: Settings,
) -> PaymentMilestoneView:
    """The draft becomes the submission under review with this approval (or the rejected
    one, when a person already refused it). Re-checks everything the proposal assumed."""
    index = args.milestone_index
    # The approval first, as the guard and the executor lock it: a decision made in the
    # meantime is committed before it is read.
    approval = await session.scalar(
        select(Approval).where(Approval.id == approval_id).with_for_update()
    )
    if approval is None:
        raise PaymentInvalidState(f"approval {approval_id} not found")
    payment, milestones = await locked_milestones(session, args.payment_id, index)
    milestone = milestones[index]
    if milestone.state == "submitted" and milestone.approval_id == approval_id:
        # The same proposal, recorded by a repeated request (a double tap).
        return await milestone_view(session, payment, milestones, index, settings=settings)
    if approval.status not in _RECORDABLE:
        raise PaymentInvalidState(
            f"approval {approval_id} is {approval.status}: send milestone {index} again"
        )
    await uploader_link(session, submitter, args.payment_id)
    _check_submittable(payment, milestones, index)
    submission = await session.get(PoMilestoneSubmission, args.submission_id)
    documents = await active_documents(session, args.submission_id)
    if (
        submission is None
        or submission.milestone_id != milestone.id
        or submission.state != "draft"
        or not documents_match(args, documents)
    ):
        raise PaymentInvalidState(
            f"milestone {index}'s documents changed while it was being sent; send it again"
        )
    now = datetime.now(UTC)
    factory = submitter if isinstance(submitter, FactoryAccess) else None
    submission.state = "submitted"
    submission.evidence_hash = args.evidence_hash
    submission.approval_id = approval_id
    submission.submitted_at = now
    submission.note = note
    submission.source = source_of(submitter)
    submission.factory_link_id = factory.link_id if factory is not None else None
    submission.submitted_by = actor_of(submitter)
    submission.updated_at = now
    session.add(
        rfq_event(
            payment.tenant_id,
            actor_of(submitter),
            MILESTONE_SUBMITTED,
            args.rfq_id,
            payment_id=payment.id,
            milestone_index=index,
            milestone_key=milestone.key,
            submission_id=submission.id,
            approval_id=approval_id,
            attempt=submission.attempt,
            document_count=len(documents),
            source=submission.source,
            factory_link_id=submission.factory_link_id,
            purchase_order_id=args.purchase_order_id,
            purchase_order_ref=args.purchase_order_ref,
        )
    )
    if approval.status == "rejected":
        # Refused before this row existed: the reject hook had nothing to send back.
        submission.state = "rejected"
        submission.decided_by = approval.decided_by
        submission.decided_at = approval.decided_at
        session.add(
            rfq_event(
                payment.tenant_id,
                approval.decided_by,
                MILESTONE_CHANGES_REQUESTED,
                args.rfq_id,
                payment_id=payment.id,
                milestone_index=index,
                submission_id=submission.id,
                approval_id=approval_id,
                purchase_order_id=args.purchase_order_id,
                purchase_order_ref=args.purchase_order_ref,
            )
        )
    else:
        milestone.state = "submitted"
        milestone.approval_id = approval_id
        milestone.submission_id = submission.id
        milestone.evidence_hash = args.evidence_hash
        milestone.failure_code = None
        milestone.updated_at = now
    await session.flush()
    return await milestone_view(session, payment, milestones, index, settings=settings)


async def _withdraw(db: Db, tenant_id: UUID, approval_id: UUID) -> None:
    """Expire a release approval whose submission could not be recorded.

    Nobody may approve a release Bido has no submission for. Should the submission exist
    after all (the commit failed only on the way back), it goes back to being the draft and
    the milestone to `open`, so the files can be sent again. Best effort: the caller is
    already failing, and a person can still reject the approval.
    """
    try:
        async with tenant_session(db, tenant_id) as session:
            expired = list(
                await session.scalars(
                    update(Approval)
                    .where(Approval.id == approval_id, Approval.status == "pending")
                    .values(status="expired")
                    .returning(Approval)
                )
            )
            for approval in expired:
                session.add(approval_audit(approval, "approval.expired", None))
            await session.flush()
        if not expired:
            return
        async with tenant_session(db, tenant_id) as session:
            recorded = await session.scalar(
                select(PoPaymentMilestone).where(
                    PoPaymentMilestone.approval_id == approval_id,
                    PoPaymentMilestone.state == "submitted",
                )
            )
            if recorded is None:
                return
            _, milestones = await locked_milestones(
                session, recorded.payment_id, recorded.milestone_index
            )
            milestone = milestones[recorded.milestone_index]
            if milestone.state != "submitted" or milestone.approval_id != approval_id:
                return
            submission = (
                await session.get(PoMilestoneSubmission, milestone.submission_id)
                if milestone.submission_id is not None
                else None
            )
            now = datetime.now(UTC)
            if submission is not None and submission.state == "submitted":
                submission.state = "draft"
                submission.evidence_hash = None
                submission.approval_id = None
                submission.submitted_at = None
                submission.updated_at = now
            milestone.state = "open"
            milestone.approval_id = None
            milestone.submission_id = None
            milestone.evidence_hash = None
            milestone.updated_at = now
            await session.flush()
    except SQLAlchemyError as exc:
        logger.error(
            "milestone release approval %s not withdrawn: %s", approval_id, type(exc).__name__
        )


async def submit_milestone(
    db: Db,
    submitter: Uploader,
    payment_id: UUID,
    index: int,
    body: SubmitMilestoneRequest,
    *,
    settings: Settings,
) -> MilestoneSubmitted:
    """Send milestone `index`'s draft for approval: a pending `release_po_milestone`.

    `PaymentsNotConfigured` without releases on this server; `PaymentInvalidState` unless
    it is the next milestone to release of a funded payment (and, for the factory, its link
    confirmed); `PaymentRefused` naming the required document types still missing.
    """
    if not releases_enabled(settings):
        raise PaymentsNotConfigured("milestone releases are not set up on this server")
    tenant_id = submitter.tenant_id
    async with tenant_session(db, tenant_id) as session:
        payment, milestones = await locked_milestones(session, payment_id, index)
        milestone = milestones[index]
        await uploader_link(session, submitter, payment_id)
        _check_submittable(payment, milestones, index)
        draft = await draft_submission(session, milestone.id)
        documents = await active_documents(session, draft.id) if draft is not None else []
        missing = _definition(milestone).required_codes - {d.doc_type for d in documents}
        if draft is None or not documents or missing:
            raise PaymentRefused(
                f"milestone {index} needs these documents first: {', '.join(sorted(missing))}"
                if missing
                else f"milestone {index} has no documents to send"
            )
        po = await session.get(IssuedPurchaseOrder, payment.purchase_order_id)
        if po is None:
            raise PaymentNotFound(f"payment {payment_id}: its purchase order is gone")
        args = _release_args(payment, po, milestones, index, draft, documents)
        call_id = release_proposal_ids(payment_id, index, 0)[1]
        ids = release_proposal_ids(payment_id, index, await _proposed_before(session, call_id))

    approval_id = await _propose(db, tenant_id, args, ids=ids, session_id=f"rfq:{args.rfq_id}")
    try:
        async with tenant_session(db, tenant_id) as session:
            view = await _record_submission(
                session, submitter, args, approval_id, body.note, settings=settings
            )
    except Exception as exc:
        # No approval may wait for a submission that was not recorded.
        await _withdraw(db, tenant_id, approval_id)
        if isinstance(exc, IntegrityError):
            raise PaymentInvalidState(
                f"milestone {index} changed while it was being sent; send it again"
            ) from None
        raise
    logger.info(
        "milestone submitted tenant=%s payment=%s milestone=%d approval=%s attempt=%d source=%s",
        tenant_id,
        payment_id,
        index,
        approval_id,
        args.attempt,
        source_of(submitter),
    )
    return MilestoneSubmitted(approval_id=approval_id, milestone=view)


# --- Sending a milestone back ------------------------------------------------------------------


async def request_milestone_changes(
    db: Db,
    principal: Principal,
    po_id: UUID,
    index: int,
    reason: str,
    *,
    reason_code: ChangeReason = "other",
    jobs: JobQueue,
    settings: Settings,
) -> PaymentMilestoneView:
    """Refuse the release of milestone `index` under review, telling the factory why.

    Decided through the approval guard (whoever may decide the approval: the requester
    when the company allows self-approval, else an approver or owner who is not), so
    `PaymentForbidden` for anyone else; `PaymentInvalidState` unless it is under review.
    The reason is stored with the decision, in the same transaction.
    """
    async with tenant_session(db, principal.tenant_id) as session:
        payment = await purchase_order_payment(session, po_id)
        milestones = await ensure_milestones(session, payment)
        if not 0 <= index < len(milestones):
            raise PaymentNotFound(f"payment {payment.id} has no milestone {index}")
        milestone = milestones[index]
        if milestone.state != "submitted" or milestone.approval_id is None:
            raise PaymentInvalidState(f"milestone {index} is not waiting for approval")
        approval_id = milestone.approval_id
        payment_id = payment.id

    send_back = MILESTONE_REJECT_HOOKS[RELEASE_PO_MILESTONE]

    async def send_back_with_reason(session: AsyncSession, approval: Approval) -> None:
        await send_back(session, approval)
        await session.execute(
            update(PoMilestoneSubmission)
            .where(
                PoMilestoneSubmission.approval_id == approval.id,
                PoMilestoneSubmission.state == "rejected",
            )
            .values(rejection_code=reason_code, rejection_reason=reason, updated_at=func.now())
            .execution_options(synchronize_session=False)
        )

    hooks: dict[str, RejectHook] = {
        **MILESTONE_REJECT_HOOKS,
        RELEASE_PO_MILESTONE: send_back_with_reason,
    }
    try:
        await decide_approval(db, principal, approval_id, "reject", jobs=jobs, on_reject=hooks)
    except ApprovalForbidden as exc:
        raise PaymentForbidden(str(exc)) from None
    except (ApprovalNotPending, ApprovalNotFound, ApprovalIntegrityError):
        raise PaymentInvalidState(
            f"milestone {index} can no longer be sent back: its approval was decided"
        ) from None
    logger.info(
        "milestone changes requested tenant=%s payment=%s milestone=%d approval=%s code=%s",
        principal.tenant_id,
        payment_id,
        index,
        approval_id,
        reason_code,
    )
    async with tenant_session(db, principal.tenant_id) as session:
        payment = await session.get(PoPayment, payment_id)
        if payment is None:
            raise PaymentNotFound(f"payment {payment_id} not found")
        milestones = await ensure_milestones(session, payment)
        return await milestone_view(session, payment, milestones, index, settings=settings)
