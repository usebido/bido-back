"""A milestone's evidence files: sniffed, sized, stored once, gathered in a draft.

One file per request: the raw bytes, with the document type and the file's name. Checked
in this order, refusing early:

* size: empty -> `PaymentRefused` (422); over `milestone_document_max_bytes` ->
  `DocumentTooLarge` (413);
* kind: from the file's first bytes (`sniff_extension`: pdf, png, jpg, webp, xlsx, mp4,
  mov), never from the type the browser declared; the name's extension must agree (a name
  without one gets the kind's) -> `DocumentTypeRefused` (415);
* who: the factory through its active link, once it confirmed the order (409 before), or
  a person of the company (no confirmation needed);
* where: an open milestone of a funded payment (`state.accepts_documents`) -> 409; the
  document type is one the milestone asks for (`catalog.definition_for`) -> 422, and it
  accepts this kind -> 415;
* the draft: the milestone's `draft` submission, found or created under the payment and
  milestone locks. A new draft after a rejected submission starts with that submission's
  files (new rows, same stored files), so the factory replaces only what was refused.
  The same file again answers the document already there (double taps); the same file
  as another document type is refused (409); at most `MAX_DOCUMENTS` files (422).

Files are stored content-addressed: `milestones/{tenant}/{payment}/{index}/{sha256}.{ext}`.
Only a draft's files can be removed (`removed_at`; the stored file stays: copies share it).
Download links last 5 minutes; only PDFs and images are ever served inline. Audit events
and logs carry ids and the document type, never names or content.
"""

import hashlib
import io
import logging
import re
import zipfile
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import (
    IssuedPurchaseOrder,
    PoFactoryLink,
    PoMilestoneDocument,
    PoMilestoneSubmission,
    PoPayment,
    PoPaymentMilestone,
)
from bido_back.payments.errors import (
    PaymentInvalidState,
    PaymentNotFound,
    PaymentRefused,
)
from bido_back.payments.milestones.catalog import DocumentType, definition_for
from bido_back.payments.milestones.links import FactoryAccess, active_link, locked_payment
from bido_back.payments.milestones.proposals import (
    MILESTONE_DOCUMENT_ADDED,
    MILESTONE_DOCUMENT_REMOVED,
)
from bido_back.payments.milestones.state import (
    accepts_documents,
    active_documents,
    ensure_milestones,
    locked_milestone,
)
from bido_back.payments.milestones.views import DocumentSource, MilestoneDocumentView
from bido_back.rfqs.audit import rfq_event
from bido_back.settings import Settings
from bido_back.storage import BlobStorage
from bido_back.storage.links import Disposition, DownloadLinks
from bido_back.uploads.files import FALLBACK_TYPE, INLINE, LINK_TTL
from bido_back.uploads.schemas import FileLinkView

logger = logging.getLogger(__name__)

Db = AsyncEngine | async_sessionmaker[AsyncSession]
# Who gathers a milestone's documents: the factory through its link, or a person of the
# company on its behalf.
Uploader = FactoryAccess | Principal

# Files in one submission at most (removed ones do not count).
MAX_DOCUMENTS: Final = 30

# What each kind is served as (the extension in the storage key decides, never the
# browser's declared type).
CONTENT_TYPES: Final[dict[str, str]] = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "webp": "image/webp",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
}
# The file name extensions that agree with each kind: jpg and jpeg are one kind, and an
# ISO-BMFF video may carry any of the video names.
_AGREEING: Final[dict[str, frozenset[str]]] = {
    "pdf": frozenset({"pdf"}),
    "png": frozenset({"png"}),
    "jpg": frozenset({"jpg", "jpeg"}),
    "webp": frozenset({"webp"}),
    "xlsx": frozenset({"xlsx"}),
    "mp4": frozenset({"mp4", "m4v", "mov"}),
    "mov": frozenset({"mov", "mp4", "m4v"}),
}
_KIND_ALIASES: Final[dict[str, str]] = {"jpeg": "jpg"}

