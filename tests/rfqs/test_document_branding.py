"""The RFQ PDF in the buyer company's name: the company logo and details from
`/v1/company`, the RFQ's own logo and terms, and all of it inside the approved arguments
(so the approval hash covers what the supplier sees)."""

import base64
import io
from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import UUID, uuid4

import pdfplumber
import pytest
import pytest_asyncio
from PIL import Image
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.events import args_hash
from bido_back.branding.schemas import LogoPlacement
from bido_back.db import tenant_session
from bido_back.db.models import Approval, CompanyBranding, CompanyLogo, Rfq
from bido_back.rfqs import service
from bido_back.rfqs.schemas import UpdateRfqRequest
from bido_back.rfqs.send import SendRfqEmailArgs, send_proposal_ids
from tests.approvals.conftest import People
from tests.onboarding.conftest import FakeStorage

from .conftest import FakeMailboxes, FakeWriter
from .test_service import buyer, make_rfq, make_supplier


def _png(color: tuple[int, int, int], size: tuple[int, int] = (240, 80)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _webp() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2400, 800), (10, 120, 60)).save(buffer, format="WEBP")
    return buffer.getvalue()


def _pdf(data: bytes) -> tuple[str, int]:
    with pdfplumber.open(io.BytesIO(data)) as doc:
        text = "\n".join(page.extract_text() or "" for page in doc.pages)
        images = sum(len(page.images) for page in doc.pages)
    return text, images


@pytest_asyncio.fixture
async def branded(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, rfq_rows: People
) -> AsyncIterator[tuple[People, FakeStorage]]:
    yield rfq_rows, FakeStorage()
    async with tenant_session(migrator_engine, rfq_rows.tenant) as session:
        await session.execute(
            update(Rfq).where(Rfq.tenant_id == rfq_rows.tenant).values(logo_asset_id=None)
        )
        await session.execute(
            delete(CompanyBranding).where(CompanyBranding.tenant_id == rfq_rows.tenant)
        )
        await session.execute(delete(CompanyLogo).where(CompanyLogo.tenant_id == rfq_rows.tenant))


async def _logo(
    engine: AsyncEngine, storage: FakeStorage, tenant: UUID, data: bytes, content_type: str
) -> UUID:
    logo_id = uuid4()
    ext = {"image/png": "png", "image/webp": "webp"}[content_type]
    key = f"branding/{tenant}/{logo_id}.{ext}"
    storage.blobs[key] = data
    async with tenant_session(engine, tenant) as session:
        session.add(
            CompanyLogo(
                id=logo_id,
                tenant_id=tenant,
                name="logo",
                content_type=content_type,
                size_bytes=len(data),
                storage_key=key,
            )
        )
    return logo_id


@pytest.mark.db
async def test_the_pdf_carries_the_company_logo_and_details(
    app_engine: AsyncEngine, branded: tuple[People, FakeStorage]
) -> None:
    people, storage = branded
    principal = buyer(people)
    logo_id = await _logo(app_engine, storage, people.tenant, _png((91, 63, 208)), "image/png")
    async with tenant_session(app_engine, people.tenant) as session:
        session.add(
            CompanyBranding(
                tenant_id=people.tenant,
                legal_name="Acme Componentes Ltda.",
                trade_name="Acme",
                tax_id="12.345.678/0001-90",
                address="Rua das Flores, 100",
                default_footer="Documento confidencial da Acme.",
                accent_color="#5B3FD0",
                default_logo_asset_id=logo_id,
            )
        )
    rfq = await make_rfq(app_engine, principal)
    supplier = await make_supplier(app_engine, principal, "Isar")
    invite = await service.add_invite(
        app_engine, principal, rfq.id, supplier.id, supplier.contacts[0].id
    )

    preview = await service.preview_document(
        app_engine, principal, rfq.id, invite.id, FakeWriter(), storage=storage
    )
    text, images = _pdf(preview.content)
    for expected in ("Acme", "Acme Componentes Ltda.", "12.345.678/0001-90", "confidencial"):
        assert expected in text, expected
    assert images >= 1  # the company logo, not a text-only header


