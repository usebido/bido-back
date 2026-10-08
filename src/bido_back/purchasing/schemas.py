"""API shapes for data imports and purchase analyses."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from bido_back.purchasing.importers import TableImportType
from bido_back.rfqs.schemas import RfqDetail


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
    # Units to build; the production plan from today on when omitted.
    quantity: Decimal | None = Field(default=None, gt=0)
    need_by: date | None = None
    safety_stock_pct: Decimal = Field(default=Decimal(0), ge=0, le=100)
    lookback_days: int = Field(default=365, ge=30, le=1095)


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
    # The buyer's supplier master: material group and evaluation (percentages).
    category: str | None = None
    otif_pct: Decimal | None = None
    evaluation_quality_pct: Decimal | None = None
    rating: Decimal | None = None
    lead_time_days: Decimal | None = None


class CandidateView(BaseModel):
    """A supplier of the item's material group that never sold it."""

    supplier_id: UUID
    name: str
    type: str
    category: str
    otif_pct: Decimal | None = None
    quality_pct: Decimal | None = None
    rating: Decimal | None = None
    lead_time_days: Decimal | None = None


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
    # The buffer counted in `to_buy` (requirements: required + safety - on hand - on order).
    safety_stock: Decimal = Decimal(0)
    candidates: list[CandidateView] = []


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
    # 'bom' (a product's BOM times a quantity) or 'requirements' (open needs).
    source: Literal["bom", "requirements"] = "bom"


class RequirementsAnalysisRequest(_Strict):
    """Analyse the open requirements (not yet in an RFQ), optionally narrowed down."""

    part_numbers: list[str] = Field(default_factory=list[str], max_length=500)
    project: str | None = Field(default=None, max_length=200)
    need_by_until: date | None = None
    lookback_days: int = Field(default=365, ge=30, le=1095)


class NewRequirementLine(_Strict):
    part_number: str = Field(min_length=1, max_length=100)
    quantity: Decimal = Field(gt=0)
    need_by: date
    unit: str | None = Field(default=None, max_length=20)
    description: str | None = Field(default=None, max_length=300)
    reference: str = Field(default="", max_length=100)


class NewRequirementsRequest(_Strict):
    lines: list[NewRequirementLine] = Field(min_length=1, max_length=200)


class RequirementView(BaseModel):
    id: UUID
    part_number: str
    description: str
    quantity: Decimal
    unit: str | None
    need_by: date
    reference: str
    location: str | None
    project: str | None
    source: Literal["import", "chat"]
    rfq_id: UUID | None


class RfqFromAnalysisRequest(_Strict):
    title: str | None = Field(default=None, max_length=200)
    response_due: date | None = None
    # Suggested suppliers invited per line (the best-ranked ones).
    max_suppliers_per_line: int = Field(default=3, ge=1, le=3)


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
    invited: list[InvitedSupplier]
    skipped: list[SkippedSupplier]
    # Line positions left without a target/maximum price: bought in another
    # currency than the RFQ's, with no exchange rate to convert the limits.
    lines_without_limits: list[int] = []
    # A requirements analysis gives one RFQ per material group: the others' ids.
    other_rfq_ids: list[UUID] = []
