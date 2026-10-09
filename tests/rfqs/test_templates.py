"""RFQ templates after onboarding, against Postgres: versions, variants, defaults, resolution."""

from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import Rfq, RfqLine, RfqTemplate, RfqTemplateVersion
from bido_back.onboarding import service as onboarding
from bido_back.rfqs.errors import InvalidState, NotFound
from bido_back.templates import service
from bido_back.templates.schemas import TemplateEdit
from bido_back.templates.service import STANDARD, RequiredFieldsRemoved
from tests.approvals.conftest import People

from .test_api import Api, api
from .test_service import buyer, make_rfq, make_supplier, pdf_text_of

pytestmark = pytest.mark.db

__all__ = ["api"]  # the HTTP fixture, shared with test_api

COLUMNS = ["Item", "Descrição", "Qtd.", "Un.", "Código"]
FOOTER = ["Prazo de entrega", "Validade da proposta", "Condição de pagamento"]


async def ready_template(
    engine: AsyncEngine,
    tenant_id: UUID,
    *,
    greeting: str = "Prezados, solicitamos cotação.",
    category: str | None = None,
    is_default: bool = False,
    status: str = "ready",
) -> UUID:
    async with tenant_session(engine, tenant_id) as session:
        row = RfqTemplate(
            tenant_id=tenant_id,
            source="search",
            status=status,
            language="pt",
            greeting=greeting if status == "ready" else None,
            columns=list(COLUMNS),
            footer=" · ".join(FOOTER),
            fields=[*COLUMNS, *FOOTER],
            category=category,
            is_default=is_default,
        )
        session.add(row)
        await session.flush()
        return row.id


def edit(greeting: str, *, footer: list[str] | None = None) -> TemplateEdit:
    return TemplateEdit(
        language="pt",
        greeting=greeting,
        columns=list(COLUMNS),
        footer_fields=list(FOOTER if footer is None else footer),
        fields=[],
    )


