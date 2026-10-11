"""The payment service against Postgres: cards, payout accounts, payment requests, views.

Fake providers (`tests.payments.fakes`) and the award World: one sent PO to Rhein,
EUR 1650.00. A USD reference rate of 1.10 on a day far in the past (a different day
per run, so runs in parallel never delete each other's rate) quotes it USD 1815.00.
"""

import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import delete, func, inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.approvals import decide_approval
from bido_back.auth import Principal
from bido_back.awards import lifecycle
from bido_back.awards import service as award_service
from bido_back.awards.orders import po_document
from bido_back.awards.render import content_hash
from bido_back.awards.schemas import CancelRequest
from bido_back.db import tenant_session
from bido_back.db.models import (
    Approval,
    AuditEvent,
    FxRate,
    Invite,
    IssuedPurchaseOrder,
    PaymentChainTransaction,
    PaymentMethod,
    PoPayment,
    SupplierPayoutAccount,
)
from bido_back.fx import DailyRates, upsert_rates
from bido_back.payments import service
from bido_back.payments.chain import hashes
from bido_back.payments.errors import (
    PaymentForbidden,
    PaymentInvalidState,
    PaymentNotFound,
    PaymentRefused,
    ProviderError,
)
from bido_back.payments.ports import (
    BankAccountDetails,
    CardAuthorization,
    CardConsumer,
    CardDisplayData,
    EnrolledCard,
)
from bido_back.payments.proposals import (
    ESCROW_CREATED,
    ESCROW_FUNDED,
    PAY_PURCHASE_ORDER,
    PAYMENT_CARD_VERIFIED,
    PAYMENT_CHARGED,
    PAYMENT_DECLINED,
    PAYMENT_FAILED,
    PAYMENT_MINTED,
    PAYMENT_REQUESTED,
    payment_proposal_ids,
)
from bido_back.payments.schemas import (
    CreatePayoutAccountRequest,
    EnrollPaymentMethodRequest,
    MilestoneInput,
    PaymentRequested,
    PayPurchaseOrderArgs,
    PayPurchaseOrderRequest,
)
from bido_back.payments.views import (
    CardSetupView,
    ChainSetupView,
    PaymentCardVerificationView,
    PaymentMethodDisplayView,
    PaymentMethodView,
    PurchaseOrderPaymentSummary,
)
from bido_back.rfqs import service as rfq_service
from bido_back.rfqs.activity import rfq_activity
from bido_back.rfqs.audit import rfq_event
from bido_back.settings import Settings
from tests.approvals.conftest import FakeJobQueue, People
from tests.awards.test_awards_db import World, approver, buyer
from tests.awards.test_lifecycle import send, sent_po, two_sent_pos
from tests.payments.fakes import CARD_ART, FakeCardGateway, FakeProviders, address
from tests.payments.flow.conftest import (
    USD_PER_EUR,
    add_payment_method,
    add_payout_account,
    load_payment,
    propose_payment,
)

pytestmark = pytest.mark.db

RATE_DAY = date(2003, 1, 6) + timedelta(days=secrets.randbelow(3000))
NO_RATE_DAY = date(1990, 1, 2)
MINT = address("stablecoin")
SETTINGS = Settings.model_validate(
    {
        "environment": "test",
        "database_url": "postgresql+asyncpg://app@localhost:5432/unused",
        "solana_stablecoin_mint": MINT,
    }
)
ACCOUNT = CreatePayoutAccountRequest(
    beneficiary_name="Shenzhen Precision Parts Co., Ltd.",
    bank_name="Bank of China",
    account_number="6222 0212-3456 7890",
    country="CN",
    currency="CNY",
    swift_bic="BKCHCNBJ45A",
    bank_code="104584000003",
    beneficiary_name_local="深圳精密零件有限公司",
    bank_name_local="中国银行深圳分行",
)
ACCOUNT_NUMBER = "6222021234567890"
ART_VIEW = PaymentMethodDisplayView(
    art_url=CARD_ART.art_url,
    background_color=CARD_ART.background_color,
    foreground_color=CARD_ART.foreground_color,
    description=CARD_ART.description,
    issuer_name=CARD_ART.issuer_name,
)
ART_JSON = ART_VIEW.model_dump()


@pytest_asyncio.fixture
async def usd_rate(app_engine: AsyncEngine, migrator_engine: AsyncEngine) -> AsyncIterator[date]:
    """USD 1.10 per EUR, published on `RATE_DAY`."""
    await upsert_rates(app_engine, DailyRates(RATE_DAY, {"USD": USD_PER_EUR}))
    yield RATE_DAY
    async with migrator_engine.begin() as conn:
        await conn.execute(delete(FxRate).where(FxRate.rate_date == RATE_DAY))


def member(people: People) -> Principal:
    return Principal(tenant_id=people.tenant, user_id=people.member, role="member")


def outsider(people: People) -> Principal:
    return Principal(tenant_id=people.other_tenant, user_id=people.other_approver, role="approver")


@dataclass
class RecordingCard(FakeCardGateway):
    consumers: list[CardConsumer] = field(default_factory=list[CardConsumer])
    keys: list[str] = field(default_factory=list[str])

    async def enroll(
        self, *, token_id: str, consumer: CardConsumer, idempotency_key: str
    ) -> EnrolledCard:
        self.consumers.append(consumer)
        self.keys.append(idempotency_key)
        return await super().enroll(
            token_id=token_id, consumer=consumer, idempotency_key=idempotency_key
        )


@dataclass
class FailingAuthorizationCard(FakeCardGateway):
    """Basis Theory is down when the payment is requested."""

    fail: bool = True

    async def authorize(
        self,
        *,
        payment_method_ref: str,
        amount: Decimal,
        currency: str,
        description: str,
        expires_at: datetime,
        idempotency_key: str,
    ) -> CardAuthorization:
        if self.fail:
            raise ProviderError("basis_theory", "create_allowance", "unavailable", status=503)
        return await super().authorize(
            payment_method_ref=payment_method_ref,
            amount=amount,
            currency=currency,
            description=description,
            expires_at=expires_at,
            idempotency_key=idempotency_key,
        )


def card_token(token_id: str) -> EnrollPaymentMethodRequest:
    return EnrollPaymentMethodRequest.model_validate({"token_id": token_id})


async def enroll(
    world: World, providers: FakeProviders, token_id: str, principal: Principal | None = None
) -> PaymentMethodView:
    return await service.enroll_payment_method(
        world.engine,
        principal or buyer(world.people),
        card_token(token_id),
        providers.card,
        consumer_country="US",
    )


async def supplier_of(world: World, name: str) -> UUID:
    async with tenant_session(world.engine, world.people.tenant) as session:
        supplier_id = await session.scalar(
            select(Invite.supplier_id).where(Invite.id == world.invites[name])
        )
    assert supplier_id is not None
    return supplier_id