@pytest.mark.db
async def test_the_rfqs_own_logo_and_terms_are_in_the_approved_arguments(
    app_engine: AsyncEngine, branded: tuple[People, FakeStorage]
) -> None:
    people, storage = branded
    principal = buyer(people)
    default_png, campaign_png = _png((91, 63, 208)), _png((200, 40, 40))
    default_id = await _logo(app_engine, storage, people.tenant, default_png, "image/png")
    campaign_id = await _logo(app_engine, storage, people.tenant, campaign_png, "image/png")
    async with tenant_session(app_engine, people.tenant) as session:
        session.add(
            CompanyBranding(
                tenant_id=people.tenant,
                trade_name="Acme",
                default_footer="Padrão",
                default_logo_asset_id=default_id,
            )
        )
    rfq = await make_rfq(app_engine, principal)
    supplier = await make_supplier(app_engine, principal, "Main")
    invite = await service.add_invite(
        app_engine, principal, rfq.id, supplier.id, supplier.contacts[0].id
    )
    await service.update_rfq(
        app_engine,
        principal,
        rfq.id,
        UpdateRfqRequest(logo_asset_id=campaign_id, footer_override="Condições desta cotação"),
    )

    await service.prepare_send(
        app_engine, principal, rfq.id, FakeWriter(), FakeMailboxes(), storage=storage
    )
    async with tenant_session(app_engine, people.tenant) as session:
        approval = await session.scalar(
            select(Approval).where(Approval.tool_call_id == send_proposal_ids(invite.id)[1])
        )
    assert approval is not None
    args = SendRfqEmailArgs.model_validate(approval.args)
    assert args.document is not None
    branding = args.document.branding
    assert branding.logo_base64 == base64.b64encode(campaign_png).decode()
    assert branding.footer_text == "Condições desta cotação"
    # What the approver approves (and the hash checks) includes the logo and the terms.
    assert approval.args_hash == args_hash(approval.args)
    plain = approval.args | {
        "document": args.document.model_copy(
            update={"branding": branding.model_copy(update={"footer_text": "Padrão"})}
        ).model_dump(mode="json")
    }
    assert args_hash(plain) != approval.args_hash


@pytest.mark.db
async def test_a_large_webp_logo_is_made_to_fit_the_document(
    app_engine: AsyncEngine, branded: tuple[People, FakeStorage]
) -> None:
    people, storage = branded
    principal = buyer(people)
    logo_id = await _logo(app_engine, storage, people.tenant, _webp(), "image/webp")
    async with tenant_session(app_engine, people.tenant) as session:
        session.add(CompanyBranding(tenant_id=people.tenant, default_logo_asset_id=logo_id))
    rfq = await make_rfq(app_engine, principal)
    supplier = await make_supplier(app_engine, principal, "Lech")
    invite = await service.add_invite(
        app_engine, principal, rfq.id, supplier.id, supplier.contacts[0].id
    )
    preview = await service.preview_document(
        app_engine, principal, rfq.id, invite.id, FakeWriter(), storage=storage
    )
    _, images = _pdf(preview.content)
    assert images >= 1

    # A logo whose bytes are gone never fails the document.
    storage.blobs.clear()
    again = await service.preview_document(
        app_engine, principal, rfq.id, invite.id, FakeWriter(), storage=storage
    )
    assert again.content.startswith(b"%PDF")


def _logo_width_in_pdf(data: bytes) -> float:
    with pdfplumber.open(io.BytesIO(data)) as doc:
        image = doc.pages[0].images[0]
        return float(image["x1"]) - float(image["x0"])


def _logo_x0_in_pdf(data: bytes) -> float:
    with pdfplumber.open(io.BytesIO(data)) as doc:
        return float(doc.pages[0].images[0]["x0"])


