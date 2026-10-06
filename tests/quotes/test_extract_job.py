"""The QUOTE_EXTRACT job against Postgres, with a scripted model (no network).

Marked `db`: skipped on a laptop without Postgres, failing in CI (see
tests/db/conftest.py).
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.config import RunnerConfig
from bido_back.db import tenant_session
from bido_back.db.models import (
    Attachment,
    Contact,
    Invite,
    Message,
    Proposal,
    ProposalLine,
    Rfq,
    RfqLine,
    Supplier,
)
from bido_back.quotes.extract_job import JOB_TYPE, handle_quote_extract, quote_extract_handler
from bido_back.worker.envelope import Envelope
from tests.agents.test_runner import Script, final, script
from tests.db.conftest import TwoTenants, app_engine, migrator_engine, two_tenants
from tests.documents.fixtures import build_xlsx

__all__ = ["app_engine", "migrator_engine", "script", "two_tenants"]

pytestmark = pytest.mark.db

CONFIG = RunnerConfig(anthropic_api_key="test-not-a-real-key")
TODAY = date(2026, 10, 6)
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

BODY = (
    "Sehr geehrte Frau Weber,\n"
    "anbei unser Angebot. Lieferzeit: 6 Wochen. Angebot gültig bis 31.12.2026.\n"
    "Lieferbedingung FCA Shenzhen. Preise in EUR.\n"
)


class FakeStorage:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.blobs[key] = data

    async def get(self, key: str) -> bytes:
        return self.blobs[key]


@dataclass(frozen=True)
class Seeded:
    tenant_id: UUID
    invite_id: UUID
    line_ids: dict[int, UUID]
    storage: FakeStorage
    engine: AsyncEngine

    async def add_message(self, *, body: str = BODY, with_sheet: bool = True) -> UUID:
        async with tenant_session(self.engine, self.tenant_id) as session:
            message = Message(
                tenant_id=self.tenant_id,
                invite_id=self.invite_id,
                direction="in",
                message_id_header=f"<{uuid4().hex}@supplier.test>",
                subject="AW: [RFQ-12] LDO regulators",
                body_text=body,
                status="stored",
                occurred_at=datetime.now(UTC),
            )
            session.add(message)
            await session.flush()
            if with_sheet:
                key = f"attachments/{self.tenant_id}/{message.id}/1-Angebot.xlsx"
                self.storage.blobs[key] = build_xlsx(
                    {
                        "Preise": [
                            ["Pos", "Teil", "Menge", "Preis"],
                            [1, "TPS7A4701RGWR", 1000, 0.45],
                            [1, "TPS7A4701RGWR", 5000, 0.41],
                        ]
                    }
                )
                session.add(
                    Attachment(
                        tenant_id=self.tenant_id,
                        message_id=message.id,
                        filename="Angebot.xlsx",
                        content_type=XLSX,
                        size_bytes=len(self.storage.blobs[key]),
                        storage_key=key,
                    )
                )
            return message.id

    def envelope(self, message_id: UUID) -> Envelope:
        return Envelope(
            type=JOB_TYPE,
            tenant_id=self.tenant_id,
            idempotency_key=f"extract:{message_id}",
            payload={"message_id": str(message_id)},
        )


@pytest_asyncio.fixture
async def seeded(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, two_tenants: TwoTenants
) -> AsyncIterator[Seeded]:
    tenant = two_tenants.a
    async with tenant_session(app_engine, tenant.tenant_id) as session:
        supplier = Supplier(tenant_id=tenant.tenant_id, name="Example Components", language="de")
        contact = Contact(tenant_id=tenant.tenant_id, kind="supplier", email="sales@supplier.test")
        rfq = Rfq(tenant_id=tenant.tenant_id, ref="RFQ-12", title="LDO", created_by=tenant.user_id)
        session.add_all([supplier, contact, rfq])
        await session.flush()
        lines = [
            RfqLine(
                tenant_id=tenant.tenant_id,
                rfq_id=rfq.id,
                position=1,
                description="LDO regulator",
                mpn="TPS7A4701RGWR",
                quantities=[1000, 5000],
            ),
            RfqLine(
                tenant_id=tenant.tenant_id,
                rfq_id=rfq.id,
                position=2,
                description="Adjustable regulator",
                mpn="LM317DCYR",
                quantities=[2000],
            ),
        ]
        invite = Invite(
            tenant_id=tenant.tenant_id,
            rfq_id=rfq.id,
            supplier_id=supplier.id,
            contact_id=contact.id,
            reply_token=uuid4().hex,
            status="responded",
        )
        session.add_all([*lines, invite])
        await session.flush()
        seeded = Seeded(
            tenant_id=tenant.tenant_id,
            invite_id=invite.id,
            line_ids={line.position: line.id for line in lines},
            storage=FakeStorage(),
            engine=app_engine,
        )
    try:
        yield seeded
    finally:
        async with tenant_session(migrator_engine, tenant.tenant_id) as session:
            for model in (
                ProposalLine,
                Proposal,
                Attachment,
                Message,
                Invite,
                RfqLine,
                Rfq,
                Contact,
                Supplier,
            ):
                await session.execute(delete(model).where(model.tenant_id == tenant.tenant_id))


# --- Scripted model output ------------------------------------------------------------------

_NONE: dict[str, object] = {"value": None, "source": None, "confidence": 0}


def _body(value: str, quote: str, confidence: float = 0.95) -> dict[str, object]:
    return {
        "value": value,
        "source": {"kind": "email_body", "quote": quote},
        "confidence": confidence,
    }


def _cell(value: str, cell: str) -> dict[str, object]:
    return {
        "value": value,
        "source": {
            "kind": "sheet",
            "attachment": "Angebot.xlsx",
            "sheet": "Preise",
            "cell": cell,
            "quote": value,
        },
        "confidence": 0.97,
    }


def _line(row: int, quantity: str, price: str, price_quote: str | None = None) -> dict[str, object]:
    unit_price = _cell(price, f"D{row}")
    if price_quote is not None:
        unit_price = {
            **unit_price,
            "source": {**cast(dict[str, Any], unit_price["source"]), "quote": price_quote},
        }
    return {
        "rfq_line_position": 1,
        "offered_mpn": _cell("TPS7A4701RGWR", f"B{row}"),
        "is_alternative": False,
        "min_quantity": _cell(quantity, f"C{row}"),
        "unit_price": unit_price,
        "unit": _NONE,
        "tooling_cost": _NONE,
        "moq": _NONE,
        "lead_time_days": _body("42", "Lieferzeit: 6 Wochen"),
        "origin_country": _NONE,
    }


def quote_output(*lines: dict[str, object]) -> dict[str, object]:
    return {
        "is_quote": True,
        "declined": False,
        "currency": _body("EUR", "Preise in EUR"),
        "valid_until": _body("2026-12-31", "Angebot gültig bis 31.12.2026"),
        "incoterm": _body("FCA", "Lieferbedingung FCA Shenzhen"),
        "payment_terms": _NONE,
        "lines": list(lines) or [_line(2, "1000", "0.45"), _line(3, "5000", "0.41")],
        "missing_fields": ["payment_terms", "origin_country"],
    }


async def run_job(seeded: Seeded, message_id: UUID) -> None:
    await handle_quote_extract(
        seeded.envelope(message_id),
        db=seeded.engine,
        storage=seeded.storage,
        runner_config=CONFIG,
        today=TODAY,
    )


async def proposals_of(seeded: Seeded) -> list[tuple[Proposal, list[ProposalLine]]]:
    async with tenant_session(seeded.engine, seeded.tenant_id) as session:
        proposals = (
            await session.scalars(
                select(Proposal)
                .where(Proposal.invite_id == seeded.invite_id)
                .order_by(Proposal.version)
            )
        ).all()
        result: list[tuple[Proposal, list[ProposalLine]]] = []
        for proposal in proposals:
            lines = (
                await session.scalars(
                    select(ProposalLine)
                    .where(ProposalLine.proposal_id == proposal.id)
                    .order_by(ProposalLine.min_quantity)
                )
            ).all()
            result.append((proposal, list(lines)))
        return result


async def status_of(seeded: Seeded, message_id: UUID) -> str:
    async with tenant_session(seeded.engine, seeded.tenant_id) as session:
        status = await session.scalar(select(Message.status).where(Message.id == message_id))
    assert status is not None
    return status


# --- Tests -------------------------------------------------------------------------------------


async def test_reply_becomes_a_proposal_with_provenance(seeded: Seeded, script: Script) -> None:
    stub = script(final(**quote_output()))
    message_id = await seeded.add_message()

    await run_job(seeded, message_id)

    # The model saw the sheet with cell markers, and nothing that could act.
    [request] = stub.calls
    assert not request.get("tools")
    user_text = cast(list[dict[str, Any]], request["messages"])[0]["content"][0]["text"]
    assert "[Angebot.xlsx Preise!D2] 0.45" in user_text

    [(proposal, lines)] = await proposals_of(seeded)
    assert proposal.version == 1
    assert proposal.message_id == message_id
    assert (proposal.currency, proposal.valid_until, proposal.incoterm) == (
        "EUR",
        date(2026, 12, 31),
        "FCA",
    )
    assert proposal.payment_terms is None
    assert proposal.fields["currency"]["source"]["quote"] == "Preise in EUR"
    assert proposal.fields["currency"]["confidence"] == 0.95
    assert proposal.fields["_review"] == {
        "needs_review": False,
        "issues": [],
        "missing_fields": ["payment_terms", "origin_country"],
        "declined": False,
    }
    assert proposal.fields["_extraction"]["agent"] == "quote_extraction"
    assert proposal.fields["_extraction"]["prompt_version"] == 1

    assert [(pl.min_quantity, pl.unit_price) for pl in lines] == [
        (Decimal("1000.0000"), Decimal("0.450000")),
        (Decimal("5000.0000"), Decimal("0.410000")),
    ]
    first = lines[0]
    assert first.rfq_line_id == seeded.line_ids[1]
    assert first.offered_mpn == "TPS7A4701RGWR"
    assert first.lead_time_days == 42
    assert not first.is_alternative
    assert not first.needs_review
    assert first.fields["unit_price"]["source"] == {
        "kind": "sheet",
        "attachment": "Angebot.xlsx",
        "page": None,
        "sheet": "Preise",
        "cell": "D2",
        "quote": "0.45",
    }
    assert first.fields["unit_price"]["value"] == "0.45"
    assert first.fields["unit_price"]["needs_review"] is False
    assert await status_of(seeded, message_id) == "extracted"


async def test_ungrounded_value_is_stored_for_review(seeded: Seeded, script: Script) -> None:
    # The model says 0.39 but cites the cell that reads 0.45.
    script(final(**quote_output(_line(2, "1000", "0.39", price_quote="0.45"))))
    message_id = await seeded.add_message()

    await run_job(seeded, message_id)

    [(proposal, [line])] = await proposals_of(seeded)
    assert line.needs_review
    assert line.unit_price == Decimal("0.390000")
    assert line.fields["unit_price"]["issues"] == ["not_grounded"]
    assert line.fields["_review"]["needs_review"] is True
    assert proposal.fields["_review"]["needs_review"] is True


async def test_second_delivery_does_nothing(seeded: Seeded, script: Script) -> None:
    script(final(**quote_output()))
    message_id = await seeded.add_message()
    await run_job(seeded, message_id)

    stub = script()  # no scripted response: a model call would fail the run
    await run_job(seeded, message_id)

    assert stub.calls == []
    assert len(await proposals_of(seeded)) == 1
    assert await status_of(seeded, message_id) == "extracted"


async def test_each_reply_is_a_new_version(seeded: Seeded, script: Script) -> None:
    script(final(**quote_output()), final(**quote_output()))
    first = await seeded.add_message()
    second = await seeded.add_message()

    handler = quote_extract_handler(db=seeded.engine, storage=seeded.storage, runner_config=CONFIG)
    await handler(seeded.envelope(first))
    await handler(seeded.envelope(second))

    proposals = await proposals_of(seeded)
    assert [(p.version, p.message_id) for p, _ in proposals] == [(1, first), (2, second)]


async def test_agent_failure_marks_the_message_failed(seeded: Seeded, script: Script) -> None:
    script(final(is_quote=True))  # breaks the output contract
    message_id = await seeded.add_message()

    await run_job(seeded, message_id)

    assert await proposals_of(seeded) == []
    assert await status_of(seeded, message_id) == "failed"


async def test_refusal_is_not_a_proposal_and_declines_the_invite(
    seeded: Seeded, script: Script
) -> None:
    script(
        final(
            is_quote=False,
            declined=True,
            currency=_NONE,
            valid_until=_NONE,
            incoterm=_NONE,
            payment_terms=_NONE,
            lines=[],
            missing_fields=[],
        )
    )
    message_id = await seeded.add_message(
        body="Leider können wir hierzu kein Angebot abgeben.", with_sheet=False
    )

    await run_job(seeded, message_id)

    assert await proposals_of(seeded) == []
    assert await status_of(seeded, message_id) == "extracted"
    async with tenant_session(seeded.engine, seeded.tenant_id) as session:
        status = await session.scalar(select(Invite.status).where(Invite.id == seeded.invite_id))
        count = await session.scalar(select(func.count()).select_from(Proposal))
    assert status == "declined"
    assert count == 0


async def test_other_tenants_cannot_be_targeted(
    seeded: Seeded, script: Script, two_tenants: TwoTenants
) -> None:
    stub = script()
    message_id = await seeded.add_message()
    foreign = Envelope(
        type=JOB_TYPE,
        tenant_id=two_tenants.b.tenant_id,
        idempotency_key=f"extract:{message_id}",
        payload={"message_id": str(message_id)},
    )

    await handle_quote_extract(
        foreign, db=seeded.engine, storage=seeded.storage, runner_config=CONFIG
    )

    assert stub.calls == []
    assert await status_of(seeded, message_id) == "stored"
