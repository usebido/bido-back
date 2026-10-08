"""Company branding: the details and logos an RFQ document carries.

Every function works in `tenant_session` for the caller's tenant (forced RLS).
Any member can edit the branding for now; it is company-wide, like templates.

Logos are uploaded straight to blob storage (the same presigned form as chat
attachments): `start_logo_upload` signs a form for `branding/<tenant>/<id>.<ext>`,
the browser posts the file, and `complete_logo_upload` reads it back, checks it
(`images.check_logo`: real image, 2 MB, an SVG rewritten safe) and records it.
The first logo of a company becomes its default.

Raster logos are prepared on upload (`images.prepare_raster_logo`): orientation applied,
transparency kept, blank margins trimmed, always stored as PNG. The file exactly as
uploaded stays under `original_storage_key`; when an opaque plain or checkerboard
background is detected the buyer can remove it (`remove_logo_background`) and bring it
back (`restore_logo_background`), each rebuilt from the original.

Deleting a logo removes the row; the default and any RFQ using it fall back
(`ON DELETE SET NULL`). The stored bytes stay: the api role cannot delete objects.

`rfq_branding` is what a rendered RFQ document uses: the RFQ's own logo and
footer when it chose one, else the company's defaults.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, cast
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bido_back.auth import Principal
from bido_back.branding.images import (
    MAX_LOGO_BYTES,
    STORED_EXTENSION,
    InvalidLogo,
    LogoType,
    check_logo,
    logo_type_for,
    prepare_raster_logo,
)
from bido_back.branding.schemas import (
    BrandingView,
    CompleteLogoUploadRequest,
    LogoAlign,
    LogoFileLink,
    LogoPlacement,
    LogoUploadForm,
    LogoView,
    UpdateBrandingRequest,
)
from bido_back.db import tenant_session
from bido_back.db.models import CompanyBranding, CompanyLogo, Rfq, Tenant
from bido_back.orgs import CLERK_API_URL
from bido_back.settings import get_settings
from bido_back.storage import BlobStorage, build_storage
from bido_back.storage.direct import DirectUploads
from bido_back.storage.keys import BlobNotFound
from bido_back.storage.links import DownloadLinks

logger = logging.getLogger(__name__)

Db = async_sessionmaker[AsyncSession]

UPLOAD_TTL: Final = timedelta(minutes=15)
LINK_TTL: Final = timedelta(minutes=5)
# Clerk serves organization images from its own image CDN only.
_CLERK_IMAGE_HOSTS: Final = ("img.clerk.com", "images.clerk.dev")


class BrandingError(Exception):
    """Base class; messages are safe for HTTP responses (ids only)."""


class NotFound(BrandingError):
    pass


class LogoRejected(BrandingError):
    pass


class ClerkImageUnavailable(BrandingError):
    pass


# --- Branding details -----------------------------------------------------------------


async def get_branding(db: Db, principal: Principal) -> BrandingView:
    async with tenant_session(db, principal.tenant_id) as session:
        return await _branding_view(session, principal.tenant_id)


async def update_branding(
    db: Db, principal: Principal, request: UpdateBrandingRequest
) -> BrandingView:
    async with tenant_session(db, principal.tenant_id) as session:
        if request.default_logo_id is not None:
            await _logo(session, request.default_logo_id)
        row = await session.get(CompanyBranding, principal.tenant_id, with_for_update=True)
        if row is None:
            row = CompanyBranding(tenant_id=principal.tenant_id)
            session.add(row)
        row.legal_name = request.legal_name or None
        row.trade_name = request.trade_name or None
        row.tax_id = request.tax_id or None
        row.address = request.address or None
        row.phone = request.phone or None
        row.website = request.website or None
        row.default_footer = request.default_footer or None
        row.accent_color = request.accent_color.upper() if request.accent_color else None
        row.default_logo_asset_id = request.default_logo_id
        placement = request.logo_placement or LogoPlacement()
        row.logo_scale = Decimal(str(placement.scale))
        row.logo_align = placement.align
        row.logo_offset_y = placement.offset_y
        row.updated_by = principal.user_id
        row.updated_at = datetime.now(UTC)
        await session.flush()
        logger.info("company branding updated")
        return await _branding_view(session, principal.tenant_id)


async def _branding_view(session: AsyncSession, tenant_id: UUID) -> BrandingView:
    row = await session.get(CompanyBranding, tenant_id)
    if row is None:
        return BrandingView()
    default = (
        await session.get(CompanyLogo, row.default_logo_asset_id)
        if row.default_logo_asset_id is not None
        else None
    )
    return BrandingView(
        legal_name=row.legal_name,
        trade_name=row.trade_name,
        tax_id=row.tax_id,
        address=row.address,
        phone=row.phone,
        website=row.website,
        default_footer=row.default_footer,
        accent_color=row.accent_color,
        default_logo=_logo_view(default, default_id=row.default_logo_asset_id) if default else None,
        logo_placement=company_logo_placement(row),
        updated_at=row.updated_at,
    )


# --- Logo library ---------------------------------------------------------------------


async def list_logos(db: Db, principal: Principal) -> list[LogoView]:
    async with tenant_session(db, principal.tenant_id) as session:
        default_id = await _default_logo_id(session, principal.tenant_id)
        rows = await session.scalars(
            select(CompanyLogo).order_by(CompanyLogo.created_at.desc(), CompanyLogo.id)
        )
        return [_logo_view(row, default_id=default_id) for row in rows]


async def start_logo_upload(
    direct: DirectUploads, principal: Principal, *, filename: str, size_bytes: int
) -> LogoUploadForm:
    content_type = _declared_type(filename)
    if size_bytes > MAX_LOGO_BYTES:
        raise LogoRejected("logos are limited to 2 MB")
    logo_id = uuid4()
    form = await direct.presign_post(
        _logo_key(principal.tenant_id, logo_id, content_type),
        content_type=content_type,
        max_bytes=size_bytes,
        expires_in=UPLOAD_TTL,
    )
    return LogoUploadForm(
        logo_id=logo_id,
        url=form.url,
        fields=form.fields,
        expires_at=form.expires_at,
        max_bytes=size_bytes,
    )


async def complete_logo_upload(
    db: Db,
    storage: BlobStorage,
    principal: Principal,
    logo_id: UUID,
    request: CompleteLogoUploadRequest,
) -> LogoView:
    """Check the uploaded file and add it to the library. Repeating it is harmless."""
    content_type = _declared_type(request.filename)
    async with tenant_session(db, principal.tenant_id) as session:
        existing = await session.get(CompanyLogo, logo_id)
        if existing is not None:
            return _logo_view(
                existing, default_id=await _default_logo_id(session, existing.tenant_id)
            )
    key = _logo_key(principal.tenant_id, logo_id, content_type)
    try:
        data = await storage.get(key)
    except BlobNotFound:
        raise NotFound(f"logo {logo_id} not found") from None
    name = request.name or _name_from(request.filename)
    return await _add_logo(
        db, storage, principal, logo_id, key, data, content_type, name, request.make_default
    )


async def rename_logo(db: Db, principal: Principal, logo_id: UUID, name: str) -> LogoView:
    async with tenant_session(db, principal.tenant_id) as session:
        row = await _logo(session, logo_id)
        row.name = name
        await session.flush()
        return _logo_view(row, default_id=await _default_logo_id(session, principal.tenant_id))


async def make_default_logo(db: Db, principal: Principal, logo_id: UUID) -> BrandingView:
    async with tenant_session(db, principal.tenant_id) as session:
        await _logo(session, logo_id)
        await _set_default(session, principal, logo_id)
        return await _branding_view(session, principal.tenant_id)


async def delete_logo(db: Db, principal: Principal, logo_id: UUID) -> None:
    async with tenant_session(db, principal.tenant_id) as session:
        row = await _logo(session, logo_id)
        await session.delete(row)
        await session.flush()
    logger.info("company logo deleted", extra={"logo_id": str(logo_id)})


async def logo_link(
    db: Db, links: DownloadLinks, principal: Principal, logo_id: UUID
) -> LogoFileLink:
    """A short-lived inline link for an `<img>`. An SVG is served as stored: sanitized."""
    async with tenant_session(db, principal.tenant_id) as session:
        row = await _logo(session, logo_id)
    content_type = cast(LogoType, row.content_type)
    link = await links.presign_get(
        row.storage_key,
        filename=f"{row.name}.{STORED_EXTENSION[content_type]}",
        content_type=content_type,
        disposition="inline",
        expires_in=LINK_TTL,
    )
    return LogoFileLink(url=link.url, expires_at=link.expires_at, content_type=content_type)


async def set_logo_background(
    db: Db, storage: BlobStorage, principal: Principal, logo_id: UUID, *, remove: bool
) -> LogoView:
    """Remove the detected background (made transparent) or keep/restore it, rebuilding
    the prepared image from the original upload either way. Also prepares a logo
    uploaded before preparation existed."""
    return await _reprepare(db, storage, principal, logo_id, remove_background=remove)


async def _reprepare(
    db: Db, storage: BlobStorage, principal: Principal, logo_id: UUID, *, remove_background: bool
) -> LogoView:
    async with tenant_session(db, principal.tenant_id) as session:
        row = await _logo(session, logo_id)
        original_key = row.original_storage_key or row.storage_key
        original_type = cast(LogoType, row.original_content_type or row.content_type)
    if original_type == "image/svg+xml":
        raise LogoRejected("an SVG logo has no background to remove")
    try:
        original = await storage.get(original_key)
    except BlobNotFound:
        raise NotFound(f"logo {logo_id} not found") from None
    try:
        prepared = prepare_raster_logo(original, original_type, remove_background=remove_background)
    except InvalidLogo as exc:
        raise LogoRejected(str(exc)) from None
    if prepared is None:
        raise LogoRejected("an SVG logo has no background to remove")
    if remove_background and not prepared.background_removable:
        raise LogoRejected("no plain or checkerboard background was found to remove")
    key = _prepared_key(principal.tenant_id, logo_id, removed=prepared.background_removed)
    await storage.put(key, prepared.data, prepared.content_type)
    async with tenant_session(db, principal.tenant_id) as session:
        row = await _logo(session, logo_id)
        row.original_storage_key = original_key
        row.original_content_type = original_type
        row.storage_key = key
        row.content_type = prepared.content_type
        row.size_bytes = len(prepared.data)
        row.width, row.height = prepared.width, prepared.height
        row.background_removable = prepared.background_removable
        row.background_removed = prepared.background_removed
        await session.flush()
        logger.info(
            "company logo background %s",
            "removed" if remove_background else "restored",
            extra={"logo_id": str(logo_id)},
        )
        return _logo_view(row, default_id=await _default_logo_id(session, principal.tenant_id))


# --- Placement ------------------------------------------------------------------------


def company_logo_placement(row: CompanyBranding) -> LogoPlacement:
    return LogoPlacement(
        scale=float(row.logo_scale),
        align=cast(LogoAlign, row.logo_align or "left"),
        offset_y=row.logo_offset_y or 0,
    )


def rfq_logo_placement(rfq: Rfq) -> LogoPlacement | None:
    """The RFQ's own placement, or None when it uses the company default."""
    if rfq.logo_scale is None and rfq.logo_align is None and rfq.logo_offset_y is None:
        return None
    return LogoPlacement(
        scale=float(rfq.logo_scale if rfq.logo_scale is not None else 1),
        align=cast(LogoAlign, rfq.logo_align or "left"),
        offset_y=rfq.logo_offset_y or 0,
    )


