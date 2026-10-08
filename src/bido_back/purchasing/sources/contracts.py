"""Neutral evidence contracts: model proposals contain locators, never business values."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

RecordKind = Literal[
    "inventory",
    "material_requirement",
    "supplier",
    "purchase",
    "supplier_performance",
    "purchase_summary",
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
    evidence: list[SourceRef] = Field(default_factory=list[SourceRef])


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


class SourceAssessment(Assessment):
    coverage: list[Coverage] = Field(default_factory=list[Coverage])
    status: Literal["assessed", "needs_review"] = "needs_review"
    persisted: bool = False


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
