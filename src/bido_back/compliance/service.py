"""Supplier compliance in the caller's tenant: the requirement set, checklists, documents.

Everything runs in one `tenant_session` per call, so RLS keeps every read and
write inside the caller's tenant. Editing the requirement set needs the
approver or owner role, re-read from `memberships`; recording a document is
open to any member. Both write an audit event with ids only.
"""

from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from typing import cast
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.auth import Principal
from bido_back.compliance.checklist import (
    DocumentRecord,
    LineInfo,
    ReceivedAttachment,
    SupplierInfo,
    build_items,
    counts_of,
    expiry_of,
    summarize,
)
from bido_back.compliance.defaults import DEFAULT_REQUIREMENTS
from bido_back.compliance.schemas import (
    DocumentView,
    RecordDocumentRequest,
    Requirement,
    RequirementScope,
    RequirementsView,
    SupplierChecklist,
    SupplierComplianceSummary,
    UpdateRequirementsRequest,
)
from bido_back.db import tenant_session
from bido_back.db.models import (
    Attachment,
    AuditEvent,
    ComplianceRequirement,
    Invite,
    Membership,
    Message,
    Rfq,
    RfqLine,
    Supplier,
    SupplierComplianceDocument,
)
from bido_back.rfqs.errors import NotFound

Db = AsyncEngine | async_sessionmaker[AsyncSession]

EDITOR_ROLES = frozenset({"approver", "owner"})


class ComplianceError(Exception):
    """Base class for refusals. Messages carry ids and reasons, never content."""


class ComplianceForbidden(ComplianceError):
    """The caller's role may not do this."""


class InvalidDocument(ComplianceError):
    """The document does not fit the requirement, the supplier or the line."""


# --- Loading ---------------------------------------------------------------------------


async def _requirements(session: AsyncSession) -> tuple[bool, list[Requirement]]:
    rows = list(
        await session.scalars(
            select(ComplianceRequirement).order_by(
                ComplianceRequirement.position, ComplianceRequirement.code
            )
        )
    )
    if not rows:
        return False, list(DEFAULT_REQUIREMENTS)
    return True, [
        Requirement(
            code=row.code,
            name=row.name,
            scope=cast(RequirementScope, row.scope),
            line_keyword=row.line_keyword,
            keywords=[str(k) for k in row.keywords],
            validity_months=row.validity_months,
            blocking=row.blocking,
        )
        for row in rows
    ]


async def _documents(
    session: AsyncSession, supplier_ids: Iterable[UUID]
) -> dict[UUID, list[DocumentRecord]]:
    rows = await session.execute(
        select(SupplierComplianceDocument, Attachment.filename)
        .outerjoin(Attachment, Attachment.id == SupplierComplianceDocument.attachment_id)
        .where(SupplierComplianceDocument.supplier_id.in_(list(supplier_ids)))
    )
    found: dict[UUID, list[DocumentRecord]] = defaultdict(list)
    for doc, filename in rows:
        found[doc.supplier_id].append(_record(doc, filename))
    return found


def _record(doc: SupplierComplianceDocument, filename: str | None) -> DocumentRecord:
    return DocumentRecord(
        id=doc.id,
        requirement_code=doc.requirement_code,
        rfq_line_id=doc.rfq_line_id,
        attachment_id=doc.attachment_id,
        filename=filename,
        url=doc.url,
        issued_on=doc.issued_on,
        expires_on=doc.expires_on,
        recorded_at=doc.created_at,
    )


async def _attachments(
    session: AsyncSession, supplier_ids: Iterable[UUID]
) -> dict[UUID, list[ReceivedAttachment]]:
    rows = await session.execute(
        select(
            Attachment.id,
            Attachment.filename,
            Invite.rfq_id,
            Message.occurred_at,
            Invite.supplier_id,
        )
        .join(Message, Message.id == Attachment.message_id)
        .join(Invite, Invite.id == Message.invite_id)
        .where(Invite.supplier_id.in_(list(supplier_ids)), Message.direction == "in")
    )
    found: dict[UUID, list[ReceivedAttachment]] = defaultdict(list)
    for attachment_id, filename, rfq_id, occurred_at, supplier_id in rows:
        found[supplier_id].append(
            ReceivedAttachment(
                id=attachment_id, filename=filename, rfq_id=rfq_id, received_at=occurred_at
            )
        )
    return found


