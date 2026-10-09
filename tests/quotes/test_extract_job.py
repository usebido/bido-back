"""The QUOTE_EXTRACT job against Postgres, with a scripted model (no network).

Marked `db`: skipped on a laptop without Postgres, failing in CI (see
tests/db/conftest.py).
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.config import RunnerConfig
from bido_back.auth import Principal
from bido_back.corrections.schemas import LineCorrectionRequest
from bido_back.corrections.service import correct_line, list_corrections
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
from bido_back.quotes.lead_time import decode_lead_time, decode_validity
from bido_back.quotes.terms import line_terms, offer_terms
from bido_back.worker.envelope import Envelope
from tests.agents.test_runner import Script, final, script
from tests.db.conftest import TwoTenants, app_engine, migrator_engine, two_tenants
from tests.documents.fixtures import build_xlsx
from tests.quotes import rfq6

__all__ = ["app_engine", "migrator_engine", "script", "two_tenants"]

pytestmark = pytest.mark.db

CONFIG = RunnerConfig(openai_api_key="test-not-a-real-key")
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

    async def add_message(
        self,
        *,
        body: str = BODY,
        with_sheet: bool = True,
        occurred_at: datetime | None = None,
        in_reply_to: str | None = None,
        subject: str = "AW: [RFQ-12] LDO regulators",
    ) -> UUID:
        async with tenant_session(self.engine, self.tenant_id) as session:
            message = Message(
                tenant_id=self.tenant_id,
                invite_id=self.invite_id,
                direction="in",
                message_id_header=f"<{uuid4().hex}@supplier.test>",
                subject=subject,
                body_text=body,
                status="stored",
                occurred_at=occurred_at or datetime.now(UTC),
                in_reply_to=in_reply_to,
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
        "qualification": _NONE,
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
    user_text = cast(list[dict[str, Any]], request["input"])[1]["content"]
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
    assert proposal.fields["_extraction"]["prompt_version"] == 6
    assert proposal.fields["extra_offers"] == []
    assert proposal.fields["offer_notes"] == []
    assert proposal.fields["_signals"] == {
        "final_offer": False,
        "final_offer_quote": None,
        "declined_lines": [],
        "questions": [],
    }
    # Two tiers with their quantities written in the sheet.
    assert [line.fields["_terms"] for line in lines] == [
        {"quantity_basis": "stated_tier", "price_applies_to_requested_quantity": True}
    ] * 2

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


AMENDMENT = "For tier 1000, delivery is now 15 days. All other terms unchanged."


def revision_output(*, unchanged: bool = True) -> dict[str, object]:
    return {
        "is_quote": True,
        "currency": _NONE,
        "valid_until": _NONE,
        "incoterm": _NONE,
        "payment_terms": _NONE,
        "reply_kind": "amendment",
        "unchanged_terms": _body("All other terms unchanged", "All other terms unchanged")
        if unchanged
        else _NONE,
        "lines": [
            {
                "rfq_line_position": 1,
                "offered_mpn": _NONE,
                "min_quantity": _body("1000", "tier 1000"),
                "unit_price": _NONE,
                "unit": _NONE,
                "tooling_cost": _NONE,
                "moq": _NONE,
                "lead_time_days": _body("15", "15 days"),
                "origin_country": _NONE,
                "qualification": _NONE,
            }
        ],
    }


async def test_delivery_revision_keeps_original_price_evidence_and_supplier_context(
    seeded: Seeded, script: Script
) -> None:
    script(final(**quote_output()))
    first = await seeded.add_message()
    await run_job(seeded, first)
    second = await seeded.add_message(body=AMENDMENT, with_sheet=False)
    stub = script(final(**revision_output()))
    await run_job(seeded, second)
    proposals = await proposals_of(seeded)
    proposal, lines = proposals[-1]
    assert proposal.currency == "EUR"
    assert [(line.min_quantity, line.unit_price, line.lead_time_days) for line in lines] == [
        (Decimal("1000"), Decimal("0.45"), 15),
        (Decimal("5000"), Decimal("0.41"), 42),
    ]
    assert lines[0].fields["unit_price"]["message_id"] == str(first)
    assert lines[0].fields["lead_time_days"]["message_id"] == str(second)
    assert not any(line.needs_review for line in lines)
    user_text = str(stub.calls[0]["input"])
    assert "prior_supplier_offer" in user_text
    assert str(first) in user_text
    assert not stub.calls[0].get("tools")


async def test_late_extraction_rebuilds_offer_in_business_order(
    seeded: Seeded, script: Script
) -> None:
    first = await seeded.add_message()
    second = await seeded.add_message(body=AMENDMENT, with_sheet=False)
    script(final(**revision_output()), final(**quote_output()))
    await run_job(seeded, second)
    await run_job(seeded, first)
    proposals = await proposals_of(seeded)
    proposal, lines = proposals[-1]
    assert proposal.message_id == first  # Extraction version is not business recency.
    assert proposal.fields["_offer_state"]["active_message_id"] == str(second)
    assert lines[0].unit_price == Decimal("0.45")
    assert lines[0].lead_time_days == 15
    assert not lines[0].needs_review
    stub = script()
    await run_job(seeded, first)
    assert not stub.calls
    assert len(await proposals_of(seeded)) == 2


async def test_ambiguous_amendment_is_durable_and_held(seeded: Seeded, script: Script) -> None:
    first = await seeded.add_message()
    script(final(**quote_output()))
    await run_job(seeded, first)
    second = await seeded.add_message(
        body="For tier 1000 delivery is now 15 days.", with_sheet=False
    )
    script(final(**revision_output(unchanged=False)))
    await run_job(seeded, second)
    proposal, lines = (await proposals_of(seeded))[-1]
    assert proposal.fields["_offer_state"]["requires_clarification"] is True
    assert all(line.needs_review for line in lines)
    assert lines[0].lead_time_days == 42
    assert proposal.fields["_quote_event"]["lines"][0]["fields"]["lead_time_days"]["parsed"] == 15


async def test_explicit_withdrawal_replaces_active_offer_with_no_lines(
    seeded: Seeded, script: Script
) -> None:
    first = await seeded.add_message()
    script(final(**quote_output()))
    await run_job(seeded, first)
    second = await seeded.add_message(body="We withdraw our entire offer.", with_sheet=False)
    script(
        final(
            is_quote=True,
            currency=_NONE,
            valid_until=_NONE,
            incoterm=_NONE,
            payment_terms=_NONE,
            reply_kind="withdrawal",
            update_evidence=_body("We withdraw our entire offer", "We withdraw our entire offer"),
            lines=[],
        )
    )
    await run_job(seeded, second)
    proposal, lines = (await proposals_of(seeded))[-1]
    assert lines == []
    assert proposal.fields["_offer_state"]["reply_kind"] == "withdrawal"
    assert proposal.fields["_offer_state"]["active_message_id"] == str(second)


async def test_buyer_correction_survives_replay_and_retains_its_audit_id(
    seeded: Seeded, script: Script, two_tenants: TwoTenants
) -> None:
    first = await seeded.add_message()
    script(final(**quote_output()))
    await run_job(seeded, first)
    proposal, lines = (await proposals_of(seeded))[-1]
    principal = Principal(tenant_id=seeded.tenant_id, user_id=two_tenants.a.user_id)
    await correct_line(
        seeded.engine,
        principal,
        proposal.id,
        lines[0].id,
        LineCorrectionRequest(unit_price=Decimal("0.44"), reason="Confirmed with supplier"),
    )
    [correction] = await list_corrections(seeded.engine, principal, proposal.id)
    second = await seeded.add_message(body=AMENDMENT, with_sheet=False)
    script(final(**revision_output()))
    await run_job(seeded, second)
    _, updated = (await proposals_of(seeded))[-1]
    assert updated[0].unit_price == Decimal("0.44")
    assert updated[0].fields["unit_price"]["corrected"] is True
    assert updated[0].fields["unit_price"]["correction_id"] == str(correction.id)


async def test_refusal_after_offer_prevents_recommending_stale_terms(
    seeded: Seeded, script: Script
) -> None:
    first = await seeded.add_message()
    script(final(**quote_output()))
    await run_job(seeded, first)
    second = await seeded.add_message(body="We cannot supply these parts.", with_sheet=False)
    script(
        final(
            is_quote=False,
            declined=True,
            currency=_NONE,
            valid_until=_NONE,
            incoterm=_NONE,
            payment_terms=_NONE,
            lines=[],
        )
    )
    await run_job(seeded, second)
    proposal, lines = (await proposals_of(seeded))[-1]
    assert all(line.needs_review for line in lines)
    assert proposal.fields["_offer_state"]["requires_clarification"] is True


async def test_an_older_revision_delivered_late_keeps_the_newer_offer_usable(
    seeded: Seeded, script: Script
) -> None:
    """Revisão 2 arrives first; Revisão 1, written earlier, arrives later. The Date
    headers and the supplier's revision numbers agree: no hold, Revisão 2 stays."""
    now = datetime.now(UTC)
    newer = await seeded.add_message(
        subject="AW: [RFQ-12] LDO regulators - Revisão 2", occurred_at=now - timedelta(hours=1)
    )
    script(
        final(
            **quote_output(),
            extra_offers=[
                {
                    "description": "Wartungsset",
                    "mpn": "WS-1",
                    "quantity": None,
                    "unit_price": "12.00",
                    "currency": "EUR",
                    "conditions": None,
                    "quote": "Wartungsset WS-1: 12,00 EUR",
                }
            ],
        )
    )
    await run_job(seeded, newer)
    older = await seeded.add_message(
        subject="AW: [RFQ-12] LDO regulators - Revisão 1", occurred_at=now - timedelta(hours=2)
    )
    script(final(**quote_output()))
    await run_job(seeded, older)

    proposal, lines = (await proposals_of(seeded))[-1]
    state = proposal.fields["_offer_state"]
    assert state["active_message_id"] == str(newer)
    assert state["issues"] == []
    assert not any(line.needs_review for line in lines)
    # The first reply's unrequested offer is still listed after the second reply.
    assert [offer["mpn"] for offer in proposal.fields["extra_offers"]] == ["WS-1"]


