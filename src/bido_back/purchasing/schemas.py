"""API shapes for data imports and purchase analyses."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from bido_back.purchasing.importers import ImportType
from bido_back.rfqs.schemas import RfqDetail


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Imports ------------------------------------------------------------------------------


class ImportRequest(_Strict):
    type: ImportType
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
    type: ImportType
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


class AnalysisSummary(BaseModel):
    items: int
    items_to_buy: int
    currency: str | None
    total_at_target: Decimal | None
    total_at_last_price: Decimal | None
    flagged_items: int


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
