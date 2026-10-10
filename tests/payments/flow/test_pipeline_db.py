"""A charged payment's chain steps against Postgres and the fake chain: each exactly once.

`FakePaymentChain` lands a transaction when it is sent unless told to drop or fail
it; a dropped one lands later only if the test says so (`land_dropped`), and
`height` is the block height. The handler never polls here (`poll_budget=0`): the
status is read once, right after the send.
"""

import asyncio
import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update

from bido_back.db import tenant_session
from bido_back.db.models import (
    PaymentChainTransaction,
    PoPayment,
    SupplierPayoutAccount,
    TenantWallet,
)
from bido_back.payments.chain.hashes import payout_ref
from bido_back.payments.errors import (
    PaymentNotFound,
    PaymentRefused,
    PaymentsNotConfigured,
    ProviderError,
)
from bido_back.payments.execute import PaymentActDeps, register_payment_act_tools
from bido_back.payments.pipeline import (
    EXPIRY_MARGIN,
    POLICY_VIOLATION,
    InvalidPaymentJob,
    PaymentAdvanceHandler,
    StepPending,
    payment_advance_envelope,
)
from bido_back.payments.ports import EscrowSpec, EscrowState, SignedTransaction
from bido_back.payments.proposals import (
    ESCROW_CREATED,
    ESCROW_FUNDED,
    PAYMENT_ADVANCE,
    PAYMENT_CHARGED,
    PAYMENT_FAILED,
    PAYMENT_MINTED,
)
from bido_back.worker.envelope import Envelope
from tests.approvals.conftest import FakeJobQueue
from tests.awards.test_awards_db import World
from tests.awards.test_lifecycle import two_sent_pos
from tests.payments.fakes import FakePaymentChain, FakeProviders, FakeWalletProvider
from tests.payments.flow.conftest import (
    add_payment_method,
    add_payout_account,
    chain_transactions,
    load_payment,
    propose_payment,
)
from tests.payments.flow.test_execute_db import Paying, start_paying

pytestmark = pytest.mark.db


@dataclass
class Charged:
    """A charged payment and the pipeline that advances it."""

    paying: Paying
    payment_id: UUID
    handler: PaymentAdvanceHandler
    jobs: FakeJobQueue

    @property
    def fake(self) -> FakePaymentChain:
        return self.paying.providers.chain

    @property
    def tenant(self) -> UUID:
        return self.paying.tenant

    def on(self, chain: FakePaymentChain) -> PaymentAdvanceHandler:
        """The same pipeline on another chain."""
        providers = dataclasses.replace(self.paying.providers.bundle(), chain=chain)
        return PaymentAdvanceHandler(self.paying.engine, providers, self.jobs, poll_budget=0)

    async def advance(self) -> str:
        return await self.handler.advance(self.tenant, self.payment_id)

    async def until(self, state: str) -> None:
        for _ in range(3):
            if await self.advance() == state:
                return
        raise AssertionError(f"the payment never reached {state}")

    async def payment(self) -> PoPayment:
        return await self.paying.payment(self.payment_id)

    async def rows(self) -> list[tuple[str, int, str]]:
        return [(r.step, r.attempt, r.state) for r in await self.transactions()]

    async def transactions(self) -> list[PaymentChainTransaction]:
        return await chain_transactions(self.paying.engine, self.tenant, self.payment_id)

    def built(self, step: str) -> int:
        return sum(1 for built, _ in self.fake.built if built == step)

    async def total(self) -> int:
        return int((await self.payment()).token_amount)

    def wakes(self) -> list[str]:
        return [job.idempotency_key for job in self.jobs.jobs]


@pytest_asyncio.fixture
async def charged(world: World, providers: FakeProviders) -> Charged:
    paying = await start_paying(world, providers)
    proposed = await paying.charged()
    jobs = FakeJobQueue()
    handler = PaymentAdvanceHandler(world.engine, providers.bundle(), jobs, poll_budget=0)
    return Charged(paying, proposed.payment_id, handler, jobs)


