"""`pay_purchase_order` against Postgres: approved by a person, claimed, charged once.

The providers are fakes (`tests.payments.fakes`); the approval guard, the executor
and the database are real. `Paying` and `start_paying` are shared with the pipeline and
sweep tests.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

import pytest
import pytest_asyncio
from pydantic import JsonValue
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.context import ToolContext
from bido_back.agents.events import args_hash
from bido_back.approvals import ApprovalForbidden, approval_execute_envelope, decide_approval
from bido_back.awards import lifecycle
from bido_back.awards.schemas import CancelRequest
from bido_back.awards.send import AWARD_REJECT_HOOKS
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    AuditEvent,
    IssuedPurchaseOrder,
    PaymentMethod,
    PoPayment,
    SupplierPayoutAccount,
    Tenant,
)
from bido_back.payments.errors import CardCredentialLost, PaymentRefused, ProviderError
from bido_back.payments.execute import (
    CHARGE_MISMATCH,
    CREDENTIAL_GRACE,
    PAYMENT_REJECT_HOOKS,
    ChargePending,
    PaymentActDeps,
    payment_tools,
    register_payment_act_tools,
)
from bido_back.payments.pipeline import payment_advance_envelope
from bido_back.payments.proposals import (
    PAYMENT_ADVANCE,
    PAYMENT_CHARGED,
    PAYMENT_DECLINED,
    PAYMENT_FAILED,
    payment_proposal_ids,
)
from bido_back.payments.schemas import MilestoneInput, PayPurchaseOrderResult
from tests.approvals.conftest import FakeJobQueue, audit_actions, load
from tests.awards.test_awards_db import World, approver, buyer
from tests.awards.test_lifecycle import sent_po
from tests.payments.fakes import FakeProviders, lost_credential, provider_down
from tests.payments.flow.conftest import (
    ProposedPayment,
    add_payment_method,
    add_payout_account,
    load_payment,
    propose_payment,
)

pytestmark = pytest.mark.db


@dataclass
class Paying:
    """A sent PO, the buyer's card, the supplier's payout account; the act tool registered."""

    world: World
    providers: FakeProviders
    po: IssuedPurchaseOrder
    method_id: UUID
    account_id: UUID

    @property
    def engine(self) -> AsyncEngine:
        return self.world.engine

    @property
    def tenant(self) -> UUID:
        return self.world.people.tenant

    async def propose(
        self,
        *,
        quote_ttl: timedelta = timedelta(hours=24),
        charge_mode: Literal["stripe", "simulated"] = "stripe",
        method_id: UUID | None = None,
        attempt: int = 0,
        card_rail: Literal["spt", "vic"] = "spt",
    ) -> ProposedPayment:
        """The buyer (the tenant's owner) asks to pay the PO: a pending approval."""
        return await propose_payment(
            self.engine,
            self.po,
            requested_by=self.world.people.owner,
            payment_method_id=method_id or self.method_id,
            payout_account_id=self.account_id,
            charge_mode=charge_mode,
            quote_ttl=quote_ttl,
            attempt=attempt,
            card_rail=card_rail,
        )

    async def charged(self) -> ProposedPayment:
        """A payment proposed, approved by an approver and charged by the worker."""
        proposed = await self.propose()
        await self.world.approve(proposed.approval_id)
        assert (await self.payment(proposed.payment_id)).state == "charged"
        return proposed

    async def payment(self, payment_id: UUID) -> PoPayment:
        return await load_payment(self.engine, self.tenant, payment_id)

    async def events(self, payment_id: UUID) -> list[AuditEvent]:
        async with tenant_session(self.engine, self.tenant) as session:
            return list(
                await session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.payload["payment_id"].astext == str(payment_id))
                    .order_by(AuditEvent.created_at, AuditEvent.id)
                )
            )

    async def actions(self, payment_id: UUID) -> list[str]:
        return [event.action for event in await self.events(payment_id)]


async def start_paying(world: World, providers: FakeProviders) -> Paying:
    register_payment_act_tools(
        world.registry,
        PaymentActDeps(db=world.engine, providers=providers.bundle(), jobs=world.jobs),
    )
    po = await sent_po(world)
    people = world.people
    method_id = await add_payment_method(world.engine, people.tenant, people.owner)
    account_id = await add_payout_account(world.engine, people.tenant, po.supplier_id, people.owner)
    return Paying(world, providers, po, method_id, account_id)


