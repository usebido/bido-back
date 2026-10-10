"""Copilot tools for a purchase plan too large for an answer (`exports.procurement_plan`).

- `export_procurement_plan` (draft): saves the analysis the buyer saw as an assessment
  snapshot (the same one `save_procurement_assessment` saves, idempotent) and answers
  where the full plan workbook is: one row per material and plant, a sheet per plant,
  the actions and the assumptions. The file is rendered on request from that snapshot
  (`GET /v1/procurement-assessments/{id}/plan.xlsx`); nothing else is stored. Its
  output is also an artifact, so the screen shows the download.
- `procurement_plan_lines` (read): the plan's rows of a saved assessment, filtered by
  plant, status or material, page by page, so "a lista da fábrica 2000" is answered
  with every row instead of the part an answer can hold.

The rows, statuses and counts come from code, the same for the file and the pages.
"""

from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Final
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from bido_back.agents.context import ToolContext
from bido_back.agents.tools import AnyTool, Tool, ToolError, ToolValidationError
from bido_back.auth import Principal
from bido_back.copilot.journey_tools import AnalyzeSourcesIn, ScopeRequestInput
from bido_back.exports.procurement_plan import (
    DEFAULT_PAGE,
    MAX_PAGE,
    PLAN_STATUSES,
    STATUS_LABELS,
    PlanLanguage,
    PlanRow,
    PlanStatus,
    page_rows,
    plan_counts,
    plan_filename,
    plan_rows,
)
from bido_back.purchasing.service import PurchasingError
from bido_back.purchasing.sources.contracts import SourceAssessment
from bido_back.purchasing.sources.mapping import default_mapper
from bido_back.purchasing.sources.service import (
    ScopeValidationError,
    SourceAccessError,
    analyze_uploads,
)
from bido_back.purchasing.sources.snapshots import get_snapshot

if TYPE_CHECKING:
    from bido_back.copilot.tools import CopilotToolDeps

PLAN_ARTIFACT: Final = "procurement_plan_file"
# Rows of one row's lists the model reads.
_ROW_LIST_ITEMS: Final = 5


def plan_download_path(assessment_id: UUID, language: PlanLanguage) -> str:
    return f"/v1/procurement-assessments/{assessment_id}/plan.xlsx?lang={language}"


class ExportPlanIn(BaseModel):
    assessment_id: UUID | None = Field(
        default=None,
        description="A saved assessment (from save_procurement_assessment or an earlier "
        "export). Leave empty to save the analysis the buyer saw: then pass its upload_ids, "
        "objective and scope exactly as in that analyze_procurement_sources call.",
    )
    upload_ids: list[UUID] = Field(default_factory=list[UUID], max_length=10)
    objective: str | None = Field(default=None, max_length=2000)
    scope: ScopeRequestInput | None = None
    language: PlanLanguage = Field(
        default="pt", description="The buyer's language: column names and statuses"
    )

    @model_validator(mode="after")
    def _one_source(self) -> "ExportPlanIn":
        if self.assessment_id is None and not (self.upload_ids and self.objective):
            raise ValueError("pass assessment_id, or the analysis' upload_ids and objective")
        return self


class StatusCount(BaseModel):
    status: str = Field(description="In the buyer's language")
    rows: int


class PlantCount(BaseModel):
    plant: str
    rows: int
    to_buy: int


class CurrencyTotalOut(BaseModel):
    currency: str
    lines: int
    total: Decimal


class PlanFileOut(BaseModel):
    assessment_id: UUID
    filename: str
    download_path: str
    language: PlanLanguage
    rows: int = Field(description="Every material and plant, with nothing to buy too")
    by_status: list[StatusCount]
    by_plant: list[PlantCount]
    totals: list[CurrencyTotalOut] = Field(
        description="Per currency; never add them up or convert them"
    )
    note: str


