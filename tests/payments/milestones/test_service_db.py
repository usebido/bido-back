"""What the buyer and the factory see of a funded payment's milestones, and sending one back.

The amounts split the order, the card charge and the escrow like the program; statuses follow
the milestone through review, release and changes requested; the factory's page carries no
chain words; asking for changes goes through the approval guard and the reason reaches the
factory.
"""

from decimal import Decimal
from typing import Final, cast

import pytest
from pydantic import BaseModel
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import CompanyBranding, PoMilestoneSubmission, Tenant
from bido_back.payments.config import explorer_tx_url
from bido_back.payments.errors import PaymentForbidden, PaymentInvalidState, PaymentNotFound
from bido_back.payments.milestones.catalog import definition_for
from bido_back.payments.milestones.service import (
    factory_order,
    list_milestones,
    request_milestone_changes,
)
from bido_back.payments.milestones.views import PaymentMilestoneView
from tests.approvals.conftest import FakeJobQueue
from tests.awards.test_awards_db import approver, buyer, tenant_name
from tests.payments.flow.test_service_db import member
from tests.payments.milestones.conftest import Funded
from tests.payments.milestones.test_documents_db import (
    SETTINGS,
    FakeStorage,
    factory_link,
    jpeg,
    outsider,
    upload,
    upload_required,
)
from tests.payments.milestones.test_submit_db import mark_released, submissions, submit_required

pytestmark = pytest.mark.db

# Words the factory never sees (keys or values), whatever the language of the page.
CHAIN_WORDS: Final = ("escrow", "solana", "wallet", "token", "signature", "hash", "chain")


def strings_of(value: object) -> list[str]:
    """Every key and string value of a JSON document."""
    if isinstance(value, dict):
        items = cast(dict[object, object], value)
        return [s for key, item in items.items() for s in (str(key), *strings_of(item))]
    if isinstance(value, list):
        return [s for item in cast(list[object], value) for s in strings_of(item)]
    return [value] if isinstance(value, str) else []


def no_chain_words(view: BaseModel) -> None:
    text = [s.lower() for s in strings_of(view.model_dump(mode="json"))]
    leaked = {word for word in CHAIN_WORDS for s in text if word in s}
    assert leaked == set(), leaked


async def buyer_view(funded: Funded) -> list[PaymentMilestoneView]:
    return await list_milestones(
        funded.engine, buyer(funded.people), funded.po_id, settings=SETTINGS
    )


async def changes(
    funded: Funded, index: int = 0, *, who: Principal | None = None, reason: str = "Retake it."
) -> PaymentMilestoneView:
    """Send milestone `index` back (by the payment's requester unless `who` is given)."""
    return await request_milestone_changes(
        funded.engine,
        who or buyer(funded.people),
        funded.po_id,
        index,
        reason,
        reason_code="defect",
        jobs=FakeJobQueue(),
        settings=SETTINGS,
    )


# --- The buyer's view --------------------------------------------------------------------------


async def test_the_milestones_split_the_order_like_the_program(funded: Funded) -> None:
    payment = await funded.payment()

    views = await buyer_view(funded)

    assert [(v.index, v.key, v.name, v.bps) for v in views] == [
        (0, "tool_start", "Tool start", 3000),
        (1, "t1", "T1", 3000),
        (2, "ppap", "PPAP", 4000),
    ]
    # PO-1: 1650.00 EUR, charged 1815.00 USD, 1815 tokens of 6 decimals.
    assert [v.po_amount for v in views] == [Decimal("495.00"), Decimal("495.00"), Decimal("660.00")]
    assert [v.charge_amount for v in views] == [
        Decimal("544.50"),
        Decimal("544.50"),
        Decimal("726.00"),
    ]
    assert sum(v.po_amount for v in views) == payment.po_total
    assert sum(v.charge_amount for v in views) == payment.charge_amount
    assert sum(v.token_amount for v in views) == payment.token_amount.scaleb(-6)
    assert [(v.po_currency, v.charge_currency) for v in views] == [("EUR", "USD")] * 3
    assert [v.status for v in views] == ["awaiting_documents", "locked", "locked"]
    for view in views:
        definition = definition_for(view.key, view.name, view.bps)
        assert [r.doc_type for r in view.requirements] == [d.code for d in definition.documents]
        assert {r.doc_type for r in view.requirements if r.required} == definition.required_codes
        assert view.checks == list(definition.checks)
        assert (view.submissions, view.approval_id, view.release_signature) == ([], None, None)


