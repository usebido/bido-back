"""Comparison history for an RFQ drafted from a source assessment, against Postgres.

The RFQ comes from a saved assessment (`draft_from_snapshot`); the supplier links
the assessment-to-RFQ handoff records on each line are written here directly.
Nothing is imported unless a test says so.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.auth import Principal
from bido_back.comparison.compute import Cell, Comparison
from bido_back.comparison.loader import load_comparison
from bido_back.comparison.policy import ComparisonPolicy, SupplierPerformance
from bido_back.db import tenant_session
from bido_back.db.models import (
    Invite,
    Item,
    Message,
    PoLine,
    Proposal,
    ProposalLine,
    PurchaseOrder,
    RfqLine,
    Supplier,
)
from bido_back.negotiation import service as negotiation
from bido_back.purchasing.sources.contracts import (
    AssessmentLine,
    Coverage,
    PlanningScope,
    SourceAssessment,
    SourceRef,
    SupplierEvidence,
)
from bido_back.purchasing.sources.snapshots import (
    AssessmentRfqRequest,
    draft_from_snapshot,
    save_snapshot,
)
from bido_back.rfqs import suppliers as supplier_service
from bido_back.rfqs.errors import NotFound
from bido_back.rfqs.schemas import ContactIn, CreateSupplierRequest, SupplierView
from tests.approvals.conftest import People
from tests.copilot.conftest import context_of, make_tools
from tests.purchasing.conftest import purchasing_rows

from .test_service import buyer

pytestmark = pytest.mark.db
__all__ = ["purchasing_rows"]

TODAY = date(2026, 10, 7)  # the copilot tools' today in tests
D = Decimal
PART = "000101"

METALNOVA = SupplierEvidence(
    supplier_code="F001",
    name="MetalNova",
    email="vendas@metalnova.example.test",
    last_price=D("9.80"),
    currency="EUR",
    ordered_at=date(2026, 8, 3),
    orders=5,
    on_time_rate=D("0.4"),
    quality_rate=D("0.9"),
    delivery_observations=5,
    quality_observations=5,
    last_received_at=date(2026, 9, 1),
)
ACOS_SUL = SupplierEvidence(
    supplier_code="F002",
    name="Aços Sul",
    email="comercial@acossul.example.test",
    last_price=D("10.40"),
    currency="EUR",
    ordered_at=date(2026, 9, 10),
    orders=4,
    on_time_rate=D(1),
    quality_rate=D(1),
    delivery_observations=4,
    quality_observations=4,
    last_received_at=date(2026, 9, 20),
)


@dataclass
class Sourced:
    principal: Principal
    rfq_id: UUID
    ref: str
    line_id: UUID
    suppliers: dict[str, UUID]


async def create_supplier(
    engine: AsyncEngine, principal: Principal, name: str, email: str
) -> SupplierView:
    return await supplier_service.create_supplier(
        engine,
        principal,
        CreateSupplierRequest(
            name=name, type="authorized_distributor", contacts=[ContactIn(name=name, email=email)]
        ),
    )


async def quote(
    engine: AsyncEngine, tenant_id: UUID, invite_id: UUID, line_id: UUID, price: str
) -> None:
    async with tenant_session(engine, tenant_id) as session:
        invite = await session.get(Invite, invite_id)
        assert invite is not None
        invite.status = "responded"
        message = Message(
            tenant_id=tenant_id,
            invite_id=invite_id,
            direction="in",
            message_id_header=f"<{invite_id}@supplier.example.test>",
            occurred_at=datetime.now(UTC),
        )
        session.add(message)
        await session.flush()
        proposal = Proposal(
            tenant_id=tenant_id,
            invite_id=invite_id,
            message_id=message.id,
            version=1,
            currency="EUR",
            valid_until=date(2026, 12, 31),
            payment_terms="30 days net",
            fields={"_review": {"declined": False}},
        )
        session.add(proposal)
        await session.flush()
        session.add(
            ProposalLine(
                tenant_id=tenant_id,
                proposal_id=proposal.id,
                rfq_line_id=line_id,
                min_quantity=D(1),
                unit_price=D(price),
                unit="pcs",
                lead_time_days=10,
                tooling_cost=D(0),
                # Freight and tax stated too: nothing but the history can hold it back.
                fields={
                    "freight_cost": {"parsed": "0", "confidence": 0.95, "needs_review": False},
                    "tax_rate_percent": {"parsed": "0", "confidence": 0.95, "needs_review": False},
                },
            )
        )


async def sourced_rfq(engine: AsyncEngine, people: People) -> Sourced:
    """An assessed motor part drafted into an RFQ; both assessed suppliers invited and quoting."""
    principal = buyer(people)
    assessment = SourceAssessment(
        lines=[
            AssessmentLine(
                part_number=PART,
                description="Motor housing",
                location="Planta A",
                unit="pcs",
                need_by=date(2026, 10, 22),
                calculated_to_buy=D(50),
                currency="EUR",
                ready=True,
                suppliers=[METALNOVA, ACOS_SUL],
                evidence=[SourceRef(source_id="erp", table="ZMM_042", row=2, column=1)],
            )
        ],
        scope=PlanningScope(
            product_code="MTR-100",
            location="Planta A",
            as_of=date(2026, 10, 5),
            horizon_end=date(2026, 10, 22),
            safety_stock=D(0),
        ),
        coverage=[
            Coverage(
                source_id="erp", filename="export.xlsx", table="ZMM_042", rows=8, inspected=True
            )
        ],
        status="assessed",
    )
    saved = await save_snapshot(
        engine,
        principal,
        source_key=f"assessment-history-{uuid4()}",
        upload_ids=[uuid4()],
        objective="Buy the components of motor MTR-100",
        assessment=assessment,
    )
    assert saved.assessment_id is not None
    draft = await draft_from_snapshot(
        engine,
        principal,
        saved.assessment_id,
        AssessmentRfqRequest(title="MTR-100 components", base_currency="EUR"),
    )
    async with tenant_session(engine, principal.tenant_id) as session:
        line = await session.scalar(select(RfqLine.id).where(RfqLine.rfq_id == draft.rfq_id))
    assert line is not None
    # The handoff invites the assessed suppliers itself (creating them from the evidence).
    assert sorted(s.name for s in draft.invited) == sorted(
        [str(METALNOVA.name), str(ACOS_SUL.name)]
    )
    async with tenant_session(engine, principal.tenant_id) as session:
        invites = {
            name: (invite_id, supplier_id)
            for invite_id, supplier_id, name in (
                await session.execute(
                    select(Invite.id, Invite.supplier_id, Supplier.name)
                    .join(Supplier, Supplier.id == Invite.supplier_id)
                    .where(Invite.rfq_id == draft.rfq_id)
                )
            ).all()
        }
    created: dict[str, UUID] = {}
    for evidence, price in ((METALNOVA, "9.00"), (ACOS_SUL, "10.50")):
        assert evidence.name is not None
        invite_id, supplier_id = invites[evidence.name]
        await quote(engine, principal.tenant_id, invite_id, line, price)
        created[evidence.name] = supplier_id
    return Sourced(principal, draft.rfq_id, draft.ref, line, created)


def link(supplier_id: UUID, evidence: SupplierEvidence) -> JsonValue:
    return {
        "supplier_id": str(supplier_id),
        "supplier_code": evidence.supplier_code,
        "name": evidence.name,
        "email": evidence.email,
    }


async def set_links(engine: AsyncEngine, sourced: Sourced, links: JsonValue) -> None:
    """What the assessment-to-RFQ handoff records: assessment supplier -> tenant supplier."""
    async with tenant_session(engine, sourced.principal.tenant_id) as session:
        line = await session.get(RfqLine, sourced.line_id)
        assert line is not None and line.source_evidence["line"]
        line.source_evidence = {**line.source_evidence, "supplier_links": links}


def cells(comparison: Comparison) -> dict[str, Cell]:
    return {column.supplier_name: column.cells[0] for column in comparison.suppliers}


async def test_the_comparison_uses_the_history_the_assessment_measured(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    world = await sourced_rfq(app_engine, rfq_rows)
    await set_links(
        app_engine,
        world,
        [
            link(world.suppliers["MetalNova"], METALNOVA),
            link(world.suppliers["Aços Sul"], ACOS_SUL),
        ],
    )

    comparison = await load_comparison(app_engine, world.principal, world.rfq_id, TODAY)

    offers = cells(comparison)
    assert offers["MetalNova"].performance == SupplierPerformance(
        basis="source_assessment",
        observations=5,
        delivery_samples=5,
        on_time_rate=D("0.4"),
        quality_samples=5,
        accepted_quantity_rate=D("0.9"),
        latest_observation_on=date(2026, 9, 1),
    )
    assert offers["Aços Sul"].performance.basis == "source_assessment"
    assert offers["Aços Sul"].performance.on_time_rate == 1
    [rec] = comparison.recommendations
    # Cost first: the cheaper offer wins, its weaker history shown with its basis. The
    # basis is a disclosure, not an evidence gap: with every cost and term stated, the
    # recommendation is ready for the buyer.
    assert rec.supplier_name == "MetalNova"
    assert rec.performance == offers["MetalNova"].performance
    assert rec.evidence_gaps == []
    assert rec.readiness == comparison.readiness == "ready_for_buyer_review"
    assert [(a.supplier_name, a.performance.basis) for a in rec.alternatives] == [
        ("Aços Sul", "source_assessment")
    ]

    # The buyer's threshold applies to it as it does to imported history.
    await negotiation.set_comparison_policy(
        app_engine, world.principal, world.rfq_id, ComparisonPolicy(minimum_on_time_rate=D("0.9"))
    )
    outcome = await make_tools(app_engine).call(
        context_of(world.principal), "get_negotiation_outcome", rfq=world.ref
    )
    [line] = outcome.lines
    assert line.supplier == "Aços Sul" and line.covered
    assert line.performance == offers["Aços Sul"].performance
    assert line.evidence_gaps == []
    assert line.readiness == outcome.readiness == "ready_for_buyer_review"
    assert [(e.supplier_name, e.reasons) for e in line.excluded] == [
        ("MetalNova", ["delivery_history_below_policy"])
    ]

    # Closing freezes the outcome with the history and its basis.
    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    [closed] = await negotiation.close(app_engine, world.principal, world.rfq_id, now=now)
    assert closed.supplier_name == "Aços Sul"
    assert closed.performance == line.performance
    assert closed.readiness == "ready_for_buyer_review"
    view = await negotiation.get_negotiation(app_engine, world.principal, world.rfq_id, now=now)
    assert view.outcome is not None
    assert view.outcome[0].performance == line.performance


async def test_imported_purchase_history_wins_over_the_assessment(
    app_engine: AsyncEngine, purchasing_rows: People
) -> None:
    world = await sourced_rfq(app_engine, purchasing_rows)
    await set_links(
        app_engine,
        world,
        [
            link(world.suppliers["MetalNova"], METALNOVA),
            link(world.suppliers["Aços Sul"], ACOS_SUL),
        ],
    )
    tenant = world.principal.tenant_id
    async with tenant_session(app_engine, tenant) as session:
        item = Item(tenant_id=tenant, part_number=PART, description="Motor housing")
        order = PurchaseOrder(
            tenant_id=tenant,
            supplier_id=world.suppliers["MetalNova"],
            po_number="4500001",
            ordered_at=TODAY - timedelta(days=60),
        )
        session.add_all([item, order])
        await session.flush()
        imported = [
            PoLine(
                tenant_id=tenant,
                purchase_order_id=order.id,
                line_no=n,
                item_id=item.id,
                quantity=D(10),
                unit_price=D("9.50"),
                promised_at=TODAY - timedelta(days=30),
                received_at=TODAY - timedelta(days=31),
                received_qty=D(10),
                rejected_qty=D(0),
            )
            for n in (1, 2, 3)
        ]
        session.add_all(imported)
        await session.flush()
        imported_ids = [row.id for row in imported]

    comparison = await load_comparison(app_engine, world.principal, world.rfq_id, TODAY)

    offers = cells(comparison)
    history = offers["MetalNova"].performance
    assert history.basis == "imported_history"
    assert (history.delivery_samples, history.on_time_rate) == (3, 1)
    assert sorted(history.evidence_po_line_ids) == sorted(imported_ids)
    assert history.issues == []
    # The other supplier has no imported history: its assessment history still counts.
    assert offers["Aços Sul"].performance.basis == "source_assessment"
    [rec] = comparison.recommendations
    assert rec.supplier_name == "MetalNova"
    assert rec.performance == history
    assert rec.readiness == "ready_for_buyer_review"


async def test_lines_without_usable_links_keep_imported_behavior_and_tenants_stay_apart(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    world = await sourced_rfq(app_engine, rfq_rows)
    other = Principal(
        tenant_id=rfq_rows.other_tenant, user_id=rfq_rows.other_approver, role="approver"
    )
    # Same name and code as the assessed supplier, in another tenant.
    foreign = await create_supplier(app_engine, other, "MetalNova", "sales@metalnova.example.test")

    async def performance() -> dict[str, SupplierPerformance]:
        comparison = await load_comparison(app_engine, world.principal, world.rfq_id, TODAY)
        return {name: cell.performance for name, cell in cells(comparison).items()}

    unknown = {"MetalNova": SupplierPerformance(), "Aços Sul": SupplierPerformance()}
    # The handoff records links; an RFQ line drafted before links existed has none.
    await set_links(app_engine, world, [])
    assert await performance() == unknown
    attempts: list[JsonValue] = [
        "F001",
        [{"supplier_id": "MetalNova", "supplier_code": "F001"}],
        [{"supplier_id": str(world.suppliers["MetalNova"]), "supplier_code": "F009"}],
        # Another tenant's supplier is never invited here, whatever its name.
        [link(foreign.id, METALNOVA), {"supplier_id": str(foreign.id), "name": "MetalNova"}],
    ]
    for links in attempts:
        await set_links(app_engine, world, links)
        assert await performance() == unknown

    with pytest.raises(NotFound):
        await load_comparison(app_engine, other, world.rfq_id, TODAY)
