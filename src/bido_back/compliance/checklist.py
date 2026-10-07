"""Build one supplier's compliance checklist. Pure: no database, no model.

A requirement applies to a supplier by scope:
  * `supplier`: always;
  * `manufacturer`: when the supplier's type is `manufacturer`;
  * `line`: once per RFQ line (of the RFQs the supplier is invited to) whose
    qualification text mentions the requirement's `line_keyword`.

Status: `received` when the document in force (the latest confirmed one) has
not expired, `expired` when it has, `missing` when there is none. Expiry is
the document's own date, else its issue date (or the day it was recorded)
plus the requirement's `validity_months`, else never.

Candidates are attachments the supplier already sent whose filename matches
one of the requirement's keywords (letters and digits only, case-insensitive,
so "AEC-Q200_report.pdf" matches "aec-q"). They are suggestions for a person
to confirm, never a status on their own.
"""

import calendar
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from bido_back.compliance.schemas import (
    BlockingItem,
    CandidateDocument,
    ChecklistCounts,
    ChecklistItem,
    ChecklistStatus,
    DocumentView,
    Requirement,
    SupplierComplianceSummary,
)


@dataclass(frozen=True, slots=True)
class SupplierInfo:
    id: UUID
    name: str
    type: str


@dataclass(frozen=True, slots=True)
class LineInfo:
    id: UUID
    rfq_id: UUID
    position: int
    mpn: str | None
    qualification: str | None


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    id: UUID
    requirement_code: str
    rfq_line_id: UUID | None
    attachment_id: UUID | None
    filename: str | None
    url: str | None
    issued_on: date | None
    expires_on: date | None
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class ReceivedAttachment:
    id: UUID
    filename: str
    rfq_id: UUID | None
    received_at: datetime


def _alnum(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def filename_matches(filename: str, keywords: Sequence[str]) -> bool:
    name = _alnum(filename)
    return any(key and key in name for key in (_alnum(k) for k in keywords))


def line_requires(line: LineInfo, requirement: Requirement) -> bool:
    if requirement.scope != "line" or requirement.line_keyword is None:
        return False
    keyword = _alnum(requirement.line_keyword)
    return bool(keyword) and keyword in _alnum(line.qualification or "")


def add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year, month = day.year + month_index // 12, month_index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def expiry_of(document: DocumentRecord, requirement: Requirement) -> date | None:
    if document.expires_on is not None:
        return document.expires_on
    if requirement.validity_months is None:
        return None
    start = document.issued_on or document.recorded_at.date()
    return add_months(start, requirement.validity_months)


def _document_view(document: DocumentRecord, expires_on: date | None) -> DocumentView:
    return DocumentView(
        id=document.id,
        requirement_code=document.requirement_code,
        rfq_line_id=document.rfq_line_id,
        attachment_id=document.attachment_id,
        filename=document.filename,
        url=document.url,
        issued_on=document.issued_on,
        expires_on=expires_on,
        recorded_at=document.recorded_at,
    )


def _item(
    requirement: Requirement,
    documents: Sequence[DocumentRecord],
    attachments: Sequence[ReceivedAttachment],
    today: date,
    line: LineInfo | None,
) -> ChecklistItem:
    line_id = line.id if line else None
    in_force = max(
        (
            d
            for d in documents
            if d.requirement_code == requirement.code and d.rfq_line_id == line_id
        ),
        key=lambda d: (d.recorded_at, str(d.id)),
        default=None,
    )
    status: ChecklistStatus = "missing"
    document: DocumentView | None = None
    if in_force is not None:
        expires_on = expiry_of(in_force, requirement)
        status = "expired" if expires_on is not None and expires_on < today else "received"
        document = _document_view(in_force, expires_on)

    candidates: list[CandidateDocument] = []
    if status != "received":
        used = in_force.attachment_id if in_force else None
        candidates = [
            CandidateDocument(attachment_id=a.id, filename=a.filename, received_at=a.received_at)
            for a in sorted(attachments, key=lambda a: a.received_at, reverse=True)
            if a.id != used
            and (line is None or a.rfq_id == line.rfq_id)
            and filename_matches(a.filename, requirement.keywords)
        ]
    return ChecklistItem(
        code=requirement.code,
        name=requirement.name,
        scope=requirement.scope,
        blocking=requirement.blocking,
        status=status,
        rfq_id=line.rfq_id if line else None,
        rfq_line_id=line_id,
        line_position=line.position if line else None,
        line_mpn=line.mpn if line else None,
        document=document,
        candidates=candidates,
    )


def build_items(
    requirements: Sequence[Requirement],
    supplier: SupplierInfo,
    lines: Sequence[LineInfo],
    documents: Sequence[DocumentRecord],
    attachments: Sequence[ReceivedAttachment],
    today: date,
) -> list[ChecklistItem]:
    items: list[ChecklistItem] = []
    for requirement in requirements:
        match requirement.scope:
            case "supplier":
                items.append(_item(requirement, documents, attachments, today, None))
            case "manufacturer":
                if supplier.type == "manufacturer":
                    items.append(_item(requirement, documents, attachments, today, None))
            case "line":
                for line in sorted(lines, key=lambda line: (str(line.rfq_id), line.position)):
                    if line_requires(line, requirement):
                        items.append(_item(requirement, documents, attachments, today, line))
    return items


def counts_of(items: Sequence[ChecklistItem]) -> ChecklistCounts:
    return ChecklistCounts(
        received=sum(i.status == "received" for i in items),
        missing=sum(i.status == "missing" for i in items),
        expired=sum(i.status == "expired" for i in items),
    )


def summarize(supplier: SupplierInfo, items: Sequence[ChecklistItem]) -> SupplierComplianceSummary:
    return SupplierComplianceSummary(
        supplier_id=supplier.id,
        supplier_name=supplier.name,
        counts=counts_of(items),
        blocking=[
            BlockingItem(code=i.code, name=i.name, status=i.status, rfq_line_id=i.rfq_line_id)
            for i in items
            if i.blocking and i.status != "received"
        ],
    )