# --- The whole way -------------------------------------------------------------------------


async def test_a_charged_payment_is_minted_escrowed_and_funded_once(charged: Charged) -> None:
    fake, payment_id = charged.fake, charged.payment_id
    # Driven by its own wake-up jobs, one step per job.
    queue = [j for j in charged.paying.world.jobs.jobs if j.type == PAYMENT_ADVANCE]
    keys: list[str] = []
    while queue:
        job = queue.pop(0)
        keys.append(job.idempotency_key)
        await charged.handler(job)
        queue += charged.jobs.jobs
        charged.jobs.jobs.clear()
    assert keys == [
        f"payment:{payment_id}:charged",
        f"payment:{payment_id}:minted",
        f"payment:{payment_id}:escrow_created",
    ]

    payment = await charged.payment()
    assert payment.state == "funded" and payment.funded_at is not None
    assert payment.wallet_address is not None and payment.vault_address is not None
    total = int(payment.token_amount)
    escrow = fake.escrow_address(buyer=payment.wallet_address, chain_po_id=payment.chain_po_id)
    assert payment.escrow_address == escrow
    assert payment.vault_address == fake.vault_address(escrow=escrow)
    # Minted once, and all of it is locked in the escrow's vault.
    assert fake.minted_total == total
    assert fake.escrows[escrow].status == "funded"
    assert fake.balances[payment.vault_address] == total
    assert fake.balances[payment.wallet_address] == 0
    assert await charged.rows() == [
        ("mint", 1, "confirmed"),
        ("create_escrow", 1, "confirmed"),
        ("fund", 1, "confirmed"),
    ]
    # The company's wallet: created once at the provider and recorded.
    wallets = charged.paying.providers.wallets
    assert list(wallets.wallets) == [charged.tenant]
    async with tenant_session(charged.paying.engine, charged.tenant) as session:
        [row] = list(await session.scalars(select(TenantWallet)))
        account = await session.get(SupplierPayoutAccount, payment.payout_account_id)
    assert payment.wallet_id == row.id and row.address == payment.wallet_address
    assert row.wallet_ref == wallets.wallets[charged.tenant].ref
    # The escrow holds the approved terms.
    [create] = [t for t in await charged.transactions() if t.step == "create_escrow"]
    spec = fake.sent[create.signature].spec
    assert spec is not None and account is not None
    assert spec.buyer == payment.wallet_address and spec.total == total
    assert spec.po_hash == bytes.fromhex(payment.po_hash) and spec.cad_hash == bytes(32)
    assert [(m.name, m.bps) for m in spec.milestones] == [("Delivery", 10_000)]
    assert spec.payout_ref == payout_ref(
        str(account.id), account.bank_fingerprint, payment.chain_po_id
    )
    assert payment.refund_after is not None
    assert spec.refund_after == int(payment.refund_after.timestamp())
    assert abs(payment.refund_after - (datetime.now(UTC) + timedelta(days=90))) < timedelta(
        minutes=5
    )
    # The company's wallet signed the fund, once.
    assert len(wallets.signed) == 1 and wallets.signed[0].startswith(f"payment:{payment_id}:")
    # Every step is on record, keyed by the RFQ for its activity feed.
    events = await charged.paying.events(payment_id)
    assert [e.action for e in events] == [
        PAYMENT_CHARGED,
        PAYMENT_MINTED,
        ESCROW_CREATED,
        ESCROW_FUNDED,
    ]
    assert all(e.payload["rfq_id"] == str(charged.paying.po.rfq_id) for e in events)
    assert events[-1].payload["escrow_address"] == escrow
    # Nothing left to do.
    assert await charged.advance() == "funded"
    assert charged.jobs.jobs == []


