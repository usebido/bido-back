"""Request models and the `pay_purchase_order` act tool's arguments and result.

Decimals travel as strings inside approval args (jsonb rewrites floats, and an
approval's args must hash the same after storage); the token amount, a u64,
travels as a string of digits. No `to`/`cc`/`suppliers` keys: the outbound
recipient guard reads those as email recipients.
"""

import re
from datetime import date, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from bido_back.awards.views import DecimalText
from bido_back.payments.views import PaymentView

Hex64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Digits = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,19}$")]
ChainPoId = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9-]{1,27}-[0-9A-HJKMNP-TV-Z]{4}$")]
CountryCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]

_CNAPS = re.compile(r"[0-9]{12}", re.ASCII)
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

BeneficiaryType = Literal["business", "individual"]

MAX_MILESTONES = 4
MAX_MILESTONE_NAME_BYTES = 24
BPS_TOTAL = 10_000


class MilestoneInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=MAX_MILESTONE_NAME_BYTES)
    bps: int = Field(ge=1, le=BPS_TOTAL)

    @model_validator(mode="after")
    def _fits_on_chain(self) -> Self:
        if len(self.name.encode("utf-8")) > MAX_MILESTONE_NAME_BYTES:
            raise ValueError(f"a milestone name is at most {MAX_MILESTONE_NAME_BYTES} bytes")
        if self.name != self.name.strip():
            raise ValueError("a milestone name has no leading or trailing spaces")
        return self


def check_milestones(milestones: list[MilestoneInput]) -> list[MilestoneInput]:
    if not 1 <= len(milestones) <= MAX_MILESTONES:
        raise ValueError(f"between 1 and {MAX_MILESTONES} milestones")
    if sum(m.bps for m in milestones) != BPS_TOTAL:
        raise ValueError(f"milestone shares (bps) must add up to {BPS_TOTAL}")
    if len({m.name for m in milestones}) != len(milestones):
        raise ValueError("milestone names must be different")
    return milestones


# --- Requests -------------------------------------------------------------------------------


class EnrollPaymentMethodRequest(BaseModel):
    """A card the browser tokenized with Basis Theory Elements (never card data)."""

    model_config = ConfigDict(extra="forbid")

    token_id: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="The Basis Theory token id returned by `bt.tokens.create`",
    )


class CreatePayoutAccountRequest(BaseModel):
    """A supplier's bank account. Stored sealed for the payout provider; shown masked only."""

    model_config = ConfigDict(extra="forbid")

    beneficiary_name: str = Field(min_length=2, max_length=140)
    bank_name: str = Field(min_length=2, max_length=140)
    account_number: str = Field(min_length=4, max_length=40, pattern=r"^[0-9A-Za-z][0-9A-Za-z -]*$")
    country: CountryCode
    currency: CurrencyCode
    swift_bic: str | None = Field(
        default=None,
        pattern=r"^[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?$",
        description="SWIFT/BIC (8 or 11 characters)",
    )
    bank_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=20,
        pattern=r"^[0-9A-Za-z-]+$",
        description="Local clearing code: the 12-digit CNAPS code for a bank in China",
    )
    bank_address: str | None = Field(default=None, max_length=200)
    beneficiary_address: str | None = Field(default=None, max_length=200)
    beneficiary_type: BeneficiaryType = Field(
        default="business", description="A company, or a person (who needs `national_id`)"
    )
    beneficiary_name_local: str | None = Field(
        default=None,
        min_length=2,
        max_length=140,
        description="The beneficiary's name in the local script: in Chinese characters, "
        "required for a CNY account in China",
    )
    bank_name_local: str | None = Field(
        default=None,
        min_length=2,
        max_length=140,
        description="The bank's name in the local script: in Chinese characters, required "
        "for a CNY account in China",
    )
    national_id: str | None = Field(
        default=None,
        min_length=4,
        max_length=40,
        pattern=r"^[0-9A-Za-z-]+$",
        description="The person's national identification number; required for an individual",
    )

    @model_validator(mode="after")
    def _routable(self) -> Self:
        if self.swift_bic is None and self.bank_code is None:
            raise ValueError("give the SWIFT/BIC or the local bank code (CNAPS in China)")
        code = self.bank_code
        if self.country == "CN" and code is not None and not _CNAPS.fullmatch(code):
            raise ValueError("a CNAPS code has 12 digits")
        if self.beneficiary_type == "individual" and self.national_id is None:
            raise ValueError("an individual beneficiary needs their national identification number")
        if self.country == "CN" and self.currency == "CNY":
            # Paid locally through CNAPS, not SWIFT; the payout provider takes the names as
            # registered in China.
            if code is None:
                raise ValueError("a CNY account in China is paid through CNAPS: give its code")
            for field, value in (
                ("beneficiary_name_local", self.beneficiary_name_local),
                ("bank_name_local", self.bank_name_local),
            ):
                if value is None or not _HAN.search(value):
                    raise ValueError(f"a CNY account in China needs {field} in Chinese characters")
        return self


class PayPurchaseOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payment_method_id: UUID
    payout_account_id: UUID
    milestones: list[MilestoneInput] | None = Field(
        default=None,
        description="How the escrow releases the payment (1-4 milestones adding up to 10000 "
        "bps). Default: one milestone, 'Delivery', 100%",
    )

    @model_validator(mode="after")
    def _milestones(self) -> Self:
        if self.milestones is not None:
            check_milestones(self.milestones)
        return self


# --- The act tool ---------------------------------------------------------------------------


class PayPurchaseOrderArgs(BaseModel):
    """Exactly what the approver approves: charge this card this amount, then fund the escrow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    payment_id: UUID
    purchase_order_id: UUID
    purchase_order_ref: str
    rfq_id: UUID
    invite_id: UUID
    supplier_id: UUID
    # The payer: the person who asked. The executor runs as the approver.
    requested_by: UUID
    # The PO document's hash (awards.render.content_hash): a changed PO is not paid.
    content_hash: Hex64
    po_currency: CurrencyCode
    po_total: DecimalText
    charge_currency: Literal["USD"]
    charge_amount: DecimalText
    fx_rate: DecimalText | None
    fx_rate_date: date | None
    quote_expires_at: datetime
    token_amount: Digits
    payment_method_id: UUID
    card_brand: str | None
    card_last4: str | None
    payout_account_id: UUID
    payout_fingerprint: Hex64
    payout_currency: CurrencyCode
    payout_account_last4: str
    chain_po_id: ChainPoId
    milestones: list[MilestoneInput]
    charge_mode: Literal["stripe", "simulated"]


PayOutcome = Literal["charged", "already_charged", "declined", "expired", "skipped", "failed"]


class PayPurchaseOrderResult(BaseModel):
    outcome: PayOutcome
    payment_id: UUID
    failure_code: str | None = None


class PaymentRequested(BaseModel):
    """A payment proposed: a person decides it at POST /v1/approvals/{approval_id}/decision."""

    approval_id: UUID
    payment: PaymentView