# --- Import from Clerk ------------------------------------------------------------------


async def import_clerk_logo(
    db: Db,
    storage: BlobStorage,
    principal: Principal,
    *,
    clerk_secret_key: str | None,
    client: httpx.AsyncClient | None = None,
    base_url: str = CLERK_API_URL,
) -> LogoView:
    """Copy the Clerk organization's image into the library (and make it the default)."""
    if not clerk_secret_key:
        raise ClerkImageUnavailable("the organization image cannot be read here")
    async with tenant_session(db, principal.tenant_id) as session:
        tenant = await session.get(Tenant, principal.tenant_id)
    org_id = tenant.clerk_org_id if tenant is not None else None
    if not org_id:
        raise ClerkImageUnavailable("this company is not linked to a Clerk organization")
    owned = client is None
    http = client or httpx.AsyncClient(timeout=5.0)
    try:
        try:
            response = await http.get(
                f"{base_url.rstrip('/')}/organizations/{org_id}",
                headers={"Authorization": f"Bearer {clerk_secret_key}"},
            )
            response.raise_for_status()
            body: object = response.json()
        except (httpx.HTTPError, ValueError):
            raise ClerkImageUnavailable("the organization could not be read") from None
        org = cast(dict[str, object], body) if isinstance(body, dict) else {}
        image_url = org.get("image_url")
        if org.get("has_image") is not True or not isinstance(image_url, str):
            raise ClerkImageUnavailable("the organization has no image")
        parsed = urlparse(image_url)
        if parsed.scheme != "https" or parsed.hostname not in _CLERK_IMAGE_HOSTS:
            raise ClerkImageUnavailable("the organization image is not on Clerk's image host")
        try:
            image = await http.get(image_url, follow_redirects=False)
            image.raise_for_status()
        except httpx.HTTPError:
            raise ClerkImageUnavailable("the organization image could not be downloaded") from None
    finally:
        if owned:
            await http.aclose()
    data = image.content
    content_type = _content_type_of(image.headers.get("content-type", ""))
    logo_id = uuid4()
    key = _logo_key(principal.tenant_id, logo_id, content_type)
    return await _add_logo(
        db, storage, principal, logo_id, key, data, content_type, "Clerk organization", True
    )