@pytest.mark.db
async def test_logo_placement_reaches_the_pdf_and_the_approval(
    app_engine: AsyncEngine, branded: tuple[People, FakeStorage]
) -> None:
    people, storage = branded
    principal = buyer(people)
    logo_id = await _logo(
        app_engine, storage, people.tenant, _png((91, 63, 208), (200, 40)), "image/png"
    )
    async with tenant_session(app_engine, people.tenant) as session:
        session.add(
            CompanyBranding(
                tenant_id=people.tenant,
                trade_name="Acme",
                default_logo_asset_id=logo_id,
                logo_scale=Decimal("1.5"),
                logo_align="center",
                logo_offset_y=-4,
            )
        )
    rfq = await make_rfq(app_engine, principal)
    supplier = await make_supplier(app_engine, principal, "Inn")
    invite = await service.add_invite(
        app_engine, principal, rfq.id, supplier.id, supplier.contacts[0].id
    )

    company = await service.preview_document(
        app_engine, principal, rfq.id, invite.id, FakeWriter(), storage=storage
    )
    # Scale 1.5: the 200 x 40 px logo is drawn 300 px wide, capped by its 240 px box.
    assert _logo_width_in_pdf(company.content) == pytest.approx(240 * 0.75, abs=2)

    await service.update_rfq(
        app_engine,
        principal,
        rfq.id,
        UpdateRfqRequest(logo_placement=LogoPlacement(scale=0.5, align="right", offset_y=0)),
    )
    own = await service.preview_document(
        app_engine, principal, rfq.id, invite.id, FakeWriter(), storage=storage
    )
    # Scale 0.5 (the RFQ's own): 100 px wide, pushed to the right of the 240 px box.
    assert _logo_width_in_pdf(own.content) == pytest.approx(100 * 0.75, abs=2)
    assert _logo_x0_in_pdf(own.content) > _logo_x0_in_pdf(company.content)

    await service.prepare_send(
        app_engine, principal, rfq.id, FakeWriter(), FakeMailboxes(), storage=storage
    )
    async with tenant_session(app_engine, people.tenant) as session:
        approval = await session.scalar(
            select(Approval).where(Approval.tool_call_id == send_proposal_ids(invite.id)[1])
        )
    assert approval is not None
    args = SendRfqEmailArgs.model_validate(approval.args)
    assert args.document is not None
    brand = args.document.branding
    assert (brand.logo_scale, brand.logo_align, brand.logo_offset_y) == (0.5, "right", 0)
    moved = approval.args | {
        "document": args.document.model_copy(
            update={"branding": brand.model_copy(update={"logo_align": "left"})}
        ).model_dump(mode="json")
    }
    assert args_hash(moved) != approval.args_hash  # placement is part of what is approved


@pytest.mark.db
async def test_a_transparent_logo_stays_transparent_in_the_pdf(
    app_engine: AsyncEngine, branded: tuple[People, FakeStorage]
) -> None:
    people, storage = branded
    principal = buyer(people)
    image = Image.new("RGBA", (200, 40), (0, 0, 0, 0))
    image.paste((91, 63, 208, 255), (50, 5, 150, 35))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    logo_id = await _logo(app_engine, storage, people.tenant, buffer.getvalue(), "image/png")
    async with tenant_session(app_engine, people.tenant) as session:
        session.add(CompanyBranding(tenant_id=people.tenant, default_logo_asset_id=logo_id))
    rfq = await make_rfq(app_engine, principal)
    supplier = await make_supplier(app_engine, principal, "Ems")
    invite = await service.add_invite(
        app_engine, principal, rfq.id, supplier.id, supplier.contacts[0].id
    )
    preview = await service.preview_document(
        app_engine, principal, rfq.id, invite.id, FakeWriter(), storage=storage
    )
    with pdfplumber.open(io.BytesIO(preview.content)) as doc:
        stream = doc.pages[0].images[0]["stream"]
        assert "SMask" in stream.attrs  # the alpha channel travels as a soft mask