class PlanLinesIn(BaseModel):
    assessment_id: UUID = Field(description="From export_procurement_plan or a saved assessment")
    plant: str | None = Field(default=None, max_length=100, description="One plant (centro)")
    statuses: list[PlanStatus] = Field(
        default_factory=list[PlanStatus],
        description="Only rows with these statuses: purchase, unavoidable_stockout, "
        "consumes_safety_stock, no_valid_source, covered. Empty = every row.",
    )
    material: str | None = Field(
        default=None, max_length=200, description="A part number, or words of its description"
    )
    page: int = Field(default=1, ge=1)
    limit: int = Field(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE)
    language: PlanLanguage = "pt"


class PlanLineOut(BaseModel):
    material: str
    description: str | None
    plant: str | None
    unit: str | None
    status: str = Field(description="In the buyer's language")
    net_shortfall: Decimal | None
    quantity: Decimal | None = Field(description="To buy, after the lot rules")
    lot_rules: str | None
    first_shortage: date | None
    first_stockout: date | None
    supplier: str | None
    supplier_code: str | None
    arrival: date | None
    unit_price: Decimal | None
    currency: str | None
    total: Decimal | None
    actions: list[str]
    affected_orders: list[str]
    affected_orders_left_out: int = 0
    confidence: str


class PlanLinesOut(BaseModel):
    rows: list[PlanLineOut]
    matched: int = Field(description="Rows matching the filters, on every page")
    page: int
    pages: int
    download_path: str
    note: str


def _principal(ctx: ToolContext) -> Principal:
    if ctx.user_id is None:
        raise ToolError("the copilot needs a signed-in user")
    return Principal(tenant_id=ctx.tenant_id, user_id=ctx.user_id)


def _line_out(row: PlanRow, language: PlanLanguage) -> PlanLineOut:
    return PlanLineOut(
        material=row.part_number,
        description=row.description,
        plant=row.plant,
        unit=row.unit,
        status=STATUS_LABELS[language][row.status],
        net_shortfall=row.net_shortfall,
        quantity=row.quantity,
        lot_rules=row.lot_rules,
        first_shortage=row.first_shortage,
        first_stockout=row.first_stockout,
        supplier=row.supplier,
        supplier_code=row.supplier_code,
        arrival=row.arrival,
        unit_price=row.unit_price,
        currency=row.currency,
        total=row.total,
        actions=row.actions[:_ROW_LIST_ITEMS],
        affected_orders=row.affected_orders[:_ROW_LIST_ITEMS],
        affected_orders_left_out=max(0, len(row.affected_orders) - _ROW_LIST_ITEMS),
        confidence=row.confidence,
    )


def plan_file_out(assessment: SourceAssessment, language: PlanLanguage) -> PlanFileOut:
    if assessment.assessment_id is None:
        raise ToolError("the analysis was not saved")
    rows = plan_rows(assessment, language)
    counts = plan_counts(rows)
    labels = STATUS_LABELS[language]
    return PlanFileOut(
        assessment_id=assessment.assessment_id,
        filename=plan_filename(assessment, language),
        download_path=plan_download_path(assessment.assessment_id, language),
        language=language,
        rows=counts.rows,
        by_status=[StatusCount(status=labels[s], rows=counts.by_status[s]) for s in PLAN_STATUSES],
        by_plant=[
            PlantCount(plant=plant or "—", rows=total, to_buy=counts.to_buy_by_plant.get(plant, 0))
            for plant, total in counts.by_plant.items()
        ],
        totals=[
            CurrencyTotalOut(currency=t.currency, lines=t.lines, total=t.total)
            for t in counts.totals
        ],
        note=(
            "The workbook holds every row (also those with nothing to buy), one sheet per "
            "plant, the actions and the assumptions; the screen shows its download. For "
            "rows in the answer, use procurement_plan_lines page by page."
        ),
    )