@dataclass(frozen=True)
class Payable:
    po: IssuedPurchaseOrder
    card: UUID
    account: UUID


async def payable(world: World, po: IssuedPurchaseOrder | None = None) -> Payable:
    """A sent PO, a card the buyer saved and an account of the PO's supplier."""
    order = po or await sent_po(world)
    people = world.people
    return Payable(
        po=order,
        card=await add_payment_method(world.engine, people.tenant, people.owner),
        account=await add_payout_account(
            world.engine, people.tenant, order.supplier_id, people.owner
        ),
    )


async def request(
    world: World,
    providers: FakeProviders,
    target: Payable,
    *,
    principal: Principal | None = None,
    card: UUID | None = None,
    account: UUID | None = None,
    milestones: list[MilestoneInput] | None = None,
    today: date = RATE_DAY,
) -> PaymentRequested:
    return await service.request_po_payment(
        world.engine,
        principal or buyer(world.people),
        target.po.id,
        PayPurchaseOrderRequest(
            payment_method_id=card or target.card,
            payout_account_id=account or target.account,
            milestones=milestones,
        ),
        providers.bundle(),
        settings=SETTINGS,
        today=today,
    )


async def payment_approvals(world: World) -> list[Approval]:
    async with tenant_session(world.engine, world.people.tenant) as session:
        return list(
            await session.scalars(
                select(Approval)
                .where(Approval.tool == PAY_PURCHASE_ORDER)
                .order_by(Approval.created_at)
            )
        )


async def actions(world: World, *names: str) -> list[AuditEvent]:
    async with tenant_session(world.engine, world.people.tenant) as session:
        return list(
            await session.scalars(
                select(AuditEvent)
                .where(AuditEvent.action.in_(names))
                .order_by(AuditEvent.created_at, AuditEvent.id)
            )
        )


async def set_payment(world: World, payment_id: UUID, **values: object) -> None:
    async with tenant_session(world.engine, world.people.tenant) as session:
        await session.execute(update(PoPayment).where(PoPayment.id == payment_id).values(**values))


async def executed(migrator_engine: AsyncEngine, world: World, approval_id: UUID) -> None:
    """Mark a payment approval executed (as the act tool would after it ran)."""
    async with tenant_session(migrator_engine, world.people.tenant) as session:
        await session.execute(
            update(Approval)
            .where(Approval.id == approval_id)
            .values(
                status="executed",
                decided_by=world.people.approver,
                decided_at=func.now(),
                executed_at=func.now(),
                self_approved=False,
            )
        )


# --- Configuration ---------------------------------------------------------------------------


def test_payments_are_off_until_every_setting_is_there() -> None:
    view = service.payments_config_view(SETTINGS)
    assert (view.enabled, view.card, view.chain) == (False, None, None)


def test_the_config_view_shows_public_values_only() -> None:
    hidden = {
        "basis_theory_api_key": "bt-private-value",
        "privy_app_secret": "privy-app-value",
        "privy_authorization_key": "privy-authorization-value",
        "solana_rpc_url": "https://rpc.example.test/private-path",
        "solana_mint_authority_key": "mint-authority-value",
        "solana_operator_key": "operator-value",
        "solana_relayer_key": "relayer-value",
        "payout_details_encryption_key": "payout-details-value",
    }
    settings = Settings.model_validate(
        {
            "environment": "test",
            "database_url": "postgresql+asyncpg://app@localhost:5432/unused",
            "basis_theory_public_key": "public-elements-key",
            "payments_card_charge": "simulated",
            "payments_card_rail": "vic",
            "privy_app_id": "privy-app",
            "privy_wallet_owner_id": "quorum-id",
            "solana_stablecoin_mint": MINT,
            "solana_payout_owner": address("payout"),
            "solana_gate_attester": address("attester"),
            **hidden,
        }
    )
    view = service.payments_config_view(settings)
    assert view.enabled
    assert view.card == CardSetupView(
        provider="basis_theory",
        environment="test",
        public_key="public-elements-key",
        simulated=True,
        rail="vic",
        agentic_api_url="https://api.test.basistheory.com/agentic",
    )
    assert view.chain == ChainSetupView(
        cluster="devnet",
        escrow_program_id=settings.solana_escrow_program_id,
        stablecoin_mint=MINT,
        stablecoin_decimals=6,
    )
    dumped = view.model_dump_json()
    assert not [value for value in hidden.values() if value in dumped]


# --- Cards -----------------------------------------------------------------------------------


async def test_a_member_saves_a_card_and_the_company_sees_it_masked(
    world: World, providers: FakeProviders
) -> None:
    people = world.people
    providers.card = card = RecordingCard()
    saved = await enroll(world, providers, "tok_a", member(people))
    assert (saved.brand, saved.last4, saved.exp_month, saved.status, saved.created_by) == (
        "visa",
        "4242",
        12,
        "active",
        people.member,
    )
    assert card.consumers == [
        CardConsumer(id=people.member, email="member@example.test", country_code="US")
    ]
    assert card.keys == [f"payment-method:{people.tenant}:tok_a"]
    assert [m.id for m in await service.list_payment_methods(world.engine, buyer(people))] == [
        saved.id
    ]
    assert saved.display == ART_VIEW
    async with tenant_session(world.engine, people.tenant) as session:
        row = await session.get(PaymentMethod, saved.id)
    assert row is not None and (row.payment_method_ref, row.fingerprint) == ("pm_tok_a", "fp-tok_a")
    assert row.display == ART_JSON
    [event] = await actions(world, "payment_method.added")
    assert event.payload == {"payment_method_id": str(saved.id), "status": "active"}
    assert event.actor_user_id == people.member

    # Another company sees none of it.
    assert await service.list_payment_methods(world.engine, outsider(people)) == []


async def test_a_card_without_branding_is_saved_with_a_null_display(
    world: World, providers: FakeProviders
) -> None:
    providers.card.enroll_display = None
    saved = await enroll(world, providers, "tok_plain")
    assert saved.display is None
    async with tenant_session(world.engine, world.people.tenant) as session:
        # SQL NULL, not JSON null: the backfill looks for `display IS NULL`.
        found = await session.scalar(
            select(PaymentMethod.id).where(
                PaymentMethod.id == saved.id, PaymentMethod.display.is_(None)
            )
        )
    assert found == saved.id


