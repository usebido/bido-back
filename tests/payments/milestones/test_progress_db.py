"""How far a payment's escrow released it, as the purchase order and the payment show it.

`released_bps` adds up the released milestones; `next_milestone` names the first one not
released while the escrow is funded ("30% paid · next: T1"). A payment whose every
milestone is released stays `funded`, with `completed_at`: never charged again, never
paid again. The releases are recorded by hand here, the way the release job records them
(`payments.milestones.release`).
"""

import secrets
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from bido_back.awards import service as award_service
from bido_back.db import tenant_session
from bido_back.db.models import (
    PaymentChainTransaction,
    PoMilestoneSubmission,
    PoPayment,
)
from bido_back.payments import service
from bido_back.payments.milestones import service as milestone_service
from bido_back.payments.milestones.state import ensure_milestones
from bido_back.payments.views import PurchaseOrderPaymentSummary
from bido_back.settings import Settings
from tests.awards.test_awards_db import buyer
from tests.payments.fakes import address
from tests.payments.milestones.conftest import Funded

pytestmark = pytest.mark.db

SETTINGS = Settings.model_validate(
    {"environment": "test", "database_url": "postgresql+asyncpg://app@localhost:5432/unused"}
)


async def released(funded: Funded, index: int) -> str:
    """Milestone `index` released by a confirmed `release` transaction; the payment
    complete (`completed_at`) with the last one. Returns the transaction's signature."""
    now = datetime.now(UTC)
    evidence = secrets.token_hex(32)
    signature = address(f"release:{uuid4()}")
    async with tenant_session(funded.engine, funded.tenant) as session:
        payment = await session.get(PoPayment, funded.payment_id)
        assert payment is not None
        milestones = await ensure_milestones(session, payment)
        milestone = milestones[index]
        submission = PoMilestoneSubmission(
            tenant_id=funded.tenant,
            payment_id=payment.id,
            milestone_id=milestone.id,
            milestone_index=index,
            attempt=1,
            state="released",
            source="buyer",
            submitted_by=funded.people.owner,
            evidence_hash=evidence,
            approval_id=uuid4(),
            submitted_at=now,
            decided_by=funded.people.approver,
            decided_at=now,
        )
        session.add(submission)
        await session.flush()
        session.add(
            PaymentChainTransaction(
                tenant_id=funded.tenant,
                payment_id=payment.id,
                step="release",
                milestone_index=index,
                attempt=1,
                signature=signature,
                last_valid_block_height=1_000,
                state="confirmed",
            )
        )
        milestone.state = "released"
        milestone.approval_id = submission.approval_id
        milestone.submission_id = submission.id
        milestone.evidence_hash = evidence
        milestone.release_signature = signature
        milestone.released_at = now
        if all(m.state == "released" for m in milestones):
            payment.completed_at = now
    return signature


async def summary(funded: Funded) -> PurchaseOrderPaymentSummary:
    order = await award_service.get_purchase_order(
        funded.engine, buyer(funded.people), funded.po_id
    )
    assert order.payment is not None
    return order.payment


async def progress(funded: Funded) -> tuple[str, int, str | None]:
    shown = await summary(funded)
    return shown.state, shown.released_bps, shown.next_milestone


async def test_the_order_shows_how_far_the_escrow_released_it(funded: Funded) -> None:
    # Funded, its milestones not looked at yet: nothing released, Tool start next.
    assert await progress(funded) == ("funded", 0, "Tool start")
    async with tenant_session(funded.engine, funded.tenant) as session:
        payment = await session.get(PoPayment, funded.payment_id)
        assert payment is not None
        await ensure_milestones(session, payment)
    assert await progress(funded) == ("funded", 0, "Tool start")

    await released(funded, 0)
    assert await progress(funded) == ("funded", 3_000, "T1")
    await released(funded, 1)
    assert await progress(funded) == ("funded", 6_000, "PPAP")
    await released(funded, 2)
    assert await progress(funded) == ("funded", 10_000, None)

    [listed] = await award_service.list_purchase_orders(funded.engine, buyer(funded.people))
    assert listed.payment_state == "funded"
    shown = await summary(funded)
    assert (shown.payment_id, shown.needs_attention) == (funded.payment_id, False)


async def test_the_payment_shows_its_releases_and_when_it_completed(funded: Funded) -> None:
    signatures = [await released(funded, index) for index in range(3)]

    view = await service.get_po_payment(funded.engine, buyer(funded.people), funded.po_id, SETTINGS)

    # The API's payment states are unchanged: complete is `funded` with `completed_at`.
    assert view.state == "funded"
    payment = await funded.payment()
    assert view.completed_at == payment.completed_at and view.completed_at is not None
    assert [(m.key, m.name, m.bps) for m in view.milestones] == [
        ("tool_start", "Tool start", 3_000),
        ("t1", "T1", 3_000),
        ("ppap", "PPAP", 4_000),
    ]
    # The payment's own steps only: each release is shown on its milestone.
    transactions = view.chain.transactions
    assert [(t.step, t.state) for t in transactions] == [
        ("mint", "confirmed"),
        ("create_escrow", "confirmed"),
        ("fund", "confirmed"),
    ]
    assert all(t.explorer_url.endswith("?cluster=devnet") for t in transactions)
    shown = await milestone_service.list_milestones(
        funded.engine, buyer(funded.people), funded.po_id, settings=SETTINGS
    )
    assert [m.release_signature for m in shown] == signatures
    assert all(
        m.release_explorer_url is not None and m.release_explorer_url.endswith("?cluster=devnet")
        for m in shown
    )
