"""The RFQ's activity feed tells a payment's milestone story in code-written English.

The events carry ids, the milestone's index and codes; the feed names the milestone
from the payment's milestones and the supplier from its order, and never shows a note,
a file name or the text of the reason changes were asked for, even when an event
carries one. The factory has no login: what it does shows under the supplier's name.
"""

import secrets
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from bido_back.db import tenant_session
from bido_back.db.models import PoMilestoneSubmission, PoPayment
from bido_back.payments.milestones.execute import MILESTONE_RELEASE_RETRIED
from bido_back.payments.milestones.links import FACTORY_ORDER_ACCEPTED
from bido_back.payments.milestones.proposals import (
    FACTORY_LINK_ISSUED,
    FACTORY_LINK_REVOKED,
    MILESTONE_CHANGES_REQUESTED,
    MILESTONE_DOCUMENT_ADDED,
    MILESTONE_DOCUMENT_REMOVED,
    MILESTONE_RELEASE_FAILED,
    MILESTONE_RELEASED,
    MILESTONE_RELEASING,
    MILESTONE_SUBMITTED,
    PAYMENT_COMPLETED,
)
from bido_back.payments.milestones.state import ensure_milestones
from bido_back.rfqs.activity import rfq_activity
from bido_back.rfqs.audit import rfq_event
from bido_back.rfqs.schemas import RfqActivityItem
from tests.awards.test_awards_db import buyer
from tests.payments.milestones.conftest import Funded

pytestmark = pytest.mark.db

# Content some event might carry by mistake: it never reaches the feed.
SECRETS = ("Steel arrived", "mill-cert-v2.pdf", "Cavity 2 is short", "token-")

type Recorded = tuple[str, UUID | None, dict[str, object]]  # (action, actor, payload)


async def record(funded: Funded, events: list[Recorded]) -> list[RfqActivityItem]:
    po = funded.paying.po
    async with tenant_session(funded.engine, funded.tenant) as session:
        for action, actor, payload in events:
            session.add(rfq_event(funded.tenant, actor, action, po.rfq_id, **payload))
    return await rfq_activity(funded.engine, buyer(funded.people), po.rfq_id, limit=200)