async def test_a_stored_display_is_an_object_and_read_back_cleaned(
    world: World, providers: FakeProviders
) -> None:
    people = world.people
    card_id = await add_payment_method(world.engine, people.tenant, people.owner)
    with pytest.raises(IntegrityError, match="payment_methods_display_check"):
        async with tenant_session(world.engine, people.tenant) as session:
            await session.execute(
                update(PaymentMethod)
                .where(PaymentMethod.id == card_id)
                .values(display=["https://cdn.example.test/a.png"])
            )
    async with tenant_session(world.engine, people.tenant) as session:
        await session.execute(
            update(PaymentMethod)
            .where(PaymentMethod.id == card_id)
            .values(display={"art_url": "http://cdn.example.test/a.png", "issuer_name": "Bank"})
        )
    [view] = await service.list_payment_methods(world.engine, buyer(people))
    assert view.display == PaymentMethodDisplayView(
        art_url=None,
        background_color=None,
        foreground_color=None,
        description=None,
        issuer_name="Bank",
    )


async def test_the_backfill_fills_only_saved_cards_without_a_display(
    world: World, providers: FakeProviders
) -> None:
    people = world.people
    tenant = people.tenant
    card = providers.card
    providers.card.enroll_display = None
    plain = await enroll(world, providers, "tok_plain")
    nothing = await enroll(world, providers, "tok_nothing")
    failing = await enroll(world, providers, "tok_failing")
    removed = await enroll(world, providers, "tok_removed")
    await service.remove_payment_method(world.engine, buyer(people), removed.id, card)
    providers.card.enroll_display = CARD_ART
    branded = await enroll(world, providers, "tok_branded")
    others = await add_payment_method(world.engine, people.other_tenant, people.other_approver)
    newer = CardDisplayData(None, "#000000", "#FFFFFF", None, "Other Bank")
    card.displays = {
        "pm_tok_plain": CARD_ART,
        "pm_tok_failing": ProviderError("basis_theory", "get_payment_method", "http_503"),
        "pm_tok_removed": CARD_ART,
        "pm_tok_branded": newer,
    }

    dry = await service.backfill_card_display(world.engine, tenant, card, apply=False)
    assert dry == service.DisplayBackfill(missing=3, filled=1, empty=1, failed=1)
    assert sorted(card.display_calls) == ["pm_tok_failing", "pm_tok_nothing", "pm_tok_plain"]
    assert {
        m.id: m.display for m in await service.list_payment_methods(world.engine, buyer(people))
    } == {
        plain.id: None,
        nothing.id: None,
        failing.id: None,
        branded.id: ART_VIEW,
    }

    done = await service.backfill_card_display(world.engine, tenant, card, apply=True)
    assert done == dry
    shown = {
        m.id: m.display for m in await service.list_payment_methods(world.engine, buyer(people))
    }
    assert shown == {plain.id: ART_VIEW, nothing.id: None, failing.id: None, branded.id: ART_VIEW}
    async with tenant_session(world.engine, tenant) as session:
        gone = await session.get(PaymentMethod, removed.id)
    assert gone is not None and gone.display is None
    async with tenant_session(world.engine, people.other_tenant) as session:
        other = await session.get(PaymentMethod, others)
    assert other is not None and other.display is None

    # Re-running asks only for what is still missing.
    card.display_calls.clear()
    again = await service.backfill_card_display(world.engine, tenant, card, apply=True)
    assert again == service.DisplayBackfill(missing=2, filled=0, empty=1, failed=1)
    assert sorted(card.display_calls) == ["pm_tok_failing", "pm_tok_nothing"]


async def test_a_card_still_provisioning_is_saved_as_pending(
    world: World, providers: FakeProviders
) -> None:
    providers.card.enroll_status = "pending"
    saved = await enroll(world, providers, "tok_p")
    assert saved.status == "pending"


async def test_a_card_bido_cannot_charge_is_refused_and_dropped_at_the_provider(
    world: World, providers: FakeProviders
) -> None:
    providers.card.enroll_status = "failed"
    with pytest.raises(PaymentRefused, match="cannot be charged"):
        await enroll(world, providers, "tok_b")
    assert providers.card.removed == ["pm_tok_b"]
    assert await service.list_payment_methods(world.engine, buyer(world.people)) == []


async def test_the_same_card_twice_is_refused_and_only_the_new_enrollment_dropped(
    world: World, providers: FakeProviders
) -> None:
    people = world.people
    # The card was saved before from another token: same fingerprint, other enrollment.
    async with tenant_session(world.engine, people.tenant) as session:
        session.add(
            PaymentMethod(
                tenant_id=people.tenant,
                created_by=people.owner,
                payment_method_ref="pm_first",
                fingerprint="fp-tok_dup",
                status="active",
            )
        )
    with pytest.raises(PaymentInvalidState, match="already saved"):
        await enroll(world, providers, "tok_dup")
    assert providers.card.removed == ["pm_tok_dup"]

    # A replayed request enrolls the same payment method again: the saved one is kept.
    await enroll(world, providers, "tok_a")
    with pytest.raises(PaymentInvalidState, match="already saved"):
        await enroll(world, providers, "tok_a")
    assert providers.card.removed == ["pm_tok_dup"]
    assert len(await service.list_payment_methods(world.engine, buyer(people))) == 2


async def test_who_may_remove_a_card(world: World, providers: FakeProviders) -> None:
    people = world.people
    owners = await enroll(world, providers, "tok_o", buyer(people))
    members = await enroll(world, providers, "tok_m", member(people))
    with pytest.raises(PaymentForbidden):
        await service.remove_payment_method(world.engine, member(people), owners.id, providers.card)
    # An approver may remove anyone's card; whoever saved one may remove it.
    await service.remove_payment_method(world.engine, approver(people), members.id, providers.card)
    await service.remove_payment_method(world.engine, buyer(people), owners.id, providers.card)
    assert providers.card.removed == ["pm_tok_m", "pm_tok_o"]
    assert await service.list_payment_methods(world.engine, buyer(people)) == []
    with pytest.raises(PaymentNotFound):
        await service.remove_payment_method(world.engine, buyer(people), owners.id, providers.card)
    with pytest.raises(PaymentNotFound):
        await service.remove_payment_method(world.engine, outsider(people), uuid4(), providers.card)
    async with tenant_session(world.engine, people.tenant) as session:
        row = await session.get(PaymentMethod, owners.id)
    assert row is not None and row.status == "removed" and row.removed_at is not None
    removed = await actions(world, "payment_method.removed")
    assert [e.payload["payment_method_id"] for e in removed] == [str(members.id), str(owners.id)]


async def test_a_card_a_payment_may_still_charge_cannot_be_removed(
    world: World, providers: FakeProviders
) -> None:
    target = await payable(world)
    await propose_payment(
        world.engine,
        target.po,
        requested_by=world.people.owner,
        payment_method_id=target.card,
        payout_account_id=target.account,
    )
    with pytest.raises(PaymentInvalidState, match="waiting for approval or being charged"):
        await service.remove_payment_method(
            world.engine, buyer(world.people), target.card, providers.card
        )
    assert providers.card.removed == []