_PDF: Final = b"%PDF-"
# A PDF may have bytes before its header (readers look in the first kilobyte).
_PDF_WINDOW: Final = 1024
_PNG: Final = b"\x89PNG\r\n\x1a\n"
_JPEG: Final = b"\xff\xd8\xff"
_ZIP: Final = b"PK\x03\x04"
_WORKBOOK: Final = "xl/workbook.xml"
_FTYP: Final = b"ftyp"
_QUICKTIME: Final = b"qt  "
# ISO-BMFF brands of still images (HEIF, AVIF): photos, not videos.
_IMAGE_BRANDS: Final = frozenset(
    {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif", b"avis"}
)
_FILENAME_JUNK: Final = re.compile(r"[\x00-\x1f\x7f/\\]")
_FILENAME_CHARS: Final = 200


class DocumentTypeRefused(PaymentRefused):
    """The file is not a kind this document type accepts, or not what its name says (415)."""


class DocumentTooLarge(PaymentRefused):
    """The file is bigger than `milestone_document_max_bytes` (413)."""


# --- Kinds -------------------------------------------------------------------------------------


def sniff_extension(data: bytes) -> str | None:
    """The file's kind from its first bytes, as an extension (`pdf`, `png`, `jpg`, `webp`,
    `xlsx`, `mp4`, `mov`); None for anything else."""
    if data.startswith(_JPEG):
        return "jpg"
    if data.startswith(_PNG):
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == _FTYP:
        brand = data[8:12]
        if brand in _IMAGE_BRANDS or len(brand) < len(_QUICKTIME):
            return None
        return "mov" if brand == _QUICKTIME else "mp4"
    if data.startswith(_ZIP):
        return "xlsx" if _is_workbook(data) else None
    if _PDF in data[:_PDF_WINDOW]:
        return "pdf"
    return None


def _is_workbook(data: bytes) -> bool:
    """A zip holding an Excel workbook part (reads the archive's directory only)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return _WORKBOOK in archive.namelist()
    except Exception:  # a malformed archive is simply not a workbook
        return False


def _kind(value: str) -> str:
    return _KIND_ALIASES.get(value, value)


def accepts_kind(document_type: DocumentType, kind: str) -> bool:
    """Whether a document type takes files of this kind (jpg and jpeg are one kind)."""
    return _kind(kind) in {_kind(accepted) for accepted in document_type.accepts}


def clean_filename(filename: str) -> str:
    """The name as stored and shown: no control characters or path separators, at most
    200 characters (the end, which holds the extension), never empty."""
    name = _FILENAME_JUNK.sub("_", filename).strip()[-_FILENAME_CHARS:]
    return name or "file"


def _named(filename: str, kind: str) -> str:
    """The stored name: cleaned, with the kind's extension when the name has none;
    `DocumentTypeRefused` when the name says another kind."""
    name = clean_filename(filename)
    extension = PurePosixPath(name).suffix.lstrip(".").lower()
    if not extension:
        return f"{name}.{kind}"
    if extension not in _AGREEING[kind]:
        raise DocumentTypeRefused(f"the file's content ({kind}) does not match its name")
    return name


def _storage_key(tenant_id: UUID, payment_id: UUID, index: int, sha256: str, kind: str) -> str:
    return f"milestones/{tenant_id}/{payment_id}/{index}/{sha256}.{kind}"


# --- Shared ------------------------------------------------------------------------------------


def source_of(uploader: Uploader) -> DocumentSource:
    return "factory" if isinstance(uploader, FactoryAccess) else "buyer"


def actor_of(uploader: Uploader) -> UUID | None:
    """The person in audit events: None for the factory (not a user of the company)."""
    return None if isinstance(uploader, FactoryAccess) else uploader.user_id


async def locked_milestones(
    session: AsyncSession, payment_id: UUID, index: int
) -> tuple[PoPayment, list[PoPaymentMilestone]]:
    """The payment and its milestones by index; the payment, then milestone `index`, locked
    `FOR UPDATE` and read fresh. `PaymentNotFound` for an unknown payment or index."""
    payment = await locked_payment(session, payment_id)
    milestones = await ensure_milestones(session, payment)
    milestone = await locked_milestone(session, payment_id, index)
    # Loaded above before the lock: read it again under the lock.
    await session.refresh(milestone)
    return payment, milestones


async def uploader_link(
    session: AsyncSession, uploader: Uploader, payment_id: UUID
) -> PoFactoryLink | None:
    """For the factory: its link, active, for this payment and confirmed (409 before);
    None for a person of the company."""
    if not isinstance(uploader, FactoryAccess):
        return None
    if uploader.payment_id != payment_id:
        raise PaymentNotFound(f"payment {payment_id} not found")
    link = await active_link(session, uploader)
    if link.accepted_at is None:
        raise PaymentInvalidState("confirm the order before sending documents")
    return link


def check_accepts_documents(payment: PoPayment, milestone: PoPaymentMilestone, index: int) -> None:
    if payment.state != "funded":
        raise PaymentInvalidState(
            f"payment {payment.id} is {payment.state}: documents are gathered while it is funded"
        )
    if not accepts_documents(payment, milestone):
        raise PaymentInvalidState(
            f"milestone {index} is {milestone.state}: documents are gathered while it is open"
        )


async def draft_submission(
    session: AsyncSession, milestone_id: UUID
) -> PoMilestoneSubmission | None:
    """The milestone's draft (at most one), if any."""
    return await session.scalar(
        select(PoMilestoneSubmission).where(
            PoMilestoneSubmission.milestone_id == milestone_id,
            PoMilestoneSubmission.state == "draft",
        )
    )


async def _draft(
    session: AsyncSession,
    payment: PoPayment,
    milestone: PoPaymentMilestone,
    uploader: Uploader,
) -> PoMilestoneSubmission:
    """The milestone's draft; a new one starts with the files of a rejected predecessor.
    Call under the milestone's lock (one draft per milestone)."""
    existing = await draft_submission(session, milestone.id)
    if existing is not None:
        return existing
    previous = await session.scalar(
        select(PoMilestoneSubmission)
        .where(PoMilestoneSubmission.milestone_id == milestone.id)
        .order_by(PoMilestoneSubmission.attempt.desc())
        .limit(1)
    )
    factory = uploader if isinstance(uploader, FactoryAccess) else None
    draft = PoMilestoneSubmission(
        tenant_id=payment.tenant_id,
        payment_id=payment.id,
        milestone_id=milestone.id,
        milestone_index=milestone.milestone_index,
        attempt=previous.attempt + 1 if previous is not None else 1,
        state="draft",
        source=source_of(uploader),
        factory_link_id=factory.link_id if factory is not None else None,
    )
    session.add(draft)
    await session.flush()
    if previous is not None and previous.state == "rejected":
        # The refused attempt's files carry over: the factory replaces only what was refused.
        for document in await active_documents(session, previous.id):
            session.add(
                PoMilestoneDocument(
                    tenant_id=document.tenant_id,
                    payment_id=document.payment_id,
                    milestone_id=document.milestone_id,
                    submission_id=draft.id,
                    doc_type=document.doc_type,
                    filename=document.filename,
                    content_type=document.content_type,
                    size_bytes=document.size_bytes,
                    sha256=document.sha256,
                    storage_key=document.storage_key,
                    source=document.source,
                    factory_link_id=document.factory_link_id,
                    uploaded_by=document.uploaded_by,
                )
            )
        await session.flush()
    return draft


def document_view(row: PoMilestoneDocument) -> MilestoneDocumentView:
    return MilestoneDocumentView(
        id=row.id,
        doc_type=row.doc_type,
        filename=row.filename,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        source="factory" if row.source == "factory" else "buyer",
        uploaded_at=row.created_at,
    )


async def _purchase_order(session: AsyncSession, payment: PoPayment) -> IssuedPurchaseOrder:
    po = await session.get(IssuedPurchaseOrder, payment.purchase_order_id)
    if po is None:
        raise PaymentNotFound(f"payment {payment.id}: its purchase order is gone")
    return po


# --- Adding and removing -----------------------------------------------------------------------


async def add_document(
    db: Db,
    storage: BlobStorage,
    uploader: Uploader,
    payment_id: UUID,
    index: int,
    *,
    doc_type: str,
    filename: str,
    content_type: str,
    data: bytes,
    settings: Settings,
) -> MilestoneDocumentView:
    """Add one file to milestone `index`'s draft (see the module docstring for the checks).

    `content_type` is what the browser declared: never trusted, the file's first bytes
    decide its kind and the type it is served with.
    """
    if not data:
        raise PaymentRefused("the file is empty")
    if len(data) > settings.milestone_document_max_bytes:
        raise DocumentTooLarge(
            f"files are limited to {settings.milestone_document_max_bytes // (1024 * 1024)} MB"
        )
    kind = sniff_extension(data)
    if kind is None:
        raise DocumentTypeRefused(
            "unsupported file: send a PDF, a photo (JPEG, PNG, WebP), an Excel workbook "
            "(xlsx) or a video (MP4, MOV)"
        )
    name = _named(filename, kind)
    digest = hashlib.sha256(data).hexdigest()
    tenant_id = uploader.tenant_id
    async with tenant_session(db, tenant_id) as session:
        payment, milestones = await locked_milestones(session, payment_id, index)
        milestone = milestones[index]
        await uploader_link(session, uploader, payment_id)
        check_accepts_documents(payment, milestone, index)
        definition = definition_for(milestone.key, milestone.name, milestone.bps)
        document_type = definition.document(doc_type)
        if document_type is None:
            codes = ", ".join(d.code for d in definition.documents)
            raise PaymentRefused(
                f"milestone {index} does not take that document type; it takes: {codes}"
            )
        if not accepts_kind(document_type, kind):
            raise DocumentTypeRefused(
                f"{document_type.code} accepts {', '.join(document_type.accepts)}; "
                f"this file is {kind}"
            )
        draft = await _draft(session, payment, milestone, uploader)
        documents = await active_documents(session, draft.id)
        same = next((d for d in documents if d.sha256 == digest), None)
        if same is not None:
            if same.doc_type != document_type.code:
                raise PaymentInvalidState(
                    f"this file is already in milestone {index}'s documents as {same.doc_type}"
                )
            return document_view(same)  # the same file again: a double tap
        if len(documents) >= MAX_DOCUMENTS:
            raise PaymentRefused(
                f"a milestone takes at most {MAX_DOCUMENTS} files: remove one before adding"
            )
        key = _storage_key(tenant_id, payment_id, index, digest, kind)
        await storage.put(key, data, CONTENT_TYPES[kind])
        factory = uploader if isinstance(uploader, FactoryAccess) else None
        row = PoMilestoneDocument(
            tenant_id=tenant_id,
            payment_id=payment_id,
            milestone_id=milestone.id,
            submission_id=draft.id,
            doc_type=document_type.code,
            filename=name,
            content_type=CONTENT_TYPES[kind],
            size_bytes=len(data),
            sha256=digest,
            storage_key=key,
            source=source_of(uploader),
            factory_link_id=factory.link_id if factory is not None else None,
            uploaded_by=actor_of(uploader),
        )
        session.add(row)
        await session.flush()
        po = await _purchase_order(session, payment)
        session.add(
            rfq_event(
                tenant_id,
                actor_of(uploader),
                MILESTONE_DOCUMENT_ADDED,
                po.rfq_id,
                payment_id=payment_id,
                milestone_index=index,
                submission_id=draft.id,
                document_id=row.id,
                doc_type=row.doc_type,
                source=row.source,
                purchase_order_id=po.id,
                purchase_order_ref=po.ref,
            )
        )
        await session.flush()
        await session.refresh(row)
        view = document_view(row)
    logger.info(
        "milestone document added tenant=%s payment=%s milestone=%d document=%s doc_type=%s",
        tenant_id,
        payment_id,
        index,
        view.id,
        view.doc_type,
    )
    return view


async def remove_document(
    db: Db, uploader: Uploader, payment_id: UUID, index: int, document_id: UUID
) -> None:
    """Take a file out of milestone `index`'s draft. `PaymentNotFound` for an unknown file
    (or one already removed), `PaymentInvalidState` for a file already sent for approval."""
    tenant_id = uploader.tenant_id
    async with tenant_session(db, tenant_id) as session:
        payment, milestones = await locked_milestones(session, payment_id, index)
        milestone = milestones[index]
        await uploader_link(session, uploader, payment_id)
        document = await session.scalar(
            select(PoMilestoneDocument)
            .where(
                PoMilestoneDocument.id == document_id,
                PoMilestoneDocument.milestone_id == milestone.id,
                PoMilestoneDocument.removed_at.is_(None),
            )
            .with_for_update()
        )
        if document is None:
            raise PaymentNotFound(f"document {document_id} not found")
        submission = await session.get(PoMilestoneSubmission, document.submission_id)
        if submission is None or submission.state != "draft":
            raise PaymentInvalidState(
                f"document {document_id} was sent for approval: only a draft's files are removed"
            )
        check_accepts_documents(payment, milestone, index)
        document.removed_at = datetime.now(UTC)
        po = await _purchase_order(session, payment)
        session.add(
            rfq_event(
                tenant_id,
                actor_of(uploader),
                MILESTONE_DOCUMENT_REMOVED,
                po.rfq_id,
                payment_id=payment_id,
                milestone_index=index,
                submission_id=submission.id,
                document_id=document.id,
                doc_type=document.doc_type,
                source=source_of(uploader),
                purchase_order_id=po.id,
                purchase_order_ref=po.ref,
            )
        )
        await session.flush()
    logger.info(
        "milestone document removed tenant=%s payment=%s milestone=%d document=%s",
        tenant_id,
        payment_id,
        index,
        document_id,
    )


# --- Reading -----------------------------------------------------------------------------------


async def document_link(
    db: Db,
    links: DownloadLinks,
    tenant_id: UUID,
    payment_id: UUID,
    index: int,
    document_id: UUID,
    *,
    disposition: Disposition,
) -> FileLinkView:
    """A 5-minute link to one of the milestone's files (not removed). `inline` only for
    PDFs and images; anything else always downloads. `PaymentNotFound` otherwise."""
    async with tenant_session(db, tenant_id) as session:
        row = await session.scalar(
            select(PoMilestoneDocument)
            .join(PoPaymentMilestone, PoPaymentMilestone.id == PoMilestoneDocument.milestone_id)
            .where(
                PoMilestoneDocument.id == document_id,
                PoMilestoneDocument.payment_id == payment_id,
                PoPaymentMilestone.milestone_index == index,
                PoMilestoneDocument.removed_at.is_(None),
            )
        )
    if row is None:
        raise PaymentNotFound(f"document {document_id} not found")
    kind = PurePosixPath(row.storage_key).suffix.lstrip(".").lower()
    served = CONTENT_TYPES.get(kind, FALLBACK_TYPE)
    shown: Disposition = "inline" if disposition == "inline" and kind in INLINE else "attachment"
    link = await links.presign_get(
        row.storage_key,
        filename=row.filename,
        content_type=served,
        disposition=shown,
        expires_in=LINK_TTL,
    )
    logger.info(
        "milestone document link tenant=%s document=%s disposition=%s",
        tenant_id,
        row.id,
        shown,
    )
    return FileLinkView(
        url=link.url, expires_at=link.expires_at, filename=row.filename, content_type=served
    )