async def test_statuses_follow_a_milestone_until_it_is_paid_out(funded: Funded) -> None:
    storage = FakeStorage()
    owner = buyer(funded.people)
    # Documents gathered for a later milestone do not unlock it.
    await upload_required(funded, owner, 1, storage=storage)
    submitted = await submit_required(funded, 0, storage=storage)

    in_review = await buyer_view(funded)
    assert [v.status for v in in_review] == ["in_review", "locked", "locked"]
    assert in_review[0].approval_id == submitted.approval_id
    assert [s.state for s in in_review[1].submissions] == ["draft"]

    signature = await mark_released(funded, 0)
    released = await buyer_view(funded)
    assert [v.status for v in released] == ["released", "awaiting_documents", "locked"]
    assert released[0].release_signature == signature
    assert released[0].release_explorer_url == explorer_tx_url("devnet", signature)
    assert released[0].released_at is not None


async def test_another_tenant_sees_no_milestones(funded: Funded) -> None:
    with pytest.raises(PaymentNotFound):
        await list_milestones(funded.engine, outsider(funded), funded.po_id, settings=SETTINGS)


# --- The factory's page ------------------------------------------------------------------------


async def test_the_factory_sees_its_order_and_what_to_send(funded: Funded) -> None:
    storage = FakeStorage()
    link, access = await factory_link(funded, accept=False)

    order = await factory_order(funded.engine, access, settings=SETTINGS)

    payment = await funded.payment()
    assert (order.purchase_order_ref, order.currency, order.total) == (
        "PO-1",
        "EUR",
        Decimal("1650.00"),
    )
    assert (order.buyer_name, order.supplier_name) == (await tenant_name(funded.world), "Rhein")
    assert (order.reserved, order.accepted_at) == (True, None)
    assert order.link_expires_at == link.expires_at == access.expires_at
    assert [m.amount for m in order.milestones] == [
        Decimal("495.00"),
        Decimal("495.00"),
        Decimal("660.00"),
    ]
    assert sum(m.amount for m in order.milestones) == payment.po_total
    assert [m.status for m in order.milestones] == ["awaiting_documents", "locked", "locked"]
    for milestone in order.milestones:
        definition = definition_for(milestone.key, milestone.label, milestone.bps)
        assert [r.doc_type for r in milestone.requirements] == [
            d.code for d in definition.documents
        ]
        assert all(r.uploaded == [] for r in milestone.requirements)
        assert milestone.label_zh
    no_chain_words(order)

    _, access = await factory_link(funded)  # a new link, confirmed
    sent = await upload_required(funded, access, 0, storage=storage)
    gathering = await factory_order(funded.engine, access, settings=SETTINGS)
    uploaded = {f.id for r in gathering.milestones[0].requirements for f in r.uploaded}
    assert uploaded == {d.id for d in sent}
    assert gathering.accepted_at is not None
    no_chain_words(gathering)