def _line_info(line: RfqLine) -> LineInfo:
    return LineInfo(
        id=line.id,
        rfq_id=line.rfq_id,
        position=line.position,
        mpn=line.mpn,
        qualification=line.qualification,
    )


async def _invited_lines(
    session: AsyncSession, supplier_id: UUID, rfq_id: UUID | None
) -> list[LineInfo]:
    """Lines of the RFQs this supplier is invited to (optionally one RFQ)."""
    invited = select(Invite.rfq_id).where(Invite.supplier_id == supplier_id)
    if rfq_id is not None:
        invited = invited.where(Invite.rfq_id == rfq_id)
    lines = await session.scalars(select(RfqLine).where(RfqLine.rfq_id.in_(invited)))
    return [_line_info(line) for line in lines]


async def _supplier(session: AsyncSession, supplier_id: UUID) -> Supplier:
    supplier = await session.scalar(select(Supplier).where(Supplier.id == supplier_id))
    if supplier is None:
        raise NotFound(f"supplier {supplier_id} not found")
    return supplier


def _info(supplier: Supplier) -> SupplierInfo:
    return SupplierInfo(id=supplier.id, name=supplier.name, type=supplier.type)


# --- Requirements ---------------------------------------------------------------------


async def get_requirements(db: Db, principal: Principal) -> RequirementsView:
    async with tenant_session(db, principal.tenant_id) as session:
        customized, requirements = await _requirements(session)
    return RequirementsView(customized=customized, requirements=requirements)


async def put_requirements(
    db: Db, principal: Principal, request: UpdateRequirementsRequest
) -> RequirementsView:
    """Replace the tenant's whole requirement set (an empty list goes back to the defaults)."""
    async with tenant_session(db, principal.tenant_id) as session:
        role = await session.scalar(
            select(Membership.role).where(
                Membership.tenant_id == principal.tenant_id,
                Membership.user_id == principal.user_id,
            )
        )
        if role not in EDITOR_ROLES:
            raise ComplianceForbidden("the approver or owner role is required")
        await session.execute(
            delete(ComplianceRequirement).where(
                ComplianceRequirement.tenant_id == principal.tenant_id
            )
        )
        for position, requirement in enumerate(request.requirements):
            session.add(
                ComplianceRequirement(
                    tenant_id=principal.tenant_id,
                    code=requirement.code,
                    name=requirement.name,
                    scope=requirement.scope,
                    line_keyword=requirement.line_keyword,
                    keywords=list(requirement.keywords),
                    validity_months=requirement.validity_months,
                    blocking=requirement.blocking,
                    position=position,
                )
            )
        session.add(
            AuditEvent(
                tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id,
                action="compliance.requirements_updated",
                payload={"codes": [r.code for r in request.requirements]},
            )
        )
        await session.flush()
        customized, requirements = await _requirements(session)
    return RequirementsView(customized=customized, requirements=requirements)


# --- Checklists ----------------------------------------------------------------------


async def supplier_checklist(
    db: Db, principal: Principal, supplier_id: UUID, today: date, rfq_id: UUID | None = None
) -> SupplierChecklist:
    async with tenant_session(db, principal.tenant_id) as session:
        supplier = await _supplier(session, supplier_id)
        if (
            rfq_id is not None
            and await session.scalar(select(Rfq.id).where(Rfq.id == rfq_id)) is None
        ):
            raise NotFound(f"rfq {rfq_id} not found")
        _, requirements = await _requirements(session)
        lines = await _invited_lines(session, supplier_id, rfq_id)
        documents = (await _documents(session, [supplier_id]))[supplier_id]
        attachments = (await _attachments(session, [supplier_id]))[supplier_id]
        info = _info(supplier)
    items = build_items(requirements, info, lines, documents, attachments, today)
    return SupplierChecklist(
        supplier_id=info.id,
        supplier_name=info.name,
        supplier_type=info.type,
        as_of=today,
        counts=counts_of(items),
        items=items,
    )


