"""What the payment flow needs from the outside world, in our own types.

Basis Theory, Stripe, Privy and Solana stay behind these protocols: their
request and response shapes never leave `bido_back.payments.providers` and
`bido_back.payments.chain`. The service, the `pay_purchase_order` act tool and
the post-charge pipeline only see the dataclasses below, so tests run them
against fakes.

Amounts: card amounts are `Decimal` in major units of their currency (USD
cents precision); token amounts are `int` base units of the stablecoin mint (6
decimals). Addresses and signatures are base58 strings.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final, Literal, Protocol
from uuid import UUID

# --- Card: Basis Theory Agentic Payments, charged on Bido's Stripe account -----------


@dataclass(frozen=True, slots=True)
class CardConsumer:
    """The person who enrolls a card (Basis Theory wants a UUID consumer id)."""

    id: UUID
    email: str | None
    country_code: str  # ISO 3166-1 alpha-2


CardStatus = Literal["active", "pending", "failed"]
# Where a payment's card credential comes from: a Stripe Shared Payment Token (`spt`) or a
# Visa Intelligent Commerce virtual card the cardholder approves with a passkey (`vic`).
CardRail = Literal["spt", "vic"]
# Basis Theory's (rail, provider) for each: what the browser SDK is told to verify.
SPT_RAIL: Final = ("spt", "stripe")
VIC_RAIL: Final = ("agentic-token", "vic")
# The cardholder's approval of one allowance with the network's passkey.
CardVerificationStatus = Literal["pending", "verified", "failed", "expired"]


@dataclass(frozen=True, slots=True)
class CardDisplayData:
    """How the issuer brands the card (Basis Theory `card.display`): public, never card data.

    Built only by `payments.card_display`, which keeps well-formed values: every field
    is optional, and a card without any is `None` rather than an all-empty display.
    """

    art_url: str | None  # the card face, an https URL
    background_color: str | None  # `#rrggbb`
    foreground_color: str | None  # `#rrggbb`
    description: str | None  # the issuer's name for the product ("Sapphire Preferred")
    issuer_name: str | None


@dataclass(frozen=True, slots=True)
class EnrolledCard:
    """A card vaulted at Basis Theory and enrolled as an agentic payment method."""

    payment_method_ref: str  # Basis Theory `pm_...`
    fingerprint: str  # the vault token's fingerprint: one card per tenant
    brand: str | None
    last4: str | None
    exp_month: int | None
    exp_year: int | None
    funding: str | None  # credit | debit | prepaid | None
    # The card on the gateway's rail: active, still provisioning, or refused.
    status: CardStatus
    display: CardDisplayData | None


@dataclass(frozen=True, slots=True)
class CardAuthorization:
    """A Basis Theory allowance: the ceiling, merchant and expiry one payment may use."""

    ref: str  # `alw_...`
    usable: bool  # the gateway's rail is active on it (`vic`: once the passkey approved it)
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class CardVerification:
    """Where the cardholder's passkey approval of an allowance stands, and what the
    browser's Basis Theory SDK needs to run it (`verifyAllowance(ref, {rail, provider})`)."""

    status: CardVerificationStatus
    rail: str  # Basis Theory's rail name, e.g. `agentic-token`
    provider: str  # e.g. `vic`


@dataclass(frozen=True, slots=True)
class IssuedCard:
    """The single-use virtual card minted for the agent: what may be shown of it.

    Its number and security code are never kept: only the last four digits and the
    expiry, which identify it on the buyer's screen."""

    credential_ref: str | None  # Basis Theory `cred_...`
    last4: str | None
    exp_month: int | None
    exp_year: int | None


ChargeOutcome = Literal["succeeded", "declined", "pending"]


@dataclass(frozen=True, slots=True)
class CardCharge:
    outcome: ChargeOutcome
    reference: str | None  # Stripe `pi_...`, or `sim_...` for a simulated charge
    decline_code: str | None  # a code we wrote (`card_declined`, ...), never provider prose
    simulated: bool
    # `vic`: the virtual card minted for the agent (None when a replay could not read it).
    issued_card: IssuedCard | None = None