async def test_the_factory_follows_review_payout_and_changes(funded: Funded) -> None:
    storage = FakeStorage()
    _, access = await factory_link(funded)
    submitted = await submit_required(funded, 0, storage=storage, submitter=access)

    in_review = await factory_order(funded.engine, access, settings=SETTINGS)
    first = in_review.milestones[0]
    assert (first.status, first.rejection_code, first.rejection_reason) == ("in_review", None, None)
    assert first.submitted_at is not None
    sent = {f.id for r in first.requirements for f in r.uploaded}
    assert sent == {d.id for d in submitted.milestone.submissions[0].documents}
    no_chain_words(in_review)

    view = await changes(funded, reason="Retake photo 2.")
    assert view.status == "changes_requested"
    sent_back = await factory_order(funded.engine, access, settings=SETTINGS)
    first = sent_back.milestones[0]
    assert (first.status, first.rejection_code, first.rejection_reason) == (
        "changes_requested",
        "defect",
        "Retake photo 2.",
    )
    # Its files wait in the new draft once the factory sends the next one.
    photos = next(r for r in first.requirements if "jpg" in r.accepts)
    await upload(funded, access, 0, photos.doc_type, storage=storage, data=jpeg())
    redrafted = (await factory_order(funded.engine, access, settings=SETTINGS)).milestones[0]
    assert redrafted.status == "changes_requested"
    assert len({f.id for r in redrafted.requirements for f in r.uploaded}) == len(sent) + 1
    no_chain_words(sent_back)

    await submit_required(funded, 0, storage=storage, submitter=access)
    await mark_released(funded, 0)
    released = await factory_order(funded.engine, access, settings=SETTINGS)
    assert [m.status for m in released.milestones] == ["released", "awaiting_documents", "locked"]
    assert released.milestones[0].released_at is not None
    no_chain_words(released)


async def test_the_buyer_is_named_as_on_its_documents(
    funded: Funded, migrator_engine: AsyncEngine
) -> None:
    _, access = await factory_link(funded)
    try:
        async with tenant_session(funded.engine, funded.tenant) as session:
            session.add(CompanyBranding(tenant_id=funded.tenant, legal_name="Acme Tooling GmbH"))
        legal = await factory_order(funded.engine, access, settings=SETTINGS)
        async with tenant_session(funded.engine, funded.tenant) as session:
            await session.execute(
                update(CompanyBranding)
                .where(CompanyBranding.tenant_id == funded.tenant)
                .values(trade_name="Acme")
            )
        trade = await factory_order(funded.engine, access, settings=SETTINGS)
    finally:
        async with tenant_session(migrator_engine, funded.tenant) as session:
            await session.execute(
                delete(CompanyBranding).where(CompanyBranding.tenant_id == funded.tenant)
            )
    assert (legal.buyer_name, trade.buyer_name) == ("Acme Tooling GmbH", "Acme")


# --- Sending a milestone back ------------------------------------------------------------------


async def test_changes_requested_send_the_milestone_back_with_the_reason(funded: Funded) -> None:
    storage = FakeStorage()
    submitted = await submit_required(funded, 0, storage=storage)

    view = await changes(funded, reason="The steel is not stamped.")

    [refused] = await submissions(funded, 0)
    assert (refused.state, refused.rejection_code, refused.rejection_reason) == (
        "rejected",
        "defect",
        "The steel is not stamped.",
    )
    assert (refused.decided_by, refused.approval_id) == (funded.people.owner, submitted.approval_id)
    assert (view.status, view.approval_id, view.evidence_hash) == ("changes_requested", None, None)
    [shown] = view.submissions
    assert (shown.rejection_code, shown.rejection_reason) == ("defect", "The steel is not stamped.")
    with pytest.raises(PaymentInvalidState, match="not waiting"):
        await changes(funded)


async def test_only_who_may_decide_the_approval_sends_it_back(
    funded: Funded, migrator_engine: AsyncEngine
) -> None:
    storage = FakeStorage()
    await submit_required(funded, 0, storage=storage)
    # A member who did not ask for the payment is no approver.
    with pytest.raises(PaymentForbidden):
        await changes(funded, who=member(funded.people))
    with pytest.raises(PaymentNotFound):
        await changes(funded, who=outsider(funded))
    # Four-eyes company: the payment's requester may not decide its releases either.
    async with tenant_session(migrator_engine, funded.tenant) as session:
        await session.execute(
            update(Tenant).where(Tenant.id == funded.tenant).values(allow_self_approval=False)
        )
    with pytest.raises(PaymentForbidden):
        await changes(funded, who=buyer(funded.people))
    [waiting] = await submissions(funded, 0)
    assert waiting.state == "submitted"

    view = await changes(funded, who=approver(funded.people))

    assert view.status == "changes_requested"
    async with tenant_session(funded.engine, funded.tenant) as session:
        refused = await session.get(PoMilestoneSubmission, waiting.id)
    assert refused is not None and refused.decided_by == funded.people.approver
