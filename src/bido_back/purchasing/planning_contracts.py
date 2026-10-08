"""Provider-neutral contracts for resumable spreadsheet planning and review."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from bido_back.agents.catalog.contracts import SheetMappingOutput
from bido_back.purchasing.inspection import SpreadsheetInspection
from bido_back.purchasing.workbook import WorkbookImportReport


class SheetTaskProposal(BaseModel):
    sheet: str
    objective: str = Field(min_length=1, max_length=1000)
    depends_on: list[str] = Field(default_factory=list[str], max_length=20)


class WorkbookPlanProposal(BaseModel):
    tasks: list[SheetTaskProposal] = Field(min_length=1, max_length=20)


class CellEvidence(BaseModel):
    sheet: str
    row: int = Field(ge=1)
    column: int = Field(ge=1)


class WorkbookIssue(BaseModel):
    severity: Literal["warning", "blocker"]
    message: str = Field(min_length=1, max_length=1000)
    evidence: list[CellEvidence] = Field(default_factory=list[CellEvidence], max_length=20)


class SheetUnderstanding(BaseModel):
    role: Literal[
        "items",
        "bom",
        "inventory",
        "purchase_history",
        "production_plan",
        "supplier_contacts",
        "material_requirements",
        "supplier_ratings",
        "consolidated",
        "notes",
        "mixed",
        "unknown",
    ]
    summary: str = Field(max_length=4000)
    mapping: SheetMappingOutput
    issues: list[WorkbookIssue] = Field(default_factory=list[WorkbookIssue], max_length=40)


class WorkbookReview(BaseModel):
    summary: str = Field(max_length=4000)
    issues: list[WorkbookIssue] = Field(default_factory=list[WorkbookIssue], max_length=80)


class PlannerInput(BaseModel):
    objective: str
    clarification: str
    workbook: SpreadsheetInspection
    samples: list[SpreadsheetInspection]
    retry_feedback: str | None = None


class SheetAnalysisInput(BaseModel):
    objective: str
    clarification: str
    page: SpreadsheetInspection
    previous: SheetUnderstanding | None
    dependencies: list[SheetUnderstanding]
    retry_feedback: str | None = None


class ReviewInput(BaseModel):
    objective: str
    clarification: str
    sheets: list[SheetUnderstanding]
    samples: list[SpreadsheetInspection]
    validation: WorkbookImportReport
    retry_feedback: str | None = None


class PlanTask(BaseModel):
    id: str
    kind: Literal["plan", "sheet", "review"]
    objective: str
    sheet: str | None = None
    depends_on: list[str] = Field(default_factory=list[str])
    status: Literal["pending", "completed", "failed"] = "pending"
    failures: int = 0
    pages_read: int = 0
    cells_read: int = 0
    next_row: int = 1
    next_column: int = 1
    result: SheetUnderstanding | None = None
    error: str | None = None


class WorkbookPlanState(BaseModel):
    objective: str
    clarification: str = ""
    revision: int = 1
    confirmation_requested: bool = False
    status: Literal["pending", "running", "ready", "needs_input", "failed", "imported"] = "pending"
    manifest: SpreadsheetInspection
    tasks: list[PlanTask]
    review: WorkbookReview | None = None
    validation: WorkbookImportReport | None = None
    mapping: SheetMappingOutput | None = None


class WorkbookPlanView(WorkbookPlanState):
    plan_id: UUID
    upload_id: UUID
    busy: bool = False