# --- Payout accounts -------------------------------------------------------------------------


async def test_only_an_approver_or_the_owner_adds_a_payout_account_and_bido_keeps_it_masked(
    world: World, providers: FakeProviders
) -> None:
    people = world.people
    rhein = await supplier_of(world, "Rhein")
    sealer = providers.bank_details
    with pytest.raises(PaymentForbidden):
        await service.create_payout_account(world.engine, member(people), rhein, ACCOUNT, sealer)
    assert sealer.sealed == []

    added = await service.create_payout_account(
        world.engine, approver(people), rhein, ACCOUNT, sealer
    )
    assert (
        added.supplier_id,
        added.method,
        added.country,
        added.currency,
        added.beneficiary_display,
        added.bank_name,
        added.swift_bic,
        added.account_last4,
        added.status,
        added.created_by,
        added.disabled_at,
    ) == (
        rhein,
        "bank_transfer",
        "CN",
        "CNY",
        "Shen…",
        "Bank of China",
        "BKCHCNBJ45A",
        "7890",
        "active",
        people.approver,
        None,
    )
    # The whole account was sealed, the number without its spaces and hyphens.
    details = BankAccountDetails(
        beneficiary_name="Shenzhen Precision Parts Co., Ltd.",
        bank_name="Bank of China",
        account_number=ACCOUNT_NUMBER,
        country="CN",
        currency="CNY",
        swift_bic="BKCHCNBJ45A",
        bank_code="104584000003",
        bank_address=None,
        beneficiary_address=None,
        beneficiary_type="business",
        beneficiary_name_local="深圳精密零件有限公司",
        bank_name_local="中国银行深圳分行",
        national_id=None,
    )
    assert sealer.sealed == [details]
    fingerprint = hashes.bank_fingerprint("CN", "104584000003", ACCOUNT_NUMBER)

    # Bido keeps them sealed, the fingerprint and the masked view: no column holds the
    # account in clear, and the sealed details open only for this supplier.
    async with tenant_session(world.engine, people.tenant) as session:
        row = await session.get(SupplierPayoutAccount, added.id)
    assert row is not None
    assert (row.bank_fingerprint, row.beneficiary_ref) == (fingerprint, None)
    opened = await sealer.open(row.details_ciphertext, tenant_id=people.tenant, supplier_id=rhein)
    assert opened == details
    with pytest.raises(ValueError, match="another supplier"):
        await sealer.open(row.details_ciphertext, tenant_id=people.tenant, supplier_id=uuid4())
    stored = [str(getattr(row, attr.key)) for attr in inspect(SupplierPayoutAccount).column_attrs]
    for secret in (
        ACCOUNT_NUMBER,
        ACCOUNT.account_number,
        ACCOUNT.beneficiary_name,
        "Precision",
        "精密",
    ):
        assert not [value for value in stored if secret in value]
    [event] = await actions(world, "supplier.payout_account_added")
    assert event.payload == {"supplier_id": str(rhein), "payout_account_id": str(added.id)}

    # Another company cannot add one to this supplier: it does not exist there.
    with pytest.raises(PaymentNotFound):
        await service.create_payout_account(world.engine, outsider(people), rhein, ACCOUNT, sealer)


async def test_the_same_account_twice_for_one_supplier_is_refused_before_sealing(
    world: World, providers: FakeProviders
) -> None:
    people = world.people
    rhein, lyon = await supplier_of(world, "Rhein"), await supplier_of(world, "Lyon")
    await service.create_payout_account(
        world.engine, buyer(people), rhein, ACCOUNT, providers.bank_details
    )
    respaced = ACCOUNT.model_copy(update={"account_number": ACCOUNT_NUMBER})
    with pytest.raises(PaymentInvalidState, match="already has this payout account"):
        await service.create_payout_account(
            world.engine, buyer(people), rhein, respaced, providers.bank_details
        )
    assert len(providers.bank_details.sealed) == 1
    # Another supplier may be paid to the same account.
    await service.create_payout_account(
        world.engine, buyer(people), lyon, ACCOUNT, providers.bank_details
    )
    assert len(providers.bank_details.sealed) == 2


async def test_a_corrected_account_is_disabled_and_added_again(
    world: World, providers: FakeProviders
) -> None:
    people = world.people
    rhein, lyon = await supplier_of(world, "Rhein"), await supplier_of(world, "Lyon")
    sealer = providers.bank_details
    typo = ACCOUNT.model_copy(update={"beneficiary_name": "Shenzen Precision Parts Co., Ltd."})
    first = await service.create_payout_account(world.engine, buyer(people), rhein, typo, sealer)

    with pytest.raises(PaymentForbidden):
        await service.disable_payout_account(world.engine, member(people), rhein, first.id)
    with pytest.raises(PaymentNotFound):
        await service.disable_payout_account(world.engine, approver(people), lyon, first.id)
    disabled = await service.disable_payout_account(world.engine, approver(people), rhein, first.id)
    assert disabled.status == "disabled" and disabled.disabled_at is not None
    # Disabling again changes nothing.
    again = await service.disable_payout_account(world.engine, approver(people), rhein, first.id)
    assert again.disabled_at == disabled.disabled_at

    fixed = await service.create_payout_account(world.engine, buyer(people), rhein, ACCOUNT, sealer)
    # Same account, corrected details: sealed anew, with the right name.
    assert [d.beneficiary_name for d in sealer.sealed] == [
        typo.beneficiary_name,
        ACCOUNT.beneficiary_name,
    ]
    listed = await service.list_payout_accounts(world.engine, member(people), rhein)
    assert [(a.id, a.status) for a in listed] == [(fixed.id, "active"), (first.id, "disabled")]
    assert [e.action for e in await actions(world, "supplier.payout_account_disabled")] == [
        "supplier.payout_account_disabled"
    ]
    with pytest.raises(PaymentNotFound):
        await service.list_payout_accounts(world.engine, buyer(people), uuid4())
    with pytest.raises(PaymentNotFound):
        await service.list_payout_accounts(world.engine, outsider(people), rhein)


# --- Requesting a payment ---------------------------------------------------------------------