async def test_an_offer_stored_before_v5_can_still_be_amended(
    seeded: Seeded, script: Script
) -> None:
    first = await seeded.add_message()
    script(final(**quote_output()))
    await run_job(seeded, first)
    # Strip what prompt v6 added from the stored event, as an older proposal has it.
    async with tenant_session(seeded.engine, seeded.tenant_id) as session:
        stored = await session.scalar(select(Proposal).where(Proposal.message_id == first))
        assert stored is not None
        event = dict(stored.fields["_quote_event"])
        event["header"] = {
            k: v for k, v in event["header"].items() if k not in ("validity", "lead_time")
        }
        event["lines"] = [
            {**line, "fields": {k: v for k, v in line["fields"].items() if k != "lead_time_terms"}}
            for line in event["lines"]
        ]
        stored.fields = {**stored.fields, "_quote_event": event}
    second = await seeded.add_message(body=AMENDMENT, with_sheet=False)
    script(final(**revision_output()))

    await run_job(seeded, second)

    proposal, lines = (await proposals_of(seeded))[-1]
    assert proposal.message_id == second
    assert [(line.unit_price, line.lead_time_days) for line in lines] == [
        (Decimal("0.45"), 15),
        (Decimal("0.41"), 42),
    ]
    assert not any(line.needs_review for line in lines)