def _content_type_of(header: str) -> LogoType:
    value = header.split(";", 1)[0].strip().lower()
    for known in STORED_EXTENSION:
        if value == known:
            return known
    raise ClerkImageUnavailable("the organization image is not PNG, JPEG, WebP or SVG")


# --- RFQ documents ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Branding:
    """What an RFQ document shows for the buyer's company. Every field may be empty."""

    company_name: str | None
    legal_name: str | None
    tax_id: str | None
    address: str | None
    phone: str | None
    website: str | None
    footer_text: str | None
    accent_color: str | None
    logo_bytes: bytes | None
    logo_content_type: str | None


async def rfq_branding(db: Db, storage: BlobStorage, tenant_id: UUID, rfq_id: UUID) -> Branding:
    """The branding for one RFQ's document: its own logo and footer, else the defaults.

    A logo whose bytes cannot be read is left out rather than failing the document.
    """
    async with tenant_session(db, tenant_id) as session:
        rfq = await session.get(Rfq, rfq_id)
        if rfq is None:
            raise NotFound(f"rfq {rfq_id} not found")
        rows = await rfq_branding_rows(session, tenant_id, rfq)
        tenant = await session.get(Tenant, tenant_id)
    row, logo = rows.branding, rows.logo
    data = await logo_bytes(storage, logo) if logo is not None else None
    company_name = (row.trade_name or row.legal_name) if row else None
    return Branding(
        company_name=company_name or (tenant.name if tenant else None),
        legal_name=row.legal_name if row else None,
        tax_id=row.tax_id if row else None,
        address=row.address if row else None,
        phone=row.phone if row else None,
        website=row.website if row else None,
        footer_text=rows.footer,
        accent_color=row.accent_color if row else None,
        logo_bytes=data,
        logo_content_type=logo.content_type if logo is not None and data else None,
    )