async def test_a_company_has_one_wallet_for_all_its_payments(
    world: World, providers: FakeProviders
) -> None:
    register_payment_act_tools(
        world.registry,
        PaymentActDeps(db=world.engine, providers=providers.bundle(), jobs=world.jobs),
    )
    people = world.people
    method_id = await add_payment_method(world.engine, people.tenant, people.owner)
    handler = PaymentAdvanceHandler(world.engine, providers.bundle(), None, poll_budget=0)
    payments: list[PoPayment] = []
    for po in await two_sent_pos(world):
        account_id = await add_payout_account(
            world.engine, people.tenant, po.supplier_id, people.owner
        )
        proposed = await propose_payment(
            world.engine,
            po,
            requested_by=people.owner,
            payment_method_id=method_id,
            payout_account_id=account_id,
        )
        await world.approve(proposed.approval_id)
        for _ in range(3):
            await handler.advance(people.tenant, proposed.payment_id)
        payments.append(await load_payment(world.engine, people.tenant, proposed.payment_id))

    assert [p.state for p in payments] == ["funded", "funded"]
    assert len({p.wallet_id for p in payments}) == 1
    assert len(providers.wallets.wallets) == 1
    assert providers.chain.minted_total == sum(int(p.token_amount) for p in payments)


# --- A transaction that may still land ------------------------------------------------------


async def test_a_dropped_mint_is_waited_for_and_never_minted_twice(charged: Charged) -> None:
    fake = charged.fake
    fake.next_landing["mint"] = "drop"

    with pytest.raises(StepPending):
        await charged.advance()
    with pytest.raises(StepPending):  # its blockhash is still valid: nothing is rebuilt
        await charged.advance()
    assert await charged.rows() == [("mint", 1, "sent")]
    assert charged.built("mint") == 1 and fake.minted_total == 0

    # It was only slow.
    [sent] = await charged.transactions()
    fake.land_dropped(sent.signature)
    assert await charged.advance() == "minted"

    assert await charged.rows() == [("mint", 1, "confirmed")]
    assert charged.built("mint") == 1 and fake.minted_total == await charged.total()
    assert charged.wakes() == [f"payment:{charged.payment_id}:minted"]


async def test_a_dropped_mint_is_rebuilt_only_once_it_can_no_longer_land(
    charged: Charged,
) -> None:
    fake = charged.fake
    fake.next_landing["mint"] = "drop"
    with pytest.raises(StepPending):
        await charged.advance()
    [first] = await charged.transactions()

    fake.height = first.last_valid_block_height + EXPIRY_MARGIN  # within the margin
    with pytest.raises(StepPending):
        await charged.advance()
    assert charged.built("mint") == 1

    fake.height = first.last_valid_block_height + EXPIRY_MARGIN + 1
    assert await charged.advance() == "minted"

    assert await charged.rows() == [("mint", 1, "expired"), ("mint", 2, "confirmed")]
    assert charged.built("mint") == 2 and fake.minted_total == await charged.total()


@dataclass
class LandsInTheLastBlock(FakePaymentChain):
    """A dropped transaction that landed in its last valid block: the first status read
    misses it, a read after the block height shows it (`armed`)."""

    armed: bool = False

    async def block_height(self) -> int:
        if self.armed:
            for signature, record in list(self.sent.items()):
                if record.landing == "drop" and signature not in self.landed:
                    self.land_dropped(signature)
        return self.height


async def test_a_mint_that_landed_in_its_last_valid_block_is_not_rebuilt(
    charged: Charged,
) -> None:
    late = LandsInTheLastBlock()
    handler = charged.on(late)
    late.next_landing["mint"] = "drop"
    with pytest.raises(StepPending):
        await handler.advance(charged.tenant, charged.payment_id)
    [sent] = await charged.transactions()

    late.height = sent.last_valid_block_height + EXPIRY_MARGIN + 1
    late.armed = True
    assert await handler.advance(charged.tenant, charged.payment_id) == "minted"

    assert await charged.rows() == [("mint", 1, "confirmed")]
    assert len([s for s, _ in late.built if s == "mint"]) == 1
    assert late.minted_total == await charged.total()


