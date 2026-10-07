"""The comparison loader against Postgres: latest proposal version per invite, FX, rules."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, insert
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.auth import Principal
from bido_back.comparison.loader import load_comparison
from bido_back.db import tenant_session
from bido_back.db.models import FxRate, Message, Proposal, ProposalLine
from bido_back.rfqs import service
from tests.approvals.conftest import People

from .test_service import buyer, make_rfq, make_supplier

pytestmark = pytest.mark.db

RATE_DAY = date(1999, 1, 4)  # no real refresh writes this day


async def test_uses_the_latest_proposal_and_the_rate_in_effect(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, rfq_rows: People
) -> None:
    principal: Principal = buyer(rfq_rows)
    rfq = await make_rfq(app_engine, principal)
    supplier = await make_supplier(app_engine, principal, "Rhein")
    other = await make_supplier(app_engine, principal, "Lyon")
    invite = await service.add_invite(
        app_engine, principal, rfq.id, supplier.id, supplier.contacts[0].id
    )
    await service.add_invite(app_engine, principal, rfq.id, other.id, other.contacts[0].id)
    line = rfq.lines[0]  # tiers 4000, 10000

    async with migrator_engine.begin() as conn:
        await conn.execute(
            insert(FxRate).values(rate_date=RATE_DAY, currency="XTS", per_eur=Decimal(2))
        )
    try:
        async with tenant_session(app_engine, rfq_rows.tenant) as session:
            for version, price in ((1, "9.99"), (2, "0.10")):
                message = Message(
                    tenant_id=rfq_rows.tenant,
                    invite_id=invite.id,
                    direction="in",
                    message_id_header=f"<v{version}-{invite.id}@supplier.example.test>",
                    occurred_at=datetime.now(UTC),
                )
                session.add(message)
                await session.flush()
                proposal = Proposal(
                    tenant_id=rfq_rows.tenant,
                    invite_id=invite.id,
                    message_id=message.id,
                    version=version,
                    currency="XTS",
                    valid_until=date(2026, 12, 31),
                    incoterm="DAP",
                    fields={"currency": {"confidence": 0.99, "needs_review": False}},
                )
                session.add(proposal)
                await session.flush()
                session.add(
                    ProposalLine(
                        tenant_id=rfq_rows.tenant,
                        proposal_id=proposal.id,
                        rfq_line_id=line.id,
                        offered_mpn=line.mpn,
                        min_quantity=Decimal(1),
                        unit_price=Decimal(price),
                        unit="pcs",
                        lead_time_days=21,
                        fields={
                            "unit_price": {"confidence": 0.6, "needs_review": True},
                            "qualification": {
                                "parsed": "AEC-Q200",
                                "confidence": 0.95,
                                "needs_review": False,
                            },
                        },
                    )
                )
            await session.flush()

        result = await load_comparison(app_engine, principal, rfq.id, date(2026, 10, 6))
    finally:
        async with migrator_engine.begin() as conn:
            await conn.execute(delete(FxRate).where(FxRate.rate_date == RATE_DAY))

    [column] = result.suppliers
    assert column.proposal_version == 2 and column.supplier_name == "Rhein"
    cell = next(c for c in column.cells if c.rfq_line_id == line.id and c.tier == 0)
    assert cell.unit_price == Decimal("0.100000")
    assert cell.unit_price_base == Decimal("0.050000")  # 0.10 XTS / 2 XTS per EUR
    assert cell.line_total_base == Decimal("200.00")  # 4000 * 0.05
    assert "low_confidence" in cell.flags and cell.low_confidence_fields == ["unit_price"]
    assert [r.currency for r in result.fx_rates] == ["XTS"]
    assert [a.supplier_name for a in result.awaiting] == ["Lyon"]
    assert column.totals[0].complete is False  # line 2 was not quoted
    # Sector rules and the recommendation come through the loader.
    assert column.supplier_type == "distributor"
    assert cell.qualification == "AEC-Q200"
    rec = next(r for r in result.recommendations if r.rfq_line_id == line.id and r.tier == 0)
    assert rec.invite_id is None  # the only offer has a low-confidence price
    assert [e.reasons for e in rec.excluded] == [["low_confidence"]]