def plan_tools(deps: "CopilotToolDeps") -> tuple[AnyTool, ...]:
    db = deps.db

    async def _assessment(ctx: ToolContext, args: ExportPlanIn) -> SourceAssessment:
        principal = _principal(ctx)
        if args.assessment_id is not None:
            try:
                return await get_snapshot(db, principal, args.assessment_id)
            except PurchasingError as exc:
                raise ToolError(str(exc)) from None
        allowed = set(deps.attachment_ids) | set(deps.earlier_attachment_ids)
        if not set(args.upload_ids) <= allowed:
            raise ToolError("that id is not one of the files attached in this conversation")
        if deps.storage is None:
            raise ToolError("file storage is not available")
        analysis = AnalyzeSourcesIn(
            upload_ids=args.upload_ids, objective=args.objective or "", scope=args.scope
        )
        try:
            return await analyze_uploads(
                db,
                deps.storage,
                principal,
                analysis.upload_ids,
                analysis.objective,
                ctx,
                deps.source_mapper or default_mapper(),
                persist=True,
                scope=analysis.scope,
            )
        except SourceAccessError as exc:
            raise ToolError(str(exc)) from None
        except ScopeValidationError as exc:
            raise ToolValidationError(
                str(exc),
                code=exc.code,
                field=f"scope.{exc.field}",
                value=exc.value,
                valid_values=exc.valid_values,
            ) from None

    async def export_procurement_plan(ctx: ToolContext, args: ExportPlanIn) -> PlanFileOut:
        return plan_file_out(await _assessment(ctx, args), args.language)

    async def procurement_plan_lines(ctx: ToolContext, args: PlanLinesIn) -> PlanLinesOut:
        try:
            assessment = await get_snapshot(db, _principal(ctx), args.assessment_id)
        except PurchasingError as exc:
            raise ToolError(str(exc)) from None
        rows = plan_rows(assessment, args.language)
        page = page_rows(
            rows,
            plant=args.plant,
            statuses=args.statuses,
            material=args.material,
            page=args.page,
            limit=args.limit,
        )
        more = page.page < page.pages
        return PlanLinesOut(
            rows=[_line_out(row, args.language) for row in page.rows],
            matched=page.matched,
            page=page.page,
            pages=page.pages,
            download_path=plan_download_path(args.assessment_id, args.language),
            note=(
                f"Page {page.page} of {page.pages}; {page.matched} rows match. "
                + (
                    f"Call again with page={page.page + 1} for the next rows, or point the "
                    "buyer to the workbook; never present these rows as all of them."
                    if more
                    else "These are the last rows that match."
                )
            ),
        )

    return (
        Tool(
            "export_procurement_plan",
            "The full purchase plan as an Excel workbook the buyer downloads: one row per "
            "material and plant (also the ones with nothing to buy), status, quantities "
            "before and after the lot rules, the recommended supplier with arrival and "
            "price, the expedite and chase actions, the affected orders, and a sheet per "
            "plant, actions and assumptions. Use it when the buyer asks for the plan in "
            "Excel or a file, for the whole plan, or for more rows than your answer shows. "
            "Pass the assessment_id of a saved assessment, or the upload_ids, objective and "
            "scope of the analyze_procurement_sources call the buyer saw (it saves that "
            "analysis, as save_procurement_assessment does). The screen shows the download: "
            "say the file is ready and give the counts; never say there is no file. Nothing "
            "is sent.",
            ExportPlanIn,
            PlanFileOut,
            "draft",
            export_procurement_plan,
            artifact_kind=PLAN_ARTIFACT,
        ),
        Tool(
            "procurement_plan_lines",
            "Rows of a saved purchase plan, filtered by plant (centro), status or material, "
            "page by page (up to 100 per page). Use it for 'a lista da fábrica 2000', 'quais "
            "materiais estão sem fonte válida', 'só as rupturas': call page after page until "
            "`pages`, or show a page and point to the workbook (export_procurement_plan). "
            "`matched` counts every matching row; never present one page as all of them. "
            "Statuses come in the buyer's language.",
            PlanLinesIn,
            PlanLinesOut,
            "read",
            procurement_plan_lines,
        ),
    )