async def test_a_send_that_may_have_been_forwarded_is_asked_about_before_a_rebuild(
    charged: Charged,
) -> None:
    fake = charged.fake
    fake.send_error["mint"] = ProviderError("solana", "sendTransaction", "timeout", retryable=True)
    with pytest.raises(ProviderError):
        await charged.advance()
    assert await charged.rows() == [("mint", 1, "sent")]
    with pytest.raises(StepPending):
        await charged.advance()

    [first] = await charged.transactions()
    fake.height = first.last_valid_block_height + EXPIRY_MARGIN + 1
    assert await charged.advance() == "minted"

    assert await charged.rows() == [("mint", 1, "expired"), ("mint", 2, "confirmed")]
    assert fake.minted_total == await charged.total()


async def test_a_node_behind_the_finalized_chain_never_expires_a_transaction(
    charged: Charged,
) -> None:
    fake = charged.fake
    fake.next_landing["mint"] = "drop"
    with pytest.raises(StepPending):
        await charged.advance()
    [first] = await charged.transactions()
    fake.height = first.last_valid_block_height + EXPIRY_MARGIN + 1
    fake.node_behind = 1

    with pytest.raises(ProviderError, match="node_behind"):
        await charged.advance()
    assert await charged.rows() == [("mint", 1, "sent")]  # not on a lagging node's word

    assert await charged.advance() == "minted"
    assert await charged.rows() == [("mint", 1, "expired"), ("mint", 2, "confirmed")]
    assert fake.minted_total == await charged.total()


async def test_concurrent_jobs_of_one_payment_mint_once(charged: Charged) -> None:
    results = await asyncio.gather(charged.advance(), charged.advance(), return_exceptions=True)

    assert "minted" in results
    assert all(r == "minted" or isinstance(r, StepPending) for r in results)
    assert charged.built("mint") == 1
    assert charged.fake.minted_total == await charged.total()
    assert charged.wakes() == [f"payment:{charged.payment_id}:minted"]


# --- The chain already shows the step done --------------------------------------------------


async def escrow_on_chain(charged: Charged, *, total: int | None = None) -> str:
    """An escrow at the payment's address, as if an earlier `create_po` had landed."""
    payment = await charged.payment()
    assert payment.wallet_address is not None
    fake = charged.fake
    address = fake.escrow_address(buyer=payment.wallet_address, chain_po_id=payment.chain_po_id)
    fake.escrows[address] = EscrowState(
        address=address,
        status="created",
        chain_po_id=payment.chain_po_id,
        buyer=payment.wallet_address,
        mint=fake.mint,
        total=int(payment.token_amount) if total is None else total,
        vault=fake.vault_address(escrow=address),
    )
    return address


async def test_an_escrow_already_on_chain_is_not_created_again(charged: Charged) -> None:
    await charged.until("minted")
    await escrow_on_chain(charged)

    assert await charged.advance() == "escrow_created"

    assert charged.built("create_escrow") == 0
    assert await charged.rows() == [("mint", 1, "confirmed")]
    [event] = [
        e for e in await charged.paying.events(charged.payment_id) if e.action == ESCROW_CREATED
    ]
    assert event.payload["signature"] is None
    assert await charged.advance() == "funded"


async def test_an_escrow_with_other_terms_fails_the_payment(charged: Charged) -> None:
    await charged.until("minted")
    await escrow_on_chain(charged, total=await charged.total() + 1)

    assert await charged.advance() == "failed"

    payment = await charged.payment()
    assert payment.failure_code == "escrow_mismatch" and payment.charged_at is not None
    assert charged.built("create_escrow") == 0