async def test_a_payment_request_freezes_the_quote_and_waits_for_a_person(
    world: World, providers: FakeProviders, usd_rate: date
) -> None:
    people = world.people
    target = await payable(world)
    before = datetime.now(UTC)
    requested = await request(world, providers, target)
    payment = requested.payment

    async with tenant_session(world.engine, people.tenant) as session:
        approval = await session.get(Approval, requested.approval_id)
        row = await session.get(PoPayment, payment.id)
        po = await session.get(IssuedPurchaseOrder, target.po.id)
        assert po is not None
        doc_hash = content_hash(await po_document(session, po))
    assert approval is not None and row is not None

    run_id, call_id = payment_proposal_ids(target.po.id, 0)
    assert (
        approval.status,
        approval.tool,
        approval.run_id,
        approval.tool_call_id,
        approval.session_id,
        approval.requested_by,
    ) == ("pending", PAY_PURCHASE_ORDER, str(run_id), call_id, f"rfq:{world.rfq.id}", people.owner)
    args = PayPurchaseOrderArgs.model_validate(approval.args)
    assert args.payment_id == row.id == payment.id
    assert (
        args.purchase_order_id,
        args.purchase_order_ref,
        args.rfq_id,
        args.invite_id,
        args.supplier_id,
        args.requested_by,
    ) == (po.id, "PO-1", world.rfq.id, po.invite_id, po.supplier_id, people.owner)
    assert (
        args.po_currency,
        args.po_total,
        args.charge_currency,
        args.charge_amount,
        Decimal(args.fx_rate or "0"),
        args.fx_rate_date,
        args.token_amount,
    ) == ("EUR", "1650.00", "USD", "1815.00", Decimal("1.1"), usd_rate, "1815000000")
    assert (args.payment_method_id, args.card_brand, args.card_last4) == (
        target.card,
        "visa",
        "4242",
    )
    assert (args.payout_account_id, args.payout_currency, args.payout_account_last4) == (
        target.account,
        "CNY",
        "4567",
    )
    assert args.content_hash == doc_hash
    assert hashes.is_chain_po_id(args.chain_po_id) and args.chain_po_id.startswith("PO-1-")
    # Bido's tooling plan by default: each milestone released through its own gate.
    assert [(m.key, m.name, m.bps) for m in args.milestones] == [
        ("tool_start", "Tool start", 3_000),
        ("t1", "T1", 3_000),
        ("ppap", "PPAP", 4_000),
    ]
    assert args.charge_mode == "stripe"
    assert args.quote_expires_at.microsecond == 0
    ttl = args.quote_expires_at - before
    assert timedelta(hours=23, minutes=59) < ttl <= timedelta(hours=24)

    # The row holds exactly the approved terms.
    assert (
        row.state,
        row.approval_id,
        row.requested_by,
        row.payment_method_id,
        row.payout_account_id,
    ) == ("pending_approval", approval.id, people.owner, target.card, target.account)
    assert (
        row.po_currency,
        row.po_total,
        row.charge_currency,
        row.charge_amount,
        row.fx_rate,
        row.fx_rate_date,
        row.quote_expires_at,
    ) == (
        "EUR",
        Decimal("1650.00"),
        "USD",
        Decimal("1815.00"),
        Decimal("1.1"),
        usd_rate,
        args.quote_expires_at,
    )
    assert (row.token_amount, row.chain_po_id, row.po_hash, row.milestones, row.charge_mode) == (
        Decimal(1_815_000_000),
        args.chain_po_id,
        doc_hash,
        [
            {"name": "Tool start", "bps": 3_000, "key": "tool_start"},
            {"name": "T1", "bps": 3_000, "key": "t1"},
            {"name": "PPAP", "bps": 4_000, "key": "ppap"},
        ],
        "stripe",
    )
    assert (row.charged_at, row.wallet_id, row.escrow_address, row.failure_code) == (
        None,
        None,
        None,
        None,
    )

    assert (payment.state, payment.purchase_order_ref, payment.approval_id) == (
        "pending_approval",
        "PO-1",
        approval.id,
    )
    assert (payment.charge_amount, payment.fx_rate, payment.simulated) == (
        Decimal("1815.00"),
        Decimal("1.1"),
        False,
    )
    assert (payment.token.mint, payment.token.decimals, payment.token.amount) == (
        MINT,
        6,
        Decimal(1815),
    )
    assert (payment.card.last4, payment.payout.account_last4, payment.payout.currency) == (
        "4242",
        "4567",
        "CNY",
    )
    assert (payment.chain.cluster, payment.chain.chain_po_id, payment.chain.transactions) == (
        "devnet",
        args.chain_po_id,
        [],
    )
    assert [(m.key, m.name, m.bps) for m in payment.milestones] == [
        ("tool_start", "Tool start", 3_000),
        ("t1", "T1", 3_000),
        ("ppap", "PPAP", 4_000),
    ]
    assert payment.needs_attention is False
    assert payment.completed_at is None
    [event] = await actions(world, PAYMENT_REQUESTED)
    assert event.actor_user_id == people.owner
    assert {k: event.payload[k] for k in ("rfq_id", "payment_id", "approval_id")} == {
        "rfq_id": str(world.rfq.id),
        "payment_id": str(payment.id),
        "approval_id": str(approval.id),
    }


async def test_the_service_records_rows_shaped_like_the_act_tool_expects(
    world: World, providers: FakeProviders, usd_rate: date
) -> None:
    """The act tool's tests build payments with `conftest.propose_payment`: same shape."""
    lyon_po, rhein_po = await two_sent_pos(world)
    ours = await request(world, providers, await payable(world, rhein_po))
    lyon = await payable(world, lyon_po)
    theirs = await propose_payment(
        world.engine,
        lyon_po,
        requested_by=world.people.owner,
        payment_method_id=lyon.card,
        payout_account_id=lyon.account,
    )
    async with tenant_session(world.engine, world.people.tenant) as session:
        rows = [await session.get(PoPayment, i) for i in (ours.payment.id, theirs.payment_id)]
        approvals = [await session.get(Approval, i) for i in (ours.approval_id, theirs.approval_id)]

    def shape(row: PoPayment | None) -> dict[str, str]:
        assert row is not None
        return {
            attr.key: type(getattr(row, attr.key)).__name__
            for attr in inspect(PoPayment).column_attrs
        }

    assert shape(rows[0]) == shape(rows[1])
    first, second = approvals
    assert first is not None and second is not None
    assert {k: type(v).__name__ for k, v in first.args.items()} == {
        k: type(v).__name__ for k, v in second.args.items()
    }
    assert (first.tool, first.session_id) == (second.tool, second.session_id)


async def test_a_simulated_charge_and_chosen_milestones_are_frozen_too(
    world: World, providers: FakeProviders, usd_rate: date
) -> None:
    providers.card.simulated_mode = True
    target = await payable(world)
    milestones = [
        MilestoneInput(name="Deposit", bps=3_000),
        MilestoneInput(name="Delivery", bps=7_000),
    ]
    requested = await request(world, providers, target, milestones=milestones)
    row = await load_payment(world.engine, world.people.tenant, requested.payment.id)
    assert row.charge_mode == "simulated" and requested.payment.simulated
    assert row.milestones == [
        {"name": "Deposit", "bps": 3_000, "key": None},
        {"name": "Delivery", "bps": 7_000, "key": None},
    ]
    assert [(m.key, m.name, m.bps) for m in requested.payment.milestones] == [
        (None, "Deposit", 3_000),
        (None, "Delivery", 7_000),
    ]