class CardGateway(Protocol):
    """Enrolls cards and charges one payment, exactly once per payment id.

    `spt`: `charge` mints the single-use Stripe Shared Payment Token from the
    allowance and charges it on Bido's Stripe account in the same call: the token
    never leaves the gateway and is never stored. Replaying a payment's charge
    returns the charge already made (looked up by payment id); when the token was
    minted but no charge exists, it raises `CardCredentialLost` (nothing was charged).

    `vic`: the cardholder approves the allowance with the Visa passkey in the
    browser first; `charge` then mints the single-use virtual card for the agent
    (`CardNotVerified` while the passkey has not approved it). Nothing charges that
    card yet: the charge is simulated, and only its last four digits and expiry
    leave the gateway.
    """

    @property
    def simulated(self) -> bool:
        """True when charges are simulated (Basis Theory test tenant, devnet only)."""
        ...

    @property
    def rail(self) -> CardRail:
        """The rail the card credential is minted on."""
        ...

    async def enroll(
        self, *, token_id: str, consumer: CardConsumer, idempotency_key: str
    ) -> EnrolledCard: ...

    async def remove(self, payment_method_ref: str) -> None: ...

    async def display(self, payment_method_ref: str) -> CardDisplayData | None:
        """The card's branding as the provider tells it now; None when it has none or the
        payment method is gone."""
        ...

    async def authorize(
        self,
        *,
        payment_method_ref: str,
        amount: Decimal,
        currency: str,
        description: str,
        expires_at: datetime,
        idempotency_key: str,
    ) -> CardAuthorization: ...

    async def verification(self, authorization_ref: str) -> CardVerification:
        """Where the cardholder's passkey approval of the allowance stands (read live)."""
        ...

    async def charge(
        self,
        *,
        authorization_ref: str,
        payment_id: UUID,
        amount: Decimal,
        currency: str,
        description: str,
        idempotency_key: str,
    ) -> CardCharge: ...


# --- Supplier bank details: sealed in Bido's database until the payout provider has them ---


@dataclass(frozen=True, slots=True)
class BankAccountDetails:
    """A supplier's payout account as the buyer typed it. Stored only sealed (encrypted)."""

    beneficiary_name: str
    bank_name: str
    account_number: str
    country: str  # ISO 3166-1 alpha-2, e.g. "CN"
    currency: str  # ISO 4217, e.g. "CNY"
    swift_bic: str | None
    bank_code: str | None  # the local clearing code: CNAPS for China
    bank_address: str | None
    beneficiary_address: str | None
    beneficiary_type: str  # "business" or "individual"
    beneficiary_name_local: str | None  # in the local script: Chinese characters for China
    bank_name_local: str | None
    national_id: str | None  # an individual's national identification number


class BankDetailsCipher(Protocol):
    """Seals a supplier's bank details for Bido's database, bound to the tenant and supplier.

    The payout provider (Tazapay, next step) is who pays the supplier's account, so it is
    where the details end up; until it is integrated they wait here, encrypted. A sealed
    value opens only with the same tenant and supplier: copied onto another row it fails.
    """

    async def seal(
        self, details: BankAccountDetails, *, tenant_id: UUID, supplier_id: UUID
    ) -> bytes: ...

    async def open(
        self, sealed: bytes, *, tenant_id: UUID, supplier_id: UUID
    ) -> BankAccountDetails: ...


# --- The company's invisible wallet: a Privy server wallet -------------------------------


@dataclass(frozen=True, slots=True)
class CustodyWallet:
    ref: str  # the Privy wallet id
    address: str  # base58 Solana address