@pytest_asyncio.fixture
async def paying(world: World, providers: FakeProviders) -> Paying:
    return await start_paying(world, providers)


def advance_jobs(paying: Paying) -> list[str]:
    return [job.idempotency_key for job in paying.world.jobs.jobs if job.type == PAYMENT_ADVANCE]


async def refused(paying: Paying, payment_id: UUID) -> tuple[str, str | None]:
    """(state, failure_code), after checking nothing reached the card."""
    card = paying.providers.card
    assert card.charge_calls == 0 and not card.charges
    payment = await paying.payment(payment_id)
    assert payment.charged_at is None and payment.charge_reference is None
    return payment.state, payment.failure_code


# --- Charged -------------------------------------------------------------------------------


async def test_an_approved_payment_is_charged_once_and_wakes_its_chain_steps(
    paying: Paying,
) -> None:
    proposed = await paying.propose()
    await paying.world.approve(proposed.approval_id)

    payment = await paying.payment(proposed.payment_id)
    assert payment.state == "charged" and payment.charged_at is not None
    assert payment.charge_reference == f"pi_{proposed.payment_id.hex[:12]}"
    assert payment.card_authorization_ref == "alw_1"
    card = paying.providers.card
    assert list(card.charges) == [proposed.payment_id] and card.charge_calls == 1
    # The allowance is built from the approved args only (a retry repeats it exactly):
    # it expires with the quote.
    [(key, allowance)] = card.authorizations.items()
    assert key == f"payment:{proposed.payment_id}:authorize"
    assert allowance.expires_at == proposed.args.quote_expires_at
    approval = await load(paying.engine, paying.tenant, proposed.approval_id)
    assert approval.status == "executed"
    assert approval.decided_by == paying.world.people.approver
    # The chain steps are woken after the commit, keyed by the state they advance from.
    assert paying.world.jobs.jobs[-1] == payment_advance_envelope(
        paying.tenant, proposed.payment_id, "charged"
    )
    [event] = await paying.events(proposed.payment_id)
    assert event.action == PAYMENT_CHARGED
    assert event.actor_user_id == paying.world.people.approver
    assert event.payload["rfq_id"] == str(paying.po.rfq_id)
    assert event.payload["purchase_order_ref"] == paying.po.ref
    assert event.payload["requested_by"] == str(paying.world.people.owner)
    assert event.payload["charge_amount"] == proposed.args.charge_amount
    assert event.payload["simulated"] is False


async def test_four_eyes_the_requester_cannot_approve_their_own_payment(
    paying: Paying, migrator_engine: AsyncEngine
) -> None:
    # The app role cannot UPDATE tenants: the policy is set by the migrator.
    async with tenant_session(migrator_engine, paying.tenant) as session:
        await session.execute(
            update(Tenant).where(Tenant.id == paying.tenant).values(allow_self_approval=False)
        )
    proposed = await paying.propose()

    with pytest.raises(ApprovalForbidden):
        await decide_approval(
            paying.engine,
            buyer(paying.world.people),
            proposed.approval_id,
            "approve",
            jobs=FakeJobQueue(),
        )
    assert await refused(paying, proposed.payment_id) == ("pending_approval", None)

    await paying.world.approve(proposed.approval_id)  # an approver, not the requester
    assert (await paying.payment(proposed.payment_id)).state == "charged"


async def test_a_rejected_payment_is_not_charged_and_frees_the_order(paying: Paying) -> None:
    proposed = await paying.propose()

    await decide_approval(
        paying.engine,
        approver(paying.world.people),
        proposed.approval_id,
        "reject",
        jobs=paying.world.jobs,
        on_reject={**AWARD_REJECT_HOOKS, **PAYMENT_REJECT_HOOKS},
    )

    assert await refused(paying, proposed.payment_id) == ("rejected", None)
    assert not paying.providers.card.authorizations
    # No longer live: the order can be paid with a new request.
    again = await paying.propose(attempt=1)
    assert (await paying.payment(again.payment_id)).state == "pending_approval"


