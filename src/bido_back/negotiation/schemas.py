"""API shapes of the negotiation: settings, per-supplier state, messages, outcome."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from bido_back.comparison.compute import DecisionOffer, ExcludedOffer, OfferAlternative
from bido_back.comparison.policy import ComparisonPolicy, SupplierPerformance

NegotiationStatus = Literal["not_started", "open", "ready_to_close", "closed"]
InviteNegotiationStatus = Literal[
    "open", "countered", "bafo_requested", "accepted", "rejected", "closed"
]
NegotiationActionName = Literal["counter", "bafo"]
NegotiationMessageStatus = Literal["proposed", "sent", "canceled", "rejected"]
LineDecisionName = Literal["no_offer", "blocked", "accept", "above_max", "counter"]
# Where one supplier stands, as the buyer reads it (`bido_back.negotiation.planner`).
SupplierPhase = Literal[
    "not_started",
    "waiting_for_supplier",
    "waiting",
    "held_for_approval",
    "accepted_internally",
    "needs_decision",
    "needs_answer",
    "closed",
]
NextActionName = Literal[
    "wait",
    "clarify",
    "revised_offer",
    "delivery_date",
    "bafo",
    "reminder",
    "accept",
    "hold",
    "close",
]
# `reminder`: the one reminder of an unanswered revised-offer or delivery-date request.
EmailKindName = Literal["revised_offer", "delivery_date", "bafo", "reminder"]
# A negotiation email's exact state (`status` keeps the older, coarser values).
EmailStateName = Literal["decided", "proposed", "sent", "canceled", "rejected", "held"]
LinePriceStatus = Literal["in_target", "ask_met", "in_range", "above_max", "no_target"]
LineDeliveryStatus = Literal[
    "on_time", "no_need_by", "unknown", "late", "still_late", "need_by_unreachable"
]
LinePlanStatus = Literal["not_quoted", "declined", "blocked", "question_open", "priced"]


class LineLimitsView(BaseModel):
    rfq_line_id: UUID
    position: int
    description: str
    mpn: str | None
    # In the RFQ's base currency. Suppliers are told the target, never the max.
    target_unit_price: Decimal | None
    max_unit_price: Decimal | None
    max_lead_time_days: int | None


class NegotiationSettings(BaseModel):
    deadline: datetime | None
    max_rounds: int
    early_close: bool
    base_currency: str
    lines: list[LineLimitsView]
    comparison_policy: ComparisonPolicy = Field(default_factory=ComparisonPolicy)


class LineLimitsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rfq_line_id: UUID
    target_unit_price: Decimal | None = Field(default=None, gt=0)
    max_unit_price: Decimal | None = Field(default=None, gt=0)
    max_lead_time_days: int | None = Field(default=None, ge=0, le=3650)

    @model_validator(mode="after")
    def _target_within_max(self) -> "LineLimitsUpdate":
        if (
            self.target_unit_price is not None
            and self.max_unit_price is not None
            and self.target_unit_price > self.max_unit_price
        ):
            raise ValueError("target_unit_price must not exceed max_unit_price")
        return self


class NegotiationSettingsUpdate(BaseModel):
    """PUT: replaces the settings given. Lines not listed keep their limits; a line
    listed has all three limits replaced. Saving opens the negotiation."""

    model_config = ConfigDict(extra="forbid")

    deadline: datetime | None = None
    # One revised-offer round, then one best-and-final request, by default.
    max_rounds: int = Field(default=2, ge=1, le=10)
    early_close: bool = True
    lines: list[LineLimitsUpdate] = Field(default_factory=list[LineLimitsUpdate], max_length=500)
    comparison_policy: ComparisonPolicy | None = None


class MessageAskView(BaseModel):
    """What one negotiation email asks for one position (decided by code)."""

    position: int
    ask_unit_price: Decimal | None
    # A better price is asked for without a number (no target was set).
    best_price: bool = False
    # Delivery by this date is asked for.
    deliver_by: date | None = None


class NegotiationMessageView(BaseModel):
    id: UUID
    # The negotiation round: emails sent before it, plus one (a replacement repeats it).
    round: int
    action: NegotiationActionName
    # `decided` reads as `proposed` and `held` as `canceled` here; `state` is exact.
    status: NegotiationMessageStatus
    approval_id: UUID | None
    proposal_id: UUID
    canceled_reason: str | None
    created_at: datetime
    sent_at: datetime | None
    # The email's number for this supplier, counting every email ever decided.
    sequence: int = 0
    kind: EmailKindName | None = None
    state: EmailStateName = "proposed"
    reply_by: date | None = None
    # A best-and-final request's cut-off (the same for every supplier of the RFQ).
    cutoff_at: datetime | None = None
    # Times rendered; `held_reason` says why it waits for the buyer.
    attempts: int = 0
    held_reason: str | None = None
    # A reminder: the sequence of the request it repeats.
    reminder_of: int | None = None
    asks: list[MessageAskView] = Field(default_factory=list[MessageAskView])


class LineDecisionView(BaseModel):
    rfq_line_id: UUID
    decision: LineDecisionName
    offered_unit_price_base: Decimal | None
    ask_unit_price: Decimal | None
    lead_time_days: int | None
    ask_lead_time_days: int | None


class LinePlanView(BaseModel):
    """One line of the supplier's current offer: price, delivery, and what Bido asks."""

    rfq_line_id: UUID
    position: int
    status: LinePlanStatus
    price: LinePriceStatus | None = None
    delivery: LineDeliveryStatus | None = None
    offered_unit_price_base: Decimal | None = None
    ask_unit_price: Decimal | None = None
    asks_best_price: bool = False
    # The date asked for, the date needed, and the supplier's expected delivery when
    # ordered today (the earliest feasible on its quote).
    deliver_by: date | None = None
    required_by: date | None = None
    expected_delivery: date | None = None
    latest_order_date: date | None = None
    # In target, on time, nothing open: acceptable as it is.
    acceptable: bool = False
    reasons: list[str] = Field(default_factory=list[str])