async def test_the_plan_milestones_may_take_other_shares_next_to_custom_ones(
    world: World, providers: FakeProviders, usd_rate: date
) -> None:
    """30/40/30, common with Chinese mold makers; a custom milestone next to them asks for
    one piece of evidence."""
    target = await payable(world)
    milestones = [
        MilestoneInput(name="Tool start", bps=3_000, key="tool_start"),
        MilestoneInput(name="T1", bps=4_000, key="t1"),
        MilestoneInput(name="Delivery", bps=3_000),
    ]
    requested = await request(world, providers, target, milestones=milestones)
    [approval] = await payment_approvals(world)
    args = PayPurchaseOrderArgs.model_validate(approval.args)
    assert args.milestones == milestones
    row = await load_payment(world.engine, world.people.tenant, requested.payment.id)
    assert [(m["key"], m["name"], m["bps"]) for m in row.milestones] == [
        ("tool_start", "Tool start", 3_000),
        ("t1", "T1", 4_000),
        (None, "Delivery", 3_000),
    ]


@pytest.mark.parametrize(
    ("milestones", "error"),
    [
        (
            [{"name": "Tool start", "bps": 5_000, "key": "tool_start"}] * 2,
            "appears at most once",
        ),
        ([{"name": "Tooling", "bps": 10_000, "key": "tool_start"}], "is named 'Tool start'"),
        ([{"name": "FAI", "bps": 10_000, "key": "fai"}], "unknown milestone key"),
        ([{"name": "A", "bps": 5_000}, {"name": "A", "bps": 5_000}], "names must be different"),
    ],
)
def test_milestones_are_refused_before_anything_is_proposed(
    milestones: list[dict[str, object]], error: str
) -> None:
    with pytest.raises(ValidationError, match=error):
        PayPurchaseOrderRequest.model_validate(
            {
                "payment_method_id": str(uuid4()),
                "payout_account_id": str(uuid4()),
                "milestones": milestones,
            }
        )


async def test_without_an_exchange_rate_nothing_is_proposed(
    world: World, providers: FakeProviders
) -> None:
    target = await payable(world)
    with pytest.raises(PaymentRefused, match="no exchange rate from EUR to USD"):
        await request(world, providers, target, today=NO_RATE_DAY)
    assert await payment_approvals(world) == []


async def test_only_a_sent_open_order_can_be_paid(
    world: World, providers: FakeProviders, usd_rate: date
) -> None:
    people = world.people
    await world.award()
    [draft] = await world.orders()
    target = await payable(world, draft)
    with pytest.raises(PaymentInvalidState, match="only a sent, open order"):
        await request(world, providers, target)

    await send(world, draft)
    await lifecycle.cancel_purchase_order(
        world.engine, buyer(people), draft.id, CancelRequest(reason="Not needed any more")
    )
    with pytest.raises(PaymentInvalidState, match="cancelled"):
        await request(world, providers, target)

    with pytest.raises(PaymentNotFound):
        await service.request_po_payment(
            world.engine,
            buyer(people),
            uuid4(),
            PayPurchaseOrderRequest(
                payment_method_id=target.card, payout_account_id=target.account
            ),
            providers.bundle(),
            settings=SETTINGS,
            today=RATE_DAY,
        )
    # Another company's order does not exist for this one.
    with pytest.raises(PaymentNotFound):
        await request(world, providers, target, principal=outsider(people))
    assert await payment_approvals(world) == []


async def test_the_card_must_be_the_requesters_and_the_account_the_suppliers(
    world: World, providers: FakeProviders, usd_rate: date
) -> None:
    people = world.people
    target = await payable(world)
    members_card = await add_payment_method(world.engine, people.tenant, people.member)
    with pytest.raises(PaymentRefused, match="use a card you saved"):
        await request(world, providers, target, card=members_card)
    for status in ("pending", "removed"):
        card = await add_payment_method(world.engine, people.tenant, people.owner, status=status)
        with pytest.raises(PaymentRefused, match=f"is {status}"):
            await request(world, providers, target, card=card)

    lyons = await add_payout_account(
        world.engine, people.tenant, await supplier_of(world, "Lyon"), people.owner
    )
    with pytest.raises(PaymentRefused, match="of this order's supplier"):
        await request(world, providers, target, account=lyons)
    await service.disable_payout_account(
        world.engine, buyer(people), target.po.supplier_id, target.account
    )
    with pytest.raises(PaymentRefused, match="is disabled"):
        await request(world, providers, target)
    assert await payment_approvals(world) == []


async def test_one_live_payment_per_order(
    world: World, providers: FakeProviders, usd_rate: date, migrator_engine: AsyncEngine
) -> None:
    people = world.people
    target = await payable(world)
    first = await request(world, providers, target)
    with pytest.raises(PaymentInvalidState):
        await request(world, providers, target)

    # Rejected, and the reject hook released the row: the next request is a new proposal.
    await decide_approval(
        world.engine, approver(people), first.approval_id, "reject", jobs=world.jobs, on_reject={}
    )
    await set_payment(world, first.payment.id, state="rejected")
    second = await request(world, providers, target)
    assert second.approval_id != first.approval_id
    [_, latest] = await payment_approvals(world)
    assert latest.run_id == str(payment_proposal_ids(target.po.id, 1)[0])

    # Charged, then failed: it stays live (a person must act) and nothing else pays the order.
    await executed(migrator_engine, world, second.approval_id)
    await set_payment(world, second.payment.id, state="failed", charged_at=func.now())
    with pytest.raises(PaymentInvalidState, match="already has payment"):
        await request(world, providers, target)

    # Completed (the escrow released every milestone): the order is paid, nothing pays it again.
    await set_payment(
        world,
        second.payment.id,
        state="funded",
        funded_at=func.now(),
        completed_at=func.now(),
    )
    with pytest.raises(PaymentInvalidState, match="already has payment"):
        await request(world, providers, target)

    # Declined: no money moved, the order can be paid again.
    await set_payment(
        world,
        second.payment.id,
        state="declined",
        charged_at=None,
        funded_at=None,
        completed_at=None,
    )
    third = await request(world, providers, target)
    [*_, newest] = await payment_approvals(world)
    assert newest.id == third.approval_id
    assert newest.run_id == str(payment_proposal_ids(target.po.id, 2)[0])