async def test_a_replayed_execution_of_a_charged_payment_only_wakes_its_next_step(
    paying: Paying,
) -> None:
    proposed = await paying.charged()
    paying.world.jobs.jobs.clear()
    [tool] = payment_tools(
        PaymentActDeps(
            db=paying.engine, providers=paying.providers.bundle(), jobs=paying.world.jobs
        )
    )
    run_id, _ = payment_proposal_ids(paying.po.id, 0)
    context = ToolContext(
        tenant_id=paying.tenant, user_id=paying.world.people.approver, run_id=run_id
    )

    result = await tool.invoke(context, proposed.args)

    assert result == PayPurchaseOrderResult(
        outcome="already_charged", payment_id=proposed.payment_id
    )
    assert paying.providers.card.charge_calls == 1
    # Its step's job may have been lost with the first run: woken again.
    assert advance_jobs(paying) == [f"payment:{proposed.payment_id}:charged"]


async def test_a_replayed_execution_of_a_completed_payment_does_nothing(paying: Paying) -> None:
    """Its escrow released every milestone: never charged again, and no step to wake."""
    proposed = await paying.charged()
    async with tenant_session(paying.engine, paying.tenant) as session:
        await session.execute(
            update(PoPayment)
            .where(PoPayment.id == proposed.payment_id)
            .values(state="funded", funded_at=func.now(), completed_at=func.now())
        )
    paying.world.jobs.jobs.clear()
    [tool] = payment_tools(
        PaymentActDeps(
            db=paying.engine, providers=paying.providers.bundle(), jobs=paying.world.jobs
        )
    )
    run_id, _ = payment_proposal_ids(paying.po.id, 0)
    context = ToolContext(
        tenant_id=paying.tenant, user_id=paying.world.people.approver, run_id=run_id
    )

    result = await tool.invoke(context, proposed.args)

    assert result == PayPurchaseOrderResult(
        outcome="already_charged", payment_id=proposed.payment_id
    )
    assert paying.providers.card.charge_calls == 1
    assert advance_jobs(paying) == []
    assert (await paying.payment(proposed.payment_id)).state == "funded"


async def test_a_payment_proposed_before_milestones_had_a_key_is_still_charged(
    paying: Paying, migrator_engine: AsyncEngine
) -> None:
    """Approvals waiting at deploy time carry milestones without `key` (and so do their
    rows): the executor still reproduces their hash and the act still finds its terms."""
    proposed = await propose_payment(
        paying.engine,
        paying.po,
        requested_by=paying.world.people.owner,
        payment_method_id=paying.method_id,
        payout_account_id=paying.account_id,
        milestones=[MilestoneInput(name="Delivery", bps=10_000)],
    )
    legacy: list[JsonValue] = [{"name": "Delivery", "bps": 10_000}]
    async with tenant_session(migrator_engine, paying.tenant) as session:
        approval = await session.get(Approval, proposed.approval_id)
        assert approval is not None
        stored: dict[str, JsonValue] = {**approval.args, "milestones": legacy}
        approval.args = stored
        approval.args_hash = args_hash(stored)
        await session.execute(
            update(PoPayment).where(PoPayment.id == proposed.payment_id).values(milestones=legacy)
        )

    await paying.world.approve(proposed.approval_id)

    payment = await paying.payment(proposed.payment_id)
    assert (payment.state, payment.milestones) == ("charged", legacy)
    assert paying.providers.card.charge_calls == 1
    approval = await load(paying.engine, paying.tenant, proposed.approval_id)
    assert approval.status == "executed"


async def test_duplicate_execution_jobs_charge_once(paying: Paying) -> None:
    proposed = await paying.propose()
    jobs = FakeJobQueue()
    await decide_approval(
        paying.engine, approver(paying.world.people), proposed.approval_id, "approve", jobs=jobs
    )
    [job] = jobs.jobs

    await asyncio.gather(paying.world.executor(job), paying.world.executor(job))

    assert paying.providers.card.charge_calls == 1
    assert list(paying.providers.card.charges) == [proposed.payment_id]
    assert (await paying.payment(proposed.payment_id)).state == "charged"
    assert advance_jobs(paying) == [f"payment:{proposed.payment_id}:charged"]


async def test_the_charge_is_made_the_way_the_approver_saw_it(paying: Paying) -> None:
    # This server simulates charges: a payment approved as a real charge is not made.
    paying.providers.card.simulated_mode = True
    real = await paying.propose()
    await paying.world.approve(real.approval_id)
    assert await refused(paying, real.payment_id) == ("failed", "charge_mode_changed")

    # One approved as a simulated charge is made, and marked as such.
    simulated = await paying.propose(charge_mode="simulated", attempt=1)
    await paying.world.approve(simulated.approval_id)
    payment = await paying.payment(simulated.payment_id)
    assert payment.state == "charged"
    assert payment.charge_reference == f"sim_{simulated.payment_id}"
    [event] = await paying.events(simulated.payment_id)
    assert event.payload["simulated"] is True


