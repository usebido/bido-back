"""Explicit buyer priorities and observable supplier performance, without scoring weights."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ComparisonPolicy(BaseModel):
    """Hard thresholds are checked before the declared lexicographic preference.

    Reliability-first sorts by evidenced on-time rate, then accepted-quantity
    rate, then price. Unknown or insufficient samples rank below known rates;
    they never become perfect performance. Rates are fractions, not percentages.
    """

    model_config = ConfigDict(extra="forbid")

    preference: Literal["cost_first", "reliability_first"] = "cost_first"
    minimum_on_time_rate: Decimal | None = Field(default=None, ge=0, le=1)
    minimum_accepted_quantity_rate: Decimal | None = Field(default=None, ge=0, le=1)
    minimum_history_samples: int = Field(default=3, ge=1, le=10000)
    history_window_days: int = Field(default=365, ge=1, le=3650)
    allow_unknown_history: bool = True
    require_on_time_delivery: bool = False
    require_complete_costs: bool = False


HistoryBasis = Literal["imported_history", "source_assessment"]


class SupplierPerformance(BaseModel):
    """Observed purchase-line outcomes for this supplier and requested item only.

    `basis` says where they come from: purchase orders imported into Bido
    (`imported_history`, evidenced by `evidence_po_line_ids`), or, when none is
    in the policy window, the supplier statistics measured by the source
    assessment the RFQ line was drafted from (`source_assessment`, evidenced by
    the line's `source_evidence`). The two are never mixed.
    """

    scope: Literal["requested_item"] = "requested_item"
    basis: HistoryBasis = "imported_history"
    observations: int = 0
    delivery_samples: int = 0
    on_time_rate: Decimal | None = None
    quality_samples: int = 0
    accepted_quantity_rate: Decimal | None = None
    latest_observation_on: date | None = None
    oldest_observation_on: date | None = None
    excluded_stale_observations: int = 0
    evidence_po_line_ids: list[UUID] = Field(default_factory=list[UUID])
    issues: list[str] = Field(default_factory=list[str])


@dataclass(frozen=True, slots=True)
class PurchaseObservation:
    po_line_id: UUID
    ordered_at: date
    quantity: Decimal
    promised_at: date | None
    received_at: date | None
    received_qty: Decimal | None
    rejected_qty: Decimal


def summarize_performance(
    observations: Sequence[PurchaseObservation], *, today: date, window_days: int
) -> SupplierPerformance:
    """Missing receipt, promised date or quantities remain unknown.

    Delivery is measured on completed receipt lines; an overdue open line is
    also a failure. Future/open lines are not successful delivery samples.
    A partial receipt never counts as complete on-time fulfillment.
    """
    cutoff = today - timedelta(days=window_days)
    current: list[PurchaseObservation] = []
    stale = 0
    for row in observations:
        if row.ordered_at > today:
            continue
        observed = (
            row.received_at if row.received_at and row.received_at <= today else row.ordered_at
        )
        if observed < cutoff:
            stale += 1
        else:
            current.append(row)
    delivery: list[bool] = []
    accepted = Decimal(0)
    received = Decimal(0)
    quality_samples = 0
    issues: set[str] = set()
    for row in current:
        arrived = row.received_at is not None and row.received_at <= today
        if row.promised_at is not None and (arrived or row.promised_at < today):
            complete = arrived and row.received_qty is not None and row.received_qty >= row.quantity
            # A dated receipt without received quantity cannot establish completeness.
            if row.received_qty is None and arrived:
                continue
            delivery.append(
                bool(
                    complete and row.received_at is not None and row.received_at <= row.promised_at
                )
            )
        if arrived and row.received_qty is not None and row.received_qty > 0:
            if row.rejected_qty < 0 or row.rejected_qty > row.received_qty:
                issues.add("invalid_received_quality_quantities")
                continue
            quality_samples += 1
            received += row.received_qty
            accepted += row.received_qty - row.rejected_qty
    dates = [
        row.received_at if row.received_at and row.received_at <= today else row.ordered_at
        for row in current
    ]
    return SupplierPerformance(
        observations=len(current),
        delivery_samples=len(delivery),
        on_time_rate=Decimal(sum(delivery)) / Decimal(len(delivery)) if delivery else None,
        quality_samples=quality_samples,
        accepted_quantity_rate=accepted / received if received > 0 else None,
        latest_observation_on=max(dates) if dates else None,
        oldest_observation_on=min(dates) if dates else None,
        excluded_stale_observations=stale,
        evidence_po_line_ids=[row.po_line_id for row in current],
        issues=sorted(issues),
    )


def evidence_issues(history: SupplierPerformance, policy: ComparisonPolicy) -> list[str]:
    issues = list(history.issues)
    if history.delivery_samples < policy.minimum_history_samples or history.on_time_rate is None:
        issues.append("delivery_history_insufficient")
    if (
        history.quality_samples < policy.minimum_history_samples
        or history.accepted_quantity_rate is None
    ):
        issues.append("quality_history_insufficient")
    if history.excluded_stale_observations:
        issues.append("stale_history_excluded")
    return issues
