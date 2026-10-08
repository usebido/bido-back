"""Neutral evidence contracts: model proposals contain locators, never business values."""

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

RecordKind = Literal[
    "inventory",
    "material_requirement",
    "supplier",
    "purchase",
    "supplier_performance",
    "purchase_summary",
    "item",
    "bom",
    "production_plan",
    "receipt",
    "delivery_schedule",
    "supplier_item",
]


class SourceRef(BaseModel):
    source_id: str
    table: str
    row: int = Field(ge=1)
    column: int = Field(ge=1)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)


class Issue(BaseModel):
    code: str
    severity: Literal["warning", "blocker"]
    message: str
    evidence: list[SourceRef] = Field(default_factory=list[SourceRef])


class Record(BaseModel):
    kind: RecordKind
    values: dict[str, str]
    evidence: dict[str, SourceRef]


class PlanningScope(BaseModel):
    """Buyer-supplied planning boundaries, kept separate from extracted evidence."""

    product_code: str | None = Field(default=None, min_length=1, max_length=200)
    area: str | None = Field(default=None, min_length=1, max_length=200)
    location: str = Field(min_length=1, max_length=200)
    as_of: date
    horizon_end: date
    safety_stock: Decimal | None = Field(default=None, ge=0, lt=10**12)
    production_lead_days: int = Field(default=0, ge=0, le=365)

    @model_validator(mode="after")
    def valid_scope(self) -> "PlanningScope":
        if self.product_code is None and self.area is None:
            raise ValueError("A product code or area is required for scoped planning")
        if self.horizon_end < self.as_of:
            raise ValueError("The planning horizon must end on or after the snapshot date")
        return self


class DemandContribution(BaseModel):
    reference: str
    product_code: str | None = None
    quantity: Decimal
    need_by: date
    evidence: list[SourceRef]


class InboundEvidence(BaseModel):
    po_number: str
    po_line: str
    quantity: Decimal
    expected_at: date | None = None
    eligible: bool = False
    evidence: list[SourceRef]


class NettingStep(BaseModel):
    need_by: date
    demand: Decimal
    incoming: Decimal
    purchase_quantity: Decimal
    projected_balance: Decimal


class SupplierEvidence(BaseModel):
    supplier_code: str | None = None
    name: str | None = None
    email: str | None = None
    last_price: Decimal | None = None
    currency: str | None = None
    ordered_at: date | None = None
    otif_pct: Decimal | None = None
    quality_pct: Decimal | None = None
    lead_time_days: Decimal | None = None
    last_price_basis: Literal["purchase_order", "received_order"] = "purchase_order"
    qualification_status: str | None = None
    evidence: list[SourceRef] = Field(default_factory=list[SourceRef])
    # Measured by Bido from reconciled source orders and receipts, in the line's price
    # currency. otif_pct/quality_pct/lead_time_days above stay the source-REPORTED values.
    orders: int = 0
    quantity: Decimal | None = None
    avg_price: Decimal | None = None
    min_price: Decimal | None = None
    on_time_rate: Decimal | None = None
    quality_rate: Decimal | None = None
    delivery_observations: int = 0
    quality_observations: int = 0
    avg_lead_time_days: Decimal | None = None
    last_received_at: date | None = None
    suggested: bool = False


LineFlagCode = Literal[
    "no_history",
    "single_source",
    "price_up",
    "supplier_late",
    "quality_issue",
    "broker_source",
    "mixed_currency",
    "covered_by_stock",
]


class LineFlag(BaseModel):
    code: LineFlagCode
    supplier: str | None = None
    value: Decimal | None = None


class PriceBenchmark(BaseModel):
    """Bido's internal price estimate from reconciled completed purchases.

    An internal reference before any supplier contact: never a quotation, an accepted
    price or an approved negotiation limit until the buyer approves it.
    """

    currency: str
    basis: Literal["received_orders"] = "received_orders"
    observations: int = Field(ge=1)
    window_days: int = Field(ge=1)
    last_price: Decimal
    last_supplier: str | None = None
    last_ordered_at: date | None = None
    avg_price: Decimal
    min_price: Decimal
    trend_pct: Decimal | None = None
    target_price: Decimal
    max_price: Decimal
    estimated_cost_at_target: Decimal | None = None
    estimated_cost_at_last: Decimal | None = None
    evidence: list[SourceRef] = Field(default_factory=list[SourceRef])