# --- Visa Intelligent Commerce: the passkey, then the agent's virtual card ---------------------


def visa(paying: Paying) -> None:
    card = paying.providers.card
    card.rail_mode, card.simulated_mode = "vic", True


async def test_a_visa_payment_approved_before_the_passkey_mints_nothing(paying: Paying) -> None:
    visa(paying)
    proposed = await paying.propose(charge_mode="simulated", card_rail="vic")
    await paying.world.approve(proposed.approval_id)

    assert await refused(paying, proposed.payment_id) == ("failed", "card_not_verified")
    payment = await paying.payment(proposed.payment_id)
    assert (payment.card_verified_at, payment.issued_card_ref) == (None, None)
    assert await paying.actions(proposed.payment_id) == [PAYMENT_FAILED]


async def test_a_visa_payment_approved_after_the_passkey_issues_the_agent_a_card(
    paying: Paying,
) -> None:
    visa(paying)
    paying.providers.card.verification_status = "verified"
    proposed = await paying.propose(charge_mode="simulated", card_rail="vic")
    await paying.world.approve(proposed.approval_id)

    payment = await paying.payment(proposed.payment_id)
    assert payment.state == "charged"
    assert payment.charge_reference == f"sim_{proposed.payment_id}"
    assert payment.card_verified_at is not None
    assert (
        payment.issued_card_ref,
        payment.issued_card_last4,
        payment.issued_card_exp_month,
        payment.issued_card_exp_year,
    ) == (f"cred_{proposed.payment_id.hex[:8]}", "4242", 12, 2030)
    [event] = await paying.events(proposed.payment_id)
    assert (event.action, event.payload["card_rail"], event.payload["simulated"]) == (
        PAYMENT_CHARGED,
        "vic",
        True,
    )


async def test_a_payment_is_minted_only_on_the_rail_it_was_approved_on(paying: Paying) -> None:
    # Approved for the Visa passkey, executed by a server on the Stripe rail: not charged.
    paying.providers.card.simulated_mode = True
    on_visa = await paying.propose(charge_mode="simulated", card_rail="vic")
    await paying.world.approve(on_visa.approval_id)
    assert await refused(paying, on_visa.payment_id) == ("failed", "charge_mode_changed")

    # And the reverse: approved without a passkey, never minted on the Visa rail.
    visa(paying)
    paying.providers.card.verification_status = "verified"
    on_stripe = await paying.propose(charge_mode="simulated", attempt=1)
    await paying.world.approve(on_stripe.approval_id)
    assert await refused(paying, on_stripe.payment_id) == ("failed", "charge_mode_changed")


# --- Not charged: what the approval assumed changed ------------------------------------------


async def test_an_expired_quote_is_not_charged(paying: Paying) -> None:
    proposed = await paying.propose(quote_ttl=timedelta(seconds=-1))

    await paying.world.approve(proposed.approval_id)

    assert await refused(paying, proposed.payment_id) == ("expired", "quote_expired")
    assert not paying.providers.card.authorizations
    assert (await load(paying.engine, paying.tenant, proposed.approval_id)).status == "executed"
    assert advance_jobs(paying) == []


async def test_an_order_changed_after_the_request_is_not_charged(paying: Paying) -> None:
    proposed = await paying.propose()
    async with tenant_session(paying.engine, paying.tenant) as session:
        await session.execute(
            update(IssuedPurchaseOrder)
            .where(IssuedPurchaseOrder.id == paying.po.id)
            .values(payment_terms="90 days net")
        )

    await paying.world.approve(proposed.approval_id)

    assert await refused(paying, proposed.payment_id) == ("failed", "purchase_order_changed")
    assert await paying.actions(proposed.payment_id) == [PAYMENT_FAILED]


async def test_a_disabled_payout_account_is_not_paid(paying: Paying) -> None:
    proposed = await paying.propose()
    async with tenant_session(paying.engine, paying.tenant) as session:
        await session.execute(
            update(SupplierPayoutAccount)
            .where(SupplierPayoutAccount.id == paying.account_id)
            .values(
                status="disabled",
                disabled_at=datetime.now(UTC),
                disabled_by=paying.world.people.owner,
            )
        )

    await paying.world.approve(proposed.approval_id)

    assert await refused(paying, proposed.payment_id) == ("failed", "payout_account_changed")