class WalletProvider(Protocol):
    async def ensure_wallet(self, *, tenant_id: UUID) -> CustodyWallet:
        """The tenant's wallet, created on first use (idempotent: one per tenant)."""
        ...

    async def sign_transaction(
        self, *, wallet_ref: str, transaction: bytes, idempotency_key: str
    ) -> bytes:
        """Sign a serialized transaction with the wallet; returns the signed wire bytes."""
        ...


# --- Solana: our stablecoin mint and the `bido_escrow` program ---------------------------


@dataclass(frozen=True, slots=True)
class SignedTransaction:
    signature: str  # the fee payer's signature = the transaction id
    wire: bytes
    last_valid_block_height: int


@dataclass(frozen=True, slots=True)
class UnsignedTransaction:
    """A transaction whose message is final, waiting for the buyer wallet's signature."""

    wire: bytes  # every signature slot zeroed
    last_valid_block_height: int


TransactionState = Literal["confirmed", "failed", "unknown"]


@dataclass(frozen=True, slots=True)
class TransactionStatus:
    state: TransactionState
    error: str | None = None  # the program error name or a code, never logs


@dataclass(frozen=True, slots=True)
class EscrowMilestone:
    name: str  # 1..24 UTF-8 bytes
    bps: int  # all milestones sum to 10_000


@dataclass(frozen=True, slots=True)
class EscrowSpec:
    """Everything fixed on chain by `create_po`; nothing here changes afterwards."""

    chain_po_id: str
    buyer: str
    po_hash: bytes  # 32 bytes
    cad_hash: bytes  # 32 bytes (zeros: no drawing)
    total: int  # base units
    milestones: tuple[EscrowMilestone, ...]
    refund_after: int  # unix seconds
    payout_ref: str  # 64 hex chars, written as the `bido:payout_ref:` memo


EscrowStatus = Literal["created", "funded", "completed", "refunded"]


@dataclass(frozen=True, slots=True)
class EscrowState:
    address: str
    status: EscrowStatus
    chain_po_id: str
    buyer: str
    mint: str
    total: int
    vault: str


class PaymentChain(Protocol):
    """Builds, signs, sends and reads the payment's transactions.

    The relayer pays every fee and rent. Builders return fully signed
    transactions except `build_fund`, which the buyer's wallet signs remotely
    and `complete_fund` finishes. Nothing here retries or decides: the
    pipeline persists each signature before `send`, and asks `status`.
    """

    @property
    def cluster(self) -> str: ...

    @property
    def mint(self) -> str: ...

    def escrow_address(self, *, buyer: str, chain_po_id: str) -> str: ...

    def vault_address(self, *, escrow: str) -> str: ...

    async def build_mint(self, *, owner: str, amount: int, memo: str) -> SignedTransaction: ...

    async def build_create_escrow(self, spec: EscrowSpec) -> SignedTransaction: ...

    async def build_fund(self, *, buyer: str, escrow: str) -> UnsignedTransaction: ...

    def complete_fund(
        self, unsigned: UnsignedTransaction, *, buyer: str, signed_by_buyer: bytes
    ) -> SignedTransaction:
        """Add the relayer's signature to the wallet-signed fund transaction.

        Raises `ProviderError("wallet", "sign_transaction", ...)` when the wallet's answer
        changed the message or carries no valid signature (nothing was sent).
        """
        ...

    async def send(self, transaction: SignedTransaction) -> None:
        """Send once. "Already processed" is success; a rejected preflight raises a
        non-retryable `ProviderError` (nothing was forwarded); a timeout or network
        error is retryable and means it MAY have been sent."""
        ...

    async def status(self, signature: str) -> TransactionStatus:
        """With the transaction history. May raise a retryable `ProviderError`
        (`node_behind`) when the node lags behind a finalized slot already seen."""
        ...

    async def block_height(self) -> int:
        """The FINALIZED block height: only past `last_valid_block_height` here can a
        transaction be declared expired (after one more `status`)."""
        ...

    async def escrow(self, address: str) -> EscrowState | None: ...