class SpendTotal(BaseModel):
    currency: str
    lines: int
    total_at_target: Decimal
    total_at_last_price: Decimal


class AssessmentLine(BaseModel):
    part_number: str
    description: str | None = None
    location: str | None = None
    unit: str | None = None
    reported_unit: str | None = None
    demand_unit: str | None = None
    inventory_unit: str | None = None
    need_by: date | None = None
    demand: Decimal | None = None
    available: Decimal | None = None
    on_order: Decimal | None = None
    safety_stock: Decimal | None = None
    calculated_to_buy: Decimal | None = None
    reported_to_buy: Decimal | None = None
    reported_estimated_cost: Decimal | None = None
    currency: str | None = None
    ready: bool = False
    suppliers: list[SupplierEvidence] = Field(default_factory=list[SupplierEvidence])
    evidence: list[SourceRef] = Field(default_factory=list[SourceRef])
    issues: list[Issue] = Field(default_factory=list[Issue])
    demand_contributions: list[DemandContribution] = Field(default_factory=list[DemandContribution])
    inbound: list[InboundEvidence] = Field(default_factory=list[InboundEvidence])
    netting: list[NettingStep] = Field(default_factory=list[NettingStep])
    price: PriceBenchmark | None = None
    flags: list[LineFlag] = Field(default_factory=list[LineFlag])


class Assessment(BaseModel):
    lines: list[AssessmentLine] = Field(default_factory=list[AssessmentLine])
    issues: list[Issue] = Field(default_factory=list[Issue])
    record_counts: dict[str, int] = Field(default_factory=dict[str, int])


class EvidenceTable(BaseModel):
    source_id: str
    name: str
    # Every value retains its source spelling; adapters preserve formatted identifiers.
    rows: list[list[str | None]]
    layout: Literal["table", "text"] = "table"


class Coverage(BaseModel):
    source_id: str
    filename: str
    table: str
    rows: int
    mapped_rows: int = 0
    inspected: bool = False


class PlanningCandidate(BaseModel):
    product_code: str
    description: str | None = None
    location: str | None = None
    area: str | None = None
    need_dates: list[date] = Field(default_factory=list[date])
    evidence: list[SourceRef] = Field(default_factory=list[SourceRef])


class SourceAssessment(Assessment):
    scope: PlanningScope | None = None
    planning_candidates: list[PlanningCandidate] = Field(default_factory=list[PlanningCandidate])
    snapshot_dates: list[date] = Field(default_factory=list[date])
    coverage: list[Coverage] = Field(default_factory=list[Coverage])
    status: Literal["assessed", "needs_review"] = "needs_review"
    persisted: bool = False
    assessment_id: UUID | None = None
    rfq_id: UUID | None = None
    # Spend per currency of the lines to buy, at the internal target and last prices.
    # Amounts in different currencies are never added up or converted.
    totals: list[SpendTotal] = Field(default_factory=list[SpendTotal])


class CellRef(BaseModel):
    row: int = Field(ge=1)
    column: int = Field(ge=1)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)


class FieldMapping(BaseModel):
    field: str
    ref: CellRef


class ColumnMapping(BaseModel):
    field: str
    column: int


class RecordProposal(BaseModel):
    kind: RecordKind
    fields: list[FieldMapping]


class TableProposal(BaseModel):
    kind: RecordKind
    header_row: int = Field(ge=1)
    first_row: int = Field(ge=1)
    last_row: int = Field(ge=1)
    # Lists keep the model's strict output schema closed while fields vary by kind.
    # Each assignment names a 1-based column; values are always copied by code.
    columns: list[ColumnMapping]


class MappingProposal(BaseModel):
    sections: list[TableProposal] = Field(default_factory=list[TableProposal])
    records: list[RecordProposal] = Field(default_factory=list[RecordProposal])
    issues: list[Issue] = Field(default_factory=list[Issue])


class TableManifest(BaseModel):
    source_id: str
    name: str
    rows: int
    columns: int
    sample: list[list[str | None]]
    sample_rows: list[int] = Field(default_factory=list[int])


class MappingTask(BaseModel):
    objective: str
    source_id: str
    table: str
    layout: Literal["table", "text"]
    first_row: int
    last_row: int
    rows: list[list[str | None]]
    previous_sections: list[TableProposal]
    manifest: list[TableManifest]
    catalog: dict[str, list[str]]