@dataclass(frozen=True, slots=True)
class RfqBrandingRows:
    """What an RFQ's document is branded with, read in the caller's tenant session."""

    branding: CompanyBranding | None
    logo: CompanyLogo | None
    footer: str | None


async def rfq_branding_rows(session: AsyncSession, tenant_id: UUID, rfq: Rfq) -> RfqBrandingRows:
    """The RFQ's own logo and footer when it chose them, else the company defaults."""
    row = await session.get(CompanyBranding, tenant_id)
    logo_id = rfq.logo_asset_id or (row.default_logo_asset_id if row else None)
    logo = await session.get(CompanyLogo, logo_id) if logo_id is not None else None
    footer = rfq.footer_override or (row.default_footer if row else None)
    return RfqBrandingRows(branding=row, logo=logo, footer=footer)


async def logo_bytes(storage: BlobStorage | None, logo: CompanyLogo) -> bytes | None:
    """The stored logo, or None (logged) when it cannot be read: a document never fails
    for its logo. Without `storage`, the process's blob storage."""
    try:
        return await (storage or default_storage()).get(logo.storage_key)
    except BlobNotFound:
        logger.warning("company logo missing in storage", extra={"logo_id": str(logo.id)})
        return None


_storage: BlobStorage | None = None


def default_storage() -> BlobStorage:
    """The process's blob storage, for callers that were not handed one (the Copilot's
    tools prepare sends without the api's storage dependency)."""
    global _storage  # noqa: PLW0603  # one per process, built on first use
    if _storage is None:
        _storage = build_storage(get_settings())
    return _storage


async def check_logo_exists(session: AsyncSession, logo_id: UUID) -> None:
    """For an RFQ choosing a logo: it must be one of the company's. Raises `NotFound`."""
    await _logo(session, logo_id)