async def test_a_payment_that_cannot_be_recorded_leaves_no_approval_waiting(
    world: World, providers: FakeProviders, usd_rate: date, monkeypatch: pytest.MonkeyPatch
) -> None:
    lyon_po, rhein_po = await two_sent_pos(world)
    lyon, rhein = await payable(world, lyon_po), await payable(world, rhein_po)

    # Both escrows would get the same id, unique per company: the second insert fails.
    def same_escrow_id(po_ref: str) -> str:
        return "PO-99-AAAA"

    monkeypatch.setattr(hashes, "new_chain_po_id", same_escrow_id)
    await request(world, providers, lyon)
    with pytest.raises(PaymentInvalidState, match="already has a payment"):
        await request(world, providers, rhein)
    [_, withdrawn] = await payment_approvals(world)
    assert withdrawn.status == "expired"
    expired = await actions(world, "approval.expired")
    assert [e.payload["approval_id"] for e in expired] == [str(withdrawn.id)]
    async with tenant_session(world.engine, world.people.tenant) as session:
        rows = await session.scalar(
            select(func.count())
            .select_from(PoPayment)
            .where(PoPayment.purchase_order_id == rhein_po.id)
        )
    assert rows == 0

    # Nothing is left waiting: the order can be paid (a new proposal).
    monkeypatch.undo()
    again = await request(world, providers, rhein)
    assert again.payment.state == "pending_approval"


# --- What people see ----------------------------------------------------------------------


async def test_a_visa_payment_waits_for_the_buyers_passkey(
    world: World, providers: FakeProviders, usd_rate: date
) -> None:
    people = world.people
    jobs = FakeJobQueue()
    card = providers.card
    card.rail_mode, card.simulated_mode = "vic", True
    target = await payable(world)
    requested = await request(world, providers, target)
    payment = requested.payment

    # The allowance is created with the request, under the act tool's key, for the browser
    # to run the Visa passkey on.
    assert payment.card_rail == "vic"
    pending = PaymentCardVerificationView(
        status="pending",
        allowance_id="alw_1",
        rail="agentic-token",
        provider="vic",
        verified_at=None,
    )
    assert payment.card_verification == pending
    assert payment.agent_card is None
    assert list(card.authorizations) == [f"payment:{payment.id}:authorize"]
    queue = await rfq_service.list_approvals(world.engine, approver(people), "pending")
    [item] = [i for i in queue if i.tool == PAY_PURCHASE_ORDER]
    assert item.payment is not None
    assert (item.payment.card_rail, item.payment.card_verified) == ("vic", False)

    # Only the person paying approves their card; Basis Theory says where it stands.
    with pytest.raises(PaymentForbidden):
        await service.confirm_card_verification(
            world.engine, approver(people), target.po.id, card, jobs
        )
    still = await service.confirm_card_verification(
        world.engine, buyer(people), target.po.id, card, jobs
    )
    assert still == pending
    card.verification_status = "verified"
    done = await service.confirm_card_verification(
        world.engine, buyer(people), target.po.id, card, jobs
    )
    assert (done.status, done.allowance_id) == ("verified", "alw_1")
    assert done.verified_at is not None
    assert [job.idempotency_key for job in jobs.jobs] == [f"approval:{requested.approval_id}"]
    # Read once: confirmed again, the provider is not asked.
    again = await service.confirm_card_verification(
        world.engine, buyer(people), target.po.id, card, jobs
    )
    assert again == done
    assert card.verification_calls == ["alw_1", "alw_1"]
    [event] = await actions(world, PAYMENT_CARD_VERIFIED)
    assert event.payload["payment_id"] == str(payment.id)
    view = await service.get_po_payment(world.engine, buyer(people), target.po.id, SETTINGS)
    assert view.card_verification == done
    queue = await rfq_service.list_approvals(world.engine, approver(people), "pending")
    assert [i for i in queue if i.tool == PAY_PURCHASE_ORDER] == []


async def test_a_visa_allowance_not_created_with_the_request_is_created_on_confirmation(
    world: World, providers: FakeProviders, usd_rate: date
) -> None:
    people = world.people
    card = FailingAuthorizationCard(rail_mode="vic", simulated_mode=True)
    jobs = FakeJobQueue()
    providers.card = card
    target = await payable(world)
    requested = await request(world, providers, target)
    assert requested.payment.card_verification is not None
    assert requested.payment.card_verification.allowance_id is None

    card.fail = False
    started = await service.confirm_card_verification(
        world.engine, buyer(people), target.po.id, card, jobs
    )
    assert (started.status, started.allowance_id) == ("pending", "alw_1")
    assert list(card.authorizations) == [f"payment:{requested.payment.id}:authorize"]


async def test_a_payment_without_the_visa_rail_has_no_passkey(
    world: World, providers: FakeProviders, usd_rate: date
) -> None:
    people = world.people
    target = await payable(world)
    requested = await request(world, providers, target)
    assert (requested.payment.card_rail, requested.payment.card_verification) == ("spt", None)
    assert providers.card.authorizations == {}
    with pytest.raises(PaymentInvalidState, match="not on the Visa rail"):
        await service.confirm_card_verification(
            world.engine, buyer(people), target.po.id, providers.card, FakeJobQueue()
        )


async def test_the_order_and_the_approval_queue_show_the_payment(
    world: World, providers: FakeProviders, usd_rate: date
) -> None:
    people = world.people
    target = await payable(world)
    requested = await request(world, providers, target)

    view = await award_service.get_purchase_order(world.engine, buyer(people), target.po.id)
    assert view.payment == PurchaseOrderPaymentSummary(
        payment_id=requested.payment.id,
        state="pending_approval",
        charge_currency="USD",
        charge_amount=Decimal("1815.00"),
        simulated=False,
        escrow_address=None,
        funded_at=None,
        needs_attention=False,
        # Nothing is released, nor next, before the escrow is funded.
        released_bps=0,
        next_milestone=None,
    )
    [summary] = await award_service.list_purchase_orders(world.engine, buyer(people))
    assert summary.payment_state == "pending_approval"

    queue = await rfq_service.list_approvals(world.engine, approver(people), "pending")
    [item] = [i for i in queue if i.tool == PAY_PURCHASE_ORDER]
    assert item.payment is not None
    assert (
        item.payment.payment_id,
        item.payment.purchase_order_id,
        item.payment.purchase_order_ref,
        item.payment.supplier_name,
        item.payment.po_currency,
        item.payment.po_total,
        item.payment.charge_currency,
        item.payment.charge_amount,
        item.payment.fx_rate,
        item.payment.fx_rate_date,
    ) == (
        requested.payment.id,
        target.po.id,
        "PO-1",
        "Rhein",
        "EUR",
        Decimal("1650.00"),
        "USD",
        Decimal("1815.00"),
        Decimal("1.1"),
        usd_rate,
    )
    assert (
        item.payment.card_brand,
        item.payment.card_last4,
        item.payment.payout_currency,
        item.payment.payout_account_last4,
        item.payment.simulated,
    ) == ("visa", "4242", "CNY", "4567", False)
    assert [(m.key, m.name, m.bps) for m in item.payment.milestones] == [
        ("tool_start", "Tool start", 3_000),
        ("t1", "T1", 3_000),
        ("ppap", "PPAP", 4_000),
    ]
    # No email recipients in a payment: the outbound guard has nothing to check.
    assert item.recipient_checks is None
    assert item.milestone_release is None
    assert all(i.payment is None for i in queue if i.tool != PAY_PURCHASE_ORDER)


