"""The assessment -> RFQ handoff starts the negotiation where the analysis left it.

`draft_from_snapshot` gives each RFQ line the internal estimate as its default
limits (converted into the RFQ's currency) and invites the assessed suppliers as
DRAFT invites, resolved to the buyer's own supplier records or created from the
evidence's company name and email. Nothing is sent or approved. Assessments are
built from the contract (`SourceAssessment`), as saved snapshots.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.api.deps import get_db, get_principal
from bido_back.api.procurement_assessments import procurement_assessments_router
from bido_back.auth import Principal
from bido_back.db import create_sessionmaker, tenant_session
from bido_back.db.models import (
    Approval,
    Base,
    Contact,
    FollowupMessage,
    FxRate,
    Invite,
    MandateSend,
    Message,
    NegotiationMandate,
    ProcurementAssessment,
    Rfq,
    RfqLine,
    Supplier,
    SupplierContact,
)
from bido_back.purchasing.sources.contracts import (
    AssessmentLine,
    Coverage,
    Issue,
    PriceBenchmark,
    SourceAssessment,
    SourceRef,
    SupplierEvidence,
)
from bido_back.purchasing.sources.handoff import AssessmentSkippedSupplier, wanted_suppliers
from bido_back.purchasing.sources.snapshots import (
    AssessmentRfqRequest,
    AssessmentRfqResult,
    draft_from_snapshot,
    save_snapshot,
)
from bido_back.rfqs import suppliers
from bido_back.rfqs.schemas import ContactIn, CreateSupplierRequest
from tests.approvals.conftest import People
from tests.purchasing.test_api import build_app
from tests.rfqs.test_service import buyer

pytestmark = pytest.mark.db

# Far in the past, so no real rate is the latest one; XTS is ISO 4217's test code.
RATE_DAY = date(2000, 1, 4)
REF = SourceRef(source_id="erp", table="ZMM_042", row=5, column=4)
# SupplierEvidence fields Bido measures, added after the first snapshots were saved.
MEASURED = (
    "orders",
    "quantity",
    "avg_price",
    "min_price",
    "on_time_rate",
    "quality_rate",
    "delivery_observations",
    "quality_observations",
    "avg_lead_time_days",
    "last_received_at",
    "suggested",
)


def supplier(
    name: str | None,
    email: str | None,
    *,
    code: str | None = None,
    suggested: bool = True,
    status: str | None = None,
    history: bool = True,
) -> SupplierEvidence:
    return SupplierEvidence(
        supplier_code=code,
        name=name,
        email=email,
        last_price=Decimal("41.28") if history else None,
        currency="BRL" if history else None,
        ordered_at=date(2026, 6, 11) if history else None,
        last_price_basis="received_order",
        qualification_status=status,
        evidence=[REF],
        orders=2 if history else 0,
        suggested=suggested,
    )


def estimate(currency: str, target: str, maximum: str) -> PriceBenchmark:
    return PriceBenchmark(
        currency=currency,
        observations=3,
        window_days=365,
        last_price=Decimal(target),
        last_supplier="MetalNova Teste",
        last_ordered_at=date(2026, 6, 11),
        avg_price=Decimal(target),
        min_price=Decimal(target),
        target_price=Decimal(target),
        max_price=Decimal(maximum),
        evidence=[REF],
    )


def line(
    part: str,
    quantity: str = "50",
    *,
    suppliers: Sequence[SupplierEvidence] = (),
    price: PriceBenchmark | None = None,
    ready: bool = True,
) -> AssessmentLine:
    return AssessmentLine(
        part_number=part,
        description=f"Component {part}",
        location="Planta A",
        unit="pcs",
        need_by=date(2026, 10, 22),
        demand=Decimal(quantity),
        available=Decimal(0),
        on_order=Decimal(0),
        safety_stock=Decimal(0),
        calculated_to_buy=Decimal(quantity),
        currency="BRL",
        ready=ready,
        suppliers=list(suppliers),
        evidence=[REF],
        issues=[]
        if ready
        else [Issue(code="unverified_bom", severity="blocker", message="BOM not validated.")],
        price=price,
    )


def assessment(*lines: AssessmentLine) -> SourceAssessment:
    return SourceAssessment(
        lines=list(lines),
        status="assessed" if all(item.ready for item in lines) else "needs_review",
        coverage=[
            Coverage(
                source_id="erp",
                filename="Exportacao_ERP_Industrial.xlsx",
                table="ZMM_042",
                rows=12,
                mapped_rows=12,
                inspected=True,
            )
        ],
    )


async def saved(
    engine: AsyncEngine, who: Principal, source: SourceAssessment, key: str = "handoff"
) -> UUID:
    snapshot = await save_snapshot(
        engine,
        who,
        source_key=key,
        upload_ids=[uuid4()],
        objective="Buy what MTR-100 needs at Planta A",
        assessment=source,
    )
    assert snapshot.assessment_id is not None
    return snapshot.assessment_id


def request(currency: str = "BRL", **changes: object) -> AssessmentRfqRequest:
    return AssessmentRfqRequest.model_validate(
        {"title": "MTR-100 components", "base_currency": currency, **changes}
    )


async def add_supplier(engine: AsyncEngine, who: Principal, name: str, *emails: str) -> UUID:
    created = await suppliers.create_supplier(
        engine,
        who,
        CreateSupplierRequest(name=name, contacts=[ContactIn(email=email) for email in emails]),
    )
    return created.id


async def counts(engine: AsyncEngine, tenant_id: UUID) -> dict[str, int]:
    models: tuple[type[Base], ...] = (
        Supplier,
        Contact,
        SupplierContact,
        Invite,
        Rfq,
        Approval,
        Message,
        NegotiationMandate,
        MandateSend,
        FollowupMessage,
    )
    async with tenant_session(engine, tenant_id) as session:
        return {
            model.__tablename__: int(
                await session.scalar(select(func.count()).select_from(model)) or 0
            )
            for model in models
        }


async def invites_of(engine: AsyncEngine, who: Principal, rfq_id: UUID) -> list[tuple[str, str]]:
    """(supplier name, contact email) of each invite, by name; all must be drafts."""
    async with tenant_session(engine, who.tenant_id) as session:
        rows = (
            await session.execute(
                select(Invite.status, Supplier.name, Contact.email)
                .join(Supplier, Supplier.id == Invite.supplier_id)
                .join(Contact, Contact.id == Invite.contact_id)
                .where(Invite.rfq_id == rfq_id)
            )
        ).all()
    assert {status for status, _, _ in rows} <= {"draft"}
    return sorted((name, email or "") for _, name, email in rows)


def nothing_sent_or_approved(after: dict[str, int]) -> bool:
    return all(
        after[table] == 0
        for table in (
            "approvals",
            "messages",
            "negotiation_mandates",
            "mandate_sends",
            "followup_messages",
        )
    )


@pytest_asyncio.fixture
async def xts_rate(migrator_engine: AsyncEngine) -> AsyncIterator[None]:
    async with migrator_engine.begin() as conn:
        await conn.execute(
            insert(FxRate).values(rate_date=RATE_DAY, currency="XTS", per_eur=Decimal(2))
        )
    yield
    async with migrator_engine.begin() as conn:
        await conn.execute(delete(FxRate).where(FxRate.rate_date == RATE_DAY))


# --- Limits ------------------------------------------------------------------------------


async def test_the_internal_estimate_becomes_each_lines_default_limits(
    app_engine: AsyncEngine, purchasing_rows: People, xts_rate: None
) -> None:
    who = buyer(purchasing_rows)
    assessment_id = await saved(
        app_engine,
        who,
        assessment(
            line("000101", price=estimate("EUR", "12.5", "13.125")),
            # 2 XTS per EUR: 100 XTS -> 50 EUR.
            line("000102", price=estimate(" xts ", "100", "110")),
            line("000103"),  # never bought: no estimate
            line("000104", price=estimate("XXX", "9", "10")),  # no rate into EUR
        ),
    )

    result = await draft_from_snapshot(app_engine, who, assessment_id, request("EUR"))

    assert result.lines_without_limits == [3, 4]
    async with tenant_session(app_engine, who.tenant_id) as session:
        limits = [
            (row.position, row.target_unit_price, row.max_unit_price)
            for row in await session.scalars(
                select(RfqLine).where(RfqLine.rfq_id == result.rfq_id).order_by(RfqLine.position)
            )
        ]
    assert limits == [
        (1, Decimal("12.5"), Decimal("13.125")),
        (2, Decimal(50), Decimal(55)),
        (3, None, None),
        (4, None, None),
    ]
    # Defaults only: no mandate or approval exists until the buyer approves one.
    assert nothing_sent_or_approved(await counts(app_engine, who.tenant_id))


# --- Suppliers -----------------------------------------------------------------------------


async def test_suggested_suppliers_are_invited_as_drafts_and_nothing_is_sent(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    who = buyer(purchasing_rows)
    known = await add_supplier(app_engine, who, "MetalNova Teste", "vendas@metalnova.example.test")
    assessment_id = await saved(
        app_engine,
        who,
        assessment(
            line(
                "000101",
                suppliers=[
                    supplier("MetalNova Teste", "VENDAS@metalnova.example.test", code="F001"),
                    supplier("PrecisaoSul Teste", "vendas@precisaosul.example.test", code="F002"),
                    # Bought from, but not among the suggested: not invited.
                    supplier(
                        "MultiPecas Teste",
                        "vendas@multipecas.example.test",
                        code="F003",
                        suggested=False,
                    ),
                ],
                price=estimate("BRL", "41.28", "43.34"),
            ),
            line(
                "000106",
                suppliers=[supplier("MetalNova Teste", "vendas@metalnova.example.test")],
            ),
        ),
    )
    before = await counts(app_engine, who.tenant_id)

    result = await draft_from_snapshot(app_engine, who, assessment_id, request())

    created = next(s for s in result.invited if s.name == "PrecisaoSul Teste")
    assert [
        (s.supplier_id == known, s.name, s.email, s.supplier_created, s.part_numbers)
        for s in result.invited
    ] == [
        (True, "MetalNova Teste", "vendas@metalnova.example.test", False, ["000101", "000106"]),
        (False, "PrecisaoSul Teste", "vendas@precisaosul.example.test", True, ["000101"]),
    ]
    assert result.skipped_suppliers == [] and result.lines_without_limits == [2]
    assert await invites_of(app_engine, who, result.rfq_id) == [
        ("MetalNova Teste", "vendas@metalnova.example.test"),
        ("PrecisaoSul Teste", "vendas@precisaosul.example.test"),
    ]
    after = await counts(app_engine, who.tenant_id)
    assert after["suppliers"] == before["suppliers"] + 1
    assert after["contacts"] == before["contacts"] + 1
    assert after["invites"] == 2
    assert nothing_sent_or_approved(after)
    async with tenant_session(app_engine, who.tenant_id) as session:
        record = await session.get(Supplier, created.supplier_id)
        # Created through the supplier service with the evidence's company name only.
        assert record is not None and record.name == "PrecisaoSul Teste"


async def test_the_buyers_suppliers_are_found_by_email_or_normalized_name(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    who = buyer(purchasing_rows)
    by_email = await add_supplier(
        app_engine, who, "Metalnova Industria Ltda", "Vendas@MetalNova.example.test"
    )
    by_name = await add_supplier(
        app_engine, who, "PrecisaoSul Teste", "comercial@precisaosul.example.test"
    )
    without_contact = await add_supplier(app_engine, who, "MultiPecas Teste")
    assessment_id = await saved(
        app_engine,
        who,
        assessment(
            line(
                "000101",
                suppliers=[
                    # Another spelling of the company: the address identifies it.
                    supplier("MetalNova Teste", "vendas@metalnova.example.test"),
                    # No address in the files: the buyer's own contact is used.
                    supplier("  precisaosul   TESTE ", None),
                    # The buyer's supplier has no contact yet: the files' address is added.
                    supplier("MultiPecas Teste", "vendas@multipecas.example.test"),
                ],
            )
        ),
    )
    before = await counts(app_engine, who.tenant_id)

    result = await draft_from_snapshot(app_engine, who, assessment_id, request())

    assert [
        (s.supplier_id, s.email, s.supplier_created, s.contact_created) for s in result.invited
    ] == [
        (by_email, "Vendas@MetalNova.example.test", False, False),
        (by_name, "comercial@precisaosul.example.test", False, False),
        (without_contact, "vendas@multipecas.example.test", False, True),
    ]
    after = await counts(app_engine, who.tenant_id)
    assert after["suppliers"] == before["suppliers"]
    assert after["contacts"] == before["contacts"] + 1


async def test_suppliers_that_cannot_be_identified_or_reached_are_skipped_with_why(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    who = buyer(purchasing_rows)
    await add_supplier(app_engine, who, "Duplicada Teste", "a@duplicada.example.test")
    await add_supplier(app_engine, who, "DUPLICADA teste", "b@duplicada.example.test")
    no_contact = await add_supplier(app_engine, who, "Sem Contato Teste")
    assessment_id = await saved(
        app_engine,
        who,
        assessment(
            line(
                "000101",
                suppliers=[
                    supplier("Sem Email Teste", None, code="F010"),
                    # A contact or department is never taken as the company name.
                    supplier(None, "compras@anonima.example.test", code="F011"),
                    supplier("Formato Ruim Teste", "vendas at formato.example.test"),
                ],
            ),
            line(
                "000102",
                suppliers=[
                    supplier("Duplicada Teste", None),
                    supplier("Sem Contato Teste", None),
                    supplier(
                        "Bloqueada Teste", "vendas@bloqueada.example.test", status="Bloqueado"
                    ),
                ],
            ),
        ),
    )
    before = await counts(app_engine, who.tenant_id)

    result = await draft_from_snapshot(app_engine, who, assessment_id, request())

    assert result.invited == []
    assert result.skipped_suppliers == [
        AssessmentSkippedSupplier(
            name="Sem Email Teste",
            supplier_code="F010",
            reason="no_contact_email",
            part_numbers=["000101"],
        ),
        AssessmentSkippedSupplier(
            email="compras@anonima.example.test",
            supplier_code="F011",
            reason="no_company_name",
            part_numbers=["000101"],
        ),
        AssessmentSkippedSupplier(
            name="Formato Ruim Teste",
            email="vendas at formato.example.test",
            reason="invalid_email",
            part_numbers=["000101"],
        ),
        AssessmentSkippedSupplier(
            name="Duplicada Teste", reason="ambiguous_identity", part_numbers=["000102"]
        ),
        AssessmentSkippedSupplier(
            supplier_id=no_contact,
            name="Sem Contato Teste",
            reason="no_contact_email",
            part_numbers=["000102"],
        ),
        AssessmentSkippedSupplier(
            name="Bloqueada Teste",
            email="vendas@bloqueada.example.test",
            reason="disqualified",
            part_numbers=["000102"],
        ),
    ]
    after = await counts(app_engine, who.tenant_id)
    assert (after["suppliers"], after["contacts"]) == (before["suppliers"], before["contacts"])
    assert after["invites"] == 0


async def test_max_suppliers_per_line_caps_each_lines_invites(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    who = buyer(purchasing_rows)
    ranked = [
        supplier(f"Fornecedor {code} Teste", f"vendas@{code.lower()}.example.test", code=code)
        for code in ("A", "B", "C", "D")
    ]
    source = assessment(
        line("000101", suppliers=ranked[:3]),
        line("000102", suppliers=[ranked[2], ranked[3]]),
    )
    capped = await draft_from_snapshot(
        app_engine,
        who,
        await saved(app_engine, who, source, "capped"),
        request(max_suppliers_per_line=1),
    )
    full = await draft_from_snapshot(
        app_engine, who, await saved(app_engine, who, source, "full"), request()
    )

    assert [(s.name, s.part_numbers) for s in capped.invited] == [
        ("Fornecedor A Teste", ["000101"]),
        ("Fornecedor C Teste", ["000102"]),
    ]
    assert [(s.name, s.part_numbers) for s in full.invited] == [
        ("Fornecedor A Teste", ["000101"]),
        ("Fornecedor B Teste", ["000101"]),
        ("Fornecedor C Teste", ["000101", "000102"]),
        ("Fornecedor D Teste", ["000102"]),
    ]
    # The second draft found the suppliers the first one created.
    assert [s.supplier_created for s in full.invited] == [False, True, False, True]
    with pytest.raises(ValueError, match="max_suppliers_per_line"):
        request(max_suppliers_per_line=4)


async def test_older_snapshots_invite_suppliers_with_history_or_an_approval(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    who = buyer(purchasing_rows)
    old = assessment(
        line(
            "000101",
            suppliers=[
                supplier("Historico Teste", "vendas@historico.example.test", suggested=False),
                supplier(
                    "Homologado Teste",
                    "vendas@homologado.example.test",
                    suggested=False,
                    status="Homologado",
                    history=False,
                ),
                supplier(
                    "Cadastro Teste",
                    "vendas@cadastro.example.test",
                    suggested=False,
                    history=False,
                ),
                supplier(
                    "Bloqueado Teste",
                    "vendas@bloqueado.example.test",
                    suggested=False,
                    status="Bloqueado",
                ),
            ],
        )
    ).model_dump(mode="json")
    # As saved before the internal estimate and the measured supplier fields existed.
    del old["totals"]
    for item in old["lines"]:
        del item["price"], item["flags"]
        for evidence in item["suppliers"]:
            for key in MEASURED:
                del evidence[key]
    async with tenant_session(app_engine, who.tenant_id) as session:
        row = ProcurementAssessment(
            tenant_id=who.tenant_id,
            user_id=who.user_id,
            source_key="before-estimates",
            upload_ids=[uuid4()],
            objective="Buy what MTR-100 needs at Planta A",
            assessment=old,
        )
        session.add(row)
        await session.flush()
        assessment_id = row.id

    result = await draft_from_snapshot(app_engine, who, assessment_id, request())

    assert [s.name for s in result.invited] == ["Historico Teste", "Homologado Teste"]
    assert [(s.name, s.reason) for s in result.skipped_suppliers] == [
        ("Bloqueado Teste", "disqualified")
    ]
    assert result.lines_without_limits == [1]


async def test_a_second_call_returns_the_same_answer_without_duplicating_anything(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    who = buyer(purchasing_rows)
    await add_supplier(app_engine, who, "MetalNova Teste", "vendas@metalnova.example.test")
    assessment_id = await saved(
        app_engine,
        who,
        assessment(
            line(
                "000101",
                suppliers=[
                    supplier("MetalNova Teste", "vendas@metalnova.example.test"),
                    supplier("PrecisaoSul Teste", "vendas@precisaosul.example.test"),
                    supplier("Sem Email Teste", None),
                ],
                price=estimate("BRL", "41.28", "43.34"),
            ),
            line("000102"),
        ),
    )

    first = await draft_from_snapshot(app_engine, who, assessment_id, request())
    after_first = await counts(app_engine, who.tenant_id)
    again = await draft_from_snapshot(app_engine, who, assessment_id, request())

    assert (first.reused, again.reused) == (False, True)
    assert again.rfq_id == first.rfq_id

    def shape(result: AssessmentRfqResult) -> object:
        return (
            [(s.supplier_id, s.name, s.email, s.part_numbers) for s in result.invited],
            result.skipped_suppliers,
            result.lines_without_limits,
        )

    assert shape(again) == shape(first)
    assert [s.name for s in first.invited] == ["MetalNova Teste", "PrecisaoSul Teste"]
    assert [s.reason for s in first.skipped_suppliers] == ["no_contact_email"]
    assert first.lines_without_limits == [2]
    assert await counts(app_engine, who.tenant_id) == after_first


async def test_concurrent_drafts_create_a_new_supplier_once(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    who = buyer(purchasing_rows)
    source = assessment(
        line("000101", suppliers=[supplier("Nova Teste", "vendas@nova.example.test")])
    )
    ids = [await saved(app_engine, who, source, key) for key in ("first", "second")]

    drafts = await asyncio.gather(
        *[draft_from_snapshot(app_engine, who, assessment_id, request()) for assessment_id in ids]
    )

    assert len({d.rfq_id for d in drafts}) == 2
    assert len({d.invited[0].supplier_id for d in drafts}) == 1
    assert sorted(d.invited[0].supplier_created for d in drafts) == [False, True]
    after = await counts(app_engine, who.tenant_id)
    assert (after["suppliers"], after["contacts"], after["invites"]) == (1, 1, 2)


async def test_another_tenants_supplier_with_the_same_email_is_never_used(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    who = buyer(purchasing_rows)
    other = Principal(
        tenant_id=purchasing_rows.other_tenant,
        user_id=purchasing_rows.other_approver,
        role="approver",
    )
    foreign = await add_supplier(
        app_engine, other, "MetalNova Teste", "vendas@metalnova.example.test"
    )
    other_before = await counts(app_engine, other.tenant_id)
    assessment_id = await saved(
        app_engine,
        who,
        assessment(
            line(
                "000101",
                suppliers=[supplier("MetalNova Teste", "vendas@metalnova.example.test")],
            )
        ),
    )

    result = await draft_from_snapshot(app_engine, who, assessment_id, request())

    [invited] = result.invited
    assert invited.supplier_id != foreign and invited.supplier_created
    async with tenant_session(app_engine, who.tenant_id) as session:
        assert await session.get(Supplier, invited.supplier_id) is not None
        assert await session.get(Supplier, foreign) is None
    assert await counts(app_engine, other.tenant_id) == other_before


def test_suggestions_win_over_history_and_disqualified_suppliers_take_no_slot() -> None:
    lines = [
        line(
            "000101",
            suppliers=[
                supplier("Bloqueada Teste", "a@bloqueada.example.test", status="bloqueada"),
                supplier("Primeira Teste", "a@primeira.example.test"),
                supplier("Historico Teste", "a@historico.example.test", suggested=False),
            ],
        )
    ]

    wanted = wanted_suppliers(lines, 1)

    assert [(w.evidence.name, w.disqualified) for w in wanted] == [
        ("Primeira Teste", False),
        ("Bloqueada Teste", True),
    ]


async def test_each_rfq_line_links_its_assessment_suppliers_to_the_invited_ones(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    who = buyer(purchasing_rows)
    known = await add_supplier(app_engine, who, "MetalNova Teste", "vendas@metalnova.example.test")
    assessment_id = await saved(
        app_engine,
        who,
        assessment(
            line(
                "000101",
                suppliers=[
                    supplier("MetalNova Teste", "vendas@metalnova.example.test", code="F001"),
                    supplier("PrecisaoSul Teste", "vendas@precisaosul.example.test", code="F002"),
                    supplier("Sem Email Teste", None, code="F009"),
                ],
            ),
            line("000106", suppliers=[supplier("MetalNova Teste", None, code="F001")]),
        ),
    )

    result = await draft_from_snapshot(app_engine, who, assessment_id, request())

    created = next(s.supplier_id for s in result.invited if s.supplier_created)
    async with tenant_session(app_engine, who.tenant_id) as session:
        evidence = [
            row.source_evidence
            for row in await session.scalars(
                select(RfqLine).where(RfqLine.rfq_id == result.rfq_id).order_by(RfqLine.position)
            )
        ]
    metalnova = {"supplier_id": str(known), "supplier_code": "F001", "name": "MetalNova Teste"}
    assert [e["supplier_links"] for e in evidence] == [
        [
            {**metalnova, "email": "vendas@metalnova.example.test"},
            {
                "supplier_id": str(created),
                "supplier_code": "F002",
                "name": "PrecisaoSul Teste",
                "email": "vendas@precisaosul.example.test",
            },
        ],
        # Found by name on this line: linked to the same supplier; the skipped one is not.
        [{**metalnova, "email": None}],
    ]
    assert all({"assessment_id", "line"} <= set(e) for e in evidence)


async def test_no_limits_come_from_a_single_purchase(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    who = buyer(purchasing_rows)
    single = estimate("BRL", "41.28", "43.34").model_copy(update={"observations": 1})
    assessment_id = await saved(app_engine, who, assessment(line("000101", price=single)))

    result = await draft_from_snapshot(app_engine, who, assessment_id, request())

    assert result.lines_without_limits == [1]


# --- API -------------------------------------------------------------------------------


async def test_the_rfq_endpoint_returns_invites_skips_and_lines_without_limits(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    who = buyer(purchasing_rows)
    assessment_id = await saved(
        app_engine,
        who,
        assessment(
            line(
                "000101",
                suppliers=[
                    supplier("PrecisaoSul Teste", "vendas@precisaosul.example.test"),
                    supplier("Sem Email Teste", None),
                ],
                price=estimate("BRL", "41.28", "43.34"),
            ),
            line("000102"),
        ),
    )
    app = build_app()
    app.include_router(procurement_assessments_router())
    sessionmaker = create_sessionmaker(app_engine)

    def principal() -> Principal:
        return who

    def db() -> async_sessionmaker[AsyncSession]:
        return sessionmaker

    app.dependency_overrides[get_principal] = principal
    app.dependency_overrides[get_db] = db
    path = f"/v1/procurement-assessments/{assessment_id}/rfq"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        too_many = await http.post(
            path, json={"title": "Components", "base_currency": "BRL", "max_suppliers_per_line": 4}
        )
        created = await http.post(
            path, json={"title": "Components", "base_currency": "BRL", "max_suppliers_per_line": 2}
        )

    assert too_many.status_code == 422
    assert created.status_code == 200, created.text
    body = created.json()
    assert [(s["name"], s["supplier_created"]) for s in body["invited"]] == [
        ("PrecisaoSul Teste", True)
    ]
    assert [(s["name"], s["reason"]) for s in body["skipped_suppliers"]] == [
        ("Sem Email Teste", "no_contact_email")
    ]
    assert body["lines_without_limits"] == [2]