# --- Helpers ----------------------------------------------------------------------------


async def _add_logo(
    db: Db,
    storage: BlobStorage,
    principal: Principal,
    logo_id: UUID,
    key: str,
    data: bytes,
    content_type: LogoType,
    name: str,
    make_default: bool,
) -> LogoView:
    try:
        checked = check_logo(data, content_type)
        prepared = prepare_raster_logo(checked.data, checked.content_type)
    except InvalidLogo as exc:
        raise LogoRejected(str(exc)) from None
    if checked.data != data:
        # The sanitized SVG replaces what was uploaded.
        await storage.put(key, checked.data, checked.content_type)
    stored_key, stored_type, stored = key, checked.content_type, checked.data
    width, height = checked.width, checked.height
    if prepared is not None:
        # The original stays under `key`; documents use the prepared PNG.
        stored_key = _prepared_key(principal.tenant_id, logo_id, removed=False)
        stored_type, stored = prepared.content_type, prepared.data
        width, height = prepared.width, prepared.height
        await storage.put(stored_key, stored, stored_type)
    async with tenant_session(db, principal.tenant_id) as session:
        row = CompanyLogo(
            id=logo_id,
            tenant_id=principal.tenant_id,
            name=name[:200],
            content_type=stored_type,
            size_bytes=len(stored),
            width=width,
            height=height,
            storage_key=stored_key,
            original_storage_key=key if prepared is not None else None,
            original_content_type=checked.content_type if prepared is not None else None,
            background_removable=prepared.background_removable if prepared else False,
            background_removed=False,
            created_by=principal.user_id,
        )
        session.add(row)
        await session.flush()
        default_id = await _default_logo_id(session, principal.tenant_id)
        if make_default or default_id is None:
            await _set_default(session, principal, logo_id)
            default_id = logo_id
        await session.refresh(row)
        logger.info("company logo added", extra={"logo_id": str(logo_id)})
        return _logo_view(row, default_id=default_id)


async def _set_default(session: AsyncSession, principal: Principal, logo_id: UUID) -> None:
    row = await session.get(CompanyBranding, principal.tenant_id, with_for_update=True)
    if row is None:
        session.add(
            CompanyBranding(
                tenant_id=principal.tenant_id,
                default_logo_asset_id=logo_id,
                updated_by=principal.user_id,
            )
        )
    else:
        await session.execute(
            update(CompanyBranding)
            .where(CompanyBranding.tenant_id == principal.tenant_id)
            .values(
                default_logo_asset_id=logo_id,
                updated_by=principal.user_id,
                updated_at=datetime.now(UTC),
            )
        )
    await session.flush()


async def _default_logo_id(session: AsyncSession, tenant_id: UUID) -> UUID | None:
    return await session.scalar(
        select(CompanyBranding.default_logo_asset_id).where(CompanyBranding.tenant_id == tenant_id)
    )


async def _logo(session: AsyncSession, logo_id: UUID) -> CompanyLogo:
    row = await session.get(CompanyLogo, logo_id)
    if row is None:
        raise NotFound(f"logo {logo_id} not found")
    return row


def _logo_view(row: CompanyLogo, *, default_id: UUID | None) -> LogoView:
    return LogoView(
        id=row.id,
        name=row.name,
        content_type=cast(LogoType, row.content_type),
        size_bytes=row.size_bytes,
        width=row.width,
        height=row.height,
        created_at=row.created_at,
        is_default=row.id == default_id,
        background_removable=row.background_removable,
        background_removed=row.background_removed,
        prepared=row.original_storage_key is not None or row.content_type == "image/svg+xml",
    )


def _declared_type(filename: str) -> LogoType:
    try:
        return logo_type_for(filename)
    except InvalidLogo as exc:
        raise LogoRejected(str(exc)) from None


def _logo_key(tenant_id: UUID, logo_id: UUID, content_type: LogoType) -> str:
    return f"branding/{tenant_id}/{logo_id}.{STORED_EXTENSION[content_type]}"


def _prepared_key(tenant_id: UUID, logo_id: UUID, *, removed: bool) -> str:
    """Where the prepared PNG lives; with and without the background are kept apart so a
    restore never has to recompute what a removal overwrote."""
    return f"branding/{tenant_id}/{logo_id}.{'nobg' if removed else 'prepared'}.png"


def _name_from(filename: str) -> str:
    stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    return stem.strip()[:200] or "Logo"
