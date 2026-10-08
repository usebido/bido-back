"""`/v1/company`: branding details, the logo library, tenant isolation, and an RFQ's
own logo and footer (`PATCH /v1/rfqs/{id}`, drafts only)."""

import io
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from PIL import Image, ImageDraw
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.api.branding import branding_router
from bido_back.api.deps import (
    ApiResources,
    get_db,
    get_direct_uploads,
    get_download_links,
    get_principal,
    get_storage,
)
from bido_back.api.rfqs import rfqs_router
from bido_back.auth import Principal
from bido_back.branding import service
from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import CompanyBranding, CompanyLogo, Invite, Rfq, RfqLine
from bido_back.settings import Settings
from bido_back.storage.direct import PresignedPost, StoredObject
from bido_back.storage.links import Disposition, FileLink
from tests.approvals.conftest import People, people
from tests.db.conftest import app_engine, migrator_engine, two_tenants

from ..onboarding.conftest import FakeStorage

__all__ = ["app_engine", "migrator_engine", "people", "two_tenants"]

UNUSED_DB = "postgresql+asyncpg://app@localhost:5432/unused"


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (300, 100), (91, 63, 208, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeDirect:
    def __init__(self) -> None:
        self.forms: list[tuple[str, str, int]] = []

    async def presign_post(
        self, key: str, *, content_type: str, max_bytes: int, expires_in: timedelta
    ) -> PresignedPost:
        self.forms.append((key, content_type, max_bytes))
        return PresignedPost(
            url="https://uploads.test/",
            fields={"key": key, "Content-Type": content_type},
            expires_at=datetime.now(UTC) + expires_in,
        )

    async def stat(self, key: str) -> StoredObject | None:
        return None


class FakeLinks:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Disposition]] = []

    async def presign_get(
        self,
        key: str,
        *,
        filename: str,
        content_type: str,
        disposition: Disposition,
        expires_in: timedelta,
    ) -> FileLink:
        self.calls.append((key, content_type, disposition))
        return FileLink(url=f"https://files.test/{key}", expires_at=datetime.now(UTC) + expires_in)


