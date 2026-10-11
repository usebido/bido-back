"""A milestone's evidence files: sniffed, sized, gathered in a draft, removed, linked.

Also the helpers the other milestone tests share: fake storage and download links, sample
files of every kind, settings with releases set up, the factory's link (issued, resolved,
confirmed) and uploads of a milestone's required documents.
"""

import io
import secrets
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import AuditEvent, PoMilestoneDocument, PoPayment, PoPaymentMilestone
from bido_back.payments.errors import (
    FactoryLinkInvalid,
    PaymentInvalidState,
    PaymentNotFound,
    PaymentRefused,
)
from bido_back.payments.milestones.catalog import (
    DocumentType,
    MilestoneDefinition,
    definition_for,
)
from bido_back.payments.milestones.documents import (
    MAX_DOCUMENTS,
    DocumentTooLarge,
    DocumentTypeRefused,
    Uploader,
    accepts_kind,
    add_document,
    clean_filename,
    document_link,
    remove_document,
    sniff_extension,
)
from bido_back.payments.milestones.links import (
    FactoryAccess,
    accept_order,
    issue_factory_link,
    resolve_factory_link,
)
from bido_back.payments.milestones.proposals import (
    MILESTONE_DOCUMENT_ADDED,
    MILESTONE_DOCUMENT_REMOVED,
)
from bido_back.payments.milestones.state import ensure_milestones
from bido_back.payments.milestones.views import FactoryLinkView, MilestoneDocumentView
from bido_back.settings import Settings
from bido_back.storage import BlobNotFound
from bido_back.storage.links import Disposition, FileLink
from tests.awards.test_awards_db import buyer
from tests.payments.fakes import address
from tests.payments.milestones.conftest import Funded

pytestmark = pytest.mark.db

FACTORY_PAGE: Final = "https://factory.example.test/order"


def release_settings(**overrides: object) -> Settings:
    """Settings with payments and milestone releases set up (fake values; nothing is called)."""
    values: dict[str, object] = {
        "environment": "test",
        "database_url": "postgresql+asyncpg://app@localhost:5432/unused",
        "basis_theory_api_key": "bt-test-value",
        "payments_card_charge": "simulated",
        "privy_app_id": "privy-app",
        "privy_app_secret": "privy-app-value",
        "privy_authorization_key": "privy-authorization-value",
        "privy_wallet_owner_id": "quorum-id",
        "solana_rpc_url": "https://rpc.example.test/private-path",
        "solana_stablecoin_mint": address("stablecoin"),
        "solana_mint_authority_key": "mint-authority-value",
        "solana_operator_key": "operator-value",
        "solana_relayer_key": "relayer-value",
        "solana_payout_owner": address("payout"),
        "solana_gate_attester": address("attester"),
        "solana_gate_attester_key": "attester-value",
        "payout_details_encryption_key": "payout-details-value",
        "factory_page_url": FACTORY_PAGE,
    }
    return Settings.model_validate({**values, **overrides})


SETTINGS: Final = release_settings()


# --- Fakes -------------------------------------------------------------------------------------