async def test_a_removed_card_is_not_charged(paying: Paying) -> None:
    proposed = await paying.propose()
    async with tenant_session(paying.engine, paying.tenant) as session:
        await session.execute(
            update(PaymentMethod)
            .where(PaymentMethod.id == paying.method_id)
            .values(status="removed", removed_at=datetime.now(UTC))
        )

    await paying.world.approve(proposed.approval_id)

    assert await refused(paying, proposed.payment_id) == ("failed", "payment_method_unavailable")


async def test_only_the_requesters_own_card_is_charged(paying: Paying) -> None:
    people = paying.world.people
    theirs = await add_payment_method(paying.engine, people.tenant, people.approver)
    proposed = await paying.propose(method_id=theirs)

    await paying.world.approve(proposed.approval_id)

    assert await refused(paying, proposed.payment_id) == ("failed", "payment_method_unavailable")


async def test_a_row_that_no_longer_holds_the_approved_terms_is_not_charged(
    paying: Paying,
) -> None:
    proposed = await paying.propose()
    async with tenant_session(paying.engine, paying.tenant) as session:
        await session.execute(
            update(PoPayment)
            .where(PoPayment.id == proposed.payment_id)
            .values(token_amount=Decimal(1))
        )

    await paying.world.approve(proposed.approval_id)

    assert await refused(paying, proposed.payment_id) == ("failed", "approval_mismatch")


# --- The card's answers -----------------------------------------------------------------------


async def test_a_declined_card_is_recorded_and_wakes_nothing(paying: Paying) -> None:
    paying.providers.card.charge_outcome = "declined"
    proposed = await paying.propose()

    await paying.world.approve(proposed.approval_id)

    payment = await paying.payment(proposed.payment_id)
    assert (payment.state, payment.failure_code) == ("declined", "card_declined")
    assert payment.charged_at is None and not paying.providers.card.charges
    assert advance_jobs(paying) == []
    assert await paying.actions(proposed.payment_id) == [PAYMENT_DECLINED]


async def test_a_card_without_the_charge_rail_is_declined_before_any_charge(
    paying: Paying,
) -> None:
    paying.providers.card.authorize_usable = False
    proposed = await paying.propose()

    await paying.world.approve(proposed.approval_id)

    assert await refused(paying, proposed.payment_id) == ("declined", "card_rail_unavailable")
    assert (await paying.payment(proposed.payment_id)).card_authorization_ref == "alw_1"


async def test_a_lost_card_credential_fails_only_after_the_grace_period(
    paying: Paying, migrator_engine: AsyncEngine
) -> None:
    paying.providers.card.charge_error = lost_credential()
    proposed = await paying.propose()

    # Stripe's search is eventually consistent: right after the approval it is retried.
    with pytest.raises(CardCredentialLost):
        await paying.world.approve(proposed.approval_id)
    assert (await paying.payment(proposed.payment_id)).state == "charging"
    assert (await load(paying.engine, paying.tenant, proposed.approval_id)).status == "approved"

    async with tenant_session(migrator_engine, paying.tenant) as session:
        await session.execute(
            update(Approval)
            .where(Approval.id == proposed.approval_id)
            .values(decided_at=datetime.now(UTC) - CREDENTIAL_GRACE - timedelta(minutes=1))
        )
    paying.providers.card.charge_error = lost_credential()
    await paying.world.executor(approval_execute_envelope(paying.tenant, proposed.approval_id))

    payment = await paying.payment(proposed.payment_id)
    assert (payment.state, payment.failure_code) == ("failed", "card_credential_lost")
    assert payment.charged_at is None and not paying.providers.card.charges
    assert paying.providers.card.charge_calls == 2
    assert await paying.actions(proposed.payment_id) == [PAYMENT_FAILED]
    # Nothing was charged: the order can be paid with a new request.
    again = await paying.propose(attempt=1)
    assert (await paying.payment(again.payment_id)).state == "pending_approval"