async def test_editing_writes_a_new_version_and_keeps_the_old_one(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    principal = buyer(rfq_rows)
    template_id = await ready_template(app_engine, rfq_rows.tenant)

    edited = await service.edit_template(app_engine, principal, template_id, edit("Olá, v2"))
    again = await service.edit_template(app_engine, principal, template_id, edit("Olá, v3"))

    assert (edited.version, again.version) == (2, 3)
    assert again.greeting == "Olá, v3" and again.footer_fields == FOOTER
    versions = await service.list_versions(app_engine, principal, template_id)
    assert [(v.version, v.current, v.greeting) for v in versions] == [
        (3, True, "Olá, v3"),
        (2, False, "Olá, v2"),
        (1, False, "Prezados, solicitamos cotação."),
    ]


async def test_a_template_never_edited_has_one_version(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    template_id = await ready_template(app_engine, rfq_rows.tenant)

    [only] = await service.list_versions(app_engine, buyer(rfq_rows), template_id)

    assert (only.version, only.current, only.columns) == (1, True, COLUMNS)


async def test_versions_cannot_be_changed_by_the_app_role(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    template_id = await ready_template(app_engine, rfq_rows.tenant)
    await service.edit_template(app_engine, buyer(rfq_rows), template_id, edit("Olá, v2"))

    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_session(app_engine, rfq_rows.tenant) as session:
            await session.execute(
                text(
                    "UPDATE rfq_template_versions SET greeting = 'forged' WHERE template_id = :id"
                ),
                {"id": template_id},
            )


async def test_an_edit_may_not_drop_required_fields(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    template_id = await ready_template(app_engine, rfq_rows.tenant)

    with pytest.raises(RequiredFieldsRemoved) as refused:
        await service.edit_template(
            app_engine, buyer(rfq_rows), template_id, edit("Olá", footer=["Frete"])
        )

    assert refused.value.keys == ["lead_time", "validity"]
    current = await service.get_template(app_engine, buyer(rfq_rows), template_id)
    assert current.version == 1


async def test_missing_required_fields_are_suggested(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    template_id = await ready_template(app_engine, rfq_rows.tenant)

    detail = await service.get_template(app_engine, buyer(rfq_rows), template_id)

    assert [(f.key, f.label) for f in detail.missing_required] == [
        ("manufacturer", "Fabricante"),
        ("need_by", "Data de entrega desejada"),
        ("incoterm_place", "Incoterm e local de entrega"),
        ("qualification", "Qualificação (ex.: AEC-Q200) e certificados"),
        ("packaging", "Embalagem"),
        ("rohs_reach", "Declarações RoHS e REACH"),
        ("moq", "Quantidade mínima (MOQ)"),
    ]


async def test_only_ready_templates_can_be_edited_and_other_tenants_see_nothing(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    building = await ready_template(app_engine, rfq_rows.tenant, status="building")
    with pytest.raises(InvalidState):
        await service.edit_template(app_engine, buyer(rfq_rows), building, edit("Olá"))

    mine = await ready_template(app_engine, rfq_rows.tenant)
    stranger = Principal(tenant_id=rfq_rows.other_tenant, user_id=rfq_rows.other_approver)
    with pytest.raises(NotFound):
        await service.get_template(app_engine, stranger, mine)


async def test_variants_have_their_own_default_per_category(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    principal = buyer(rfq_rows)
    general = await ready_template(app_engine, rfq_rows.tenant, is_default=True)

    variant = await service.create_variant(app_engine, principal, general, "drawing")
    assert (variant.category, variant.is_default, variant.version) == ("drawing", False, 1)
    await service.make_default(app_engine, principal, variant.id)
    # Onboarding's "use this template" on the general one leaves the variant's default alone.
    await onboarding.use_template(app_engine, principal, general)

    listed = {t.id: t for t in await service.list_templates(app_engine, principal)}
    assert listed[general].is_default and listed[variant.id].is_default

    second = await service.create_variant(app_engine, principal, general, "drawing")
    await service.make_default(app_engine, principal, second.id)
    listed = {t.id: t for t in await service.list_templates(app_engine, principal)}
    assert listed[second.id].is_default and not listed[variant.id].is_default
    assert listed[general].is_default
    state = await onboarding.get_state(app_engine, principal)
    assert state.default_template is not None and state.default_template.id == general


async def test_resolution_prefers_the_category_variant_then_general_then_standard(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    principal = buyer(rfq_rows)
    rfq = await make_rfq(app_engine, principal)  # both lines are "catalog"

    async def resolve() -> service.ResolvedTemplate:
        async with tenant_session(app_engine, rfq_rows.tenant) as session:
            row = await session.get(Rfq, rfq.id)
            assert row is not None
            lines = list(await session.scalars(select(RfqLine).where(RfqLine.rfq_id == rfq.id)))
            return await service.resolve_for_rfq(session, row, lines)

    assert await resolve() == STANDARD

    general = await ready_template(app_engine, rfq_rows.tenant, greeting="Geral", is_default=True)
    assert (await resolve()).template_id == general

    variant = await service.create_variant(app_engine, principal, general, "catalog")
    await service.make_default(app_engine, principal, variant.id)
    resolved = await resolve()
    assert (resolved.template_id, resolved.version) == (variant.id, 1)
    assert resolved.content.greeting == "Geral"


async def test_preview_and_template_routes_over_http(
    api: Api, app_engine: AsyncEngine, rfq_rows: People
) -> None:
    http = api.http
    template_id = await ready_template(
        app_engine, rfq_rows.tenant, greeting="Prezados, segue RFQ.", is_default=True
    )

    listed = await http.get("/v1/rfq-templates")
    assert listed.status_code == 200, listed.text
    assert [t["id"] for t in listed.json()] == [str(template_id)]

    required = await http.get("/v1/rfq-templates/required-fields", params={"language": "de"})
    assert required.status_code == 200
    assert {"key": "lead_time", "kind": "ask", "label": "Lieferzeit", "conditional": False} in (
        required.json()
    )

    dropped = await http.put(
        f"/v1/rfq-templates/{template_id}",
        json={"language": "pt", "greeting": "Olá", "columns": COLUMNS, "footer_fields": []},
    )
    assert dropped.status_code == 422
    assert "lead_time" in dropped.json()["detail"]

    saved = await http.put(
        f"/v1/rfq-templates/{template_id}",
        json={
            "language": "pt",
            "greeting": "Olá, nova versão.",
            "columns": COLUMNS,
            "footer_fields": [*FOOTER, "Embalagem"],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 2
    assert "packaging" not in [f["key"] for f in saved.json()["missing_required"]]
    versions = (await http.get(f"/v1/rfq-templates/{template_id}/versions")).json()
    assert [v["version"] for v in versions] == [2, 1]

    variant = await http.post(
        f"/v1/rfq-templates/{template_id}/variants", json={"category": "cell"}
    )
    assert variant.status_code == 201, variant.text
    made = await http.post(f"/v1/rfq-templates/{variant.json()['id']}/default")
    assert made.status_code == 200 and made.json()["is_default"] is True

    rfq = (
        await http.post("/v1/rfqs", json={"text": "Need MLCC and connectors", "currency": "EUR"})
    ).json()
    supplier = await make_supplier(app_engine, buyer(rfq_rows), "Rhein")
    invite = await http.post(
        f"/v1/rfqs/{rfq['id']}/invites",
        json={"supplier_id": str(supplier.id), "contact_id": str(supplier.contacts[0].id)},
    )
    preview = await http.get(f"/v1/rfqs/{rfq['id']}/invites/{invite.json()['id']}/preview")
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["template_id"] == str(template_id) and body["template_version"] == 2
    assert body["language"] == "de"
    assert body["subject"].startswith(f"[{rfq['ref']}] Angebotsanfrage: ")
    # A cover note; the template's greeting (translated by the fake writer) opens the PDF.
    assert body["text"].startswith("Sehr geehrte Damen und Herren,")
    assert "<table" not in body["html"]
    assert body["attachment_name"] == f"{rfq['ref']}-rhein.pdf"
    pdf = await http.get(f"/v1/rfqs/{rfq['id']}/invites/{invite.json()['id']}/document.pdf")
    assert pdf.status_code == 200, pdf.text
    assert "<de> Olá, nova versão." in pdf_text_of(pdf.content)

    missing = await http.get(f"/v1/rfqs/{rfq['id']}/invites/{template_id}/preview")
    assert missing.status_code == 404


async def test_versions_are_tenant_isolated(app_engine: AsyncEngine, rfq_rows: People) -> None:
    template_id = await ready_template(app_engine, rfq_rows.tenant)
    await service.edit_template(app_engine, buyer(rfq_rows), template_id, edit("Olá, v2"))

    async with tenant_session(app_engine, rfq_rows.other_tenant) as session:
        seen = list(await session.scalars(select(RfqTemplateVersion)))

    assert seen == []