class SupplierStateView(BaseModel):
    """Where the supplier stands now, computed in code, and what Bido does next."""

    phase: SupplierPhase
    # One English sentence written by code from codes and dates (never supplier text).
    label: str
    # Stable code of the reason (e.g. no_progress, supplier_question, daily_limit).
    reason: str
    # Waiting until this moment (a reply-by date, the cut-off, the daily limit).
    until: datetime | None = None
    next_action: NextActionName
    # Accepted internally: order by this date to meet the need-by dates.
    order_by: date | None = None
    valid_until: date | None = None
    warnings: list[str] = Field(default_factory=list[str])


class NegotiationDecisionView(BaseModel):
    """The planner's last recorded decision for the supplier (by the sweep)."""

    action: NextActionName
    phase: SupplierPhase
    reason: str
    proposal_version: int | None = None
    decided_at: datetime


class ReleaseHoldRequest(BaseModel):
    """The offer the buyer reviewed (`latest_proposal_id`): released only if it is still
    the supplier's latest."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: UUID


class InviteNegotiation(BaseModel):
    invite_id: UUID
    supplier_name: str
    invite_status: str
    status: InviteNegotiationStatus
    rounds_used: int
    latest_proposal_id: UUID | None
    latest_proposal_version: int | None
    # How the latest offer stands against the limits right now (recomputed).
    lines: list[LineDecisionView]
    messages: list[NegotiationMessageView]
    strategy_reasons: list[str] = Field(default_factory=list[str])
    stalled_rounds: int = 0
    # What the buyer reads about this supplier now, and the planner's line view.
    state: SupplierStateView | None = None
    line_plans: list[LinePlanView] = Field(default_factory=list[LinePlanView])
    last_decision: NegotiationDecisionView | None = None


class OutcomeLine(BaseModel):
    rfq_line_id: UUID
    tier: int
    quantity: Decimal
    invite_id: UUID | None
    supplier_name: str | None
    effective_unit_price_base: Decimal | None
    lead_time_days: int | None
    within_target: bool | None
    # The recommended price is above the buyer's target: said, never hidden. It is within
    # the maximum (an offer above the maximum is never recommended); the buyer decides.
    above_target: bool = False
    justification: str
    readiness: Literal["ready_for_buyer_review", "needs_review", "no_feasible_offer"] = (
        "needs_review"
    )
    policy: ComparisonPolicy = Field(default_factory=ComparisonPolicy)
    policy_reasons: list[str] = Field(default_factory=list[str])
    decision_version: int = 1
    source_evidence: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    offer: DecisionOffer | None = None
    evidence_gaps: list[str] = Field(default_factory=list[str])
    alternatives: list[OfferAlternative] = Field(default_factory=list[OfferAlternative])
    excluded: list[ExcludedOffer] = Field(default_factory=list[ExcludedOffer])
    performance: SupplierPerformance = Field(default_factory=SupplierPerformance)
    proposal_id: UUID | None = None
    proposal_line_id: UUID | None = None
    evidence_message_ids: list[UUID] = Field(default_factory=list[UUID])
    landed_total_base: Decimal | None = None

    @model_validator(mode="before")
    @classmethod
    def _derive_above_target(cls, data: object) -> object:
        # An outcome stored before `above_target` existed: derived from `within_target`.
        if not isinstance(data, dict):
            return data
        fields = cast(dict[str, object], data)
        if "above_target" in fields:
            return fields
        return {**fields, "above_target": fields.get("within_target") is False}


class NegotiationWarningView(BaseModel):
    """Something the buyer should know before deciding (e.g. a need-by date at risk)."""

    code: str
    supplier_name: str | None = None
    position: int | None = None
    on: date | None = None
    text: str


class RfqNegotiation(BaseModel):
    rfq_id: UUID
    status: NegotiationStatus
    settings: NegotiationSettings
    ready_at: datetime | None
    closed_at: datetime | None
    invites: list[InviteNegotiation]
    # Set when the negotiation is closed: the best eligible offer per line and tier.
    outcome: list[OutcomeLine] | None
    # Set while the negotiation is ready to close: what closing now would keep (the best
    # eligible offer per line and tier, never one above the maximum; `above_target` says
    # when it is above the target).
    recommendation: list[OutcomeLine] | None = None
    # The one best-and-final cut-off for every supplier, once the final round started.
    cutoff_at: datetime | None = None
    warnings: list[NegotiationWarningView] = Field(default_factory=list[NegotiationWarningView])