async def test_a_charge_of_another_amount_is_left_to_a_person(paying: Paying) -> None:
    # A replayed charge found one of another amount or currency: a charge exists.
    paying.providers.card.charge_error = ProviderError(
        "stripe", "find_payment_intent", CHARGE_MISMATCH
    )
    proposed = await paying.propose()

    await paying.world.approve(proposed.approval_id)

    payment = await paying.payment(proposed.payment_id)
    assert (payment.state, payment.failure_code) == ("failed", CHARGE_MISMATCH)
    assert payment.charged_at is not None and payment.charge_reference is None
    assert advance_jobs(paying) == []
    # It stays the order's live payment: the order is not charged again.
    with pytest.raises(IntegrityError):
        await paying.propose(attempt=1)


@pytest.mark.parametrize(
    "error",
    [
        ProviderError("stripe", "create_payment_intent", "invalid_request", status=400),
        PaymentRefused("a card charge is at least 0.50 USD"),
    ],
)
async def test_a_charge_refused_outright_fails_without_a_charge(
    paying: Paying, error: Exception
) -> None:
    paying.providers.card.charge_error = error
    proposed = await paying.propose()

    await paying.world.approve(proposed.approval_id)

    payment = await paying.payment(proposed.payment_id)
    assert (payment.state, payment.failure_code) == ("failed", "card_charge_failed")
    assert payment.charged_at is None


def test_the_charge_mismatch_code_is_the_gateways() -> None:
    from bido_back.payments.card import CHARGE_MISMATCH as GATEWAY_CODE

    assert CHARGE_MISMATCH == GATEWAY_CODE


async def test_a_provider_outage_is_retried_and_charges_once(paying: Paying) -> None:
    paying.providers.card.charge_error = provider_down()
    proposed = await paying.propose()

    with pytest.raises(ProviderError):
        await paying.world.approve(proposed.approval_id)

    # The approval stays approved and the job is retried; the claim was committed.
    assert (await load(paying.engine, paying.tenant, proposed.approval_id)).status == "approved"
    payment = await paying.payment(proposed.payment_id)
    assert (payment.state, payment.card_authorization_ref) == ("charging", "alw_1")

    await paying.world.executor(approval_execute_envelope(paying.tenant, proposed.approval_id))

    assert (await paying.payment(proposed.payment_id)).state == "charged"
    card = paying.providers.card
    assert len(card.charges) == 1 and card.charge_calls == 2
    assert len(card.authorizations) == 1  # the same allowance


async def test_a_charge_still_processing_is_retried_until_it_settles(paying: Paying) -> None:
    paying.providers.card.charge_outcome = "pending"
    proposed = await paying.propose()

    with pytest.raises(ChargePending):
        await paying.world.approve(proposed.approval_id)
    assert (await paying.payment(proposed.payment_id)).state == "charging"

    paying.providers.card.charge_outcome = "succeeded"
    await paying.world.executor(approval_execute_envelope(paying.tenant, proposed.approval_id))

    assert (await paying.payment(proposed.payment_id)).state == "charged"
    assert len(paying.providers.card.charges) == 1


# --- The order is cancelled ---------------------------------------------------------------------


async def cancel(paying: Paying) -> None:
    await lifecycle.cancel_purchase_order(
        paying.engine,
        buyer(paying.world.people),
        paying.po.id,
        CancelRequest(reason="The project was stopped"),
    )


async def test_cancelling_the_order_expires_its_payment_waiting_for_approval(
    paying: Paying,
) -> None:
    proposed = await paying.propose()

    await cancel(paying)

    assert await refused(paying, proposed.payment_id) == ("expired", "purchase_order_cancelled")
    assert (await load(paying.engine, paying.tenant, proposed.approval_id)).status == "expired"
    assert "approval.expired" in await audit_actions(
        paying.engine, paying.tenant, proposed.approval_id
    )


async def test_an_approved_payment_of_a_cancelled_order_is_skipped(paying: Paying) -> None:
    proposed = await paying.propose()
    jobs = FakeJobQueue()
    await decide_approval(
        paying.engine, approver(paying.world.people), proposed.approval_id, "approve", jobs=jobs
    )

    await cancel(paying)  # before the worker ran it
    await paying.world.executor(jobs.jobs[0])

    assert await refused(paying, proposed.payment_id) == ("expired", "purchase_order_cancelled")
    assert (await load(paying.engine, paying.tenant, proposed.approval_id)).status == "executed"


async def test_cancelling_leaves_a_charged_payment_alone(paying: Paying) -> None:
    proposed = await paying.charged()

    await cancel(paying)

    # Money moved: refunding it is a separate step, not a side effect of the cancel.
    assert (await paying.payment(proposed.payment_id)).state == "charged"