async def test_the_feed_tells_the_milestone_story_without_content(funded: Funded) -> None:
    people, po = funded.people, funded.paying.po
    now = datetime.now(UTC)
    async with tenant_session(funded.engine, funded.tenant) as session:
        payment = await session.get(PoPayment, funded.payment_id)
        assert payment is not None
        t1 = (await ensure_milestones(session, payment))[1]
        # T1's first attempt, sent back: the code is stored with the decision.
        refused = PoMilestoneSubmission(
            tenant_id=funded.tenant,
            payment_id=payment.id,
            milestone_id=t1.id,
            milestone_index=1,
            attempt=1,
            state="rejected",
            source="factory",
            evidence_hash=secrets.token_hex(32),
            approval_id=uuid4(),
            submitted_at=now,
            decided_by=people.approver,
            decided_at=now,
            rejection_code="dimensional_nonconformance",
            rejection_reason="Cavity 2 is short",
        )
        session.add(refused)
        await session.flush()
        refused_id = refused.id
    ids: dict[str, object] = {
        "payment_id": funded.payment_id,
        "purchase_order_id": po.id,
        "purchase_order_ref": po.ref,
    }
    feed = await record(
        funded,
        [
            (FACTORY_LINK_ISSUED, people.owner, {**ids, "link_id": "l1", "token": "token-x"}),
            (FACTORY_ORDER_ACCEPTED, None, {**ids, "link_id": "l1"}),
            (
                MILESTONE_DOCUMENT_ADDED,
                None,
                {
                    **ids,
                    "source": "factory",
                    "milestone_index": 0,
                    "doc_type": "steel_certificate",
                    "filename": "mill-cert-v2.pdf",
                },
            ),
            (
                MILESTONE_DOCUMENT_REMOVED,
                people.owner,
                {**ids, "source": "buyer", "milestone_index": 0, "doc_type": "tool_start_photo"},
            ),
            (
                MILESTONE_SUBMITTED,
                None,
                {**ids, "milestone_index": 0, "attempt": 1, "note": "Steel arrived"},
            ),
            (MILESTONE_RELEASING, people.approver, {**ids, "milestone_index": 0}),
            (
                MILESTONE_RELEASED,
                None,
                {**ids, "invite_id": po.invite_id, "milestone_index": 0, "signature": "sig"},
            ),
            (
                MILESTONE_CHANGES_REQUESTED,
                people.approver,
                {
                    **ids,
                    "milestone_index": 1,
                    "submission_id": refused_id,
                    "reason": "Cavity 2 is short",
                },
            ),
            (MILESTONE_SUBMITTED, None, {**ids, "milestone_index": 1, "attempt": 2}),
            (
                MILESTONE_RELEASE_FAILED,
                None,
                {**ids, "milestone_index": 2, "failure_code": "chain_release_failed"},
            ),
            (
                MILESTONE_RELEASE_RETRIED,
                people.owner,
                {**ids, "milestone_index": 2, "previous_failure_code": "chain_release_failed"},
            ),
            (PAYMENT_COMPLETED, None, {**ids, "milestones": 3}),
            (FACTORY_LINK_REVOKED, people.owner, ids),
            ("approval.requested", None, {"tool": "release_po_milestone"}),
        ],
    )

    lines = {item.action: (item.kind, item.actor, item.summary) for item in feed}
    order = f"purchase order {po.ref}"
    owner = lines[FACTORY_LINK_ISSUED][1]
    approver = lines[MILESTONE_RELEASING][1]
    assert owner is not None and approver is not None and "Bido" not in (owner, approver)
    assert lines[FACTORY_LINK_ISSUED] == (
        "user",
        owner,
        f"Factory link created for {order}: the factory sends its documents there",
    )
    assert lines[FACTORY_ORDER_ACCEPTED] == (
        "user",
        "Rhein",
        f"Purchase order {po.ref} and its milestones confirmed by the factory",
    )
    assert lines[MILESTONE_DOCUMENT_ADDED] == (
        "user",
        "Rhein",
        f"Document added to milestone Tool start of {order}: Steel certificate",
    )
    assert lines[MILESTONE_DOCUMENT_REMOVED] == (
        "user",
        owner,
        f"Document removed from milestone Tool start of {order}: Photos of the tool steel, "
        "marked with the tool ID and PO number",
    )
    assert sorted((i.actor, i.summary) for i in feed if i.action == MILESTONE_SUBMITTED) == [
        ("Rhein", f"Milestone T1 of {order} sent for approval again"),
        ("Rhein", f"Milestone Tool start of {order} sent for approval"),
    ]
    assert lines[MILESTONE_RELEASING] == (
        "system",
        approver,
        f"Milestone Tool start of {order} approved: releasing it from the escrow",
    )
    assert lines[MILESTONE_RELEASED] == (
        "system",
        "Bido",
        f"Milestone Tool start of {order} released from the escrow (30% of the total)",
    )
    assert lines[MILESTONE_CHANGES_REQUESTED] == (
        "user",
        approver,
        f"Changes requested on milestone T1 of {order} (dimensional nonconformance): the "
        "factory sends it again",
    )
    assert lines[MILESTONE_RELEASE_FAILED] == (
        "system",
        "Bido",
        f"Milestone PPAP of {order} not released (chain release failed): the money stays in "
        "the escrow",
    )
    assert lines[MILESTONE_RELEASE_RETRIED] == (
        "user",
        owner,
        f"Release of milestone PPAP of {order} retried after it failed (chain release failed)",
    )
    assert lines[PAYMENT_COMPLETED] == (
        "system",
        "Bido",
        f"Payment of {order} completed: the escrow released every milestone",
    )
    assert lines[FACTORY_LINK_REVOKED] == ("user", owner, f"Factory link for {order} revoked")
    assert "Approval requested: milestone release" in [
        item.summary for item in feed if item.action == "approval.requested"
    ]
    # Every milestone event is about the order's supplier.
    milestone_events = [i for i in feed if i.action.startswith("purchase_order.milestone_")]
    assert {i.supplier_name for i in milestone_events} == {"Rhein"}
    shown = " ".join(item.summary for item in feed)
    assert not [secret for secret in SECRETS if secret in shown]


async def test_a_milestone_the_feed_cannot_name_is_counted_from_one(funded: Funded) -> None:
    """Without the milestone rows the feed counts milestones from one; without the payment
    it cannot name the supplier either."""
    ref = funded.paying.po.ref
    feed = await record(
        funded,
        [
            (
                MILESTONE_SUBMITTED,
                None,
                {
                    "payment_id": funded.payment_id,
                    "milestone_index": 1,
                    "purchase_order_ref": ref,
                },
            ),
            (MILESTONE_SUBMITTED, None, {"purchase_order_ref": ref}),
        ],
    )

    submitted = sorted(
        (item.actor, item.summary) for item in feed if item.action == MILESTONE_SUBMITTED
    )
    assert submitted == [
        ("Rhein", f"Milestone 2 of purchase order {ref} sent for approval"),
        ("The factory", f"A milestone of purchase order {ref} sent for approval"),
    ]