async def rfq_summary(
    db: Db, principal: Principal, rfq_id: UUID, today: date
) -> list[SupplierComplianceSummary]:
    """One summary per supplier invited to the RFQ, in invite order; [] for an unknown RFQ."""
    async with tenant_session(db, principal.tenant_id) as session:
        suppliers: dict[UUID, Supplier] = {}
        for supplier in await session.scalars(
            select(Supplier)
            .join(Invite, Invite.supplier_id == Supplier.id)
            .where(Invite.rfq_id == rfq_id)
            .order_by(Invite.created_at, Invite.id)
        ):
            suppliers.setdefault(supplier.id, supplier)
        if not suppliers:
            return []
        _, requirements = await _requirements(session)
        lines = [
            _line_info(line)
            for line in await session.scalars(select(RfqLine).where(RfqLine.rfq_id == rfq_id))
        ]
        documents = await _documents(session, suppliers)
        attachments = await _attachments(session, suppliers)
        infos = [_info(s) for s in suppliers.values()]
    return [
        summarize(
            info,
            build_items(requirements, info, lines, documents[info.id], attachments[info.id], today),
        )
        for info in infos
    ]


# --- Documents -----------------------------------------------------------------------


async def record_document(
    db: Db, principal: Principal, supplier_id: UUID, request: RecordDocumentRequest
) -> DocumentView:
    """Confirm a document for one requirement of one supplier (and line)."""
    async with tenant_session(db, principal.tenant_id) as session:
        await _supplier(session, supplier_id)
        _, requirements = await _requirements(session)
        requirement = next((r for r in requirements if r.code == request.requirement_code), None)
        if requirement is None:
            raise InvalidDocument(f"unknown requirement {request.requirement_code}")

        if requirement.scope == "line":
            if request.rfq_line_id is None:
                raise InvalidDocument(f"requirement {requirement.code} needs rfq_line_id")
            lines = await _invited_lines(session, supplier_id, None)
            if request.rfq_line_id not in {line.id for line in lines}:
                raise NotFound(f"rfq line {request.rfq_line_id} not found for this supplier")
        elif request.rfq_line_id is not None:
            raise InvalidDocument(f"requirement {requirement.code} is not per line")

        filename: str | None = None
        if request.attachment_id is not None:
            filename = await session.scalar(
                select(Attachment.filename)
                .join(Message, Message.id == Attachment.message_id)
                .join(Invite, Invite.id == Message.invite_id)
                .where(
                    Attachment.id == request.attachment_id,
                    Invite.supplier_id == supplier_id,
                    Message.direction == "in",
                )
            )
            if filename is None:
                raise NotFound(f"attachment {request.attachment_id} not found for this supplier")

        doc = SupplierComplianceDocument(
            tenant_id=principal.tenant_id,
            supplier_id=supplier_id,
            requirement_code=requirement.code,
            rfq_line_id=request.rfq_line_id,
            attachment_id=request.attachment_id,
            url=request.url,
            issued_on=request.issued_on,
            expires_on=request.expires_on,
            recorded_by=principal.user_id,
        )
        session.add(doc)
        await session.flush()
        await session.refresh(doc)
        session.add(
            AuditEvent(
                tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id,
                action="compliance.document_recorded",
                payload={
                    "supplier_id": str(supplier_id),
                    "document_id": str(doc.id),
                    "requirement_code": requirement.code,
                },
            )
        )
        record = _record(doc, filename)
    return DocumentView(
        id=record.id,
        requirement_code=record.requirement_code,
        rfq_line_id=record.rfq_line_id,
        attachment_id=record.attachment_id,
        filename=record.filename,
        url=record.url,
        issued_on=record.issued_on,
        expires_on=expiry_of(record, requirement),
        recorded_at=record.recorded_at,
    )