class FakeStorage:
    """`BlobStorage` in memory; remembers each key's content type and every put."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.types: dict[str, str] = {}
        self.puts: list[str] = []

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.blobs[key] = data
        self.types[key] = content_type
        self.puts.append(key)

    async def get(self, key: str) -> bytes:
        if key not in self.blobs:
            raise BlobNotFound(key)
        return self.blobs[key]


@dataclass
class FakeLinks:
    """`DownloadLinks` that records what it signed."""

    calls: list[tuple[str, str, str, Disposition]] = field(
        default_factory=list[tuple[str, str, str, Disposition]]
    )

    async def presign_get(
        self,
        key: str,
        *,
        filename: str,
        content_type: str,
        disposition: Disposition,
        expires_in: timedelta,
    ) -> FileLink:
        self.calls.append((key, filename, content_type, disposition))
        return FileLink(
            url=f"https://files.example.test/{key}?d={disposition}",
            expires_at=datetime.now(UTC) + expires_in,
        )


# --- Sample files (each call a new file: random bytes after the magic) -------------------------


def pdf() -> bytes:
    return b"%PDF-1.7\n%" + secrets.token_bytes(24)


def png() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + secrets.token_bytes(24)


def jpeg() -> bytes:
    return b"\xff\xd8\xff\xe0" + secrets.token_bytes(24)


def webp() -> bytes:
    return b"RIFF\x24\x00\x00\x00WEBPVP8 " + secrets.token_bytes(24)


def _zip(part: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(part, f"<x>{secrets.token_hex(8)}</x>")
    return buffer.getvalue()


def xlsx() -> bytes:
    return _zip("xl/workbook.xml")


def docx() -> bytes:
    return _zip("word/document.xml")


def mp4() -> bytes:
    return b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00" + secrets.token_bytes(24)


def mov() -> bytes:
    return b"\x00\x00\x00\x14ftypqt  \x00\x00\x02\x00" + secrets.token_bytes(24)


def heic() -> bytes:
    return b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00" + secrets.token_bytes(24)


SAMPLES: Final = (("pdf", pdf), ("jpg", jpeg), ("png", png), ("xlsx", xlsx), ("mp4", mp4))


def sample(document_type: DocumentType) -> tuple[str, bytes]:
    """(name, bytes) of a new file this document type accepts."""
    for kind, make in SAMPLES:
        if accepts_kind(document_type, kind):
            return f"{document_type.code}-{secrets.token_hex(3)}.{kind}", make()
    raise AssertionError(f"no sample for {document_type.code}")


# --- Helpers -----------------------------------------------------------------------------------


def outsider(funded: Funded) -> Principal:
    """An approver of the other tenant."""
    people = funded.people
    return Principal(tenant_id=people.other_tenant, user_id=people.other_approver, role="approver")


async def definition_at(funded: Funded, index: int) -> MilestoneDefinition:
    """What milestone `index` of the funded payment asks for (from its row, as the service)."""
    async with tenant_session(funded.engine, funded.tenant) as session:
        payment = await session.get(PoPayment, funded.payment_id)
        assert payment is not None
        row = (await ensure_milestones(session, payment))[index]
        return definition_for(row.key, row.name, row.bps)


async def factory_link(
    funded: Funded, *, accept: bool = True, settings: Settings = SETTINGS
) -> tuple[FactoryLinkView, FactoryAccess]:
    """The factory's link, issued by the buyer, resolved from its token (and confirmed)."""
    link = await issue_factory_link(
        funded.engine, buyer(funded.people), funded.po_id, settings=settings
    )
    access = await resolve_factory_link(funded.engine, link.token)
    if accept:
        await accept_order(funded.engine, access)
    return link, access


async def upload(
    funded: Funded,
    uploader: Uploader,
    index: int,
    doc_type: str,
    *,
    storage: FakeStorage,
    data: bytes | None = None,
    filename: str | None = None,
    settings: Settings = SETTINGS,
) -> MilestoneDocumentView:
    """Add one file of `doc_type`: a fitting sample unless `data` is given (then named after
    its kind unless `filename` is)."""
    if data is None:
        document_type = (await definition_at(funded, index)).document(doc_type)
        assert document_type is not None
        name, data = sample(document_type)
    else:
        name = f"{doc_type}-{secrets.token_hex(3)}.{sniff_extension(data) or 'bin'}"
    return await add_document(
        funded.engine,
        storage,
        uploader,
        funded.payment_id,
        index,
        doc_type=doc_type,
        filename=filename or name,
        content_type="application/octet-stream",
        data=data,
        settings=settings,
    )


async def upload_required(
    funded: Funded, uploader: Uploader, index: int, *, storage: FakeStorage
) -> list[MilestoneDocumentView]:
    """One file of every document type milestone `index` requires."""
    definition = await definition_at(funded, index)
    return [
        await upload(funded, uploader, index, code, storage=storage)
        for code in sorted(definition.required_codes)
    ]


async def document_rows(funded: Funded, index: int) -> list[PoMilestoneDocument]:
    """Every file row of milestone `index`, removed ones too, oldest first."""
    async with tenant_session(funded.engine, funded.tenant) as session:
        return list(
            await session.scalars(
                select(PoMilestoneDocument)
                .join(PoPaymentMilestone, PoPaymentMilestone.id == PoMilestoneDocument.milestone_id)
                .where(
                    PoMilestoneDocument.payment_id == funded.payment_id,
                    PoPaymentMilestone.milestone_index == index,
                )
                .order_by(PoMilestoneDocument.created_at, PoMilestoneDocument.id)
            )
        )


