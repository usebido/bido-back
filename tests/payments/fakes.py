"""In-memory payment providers: a card gateway, a vault, a wallet provider and a chain.

They follow the contracts in `bido_back.payments.ports` closely enough to test
the flow's guarantees: the fake card gateway counts real charges per payment
(a double charge shows), the fake chain mints, creates and funds like the
program does (the escrow exists once, it is funded once, a mint is NOT
idempotent), and transactions can be dropped, delayed or failed to test the
retry rules. Switches are plain attributes set by the test.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.signature import Signature

from bido_back.payments.config import PaymentProviders
from bido_back.payments.errors import CardCredentialLost, CardNotVerified, ProviderError
from bido_back.payments.ports import (
    VIC_RAIL,
    BankAccountDetails,
    CardAuthorization,
    CardCharge,
    CardConsumer,
    CardDisplayData,
    CardRail,
    CardVerification,
    CardVerificationStatus,
    ChargeOutcome,
    CustodyWallet,
    EnrolledCard,
    EscrowSpec,
    EscrowState,
    IssuedCard,
    SignedTransaction,
    TransactionStatus,
    UnsignedTransaction,
)


def address(label: str) -> str:
    """A valid, deterministic base58 address for a label."""
    return str(Pubkey(hashlib.sha256(label.encode()).digest()))


# --- Card ------------------------------------------------------------------------------------

CARD_ART = CardDisplayData(
    art_url="https://cdn.example.test/art/visa-signature.png",
    background_color="#1A1F71",
    foreground_color="#FFFFFF",
    description="Visa Signature",
    issuer_name="Example Bank",
)


@dataclass
class FakeCardGateway:
    simulated_mode: bool = False
    # `vic`: each allowance waits for the passkey (`verification_status`) before a mint.
    rail_mode: CardRail = "spt"
    verification_status: CardVerificationStatus = "pending"
    verification_calls: list[str] = field(default_factory=list[str])
    enroll_status: Literal["active", "pending", "failed"] = "active"
    enroll_display: CardDisplayData | None = CARD_ART
    # What `display` answers per payment method ref (absent: no display); an exception is raised.
    displays: dict[str, CardDisplayData | Exception] = field(
        default_factory=dict[str, CardDisplayData | Exception]
    )
    display_calls: list[str] = field(default_factory=list[str])
    authorize_usable: bool = True
    charge_outcome: ChargeOutcome = "succeeded"
    # Raised by the next `charge` (then cleared): a provider failure or a lost credential.
    charge_error: Exception | None = None
    enrolled: list[str] = field(default_factory=list[str])
    removed: list[str] = field(default_factory=list[str])
    authorizations: dict[str, CardAuthorization] = field(
        default_factory=dict[str, CardAuthorization]
    )
    # payment_id -> the charge actually made (one per payment, or a double charge shows).
    charges: dict[UUID, CardCharge] = field(default_factory=dict[UUID, CardCharge])
    charge_calls: int = 0

    @property
    def simulated(self) -> bool:
        return self.simulated_mode

    @property
    def rail(self) -> CardRail:
        return self.rail_mode

    async def verification(self, authorization_ref: str) -> CardVerification:
        self.verification_calls.append(authorization_ref)
        rail, provider = VIC_RAIL
        return CardVerification(status=self.verification_status, rail=rail, provider=provider)

    async def enroll(
        self, *, token_id: str, consumer: CardConsumer, idempotency_key: str
    ) -> EnrolledCard:
        self.enrolled.append(token_id)
        return EnrolledCard(
            payment_method_ref=f"pm_{token_id}",
            fingerprint=f"fp-{token_id}",
            brand="visa",
            last4="4242",
            exp_month=12,
            exp_year=2030,
            funding="credit",
            status=self.enroll_status,
            display=self.enroll_display,
        )

    async def remove(self, payment_method_ref: str) -> None:
        self.removed.append(payment_method_ref)

    async def display(self, payment_method_ref: str) -> CardDisplayData | None:
        self.display_calls.append(payment_method_ref)
        answer = self.displays.get(payment_method_ref)
        if isinstance(answer, Exception):
            raise answer
        return answer

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
        existing = self.authorizations.get(idempotency_key)
        if existing is not None:
            return existing
        usable = (
            self.verification_status == "verified"
            if self.rail_mode == "vic"
            else self.authorize_usable
        )
        authorization = CardAuthorization(
            ref=f"alw_{len(self.authorizations) + 1}", usable=usable, expires_at=expires_at
        )
        self.authorizations[idempotency_key] = authorization
        return authorization

    async def charge(
        self,
        *,
        authorization_ref: str,
        payment_id: UUID,
        amount: Decimal,
        currency: str,
        description: str,
        idempotency_key: str,
    ) -> CardCharge:
        self.charge_calls += 1
        if self.charge_error is not None:
            error, self.charge_error = self.charge_error, None
            raise error
        existing = self.charges.get(payment_id)
        if existing is not None:
            return existing
        if self.rail_mode == "vic":
            if self.verification_status != "verified":
                raise CardNotVerified(f"payment {payment_id}: the allowance is not approved")
            card = IssuedCard("cred_" + payment_id.hex[:8], "4242", 12, 2030)
            charge = CardCharge("succeeded", f"sim_{payment_id}", None, True, issued_card=card)
            self.charges[payment_id] = charge
            return charge
        if self.charge_outcome == "succeeded":
            reference = f"sim_{payment_id}" if self.simulated_mode else f"pi_{payment_id.hex[:12]}"
            charge = CardCharge("succeeded", reference, None, self.simulated_mode)
            self.charges[payment_id] = charge
            return charge
        if self.charge_outcome == "declined":
            return CardCharge("declined", None, "card_declined", self.simulated_mode)
        return CardCharge("pending", f"pi_{payment_id.hex[:12]}", None, self.simulated_mode)


def lost_credential() -> CardCredentialLost:
    return CardCredentialLost("payment credential replayed")


def provider_down(retryable: bool = True) -> ProviderError:
    return ProviderError("basis_theory", "credentials", "unavailable", status=503, retryable=True)


# --- Vault and wallets --------------------------------------------------------------------------


@dataclass
class FakeBankDetailsCipher:
    """Seals into opaque bytes (no plaintext inside) that open only for the same row."""

    sealed: list[BankAccountDetails] = field(default_factory=list[BankAccountDetails])
    _boxes: dict[bytes, tuple[BankAccountDetails, UUID, UUID]] = field(
        default_factory=dict[bytes, tuple[BankAccountDetails, UUID, UUID]]
    )

    async def seal(
        self, details: BankAccountDetails, *, tenant_id: UUID, supplier_id: UUID
    ) -> bytes:
        self.sealed.append(details)
        box = b"sealed:" + uuid4().bytes
        self._boxes[box] = (details, tenant_id, supplier_id)
        return box

    async def open(
        self, sealed: bytes, *, tenant_id: UUID, supplier_id: UUID
    ) -> BankAccountDetails:
        details, tenant, supplier = self._boxes[sealed]
        if (tenant, supplier) != (tenant_id, supplier_id):
            raise ValueError("sealed for another supplier")
        return details


@dataclass
class FakeWalletProvider:
    wallets: dict[UUID, CustodyWallet] = field(default_factory=dict[UUID, CustodyWallet])
    signed: list[str] = field(default_factory=list[str])
    sign_error: Exception | None = None

    async def ensure_wallet(self, *, tenant_id: UUID) -> CustodyWallet:
        wallet = self.wallets.get(tenant_id)
        if wallet is None:
            keypair = Keypair.from_seed(hashlib.sha256(tenant_id.bytes).digest())
            wallet = CustodyWallet(ref=f"wal_{tenant_id.hex[:8]}", address=str(keypair.pubkey()))
            self.wallets[tenant_id] = wallet
        return wallet

    async def sign_transaction(
        self, *, wallet_ref: str, transaction: bytes, idempotency_key: str
    ) -> bytes:
        if self.sign_error is not None:
            error, self.sign_error = self.sign_error, None
            raise error
        self.signed.append(idempotency_key)
        return b"signed:" + transaction


# --- Chain -----------------------------------------------------------------------------------

Step = Literal["mint", "create_escrow", "fund"]
Landing = Literal["confirm", "fail", "drop"]


@dataclass
class _Sent:
    step: Step
    landing: Landing
    owner: str | None = None
    amount: int = 0
    spec: EscrowSpec | None = None
    escrow: str | None = None


@dataclass
class FakePaymentChain:
    """A tiny model of the mint and the escrow program, with controllable landings."""

    cluster_name: str = "devnet"
    mint_address: str = field(default_factory=lambda: address("mint"))
    height: int = 1_000
    # How the next transaction of a step lands (then reset to "confirm").
    next_landing: dict[str, Landing] = field(default_factory=dict[str, Landing])
    # Raised by the next `send` of a step (then cleared).
    send_error: dict[str, Exception] = field(default_factory=dict[str, Exception])
    built: list[tuple[Step, str]] = field(default_factory=list[tuple[Step, str]])
    sent: dict[str, _Sent] = field(default_factory=dict[str, _Sent])
    landed: dict[str, TransactionStatus] = field(default_factory=dict[str, TransactionStatus])
    balances: dict[str, int] = field(default_factory=dict[str, int])
    escrows: dict[str, EscrowState] = field(default_factory=dict[str, EscrowState])
    # The next `status` calls answer like an RPC node behind a finalized slot (retryable).
    node_behind: int = 0
    _pending: dict[str, _Sent] = field(default_factory=dict[str, _Sent])

    @property
    def cluster(self) -> str:
        return self.cluster_name

    @property
    def mint(self) -> str:
        return self.mint_address

    @property
    def minted_total(self) -> int:
        return sum(s.amount for sig, s in self.sent.items() if s.step == "mint" and self._ok(sig))

    def _ok(self, signature: str) -> bool:
        status = self.landed.get(signature)
        return status is not None and status.state == "confirmed"

    def escrow_address(self, *, buyer: str, chain_po_id: str) -> str:
        return address(f"escrow:{buyer}:{chain_po_id}")

    def vault_address(self, *, escrow: str) -> str:
        return address(f"vault:{escrow}")

    def _signed(self, step: Step, record: _Sent) -> SignedTransaction:
        signature = str(Signature.new_unique())
        self.built.append((step, signature))
        self._pending[signature] = record
        return SignedTransaction(signature, f"{step}:{signature}".encode(), self.height + 150)

    async def build_mint(self, *, owner: str, amount: int, memo: str) -> SignedTransaction:
        return self._signed("mint", _Sent("mint", "confirm", owner=owner, amount=amount))

    async def build_create_escrow(self, spec: EscrowSpec) -> SignedTransaction:
        escrow = self.escrow_address(buyer=spec.buyer, chain_po_id=spec.chain_po_id)
        return self._signed(
            "create_escrow", _Sent("create_escrow", "confirm", spec=spec, escrow=escrow)
        )

    async def build_fund(self, *, buyer: str, escrow: str) -> UnsignedTransaction:
        signature = str(Signature.new_unique())
        self._pending[signature] = _Sent("fund", "confirm", owner=buyer, escrow=escrow)
        return UnsignedTransaction(f"fund:{signature}".encode(), self.height + 150)

    def complete_fund(
        self, unsigned: UnsignedTransaction, *, buyer: str, signed_by_buyer: bytes
    ) -> SignedTransaction:
        if signed_by_buyer != b"signed:" + unsigned.wire:
            raise ProviderError("wallet", "sign_transaction", "message_changed")
        signature = unsigned.wire.decode().removeprefix("fund:")
        self.built.append(("fund", signature))
        return SignedTransaction(signature, unsigned.wire, unsigned.last_valid_block_height)

    async def send(self, transaction: SignedTransaction) -> None:
        record = self._pending.get(transaction.signature)
        if record is None:
            raise AssertionError("sending a transaction the fake chain never built")
        error = self.send_error.pop(record.step, None)
        if error is not None:
            raise error
        if transaction.signature in self.sent:
            return  # re-sending the same bytes is a no-op on chain
        record.landing = self.next_landing.pop(record.step, "confirm")
        self.sent[transaction.signature] = record
        if record.landing == "drop":
            return
        if record.landing == "fail":
            self.landed[transaction.signature] = TransactionStatus("failed", "SimulatedFailure")
            return
        self.landed[transaction.signature] = self._apply(record)

    def _apply(self, record: _Sent) -> TransactionStatus:
        if record.step == "mint":
            assert record.owner is not None
            self.balances[record.owner] = self.balances.get(record.owner, 0) + record.amount
            return TransactionStatus("confirmed")
        if record.step == "create_escrow":
            assert record.spec is not None and record.escrow is not None
            if record.escrow in self.escrows:
                return TransactionStatus("failed", "AccountAlreadyInUse")
            self.escrows[record.escrow] = EscrowState(
                address=record.escrow,
                status="created",
                chain_po_id=record.spec.chain_po_id,
                buyer=record.spec.buyer,
                mint=self.mint_address,
                total=record.spec.total,
                vault=self.vault_address(escrow=record.escrow),
            )
            return TransactionStatus("confirmed")
        assert record.escrow is not None and record.owner is not None
        escrow = self.escrows.get(record.escrow)
        if escrow is None:
            return TransactionStatus("failed", "AccountNotInitialized")
        if escrow.status != "created":
            return TransactionStatus("failed", "AlreadyFunded")
        if self.balances.get(record.owner, 0) < escrow.total:
            return TransactionStatus("failed", "InsufficientFunds")
        self.balances[record.owner] -= escrow.total
        self.balances[escrow.vault] = self.balances.get(escrow.vault, 0) + escrow.total
        self.escrows[record.escrow] = EscrowState(
            address=escrow.address,
            status="funded",
            chain_po_id=escrow.chain_po_id,
            buyer=escrow.buyer,
            mint=escrow.mint,
            total=escrow.total,
            vault=escrow.vault,
        )
        return TransactionStatus("confirmed")

    async def status(self, signature: str) -> TransactionStatus:
        if self.node_behind:
            self.node_behind -= 1
            raise ProviderError("solana", "getSignatureStatuses", "node_behind", retryable=True)
        return self.landed.get(signature, TransactionStatus("unknown"))

    async def block_height(self) -> int:
        return self.height

    async def escrow(self, address: str) -> EscrowState | None:
        return self.escrows.get(address)

    def land_dropped(self, signature: str) -> None:
        """A dropped transaction lands after all (it was only slow)."""
        record = self.sent[signature]
        self.landed[signature] = self._apply(record)


@dataclass
class FakeProviders:
    card: FakeCardGateway = field(default_factory=FakeCardGateway)
    bank_details: FakeBankDetailsCipher = field(default_factory=FakeBankDetailsCipher)
    wallets: FakeWalletProvider = field(default_factory=FakeWalletProvider)
    chain: FakePaymentChain = field(default_factory=FakePaymentChain)

    def bundle(self) -> PaymentProviders:
        return PaymentProviders(
            card=self.card,
            bank_details=self.bank_details,
            wallets=self.wallets,
            chain=self.chain,
        )
