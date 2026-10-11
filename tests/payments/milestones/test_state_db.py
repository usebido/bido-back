"""A funded payment's milestones as rows: the tooling plan, split like the program, in order."""

import pytest

from bido_back.db import tenant_session
from bido_back.db.models import PoPayment
from bido_back.payments.milestones.catalog import TOOLING_PLAN, milestone_amounts
from bido_back.payments.milestones.state import (
    accepts_documents,
    ensure_milestones,
    milestone_status,
    submittable,
)
from tests.payments.milestones.conftest import Funded

pytestmark = pytest.mark.db


async def test_a_funded_payment_has_the_tooling_plan_as_rows(funded: Funded) -> None:
    escrow = funded.chain.escrows[(await funded.payment()).escrow_address or ""]
    async with tenant_session(funded.engine, funded.tenant) as session:
        payment = await session.get(PoPayment, funded.payment_id)
        assert payment is not None
        rows = await ensure_milestones(session, payment)
        again = await ensure_milestones(session, payment)

    assert [r.id for r in again] == [r.id for r in rows]
    assert [(r.milestone_index, r.key, r.name, r.bps) for r in rows] == [
        (0, "tool_start", "Tool start", 3000),
        (1, "t1", "T1", 3000),
        (2, "ppap", "PPAP", 4000),
    ]
    total = int(payment.token_amount)
    amounts = milestone_amounts(total, [m.bps for m in TOOLING_PLAN])
    assert [int(r.token_amount) for r in rows] == amounts
    assert sum(amounts) == total
    # The escrow on chain holds the same split, gate = index.
    assert [(m.index, m.gate, m.amount) for m in escrow.milestones] == [
        (0, 0, amounts[0]),
        (1, 1, amounts[1]),
        (2, 2, amounts[2]),
    ]
    assert [r.state for r in rows] == ["open", "open", "open"]
    assert [milestone_status(payment, rows, i, None) for i in range(3)] == [
        "awaiting_documents",
        "locked",
        "locked",
    ]
    assert [submittable(payment, rows, i) for i in range(3)] == [True, False, False]
    assert all(accepts_documents(payment, row) for row in rows)


def test_the_program_split_gives_the_last_milestone_the_rest() -> None:
    assert milestone_amounts(1_000_001, [3000, 3000, 4000]) == [300_000, 300_000, 400_001]
    assert milestone_amounts(10, [3333, 3333, 3334]) == [3, 3, 4]