async def audit_payloads(funded: Funded, action: str) -> list[dict[str, object]]:
    async with tenant_session(funded.engine, funded.tenant) as session:
        events = await session.scalars(
            select(AuditEvent)
            .where(AuditEvent.action == action)
            .order_by(AuditEvent.created_at, AuditEvent.id)
        )
        return [dict(e.payload) for e in events]


# --- Kinds (no database) -----------------------------------------------------------------------


def test_the_first_bytes_decide_the_kind() -> None:
    assert sniff_extension(pdf()) == "pdf"
    assert sniff_extension(b"\r\n\r\n%PDF-1.4\n" + secrets.token_bytes(8)) == "pdf"
    assert sniff_extension(png()) == "png"
    assert sniff_extension(jpeg()) == "jpg"
    assert sniff_extension(webp()) == "webp"
    assert sniff_extension(xlsx()) == "xlsx"
    assert sniff_extension(mp4()) == "mp4"
    assert sniff_extension(mov()) == "mov"
    # Not evidence Bido takes: a Word file, a HEIC photo, text, a broken zip, nothing.
    assert sniff_extension(docx()) is None
    assert sniff_extension(heic()) is None
    assert sniff_extension(b"just some text, not a file kind") is None
    assert sniff_extension(b"PK\x03\x04" + secrets.token_bytes(40)) is None
    assert sniff_extension(b"") is None
    # A PDF header past the first kilobyte does not count.
    assert sniff_extension(b"x" * 1100 + b"%PDF-1.7") is None


def test_jpg_and_jpeg_are_one_kind_and_names_are_cleaned() -> None:
    photos = DocumentType(
        code="photo", label="", label_zh="", required=True, accepts=("jpeg",), description=""
    )
    assert accepts_kind(photos, "jpg") and accepts_kind(photos, "jpeg")
    assert not accepts_kind(photos, "png")
    assert clean_filename("../../etc/pass\x00wd.pdf") == ".._.._etc_pass_wd.pdf"
    assert clean_filename("   ") == "file"
    assert len(clean_filename("a" * 300 + ".pdf")) == 200
    assert clean_filename("a" * 300 + ".pdf").endswith(".pdf")


# --- Adding ------------------------------------------------------------------------------------


async def test_the_factory_sends_files_once_it_confirmed_the_order(funded: Funded) -> None:
    storage = FakeStorage()
    _, access = await factory_link(funded, accept=False)
    [code, *_] = sorted((await definition_at(funded, 0)).required_codes)
    with pytest.raises(PaymentInvalidState, match="confirm the order"):
        await upload(funded, access, 0, code, storage=storage)
    assert storage.puts == [] and await document_rows(funded, 0) == []

    await accept_order(funded.engine, access)
    document_type = (await definition_at(funded, 0)).document(code)
    assert document_type is not None
    name, data = sample(document_type)
    added = await upload(funded, access, 0, code, storage=storage, data=data, filename=name)

    [row] = await document_rows(funded, 0)
    kind = name.rsplit(".", 1)[1]
    digest = added.sha256
    assert (added.id, added.doc_type, added.filename, added.source) == (
        row.id,
        code,
        name,
        "factory",
    )
    assert (row.factory_link_id, row.uploaded_by, row.size_bytes) == (
        access.link_id,
        None,
        len(data),
    )
    assert row.storage_key == f"milestones/{funded.tenant}/{funded.payment_id}/0/{digest}.{kind}"
    assert storage.blobs[row.storage_key] == data
    assert storage.types[row.storage_key] == added.content_type
    [event] = await audit_payloads(funded, MILESTONE_DOCUMENT_ADDED)
    assert event["document_id"] == str(row.id) and event["doc_type"] == code
    assert event["source"] == "factory" and event["milestone_index"] == 0
    # Ids and the document type only: never the file's name.
    assert name not in str(event)


async def test_a_person_of_the_company_needs_no_confirmation(funded: Funded) -> None:
    storage = FakeStorage()
    [code, *_] = sorted((await definition_at(funded, 0)).required_codes)
    added = await upload(funded, buyer(funded.people), 0, code, storage=storage)

    [row] = await document_rows(funded, 0)
    assert added.source == "buyer"
    assert (row.source, row.uploaded_by, row.factory_link_id) == (
        "buyer",
        funded.people.owner,
        None,
    )