async def test_a_funded_escrow_is_not_funded_again(charged: Charged) -> None:
    await charged.until("escrow_created")
    payment = await charged.payment()
    assert payment.escrow_address is not None
    escrow = charged.fake.escrows[payment.escrow_address]
    charged.fake.escrows[payment.escrow_address] = dataclasses.replace(escrow, status="funded")

    assert await charged.advance() == "funded"

    assert charged.built("fund") == 0 and charged.paying.providers.wallets.signed == []


@dataclass
class FundedMeanwhile(FakePaymentChain):
    """Another `fund` of the escrow lands just before ours, which then fails `AlreadyFunded`."""

    async def send(self, transaction: SignedTransaction) -> None:
        if transaction.wire.startswith(b"fund:"):
            for address, escrow in list(self.escrows.items()):
                self.escrows[address] = dataclasses.replace(escrow, status="funded")
        await super().send(transaction)


async def test_a_fund_that_failed_because_the_escrow_is_funded_counts_as_done(
    charged: Charged,
) -> None:
    handler = charged.on(FundedMeanwhile())
    for state in ("minted", "escrow_created", "funded"):
        assert await handler.advance(charged.tenant, charged.payment_id) == state

    [fund] = [t for t in await charged.transactions() if t.step == "fund"]
    assert (fund.state, fund.error) == ("failed", "AlreadyFunded")
    assert (await charged.payment()).funded_at is not None


# --- Failures --------------------------------------------------------------------------------


async def test_a_signing_error_is_retried_and_funds_once(charged: Charged) -> None:
    await charged.until("escrow_created")
    wallets = charged.paying.providers.wallets
    wallets.sign_error = ProviderError(
        "privy", "sign_transaction", "unavailable", status=503, retryable=True
    )

    with pytest.raises(ProviderError):
        await charged.advance()
    assert [step for step, _, _ in await charged.rows()] == ["mint", "create_escrow"]

    assert await charged.advance() == "funded"
    assert len(wallets.signed) == 1 and charged.built("fund") == 1


@dataclass
class UnusableSignature(FakeWalletProvider):
    """The wallet answers once with a signature over another message."""

    tampered: int = 1

    async def sign_transaction(
        self, *, wallet_ref: str, transaction: bytes, idempotency_key: str
    ) -> bytes:
        signed = await super().sign_transaction(
            wallet_ref=wallet_ref, transaction=transaction, idempotency_key=idempotency_key
        )
        if self.tampered:
            self.tampered -= 1
            return signed + b"!"
        return signed


async def test_an_unusable_wallet_signature_records_nothing_and_is_retried(
    charged: Charged,
) -> None:
    await charged.until("escrow_created")
    wallets = UnusableSignature()
    providers = dataclasses.replace(charged.paying.providers.bundle(), wallets=wallets)
    handler = PaymentAdvanceHandler(charged.paying.engine, providers, None, poll_budget=0)

    with pytest.raises(ProviderError, match="message_changed"):
        await handler.advance(charged.tenant, charged.payment_id)
    assert [step for step, _, _ in await charged.rows()] == ["mint", "create_escrow"]

    assert await handler.advance(charged.tenant, charged.payment_id) == "funded"
    # A fresh Privy key per try: Privy caches an answer under its key.
    assert len(set(wallets.signed)) == 2 and charged.built("fund") == 1


@dataclass
class RefusesTheEscrow(FakePaymentChain):
    """`build_create_escrow` refuses before any call (the chain code's own checks)."""

    refusal: Exception = dataclasses.field(
        default_factory=lambda: PaymentsNotConfigured("the escrow program does not allow us")
    )

    async def build_create_escrow(self, spec: EscrowSpec) -> SignedTransaction:
        raise self.refusal