@pytest_asyncio.fixture
async def metalnova(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine, two_tenants: TwoTenants
) -> AsyncIterator[Seeded]:
    """RFQ-6: a supplier created from the chat (language "en", no country)."""
    tenant = two_tenants.a
    async with tenant_session(app_engine, tenant.tenant_id) as session:
        supplier = Supplier(tenant_id=tenant.tenant_id, name="MetalNova Teste Ltda", language="en")
        contact = Contact(tenant_id=tenant.tenant_id, kind="supplier", email="sales@metal.test")
        rfq = Rfq(
            tenant_id=tenant.tenant_id, ref="RFQ-6", title="MTR-100", created_by=tenant.user_id
        )
        session.add_all([supplier, contact, rfq])
        await session.flush()
        lines = [
            RfqLine(
                tenant_id=tenant.tenant_id,
                rfq_id=rfq.id,
                position=position,
                description=description,
                part_number=part,
                quantities=[quantity],
                unit=unit,
            )
            for position, description, part, quantity, unit in (
                (1, "Rolamento motor MTR-100", "000101", 70, "pcs"),
                (2, "Eixo motor MTR-100", "000102", 15, "pcs"),
                (3, "Fio de cobre esmaltado", "000104", 12.5, "kg"),
            )
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
            for model in (ProposalLine, Proposal, Message, Invite, RfqLine, Rfq, Contact, Supplier):
                await session.execute(delete(model).where(model.tenant_id == tenant.tenant_id))


async def test_rfq6_first_reply_stores_lead_time_validity_and_quantity_terms(
    metalnova: Seeded, script: Script
) -> None:
    stub = script(final(**rfq6.first_reply().model_dump(mode="json")))
    message_id = await metalnova.add_message(
        body=rfq6.REPLY_1, with_sheet=False, occurred_at=rfq6.REPLY_1_AT, subject=rfq6.SUBJECT
    )

    await run_job(metalnova, message_id)

    # The model may complete a date without a year; durations are counted by code.
    user_text = cast(list[dict[str, Any]], stub.calls[0]["input"])[1]["content"]
    assert '"message_date":"2026-10-09"' in user_text
    [(proposal, lines)] = await proposals_of(metalnova)
    # "valid for 15 days" from the message's date.
    assert proposal.valid_until == date(2026, 10, 24)
    validity = decode_validity(proposal.fields["validity"]["parsed"])
    assert validity is not None and validity.anchor == date(2026, 10, 9)
    # "approximately 10 business days" for all items, with Brazil's holidays (BRL): 17 days.
    by_position = sorted(lines, key=lambda line: line.fields["_review"]["rfq_line_position"])
    assert [line.lead_time_days for line in by_position] == [17, 17, 17]
    assert not any(line.needs_review for line in by_position)
    assert [line.min_quantity for line in by_position] == [None, None, None]
    terms = [line_terms(line.fields) for line in by_position]
    assert all(
        t.quantity is not None and t.quantity.price_applies_to_requested_quantity for t in terms
    )
    shaft = terms[1].lead_time
    assert shaft is not None and shaft.risk_note == rfq6.SHAFT_RISK and shaft.approximate
    offer = offer_terms(proposal.fields)
    assert offer.notes == ("All items are supplied in standard industrial packaging.",)
    assert not offer.signals.final_offer


async def test_rfq6_second_reply_is_read_per_line_in_business_days(
    metalnova: Seeded, script: Script
) -> None:
    script(final(**rfq6.first_reply().model_dump(mode="json")))
    first = await metalnova.add_message(
        body=rfq6.REPLY_1, with_sheet=False, occurred_at=rfq6.REPLY_1_AT, subject=rfq6.SUBJECT
    )
    await run_job(metalnova, first)
    stub = script(final(**rfq6.second_reply().model_dump(mode="json")))
    second = await metalnova.add_message(
        body=rfq6.REPLY_2, with_sheet=False, occurred_at=rfq6.REPLY_2_AT, subject=rfq6.SUBJECT
    )

    await run_job(metalnova, second)

    assert "prior_supplier_offer" in str(stub.calls[0]["input"])
    proposal, _ = (await proposals_of(metalnova))[-1]
    # What this message says, before reconciliation decides what it changes.
    event = proposal.fields["_quote_event"]
    lead_days = [line["fields"]["lead_time_days"] for line in event["lines"]]
    assert [entry["parsed"] for entry in lead_days] == [12, 19, 13]
    assert all(entry["issues"] == [] for entry in lead_days)
    leads = [
        decode_lead_time(line["fields"]["lead_time_terms"]["parsed"]) for line in event["lines"]
    ]
    assert [(lead.amount, lead.unit, lead.basis) for lead in leads if lead is not None] == [
        (Decimal(7), "business_days", "order"),
        (Decimal(12), "business_days", "order"),
        (Decimal(8), "business_days", "order"),
    ]
    assert event["header"]["valid_until"]["parsed"] == "2026-10-24"
    validity = decode_validity(event["header"]["validity"]["parsed"])
    assert validity is not None and validity.anchor_kind == "original_offer"
