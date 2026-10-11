"""What the API shows of payments. No imports from the awards or RFQ modules (no cycles):
the purchase-order view and the approval queue import these."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

PaymentState = Literal[
    "pending_approval",
    "charging",
    "charged",
    "minted",
    "escrow_created",
    "funded",
    "rejected",
    "expired",
    "declined",
    "failed",
]
# The payment's own steps. Milestone releases are shown on each milestone
# (`GET .../payment/milestones`: `release_signature`, `release_explorer_url`).
ChainStep = Literal["mint", "create_escrow", "fund"]
ChainTransactionState = Literal["sent", "confirmed", "failed", "expired"]
PaymentMethodStatus = Literal["active", "pending", "failed", "removed"]
PayoutAccountStatus = Literal["active", "disabled"]


class PaymentMilestone(BaseModel):
    """One milestone of the escrow: its share is released when its gate is attested."""

    name: str
    bps: int = Field(description="Share of the total in basis points; all add up to 10000")
    key: str | None = Field(
        default=None,
        description="Its milestone in Bido's tooling plan (`tool_start`, `t1`, `ppap`), which "
        "fixes what the factory sends for it; null for a custom milestone",
    )


class PaymentChainTransactionView(BaseModel):
    step: ChainStep
    attempt: int
    signature: str
    state: ChainTransactionState
    explorer_url: str
    error: str | None
    created_at: datetime


class PaymentMethodDisplayView(BaseModel):
    """How the issuer brands the card, to draw it: every field may be null."""

    art_url: str | None = Field(description="The card face image, an https URL")
    background_color: str | None = Field(description="`#rrggbb`")
    foreground_color: str | None = Field(description="`#rrggbb`, for text on the background")
    description: str | None = Field(description="The issuer's name for the card product")
    issuer_name: str | None


class PaymentCardView(BaseModel):
    payment_method_id: UUID
    brand: str | None
    last4: str | None
    display: PaymentMethodDisplayView | None


class PaymentCardVerificationView(BaseModel):
    """The cardholder's Visa passkey approval of this payment (`card_rail` `vic` only).

    The browser runs it with Basis Theory's `@basis-theory/web-agentic` SDK:
    `AgenticVerification({apiKey: <public key>, ...}).verifyAllowance(allowance_id,
    {rail, provider})`, then confirms at POST
    /v1/purchase-orders/{po_id}/payment/card-verification."""

    status: Literal["pending", "verified", "failed", "expired"] = Field(
        description="`verified` once the passkey approved it; `failed`/`expired`: request "
        "the payment again"
    )
    allowance_id: str | None = Field(
        description="The Basis Theory allowance to verify; null until it is created"
    )
    rail: str = Field(description="Pass to `verifyAllowance` as is (`agentic-token`)")
    provider: str = Field(description="Pass to `verifyAllowance` as is (`vic`)")
    verified_at: datetime | None


class IssuedCardView(BaseModel):
    """The single-use virtual card Visa issued for the agent: never its number or code."""

    brand: Literal["visa"]
    last4: str | None
    exp_month: int | None
    exp_year: int | None


class PaymentPayoutView(BaseModel):
    payout_account_id: UUID
    currency: str
    country: str
    bank_name: str
    account_last4: str


class PaymentTokenView(BaseModel):
    mint: str | None = Field(description="The stablecoin mint address")
    decimals: int
    amount: Decimal = Field(description="Tokens minted and locked, in major units (USD)")


class PaymentChainView(BaseModel):
    cluster: str
    chain_po_id: str
    wallet_address: str | None = Field(description="The company's wallet (it signs `fund`)")
    escrow_address: str | None
    vault_address: str | None
    refund_after: datetime | None
    transactions: list[PaymentChainTransactionView]


class PaymentView(BaseModel):
    id: UUID
    purchase_order_id: UUID
    purchase_order_ref: str
    state: PaymentState
    approval_id: UUID
    requested_by: UUID
    po_currency: str
    po_total: Decimal
    charge_currency: str
    charge_amount: Decimal
    fx_rate: Decimal | None = Field(
        description="USD per unit of the PO currency (ECB reference rates); null when the PO "
        "is in USD"
    )
    fx_rate_date: date | None
    quote_expires_at: datetime
    simulated: bool = Field(
        description="The card charge was simulated (Basis Theory test tenant): no money moved"
    )
    card: PaymentCardView
    card_rail: Literal["spt", "vic"] = Field(
        description="`vic`: the buyer approves the payment with the Visa passkey "
        "(`card_verification`) and the agent gets a single-use virtual card (`agent_card`)"
    )
    card_verification: PaymentCardVerificationView | None = Field(
        description="The Visa passkey approval; null on the `spt` rail"
    )
    agent_card: IssuedCardView | None = Field(
        description="The virtual card minted for the agent once the payment is executed"
    )
    payout: PaymentPayoutView
    token: PaymentTokenView
    milestones: list[PaymentMilestone]
    chain: PaymentChainView
    failure_code: str | None
    needs_attention: bool = Field(
        description="The card was charged but the escrow was not funded: a person must act"
    )
    created_at: datetime
    charged_at: datetime | None
    funded_at: datetime | None
    completed_at: datetime | None = Field(
        default=None,
        description="When the escrow released its last milestone; the payment stays `funded`",
    )


class PurchaseOrderPaymentSummary(BaseModel):
    """The PO's live payment (or its latest), as the purchase-order view shows it."""

    payment_id: UUID
    state: PaymentState
    charge_currency: str
    charge_amount: Decimal
    simulated: bool
    escrow_address: str | None
    funded_at: datetime | None
    needs_attention: bool
    released_bps: int = Field(
        default=0,
        description="Share of the total the escrow released so far, in basis points "
        "(10000: all of it)",
    )
    next_milestone: str | None = Field(
        default=None,
        description="The milestone to release next (its name) while the escrow is funded; "
        "null before it is funded and once everything is released",
    )