@pytest.mark.parametrize(
    ("refusal", "code"),
    [
        (PaymentsNotConfigured("our operator is not the program's"), "chain_not_configured"),
        (PaymentRefused("milestone shares must add up to 10000"), "chain_create_escrow_failed"),
    ],
)
async def test_an_escrow_the_chain_code_refuses_fails_the_payment(
    charged: Charged, refusal: Exception, code: str
) -> None:
    handler = charged.on(RefusesTheEscrow(refusal=refusal))
    assert await handler.advance(charged.tenant, charged.payment_id) == "minted"

    assert await handler.advance(charged.tenant, charged.payment_id) == "failed"

    payment = await charged.payment()
    assert payment.failure_code == code and payment.charged_at is not None
    assert [step for step, _, _ in await charged.rows()] == ["mint"]


async def test_a_wallet_policy_refusal_fails_the_payment(charged: Charged) -> None:
    await charged.until("escrow_created")
    charged.paying.providers.wallets.sign_error = ProviderError(
        "privy", "sign_transaction", POLICY_VIOLATION, status=403
    )

    assert await charged.advance() == "failed"

    payment = await charged.payment()
    assert payment.failure_code == "chain_fund_failed" and payment.charged_at is not None
    assert charged.built("fund") == 0


async def test_a_failed_fund_leaves_the_charged_payment_for_a_person(charged: Charged) -> None:
    await charged.until("escrow_created")
    charged.fake.next_landing["fund"] = "fail"

    assert await charged.advance() == "failed"

    payment = await charged.payment()
    assert payment.failure_code == "chain_fund_failed"
    # Charged, not funded: it needs attention, and it keeps the order from a second charge.
    assert payment.charged_at is not None and payment.funded_at is None
    [fund] = [t for t in await charged.transactions() if t.step == "fund"]
    assert (fund.state, fund.error) == ("failed", "SimulatedFailure")
    assert (await charged.paying.actions(charged.payment_id))[-1] == PAYMENT_FAILED
    assert charged.wakes() == [
        f"payment:{charged.payment_id}:minted",
        f"payment:{charged.payment_id}:escrow_created",
    ]
    assert await charged.advance() == "failed"  # nothing more happens


async def test_a_refused_mint_fails_the_payment(charged: Charged) -> None:
    charged.fake.send_error["mint"] = ProviderError(
        "solana", "sendTransaction", "InvalidAccountData", retryable=False
    )

    assert await charged.advance() == "failed"

    payment = await charged.payment()
    assert payment.failure_code == "chain_mint_failed"
    [mint] = await charged.transactions()
    assert (mint.state, mint.error) == ("failed", "InvalidAccountData")
    assert charged.fake.minted_total == 0


async def test_a_step_refuses_a_payment_that_no_longer_holds_its_approved_terms(
    charged: Charged,
) -> None:
    async with tenant_session(charged.paying.engine, charged.tenant) as session:
        await session.execute(
            update(PoPayment)
            .where(PoPayment.id == charged.payment_id)
            .values(token_amount=PoPayment.token_amount * 2, updated_at=func.now())
        )

    assert await charged.advance() == "failed"

    assert (await charged.payment()).failure_code == "approval_mismatch"
    assert charged.fake.built == [] and charged.fake.minted_total == 0


async def test_bad_jobs_are_refused(charged: Charged) -> None:
    payment_id = charged.payment_id

    def job(key: str, payload: dict[str, str]) -> Envelope:
        return Envelope(
            type=PAYMENT_ADVANCE,
            tenant_id=charged.tenant,
            idempotency_key=key,
            payload={**payload},
        )

    for bad in (
        job(f"payment:{payment_id}:charged", {}),
        job(f"payment:{payment_id}:charged", {"payment_id": "not-a-uuid"}),
        job(f"payment:{uuid4()}:charged", {"payment_id": str(payment_id)}),
    ):
        with pytest.raises(InvalidPaymentJob):
            await charged.handler(bad)
    with pytest.raises(PaymentNotFound):
        await charged.handler(payment_advance_envelope(charged.tenant, uuid4(), "charged"))
    assert charged.fake.built == []
