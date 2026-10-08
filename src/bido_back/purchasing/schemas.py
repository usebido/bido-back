"""API shapes for data imports and purchase analyses."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bido_back.purchasing.importers import TableImportType
from bido_back.rfqs.schemas import CurrencyCode, CurrencySource, RfqDetail


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Imports ------------------------------------------------------------------------------


class ImportRequest(_Strict):
    type: TableImportType
    # A file uploaded with POST /v1/onboarding/uploads (.xlsx or .csv).
    upload_id: UUID
    # Check the file and report what would be imported, without writing.
    dry_run: bool = False
    # How numbers like "1,500" are written when it is ambiguous; guessed otherwise.
    decimal_separator: Literal[",", "."] | None = None
    # The workbook sheet to read; the first one when omitted.
    sheet: str | None = Field(default=None, max_length=100)


class ImportRowError(BaseModel):
    row: int
    field: str | None
    message: str


class ImportReport(BaseModel):
    type: TableImportType
    dry_run: bool
    rows_total: int
    rows_imported: int
    rows_failed: int
    # Field -> the header it was read from.
    columns: dict[str, str]
    missing_required: list[str]
    unknown_columns: list[str]
    errors: list[ImportRowError]
    created: dict[str, int]
    updated: dict[str, int]
    # Rows left out on purpose, e.g. {"po_lines_duplicate": 2}.
    skipped: dict[str, int] = {}


class ProductView(BaseModel):
    product: str
    bom_items: int
    planned_quantity: Decimal | None
    next_need_by: date | None


# --- Analyses -----------------------------------------------------------------------------


class AnalysisRequest(_Strict):
    product: str = Field(min_length=1, max_length=200)
    # Units to build; dated production demand in the planning window when omitted.
    quantity: Decimal | None = Field(default=None, gt=0)
    need_by: date | None = None
    safety_stock_pct: Decimal = Field(default=Decimal(0), ge=0, le=100)
    lookback_days: int = Field(default=365, ge=30, le=1095)
    # Demand dates, not the dates on which the buyer is negotiating.
    planning_start: date | None = None
    planning_end: date | None = None
    location: str | None = Field(default=None, max_length=200)
    stock_as_of: date | None = None
    max_stock_age_days: int | None = Field(default=None, ge=0, le=1095)

    @model_validator(mode="after")
    def valid_horizon(self) -> "AnalysisRequest":
        end = self.planning_end or self.need_by
        if self.planning_start is not None and end is not None and end < self.planning_start:
            raise ValueError("planning_end must not precede planning_start")
        if self.location is not None:
            self.location = self.location.strip()
            if not self.location:
                raise ValueError("location must not be blank")
        return self


class SupplierStatView(BaseModel):
    supplier_id: UUID
    name: str
    type: str
    orders: int
    quantity: Decimal
    last_price: Decimal
    last_ordered_at: date
    avg_price: Decimal
    min_price: Decimal
    on_time_rate: Decimal | None
    quality_rate: Decimal | None
    suggested: bool
    delivery_observations: int = 0
    quality_observations: int = 0
    last_received_at: date | None = None


class DemandAllocationView(BaseModel):
    need_by: date | None
    required: Decimal
    gross_required: Decimal
    stock_used: Decimal
    inbound_used: Decimal
    to_buy: Decimal
    planned_supply_used: Decimal = Decimal(0)


class CalculationView(BaseModel):
    stock_known: bool = True
    stock_issues: list[str] = []
    gross_on_hand: Decimal = Decimal(0)
    reserved_qty: Decimal = Decimal(0)
    blocked_qty: Decimal = Decimal(0)
    allocations: list[DemandAllocationView] = []
    buyer_quantity_override: Decimal | None = None
    excluded_inbound: Decimal = Decimal(0)


class FlagView(BaseModel):
    code: str
    supplier: str | None = None
    value: Decimal | None = None


class AnalysisLineView(BaseModel):
    id: UUID
    position: int
    item_id: UUID
    part_number: str
    description: str
    mpn: str | None
    manufacturer: str | None
    unit: str
    required: Decimal
    on_hand: Decimal
    on_order: Decimal
    to_buy: Decimal
    need_by: date | None
    currency: str | None
    last_price: Decimal | None
    avg_price: Decimal | None
    min_price: Decimal | None
    trend_pct: Decimal | None
    target_price: Decimal | None
    max_price: Decimal | None
    suppliers: list[SupplierStatView]
    flags: list[FlagView]
    rfq_line_id: UUID | None
    calculation: CalculationView = Field(default_factory=CalculationView)


class CurrencyTotalView(BaseModel):
    currency: str
    items_to_buy: int
    total_at_target: Decimal
    total_at_last_price: Decimal


class AnalysisSummary(BaseModel):
    items: int
    items_to_buy: int
    # Set when every line to buy is in one currency; otherwise see `totals`.
    currency: str | None
    total_at_target: Decimal | None
    total_at_last_price: Decimal | None
    flagged_items: int
    # Spend per currency (amounts in different currencies are never added up).
    totals: list[CurrencyTotalView] = []


class AnalysisView(BaseModel):
    id: UUID
    product: str
    quantity: Decimal
    need_by: date | None
    safety_stock_pct: Decimal
    lookback_days: int
    summary: AnalysisSummary
    lines: list[AnalysisLineView]
    rfq_id: UUID | None
    created_at: datetime
    planning_start: date | None = None
    planning_end: date | None = None
    location: str | None = None
    stock_as_of: date | None = None
    max_stock_age_days: int | None = None
    demand_source: Literal["explicit_quantity", "production_plan"] = "explicit_quantity"


class RfqFromAnalysisRequest(_Strict):
    title: str | None = Field(default=None, max_length=200)
    response_due: date | None = None
    # Suggested suppliers invited per line (the best-ranked ones).
    max_suppliers_per_line: int = Field(default=3, ge=1, le=3)
    currency: CurrencyCode | None = Field(
        default=None,
        description=(
            "The RFQ's currency, when the buyer said it. Without it: the analysis's currency "
            "when its lines to buy were all bought in one, else the purchase history's; when "
            "neither tells, the request is refused (422) and the buyer has to say"
        ),
    )


class PlannedRfqLine(BaseModel):
    description: str
    part_number: str | None
    quantity: Decimal
    unit: str
    need_by: date | None


class PlannedSupplier(BaseModel):
    supplier_id: UUID
    name: str
    # The contact the draft invite would use; None = the supplier would be skipped.
    email: str | None


class RfqFromAnalysisPlan(BaseModel):
    """What `create_rfq_from_analysis` would create, for the buyer to confirm first."""

    analysis_id: UUID
    title: str
    currency: str
    currency_source: CurrencySource | None
    lines: list[PlannedRfqLine]
    suppliers: list[PlannedSupplier]


class InvitedSupplier(BaseModel):
    supplier_id: UUID
    name: str


class SkippedSupplier(BaseModel):
    supplier_id: UUID
    name: str
    reason: str


class RfqFromAnalysis(BaseModel):
    analysis_id: UUID
    rfq: RfqDetail
    currency_source: CurrencySource | None = Field(
        default=None, description="Where the RFQ's base_currency came from"
    )
    invited: list[InvitedSupplier]
    skipped: list[SkippedSupplier]
    # Line positions left without a target/maximum price: bought in another
    # currency than the RFQ's, with no exchange rate to convert the limits.
    lines_without_limits: list[int] = []