class Api:
    def __init__(self, people: People, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.people = people
        self.sessionmaker = sessionmaker
        self.storage = FakeStorage()
        self.direct = FakeDirect()
        self.links = FakeLinks()
        self.principal = Principal(tenant_id=people.tenant, user_id=people.owner, role="owner")

    def client(self, principal: Principal | None = None) -> httpx.AsyncClient:
        app = FastAPI()
        app.state.resources = ApiResources(
            Settings.model_validate({"environment": "test", "database_url": UNUSED_DB})
        )
        app.include_router(branding_router())
        app.include_router(rfqs_router())
        who = principal or self.principal
        app.dependency_overrides[get_principal] = lambda: who
        app.dependency_overrides[get_db] = lambda: self.sessionmaker
        app.dependency_overrides[get_storage] = lambda: self.storage
        app.dependency_overrides[get_direct_uploads] = lambda: self.direct
        app.dependency_overrides[get_download_links] = lambda: self.links
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def upload(
        self, http: httpx.AsyncClient, filename: str, data: bytes
    ) -> dict[str, object]:
        started = await http.post(
            "/v1/company/logos", json={"filename": filename, "size_bytes": len(data)}
        )
        assert started.status_code == 201, started.text
        form = started.json()
        self.storage.blobs[form["fields"]["key"]] = data
        done = await http.post(
            f"/v1/company/logos/{form['logo_id']}/complete", json={"filename": filename}
        )
        assert done.status_code == 200, done.text
        return done.json()


@pytest_asyncio.fixture
async def api(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, people: People
) -> AsyncIterator[Api]:
    yield Api(people, create_sessionmaker(app_engine))
    for tenant_id in (people.tenant, people.other_tenant):
        async with tenant_session(migrator_engine, tenant_id) as session:
            await session.execute(
                update(Rfq).where(Rfq.tenant_id == tenant_id).values(logo_asset_id=None)
            )
            for model in (Invite, RfqLine, Rfq):
                await session.execute(delete(model).where(model.tenant_id == tenant_id))
            await session.execute(
                delete(CompanyBranding).where(CompanyBranding.tenant_id == tenant_id)
            )
            await session.execute(delete(CompanyLogo).where(CompanyLogo.tenant_id == tenant_id))


@pytest.mark.db
async def test_first_logo_becomes_the_default_and_branding_round_trips(api: Api) -> None:
    async with api.client() as http:
        empty = await http.get("/v1/company/branding")
        assert empty.status_code == 200 and empty.json()["default_logo"] is None

        logo = await api.upload(http, "Acme Logo.png", _png())
        assert logo["name"] == "Acme Logo" and logo["is_default"] is True
        assert (logo["width"], logo["height"], logo["content_type"]) == (300, 100, "image/png")
        key, content_type, max_bytes = api.direct.forms[0]
        assert key == f"branding/{api.people.tenant}/{logo['id']}.png"
        assert (content_type, max_bytes) == ("image/png", len(_png()))

        second = await api.upload(http, "mono.png", _png())
        assert second["is_default"] is False

        saved = await http.put(
            "/v1/company/branding",
            json={
                "legal_name": "Acme Componentes Ltda.",
                "tax_id": "12.345.678/0001-90",
                "address": "Rua A, 100\nSão Paulo - SP",
                "default_footer": "Documento confidencial.",
                "accent_color": "#5b3fd0",
                "default_logo_id": second["id"],
            },
        )
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["accent_color"] == "#5B3FD0"
        assert body["default_logo"]["id"] == second["id"]

        listed = (await http.get("/v1/company/logos")).json()["logos"]
        assert {each["id"]: each["is_default"] for each in listed} == {
            logo["id"]: False,
            second["id"]: True,
        }
        made = await http.post(f"/v1/company/logos/{logo['id']}/default")
        assert made.json()["default_logo"]["id"] == logo["id"]

        link = await http.get(f"/v1/company/logos/{logo['id']}/file")
        assert link.status_code == 200
        assert api.links.calls[-1][1:] == ("image/png", "inline")

        renamed = await http.patch(f"/v1/company/logos/{logo['id']}", json={"name": "Principal"})
        assert renamed.json()["name"] == "Principal"

        deleted = await http.delete(f"/v1/company/logos/{logo['id']}")
        assert deleted.status_code == 204
        after = (await http.get("/v1/company/branding")).json()
        assert after["default_logo"] is None  # fell back, not a dangling id


@pytest.mark.db
async def test_uploads_are_checked(api: Api) -> None:
    async with api.client() as http:
        too_big = await http.post(
            "/v1/company/logos", json={"filename": "big.png", "size_bytes": 3 * 1024 * 1024}
        )
        assert too_big.status_code == 422
        wrong_type = await http.post(
            "/v1/company/logos", json={"filename": "page.html", "size_bytes": 10}
        )
        assert wrong_type.status_code == 422

        started = (
            await http.post("/v1/company/logos", json={"filename": "fake.png", "size_bytes": 10})
        ).json()
        missing = await http.post(
            f"/v1/company/logos/{started['logo_id']}/complete", json={"filename": "fake.png"}
        )
        assert missing.status_code == 404
        api.storage.blobs[started["fields"]["key"]] = b"<html>not a png</html>"
        rejected = await http.post(
            f"/v1/company/logos/{started['logo_id']}/complete", json={"filename": "fake.png"}
        )
        assert rejected.status_code == 422

        svg = (
            b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 40" onload="x()">'
            b"<script>alert(1)</script><rect width='100' height='40'/></svg>"
        )
        logo = await api.upload(http, "vector.svg", svg)
        stored = api.storage.blobs[f"branding/{api.people.tenant}/{logo['id']}.svg"]
        assert b"script" not in stored and b"onload" not in stored and b"<rect" in stored
        assert (logo["width"], logo["height"]) == (100, 40)


@pytest.mark.db
async def test_another_tenant_sees_nothing(api: Api) -> None:
    async with api.client() as http:
        logo = await api.upload(http, "acme.png", _png())
    other = Principal(
        tenant_id=api.people.other_tenant, user_id=api.people.other_approver, role="approver"
    )
    async with api.client(other) as http:
        assert (await http.get("/v1/company/logos")).json() == {"logos": []}
        assert (await http.get("/v1/company/branding")).json()["default_logo"] is None
        for response in (
            await http.get(f"/v1/company/logos/{logo['id']}/file"),
            await http.post(f"/v1/company/logos/{logo['id']}/default"),
            await http.delete(f"/v1/company/logos/{logo['id']}"),
            await http.put("/v1/company/branding", json={"default_logo_id": logo["id"]}),
        ):
            assert response.status_code == 404


async def _draft_rfq(engine: AsyncEngine, people: People) -> UUID:
    async with tenant_session(engine, people.tenant) as session:
        rfq = Rfq(
            tenant_id=people.tenant,
            ref=f"RFQ-{uuid4().hex[:6]}",
            title="Chapas",
            created_by=people.owner,
        )
        session.add(rfq)
        await session.flush()
        return rfq.id


@pytest.mark.db
async def test_an_rfq_picks_its_logo_and_footer_while_draft(
    api: Api, app_engine: AsyncEngine
) -> None:
    rfq_id = await _draft_rfq(app_engine, api.people)
    async with api.client() as http:
        default = await api.upload(http, "acme.png", _png())
        special = await api.upload(http, "campanha.png", _png())
        await http.put(
            "/v1/company/branding",
            json={
                "trade_name": "Acme",
                "default_footer": "Padrão",
                "default_logo_id": default["id"],
            },
        )

        branding = await service.rfq_branding(
            api.sessionmaker, api.storage, api.people.tenant, rfq_id
        )
        assert branding.company_name == "Acme" and branding.footer_text == "Padrão"
        # The prepared logo: a PNG of the same mark (a solid block has no margins to trim).
        assert branding.logo_bytes is not None and branding.logo_content_type == "image/png"
        assert Image.open(io.BytesIO(branding.logo_bytes)).size == (300, 100)

        patched = await http.patch(
            f"/v1/rfqs/{rfq_id}",
            json={"logo_asset_id": special["id"], "footer_override": "Condições desta cotação"},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["logo_asset_id"] == special["id"]
        assert patched.json()["footer_override"] == "Condições desta cotação"
        branding = await service.rfq_branding(
            api.sessionmaker, api.storage, api.people.tenant, rfq_id
        )
        assert branding.footer_text == "Condições desta cotação"

        unknown = await http.patch(f"/v1/rfqs/{rfq_id}", json={"logo_asset_id": str(uuid4())})
        assert unknown.status_code == 404

        back = await http.patch(
            f"/v1/rfqs/{rfq_id}", json={"logo_asset_id": None, "footer_override": None}
        )
        assert back.json()["logo_asset_id"] is None and back.json()["footer_override"] is None

    async with tenant_session(app_engine, api.people.tenant) as session:
        await session.execute(update(Rfq).where(Rfq.id == rfq_id).values(status="open"))
    async with api.client() as http:
        frozen = await http.patch(f"/v1/rfqs/{rfq_id}", json={"logo_asset_id": special["id"]})
        assert frozen.status_code == 409


def _logo_on_white() -> bytes:
    """A mark with wide white margins, as many logos found online are."""
    image = Image.new("RGB", (400, 200), (255, 255, 255))
    ImageDraw.Draw(image).ellipse((150, 50, 250, 150), fill=(220, 30, 30))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.db
async def test_uploads_are_prepared_and_the_background_can_be_removed_and_restored(
    api: Api,
) -> None:
    async with api.client() as http:
        logo = await api.upload(http, "acme.png", _logo_on_white())
        original_key = f"branding/{api.people.tenant}/{logo['id']}.png"
        assert api.storage.blobs[original_key] == _logo_on_white()  # the upload, untouched
        assert (logo["width"], logo["height"]) == (109, 109)  # margins trimmed
        assert logo["background_removable"] is True and logo["background_removed"] is False
        assert logo["prepared"] is True

        removed = await http.post(
            f"/v1/company/logos/{logo['id']}/background", json={"remove": True}
        )
        assert removed.status_code == 200, removed.text
        assert removed.json()["background_removed"] is True
        nobg = api.storage.blobs[f"branding/{api.people.tenant}/{logo['id']}.nobg.png"]
        corner = Image.open(io.BytesIO(nobg)).convert("RGBA").getpixel((0, 0))
        assert isinstance(corner, tuple) and corner[3] == 0

        await http.get(f"/v1/company/logos/{logo['id']}/file")
        assert api.links.calls[-1][0].endswith(".nobg.png")  # what is shown and printed

        restored = await http.post(
            f"/v1/company/logos/{logo['id']}/background", json={"remove": False}
        )
        assert restored.json()["background_removed"] is False
        await http.get(f"/v1/company/logos/{logo['id']}/file")
        assert api.links.calls[-1][0].endswith(".prepared.png")


@pytest.mark.db
async def test_background_removal_is_refused_when_there_is_none(api: Api) -> None:
    async with api.client() as http:
        transparent = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
        ImageDraw.Draw(transparent).rectangle((50, 25, 150, 75), fill=(10, 10, 10, 255))
        buffer = io.BytesIO()
        transparent.save(buffer, format="PNG")
        logo = await api.upload(http, "clean.png", buffer.getvalue())
        assert logo["background_removable"] is False
        refused = await http.post(
            f"/v1/company/logos/{logo['id']}/background", json={"remove": True}
        )
        assert refused.status_code == 422


@pytest.mark.db
async def test_a_logo_from_before_preparation_is_prepared_on_request(
    api: Api, app_engine: AsyncEngine
) -> None:
    logo_id = uuid4()
    key = f"branding/{api.people.tenant}/{logo_id}.png"
    api.storage.blobs[key] = _logo_on_white()
    async with tenant_session(app_engine, api.people.tenant) as session:
        session.add(
            CompanyLogo(
                id=logo_id,
                tenant_id=api.people.tenant,
                name="old",
                content_type="image/png",
                size_bytes=len(_logo_on_white()),
                width=400,
                height=200,
                storage_key=key,
            )
        )
    async with api.client() as http:
        listed = (await http.get("/v1/company/logos")).json()["logos"]
        assert [each["prepared"] for each in listed] == [False]
        prepared = await http.post(
            f"/v1/company/logos/{logo_id}/background", json={"remove": False}
        )
        assert prepared.status_code == 200, prepared.text
        body = prepared.json()
        assert body["prepared"] is True and body["background_removable"] is True
        assert (body["width"], body["height"]) == (109, 109)
        assert api.storage.blobs[key] == _logo_on_white()  # the original stays


@pytest.mark.db
async def test_logo_placement_defaults_bounds_and_rfq_override(
    api: Api, app_engine: AsyncEngine
) -> None:
    rfq_id = await _draft_rfq(app_engine, api.people)
    async with api.client() as http:
        assert (await http.get("/v1/company/branding")).json()["logo_placement"] == {
            "scale": 1.0,
            "align": "left",
            "offset_y": 0,
        }
        for bad in ({"scale": 3}, {"scale": 0.2}, {"align": "top"}, {"offset_y": 40}):
            refused = await http.put("/v1/company/branding", json={"logo_placement": bad})
            assert refused.status_code == 422, bad
        saved = await http.put(
            "/v1/company/branding",
            json={"logo_placement": {"scale": 1.5, "align": "center", "offset_y": -6}},
        )
        assert saved.json()["logo_placement"] == {"scale": 1.5, "align": "center", "offset_y": -6}

        rfq = (await http.get(f"/v1/rfqs/{rfq_id}")).json()
        assert rfq["logo_placement"] is None  # the company default
        patched = await http.patch(
            f"/v1/rfqs/{rfq_id}", json={"logo_placement": {"scale": 0.75, "align": "right"}}
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["logo_placement"] == {"scale": 0.75, "align": "right", "offset_y": 0}
        cleared = await http.patch(f"/v1/rfqs/{rfq_id}", json={"logo_placement": None})
        assert cleared.json()["logo_placement"] is None
        bad = await http.patch(f"/v1/rfqs/{rfq_id}", json={"logo_placement": {"scale": 9}})
        assert bad.status_code == 422
