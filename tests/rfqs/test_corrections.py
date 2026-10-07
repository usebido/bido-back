"""Correcting extracted quote values against Postgres: history, review state, comparison."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.auth import Principal
from bido_back.comparison.loader import load_comparison
from bido_back.corrections import service
from bido_back.corrections.schemas import HeaderCorrectionRequest, LineCorrectionRequest
from bido_back.db import tenant_session
from bido_back.db.models import FieldCorrection, Message, Proposal, ProposalLine
from bido_back.rfqs import service as rfqs
from bido_back.rfqs.errors import InvalidState, NotFound
from bido_back.rfqs.schemas import RfqCreated
from tests.approvals.conftest import People

from .test_service import buyer, make_rfq, make_supplier

pytestmark = pytest.mark.db

TODAY = date(2026, 10, 6)


def _field(value: str | None, *, confidence: float = 0.95, issues: list[str] | None = None) -> Any:
    return {
        "value": value,
        "parsed": value,
        "source": {"kind": "sheet", "attachment": "quote.xlsx", "cell": "F2", "quote": value},
        "confidence": confidence,
        "needs_review": bool(issues),
        "issues": issues or [],
    }


@dataclass
class Quoted:
    rfq: RfqCreated
    proposal_id: UUID
    line_id: UUID


async def quoted_rfq(engine: AsyncEngine, principal: Principal) -> Quoted:
    """An RFQ with one supplier answer: a doubtful price and a doubtful currency."""
    rfq = await make_rfq(engine, principal)
    supplier = await make_supplier(engine, principal, f"Rhein{uuid4().hex[:6]}")
    invite = await rfqs.add_invite(engine, principal, rfq.id, supplier.id, supplier.contacts[0].id)
    async with tenant_session(engine, principal.tenant_id) as session:
        message = Message(
            tenant_id=principal.tenant_id,
            invite_id=invite.id,
            direction="in",
            message_id_header=f"<{uuid4()}@supplier.example.test>",
            occurred_at=datetime.now(UTC),
        )
        session.add(message)
        await session.flush()
        proposal = Proposal(
            tenant_id=principal.tenant_id,
            invite_id=invite.id,
            message_id=message.id,
            version=1,
            currency="USD",
            valid_until=date(2026, 12, 31),
            incoterm="DAP",
            fields={
                "currency": _field("USD", confidence=0.4, issues=["low_confidence"]),
                "valid_until": _field("2026-12-31"),
                "_review": {"needs_review": True, "issues": [], "missing_fields": []},
            },
        )
        session.add(proposal)
        await session.flush()
        line = ProposalLine(
            tenant_id=principal.tenant_id,
            proposal_id=proposal.id,
            rfq_line_id=rfq.lines[0].id,
            offered_mpn=rfq.lines[0].mpn,
            min_quantity=Decimal(1),
            unit_price=Decimal("0.45"),
            unit="pcs",
            lead_time_days=42,
            fields={
                "unit_price": _field("0.45", confidence=0.5, issues=["low_confidence"]),
                "lead_time_days": _field("42"),
                "_review": {"needs_review": True, "issues": ["currency_unverified"]},
            },
            needs_review=True,
        )
        session.add(line)
        await session.flush()
        return Quoted(rfq=rfq, proposal_id=proposal.id, line_id=line.id)


async def test_correcting_a_price_records_history_and_updates_the_comparison(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    principal = buyer(rfq_rows)
    quoted = await quoted_rfq(app_engine, principal)

    corrected = await service.correct_line(
        app_engine,
        principal,
        quoted.proposal_id,
        quoted.line_id,
        LineCorrectionRequest(unit_price=Decimal("0.42"), reason="typo in the sheet"),
    )

    assert corrected.unit_price == Decimal("0.420000")
    assert corrected.corrected_fields == ["unit_price"]
    assert corrected.review_fields == []
    # The line still waits for the currency to be trusted.
    assert corrected.needs_review is True

    [row] = await service.list_corrections(app_engine, principal, quoted.proposal_id)
    assert (row.field, row.action, row.old_value, row.new_value) == (
        "unit_price",
        "corrected",
        "0.45",
        "0.42",
    )
    assert row.reason == "typo in the sheet" and row.corrected_by == principal.user_id
    assert row.old_provenance["confidence"] == 0.5
    assert row.old_provenance["source"] == {
        "kind": "sheet",
        "attachment": "quote.xlsx",
        "cell": "F2",
        "quote": "0.45",
    }

    comparison = await load_comparison(app_engine, principal, quoted.rfq.id, TODAY)
    cell = next(c for c in comparison.suppliers[0].cells if c.tier == 0)
    assert cell.unit_price == Decimal("0.420000")
    assert cell.proposal_line_id == quoted.line_id
    assert cell.corrected_fields == ["unit_price"]
    assert "unit_price" not in cell.low_confidence_fields


async def test_correcting_the_currency_clears_the_lines_currency_doubt(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    principal = buyer(rfq_rows)
    quoted = await quoted_rfq(app_engine, principal)
    await service.correct_line(
        app_engine,
        principal,
        quoted.proposal_id,
        quoted.line_id,
        LineCorrectionRequest(unit_price=Decimal("0.45")),
    )

    view = await service.correct_header(
        app_engine, principal, quoted.proposal_id, HeaderCorrectionRequest(currency="EUR")
    )

    assert view.currency == "EUR" and view.corrected_fields == ["currency"]
    assert view.needs_review is False
    [line] = view.lines
    assert line.needs_review is False

    rows = await service.list_corrections(app_engine, principal, quoted.proposal_id)
    # Sending the value a field already had is a confirmation.
    assert [(r.field, r.action) for r in rows] == [
        ("unit_price", "confirmed"),
        ("currency", "corrected"),
    ]
    comparison = await load_comparison(app_engine, principal, quoted.rfq.id, TODAY)
    [column] = comparison.suppliers
    assert column.currency == "EUR" and column.corrected_fields == ["currency"]
    assert "needs_review" not in column.flags


async def test_clearing_and_refusals(app_engine: AsyncEngine, rfq_rows: People) -> None:
    principal = buyer(rfq_rows)
    quoted = await quoted_rfq(app_engine, principal)

    cleared = await service.correct_line(
        app_engine,
        principal,
        quoted.proposal_id,
        quoted.line_id,
        LineCorrectionRequest(lead_time_days=None),
    )
    assert cleared.lead_time_days is None and cleared.corrected_fields == ["lead_time_days"]

    with pytest.raises(InvalidState):
        await service.correct_line(
            app_engine,
            principal,
            quoted.proposal_id,
            quoted.line_id,
            LineCorrectionRequest(is_alternative=None),
        )
    with pytest.raises(NotFound):
        # A line of another RFQ is not a valid match.
        await service.correct_line(
            app_engine,
            principal,
            quoted.proposal_id,
            quoted.line_id,
            LineCorrectionRequest(rfq_line_id=uuid4()),
        )
    with pytest.raises(NotFound):
        await service.correct_line(
            app_engine,
            principal,
            uuid4(),
            quoted.line_id,
            LineCorrectionRequest(unit_price=Decimal(1)),
        )
    with pytest.raises(ValueError, match="at least one field"):
        LineCorrectionRequest(reason="nothing")


async def test_another_tenant_cannot_correct_or_read(
    app_engine: AsyncEngine, rfq_rows: People
) -> None:
    quoted = await quoted_rfq(app_engine, buyer(rfq_rows))
    outsider = Principal(tenant_id=rfq_rows.other_tenant, user_id=rfq_rows.other_approver)

    with pytest.raises(NotFound):
        await service.correct_line(
            app_engine,
            outsider,
            quoted.proposal_id,
            quoted.line_id,
            LineCorrectionRequest(unit_price=Decimal(0)),
        )
    with pytest.raises(NotFound):
        await service.get_proposal(app_engine, outsider, quoted.proposal_id)

    async with tenant_session(app_engine, rfq_rows.other_tenant) as session:
        assert list(await session.scalars(select(FieldCorrection))) == []