async def test_files_are_checked_before_anything_is_stored(funded: Funded) -> None:
    storage = FakeStorage()
    owner = buyer(funded.people)
    definition = await definition_at(funded, 0)
    images_only = next(
        d for d in definition.documents if accepts_kind(d, "jpg") and not accepts_kind(d, "pdf")
    )

    with pytest.raises(PaymentRefused, match="empty"):
        await upload(funded, owner, 0, images_only.code, storage=storage, data=b"")
    with pytest.raises(DocumentTooLarge):
        await upload(
            funded,
            owner,
            0,
            images_only.code,
            storage=storage,
            data=b"\xff\xd8\xff" + b"0" * (1024 * 1024),
            settings=release_settings(milestone_document_max_bytes=1024 * 1024),
        )
    # Not a kind Bido takes, a kind this document type does not take, a name saying
    # another kind, a document type the milestone does not ask for.
    with pytest.raises(DocumentTypeRefused, match="unsupported"):
        await upload(funded, owner, 0, images_only.code, storage=storage, data=docx())
    with pytest.raises(DocumentTypeRefused, match="accepts"):
        await upload(funded, owner, 0, images_only.code, storage=storage, data=pdf())
    with pytest.raises(DocumentTypeRefused, match="does not match its name"):
        await upload(
            funded, owner, 0, images_only.code, storage=storage, data=jpeg(), filename="a.png"
        )
    later = await definition_at(funded, 2)
    foreign = next(d for d in later.documents if definition.document(d.code) is None)
    with pytest.raises(PaymentRefused, match="does not take that document type"):
        await upload(funded, owner, 0, foreign.code, storage=storage, data=pdf())
    assert storage.puts == [] and await document_rows(funded, 0) == []

    # A name without an extension gets the file's; `.jpeg` is a jpg.
    named = await upload(
        funded, owner, 0, images_only.code, storage=storage, data=jpeg(), filename="IMG_0001"
    )
    jpeg_named = await upload(
        funded, owner, 0, images_only.code, storage=storage, data=jpeg(), filename="b.JPEG"
    )
    assert (named.filename, named.content_type) == ("IMG_0001.jpg", "image/jpeg")
    assert jpeg_named.filename == "b.JPEG"


async def test_the_same_file_twice_is_one_document(funded: Funded) -> None:
    storage = FakeStorage()
    owner = buyer(funded.people)
    definition = await definition_at(funded, 0)
    photos = next(d for d in definition.documents if accepts_kind(d, "jpg"))
    other = next(
        d for d in definition.documents if d.code != photos.code and accepts_kind(d, "jpg")
    )
    data = jpeg()

    first = await upload(funded, owner, 0, photos.code, storage=storage, data=data)
    again = await upload(funded, owner, 0, photos.code, storage=storage, data=data)
    assert again == first
    assert len(await document_rows(funded, 0)) == 1 and len(storage.puts) == 1
    # The same file as another document type is refused: one file, one type.
    with pytest.raises(PaymentInvalidState, match="already"):
        await upload(funded, owner, 0, other.code, storage=storage, data=data)


async def test_a_milestone_takes_at_most_thirty_files(funded: Funded) -> None:
    storage = FakeStorage()
    owner = buyer(funded.people)
    photos = next(d for d in (await definition_at(funded, 0)).documents if accepts_kind(d, "jpg"))
    for _ in range(MAX_DOCUMENTS):
        await upload(funded, owner, 0, photos.code, storage=storage, data=jpeg())

    with pytest.raises(PaymentRefused, match="at most 30"):
        await upload(funded, owner, 0, photos.code, storage=storage, data=jpeg())
    assert len(await document_rows(funded, 0)) == MAX_DOCUMENTS


async def test_later_milestones_gather_files_while_open(funded: Funded) -> None:
    storage = FakeStorage()
    owner = buyer(funded.people)
    # T1's report may come with the tool's photos: documents for any open milestone.
    added = await upload_required(funded, owner, 1, storage=storage)
    assert {d.doc_type for d in added} == (await definition_at(funded, 1)).required_codes
    # The tooling plan has three milestones.
    with pytest.raises(PaymentNotFound):
        await upload(funded, owner, 3, added[0].doc_type, storage=storage, data=pdf())


async def test_only_a_funded_payment_gathers_files(funded: Funded) -> None:
    storage = FakeStorage()
    owner = buyer(funded.people)
    [code, *_] = sorted((await definition_at(funded, 0)).required_codes)
    async with tenant_session(funded.engine, funded.tenant) as session:
        await session.execute(
            update(PoPayment)
            .where(PoPayment.id == funded.payment_id)
            .values(state="failed", failure_code="chain_fund_failed")
        )
    with pytest.raises(PaymentInvalidState, match="failed"):
        await upload(funded, owner, 0, code, storage=storage)