async def test_the_payment_view_follows_the_live_payment_and_its_chain_record(
    world: World, providers: FakeProviders, usd_rate: date
) -> None:
    people = world.people
    target = await payable(world)
    with pytest.raises(PaymentNotFound, match="has no payment"):
        await service.get_po_payment(world.engine, buyer(people), target.po.id, SETTINGS)
    with pytest.raises(PaymentNotFound):
        await service.get_po_payment(world.engine, buyer(people), uuid4(), SETTINGS)
    requested = await request(world, providers, target)
    payment_id = requested.payment.id

    # Charged and minted, then the escrow failed: a person must act.
    signature = address("mint-transaction")
    async with tenant_session(world.engine, people.tenant) as session:
        session.add(
            PaymentChainTransaction(
                tenant_id=people.tenant,
                payment_id=payment_id,
                step="mint",
                attempt=1,
                signature=signature,
                last_valid_block_height=1_150,
                state="confirmed",
            )
        )
    await set_payment(
        world, payment_id, state="failed", charged_at=func.now(), failure_code="chain_failed"
    )
    view = await service.get_po_payment(world.engine, buyer(people), target.po.id, SETTINGS)
    assert (view.id, view.state, view.needs_attention, view.failure_code) == (
        payment_id,
        "failed",
        True,
        "chain_failed",
    )
    [tx] = view.chain.transactions
    assert (tx.step, tx.attempt, tx.signature, tx.state, tx.explorer_url) == (
        "mint",
        1,
        signature,
        "confirmed",
        f"https://explorer.solana.com/tx/{signature}?cluster=devnet",
    )
    po_view = await award_service.get_purchase_order(world.engine, buyer(people), target.po.id)
    assert po_view.payment is not None and po_view.payment.needs_attention

    # A later payment that ended (never live) does not hide the live one...
    async with tenant_session(world.engine, people.tenant) as session:
        row = await session.get(PoPayment, payment_id)
        assert row is not None
        session.add(
            PoPayment(
                id=uuid4(),
                tenant_id=people.tenant,
                purchase_order_id=row.purchase_order_id,
                approval_id=uuid4(),
                requested_by=row.requested_by,
                payment_method_id=row.payment_method_id,
                payout_account_id=row.payout_account_id,
                state="expired",
                po_currency=row.po_currency,
                po_total=row.po_total,
                charge_currency=row.charge_currency,
                charge_amount=row.charge_amount,
                quote_expires_at=row.quote_expires_at,
                token_amount=row.token_amount,
                chain_po_id="PO-1-ZZZZ",
                po_hash=row.po_hash,
                milestones=row.milestones,
                charge_mode=row.charge_mode,
                created_at=datetime.now(UTC) + timedelta(minutes=1),
            )
        )
    shown = await service.get_po_payment(world.engine, buyer(people), target.po.id, SETTINGS)
    assert shown.id == payment_id
    # ...and once none is live, the latest shows.
    await set_payment(world, payment_id, state="declined", charged_at=None)
    shown = await service.get_po_payment(world.engine, buyer(people), target.po.id, SETTINGS)
    assert (shown.state, shown.chain.chain_po_id) == ("expired", "PO-1-ZZZZ")
    with pytest.raises(PaymentNotFound):
        await service.get_po_payment(world.engine, outsider(people), target.po.id, SETTINGS)


async def test_the_rfq_feed_tells_the_payment_story(
    world: World, providers: FakeProviders, usd_rate: date
) -> None:
    people = world.people
    target = await payable(world)
    requested = await request(world, providers, target)
    events: list[tuple[str, dict[str, object]]] = [
        (
            PAYMENT_CHARGED,
            {"charge_currency": "USD", "charge_amount": "1815.00", "simulated": True},
        ),
        (PAYMENT_MINTED, {}),
        (ESCROW_CREATED, {}),
        (ESCROW_FUNDED, {}),
        (PAYMENT_DECLINED, {"failure_code": "card_declined"}),
        # Before the charge (the act tool), and at a chain step after it (the pipeline).
        (PAYMENT_FAILED, {"failure_code": "card_charge_failed"}),
        (PAYMENT_FAILED, {"step": "fund", "failure_code": "chain_fund_failed"}),
    ]
    async with tenant_session(world.engine, people.tenant) as session:
        for action, extra in events:
            session.add(
                rfq_event(
                    people.tenant,
                    None,
                    action,
                    world.rfq.id,
                    payment_id=requested.payment.id,
                    purchase_order_ref="PO-1",
                    **extra,
                )
            )
    feed = await rfq_activity(world.engine, buyer(people), world.rfq.id, limit=200)
    lines = {item.action: (item.kind, item.actor, item.summary) for item in feed}
    assert lines[PAYMENT_REQUESTED][0] == "user"
    assert lines[PAYMENT_REQUESTED][2] == (
        "Payment of purchase order PO-1 requested (USD 1815.00): waits for approval"
    )
    assert lines[PAYMENT_CHARGED] == (
        "system",
        "Bido",
        "Card charged (USD 1815.00) for purchase order PO-1 (simulated: no money moved)",
    )
    assert lines[PAYMENT_MINTED][2] == (
        "Stablecoin for purchase order PO-1 minted into the company's wallet"
    )
    assert lines[ESCROW_CREATED][2] == "Escrow for purchase order PO-1 created on Solana"
    assert lines[ESCROW_FUNDED][2] == (
        "Escrow for purchase order PO-1 funded: the payment waits for its milestones"
    )
    assert (
        lines[PAYMENT_DECLINED][2]
        == "Card payment for purchase order PO-1 declined (card declined)"
    )
    assert sorted(i.summary for i in feed if i.action == PAYMENT_FAILED) == [
        "Payment of purchase order PO-1 failed (card charge failed)",
        "Payment of purchase order PO-1 failed (chain fund failed): the card was charged, "
        "a person must act",
    ]
    approvals = [i.summary for i in feed if i.action == "approval.requested"]
    assert "Approval requested: purchase order payment" in approvals