class PaymentApprovalView(BaseModel):
    """What an approver sees for a `pay_purchase_order` approval."""

    payment_id: UUID
    purchase_order_id: UUID
    purchase_order_ref: str
    supplier_name: str | None
    po_currency: str
    po_total: Decimal
    charge_currency: str
    charge_amount: Decimal
    fx_rate: Decimal | None = Field(
        description="USD per unit of the PO currency (ECB reference rates); null when the PO "
        "is in USD"
    )
    fx_rate_date: date | None
    quote_expires_at: datetime
    card_brand: str | None
    card_last4: str | None
    payout_currency: str
    payout_account_last4: str
    milestones: list[PaymentMilestone]
    simulated: bool
    card_rail: Literal["spt", "vic"] = Field(
        description="`vic`: the buyer approves the card with the Visa passkey first"
    )
    card_verified: bool | None = Field(
        description="`vic`: whether the buyer's Visa passkey approved the card; approving "
        "before it fails the payment (`card_not_verified`). Null on the `spt` rail"
    )


class PaymentMethodView(BaseModel):
    id: UUID
    brand: str | None
    last4: str | None
    exp_month: int | None
    exp_year: int | None
    funding: str | None
    display: PaymentMethodDisplayView | None = Field(
        description="The issuer's branding of the card; null when the provider gave none"
    )
    status: PaymentMethodStatus
    created_by: UUID
    created_at: datetime


class PayoutAccountView(BaseModel):
    id: UUID
    supplier_id: UUID
    method: Literal["bank_transfer"]
    country: str
    currency: str
    beneficiary_display: str = Field(description="The beneficiary's name, masked")
    bank_name: str
    swift_bic: str | None
    account_last4: str
    status: PayoutAccountStatus
    created_by: UUID
    created_at: datetime
    disabled_at: datetime | None


class CardSetupView(BaseModel):
    provider: Literal["basis_theory"]
    environment: Literal["test", "production"]
    public_key: str | None = Field(description="Basis Theory public key for Elements")
    simulated: bool
    rail: Literal["spt", "vic"] = Field(
        description="`vic`: each payment is approved with the Visa passkey "
        "(`@basis-theory/web-agentic`)"
    )
    agentic_api_url: str = Field(
        description="`apiBaseUrl` for `AgenticVerification` (Basis Theory's agentic API)"
    )


class ChainSetupView(BaseModel):
    cluster: str
    escrow_program_id: str
    stablecoin_mint: str | None
    stablecoin_decimals: int


class PaymentsConfigView(BaseModel):
    enabled: bool = Field(description="Every payment provider is set up on this server")
    card: CardSetupView | None
    chain: ChainSetupView | None