async def test_another_tenant_cannot_add_files(funded: Funded) -> None:
    storage = FakeStorage()
    [code, *_] = sorted((await definition_at(funded, 0)).required_codes)
    with pytest.raises(PaymentNotFound):
        await upload(funded, outsider(funded), 0, code, storage=storage)
    assert storage.puts == []


async def test_a_revoked_link_sends_nothing(funded: Funded) -> None:
    storage = FakeStorage()
    _, access = await factory_link(funded)
    await factory_link(funded)  # a new link: the first one stops
    [code, *_] = sorted((await definition_at(funded, 0)).required_codes)
    with pytest.raises(FactoryLinkInvalid):
        await upload(funded, access, 0, code, storage=storage)
    forged = FactoryAccess(
        tenant_id=access.tenant_id,
        link_id=access.link_id,
        payment_id=uuid4(),
        expires_at=access.expires_at,
    )
    with pytest.raises(PaymentNotFound):
        await upload(funded, forged, 0, code, storage=storage)


# --- Removing ----------------------------------------------------------------------------------


async def test_a_draft_file_is_removed_and_its_row_kept(funded: Funded) -> None:
    storage = FakeStorage()
    _, access = await factory_link(funded)
    [code, *_] = sorted((await definition_at(funded, 0)).required_codes)
    added = await upload(funded, access, 0, code, storage=storage)

    await remove_document(funded.engine, access, funded.payment_id, 0, added.id)

    [row] = await document_rows(funded, 0)
    assert row.id == added.id and row.removed_at is not None
    assert row.storage_key in storage.blobs  # the stored file stays (copies share it)
    with pytest.raises(PaymentNotFound):
        await remove_document(funded.engine, access, funded.payment_id, 0, added.id)
    with pytest.raises(PaymentNotFound):
        await remove_document(funded.engine, access, funded.payment_id, 1, uuid4())
    [event] = await audit_payloads(funded, MILESTONE_DOCUMENT_REMOVED)
    assert event["document_id"] == str(added.id) and event["doc_type"] == code
    # The same file can come back afterwards.
    again = await upload(
        funded, access, 0, code, storage=storage, data=storage.blobs[row.storage_key]
    )
    assert again.id != added.id and again.sha256 == added.sha256


# --- Reading -----------------------------------------------------------------------------------


async def test_links_serve_pdfs_and_images_inline_and_the_rest_as_downloads(
    funded: Funded,
) -> None:
    storage = FakeStorage()
    links = FakeLinks()
    owner = buyer(funded.people)
    definition = await definition_at(funded, 1)
    report = next(d for d in definition.documents if accepts_kind(d, "xlsx"))
    photo = next(d for d in definition.documents if accepts_kind(d, "jpg"))
    sheet = await upload(funded, owner, 1, report.code, storage=storage, data=xlsx())
    image = await upload(funded, owner, 1, photo.code, storage=storage, data=jpeg())

    inline = await document_link(
        funded.engine, links, funded.tenant, funded.payment_id, 1, image.id, disposition="inline"
    )
    download = await document_link(
        funded.engine, links, funded.tenant, funded.payment_id, 1, sheet.id, disposition="inline"
    )

    assert (inline.filename, inline.content_type) == (image.filename, "image/jpeg")
    assert download.content_type.startswith("application/vnd.openxmlformats")
    assert [(c[2], c[3]) for c in links.calls] == [
        ("image/jpeg", "inline"),
        (download.content_type, "attachment"),
    ]
    assert inline.expires_at - datetime.now(UTC) <= timedelta(minutes=5)
    for wrong in (
        (funded.tenant, funded.payment_id, 0, image.id),  # another milestone
        (funded.tenant, uuid4(), 1, image.id),  # another payment
        (funded.people.other_tenant, funded.payment_id, 1, image.id),  # another tenant
        (funded.tenant, funded.payment_id, 1, uuid4()),  # no such file
    ):
        with pytest.raises(PaymentNotFound):
            await document_link(funded.engine, links, *wrong, disposition="attachment")
    await remove_document(funded.engine, owner, funded.payment_id, 1, image.id)
    with pytest.raises(PaymentNotFound):
        await document_link(
            funded.engine,
            links,
            funded.tenant,
            funded.payment_id,
            1,
            image.id,
            disposition="inline",
        )
