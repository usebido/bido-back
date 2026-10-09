"""Neutral evidence contracts: model proposals contain locators, never business values."""

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

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


CanonicalBasis = Literal[
    # The mapper interpreted the section's distinct spelling; code checked it.
    "value_map",
    # A spreadsheet number cell: no locale is involved in reading it.
    "native_number",
    # Other values of the same column, sheet or source prove the decimal separator.
    "decimal_separator",
    # Other dates of the same column, sheet or source prove the day/month order.
    "date_order",
    # The mapper declared the order from the source's language; no value contradicts it.
    "declared_date_order",
    # A recognized report's documented convention decided the reading (an ERP's sign of
    # MRP elements, a price per price unit, a status implied by the report).
    "report_convention",
    # A quantity, price or unit converted with the item's alternative unit of measure.
    "unit_conversion",
]


class CanonicalValue(BaseModel):
    """What code derived from a copied source value, and what decided it.

    The source spelling stays in `Record.values` and its cell in `Record.evidence`.
    `value` is a vocabulary term, a plain decimal or an ISO date; None means the
    interpretations disagree and the value stays unresolved.
    """

    value: str | None
    source: str
    basis: CanonicalBasis
    evidence: list[SourceRef] = Field(default_factory=list[SourceRef])


class Record(BaseModel):
    kind: RecordKind
    values: dict[str, str]
    evidence: dict[str, SourceRef]
    canonical: dict[str, CanonicalValue] = Field(default_factory=dict[str, CanonicalValue])


class PlanningScope(BaseModel):
    """Buyer-supplied planning boundaries, kept separate from extracted evidence."""

    product_code: str | None = Field(default=None, min_length=1, max_length=200)
    area: str | None = Field(default=None, min_length=1, max_length=200)
    location: str = Field(min_length=1, max_length=200)
    as_of: date
    horizon_end: date
    safety_stock: Decimal | None = Field(default=None, ge=0, lt=10**12)
    production_lead_days: int = Field(default=0, ge=0, le=365)
    # Every directly required material of the location: an MRP list (dated requirements
    # per material) without production plans, settled by code from the evidence.
    all_materials: bool = False

    @model_validator(mode="after")
    def valid_scope(self) -> "PlanningScope":
        if self.product_code is None and self.area is None and not self.all_materials:
            raise ValueError("A product code or area is required for scoped planning")
        if self.horizon_end < self.as_of:
            raise ValueError("The planning horizon must end on or after the snapshot date")
        return self


class ScopeRequest(BaseModel):
    """The buyer's planning boundaries as they stated them; every field is optional.

    Code resolves the request against the mapped evidence into a `PlanningScope`
    (`bido_back.purchasing.sources.scope`), or asks a question. A complete request
    (product code or area, location, as_of and horizon_end) is a planning scope.
    """

    product_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="The product's exact code, only when the buyer gave it (e.g. MTR-100)",
    )
    product: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="The product as the buyer named it, in their words (e.g. 'Motor MTR-100'); "
        "its exact code is found in the sources",
    )
    area: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Only when the buyer asks for a whole area or department, not one product",
    )
    location: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="The plant or site as the buyer wrote it (e.g. 'Planta A')",
    )
    as_of: date | None = Field(
        default=None,
        description="The reference (export/snapshot) date, only when the buyer gave one; "
        "empty = the latest stock snapshot of the plant",
    )
    horizon_end: date | None = Field(
        default=None, description="The last need date to include (inclusive), when given as a date"
    )
    horizon_days: int | None = Field(
        default=None,
        ge=0,
        le=365,
        description="A horizon given in days after the reference date ('next 14 days' = 14)",
    )
    safety_stock: Decimal | None = Field(
        default=None,
        ge=0,
        lt=10**12,
        description="The buyer's explicit safety-stock policy ('no additional safety stock' "
        "= 0); empty = the sources' own policy",
    )
    production_lead_days: int = Field(
        default=0,
        ge=0,
        le=365,
        description="Days before the production date that components are needed, when given",
    )

    @field_validator("product_code", "product", "area", "location", mode="before")
    @classmethod
    def blank_is_unknown(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def valid_request(self) -> "ScopeRequest":
        if (
            self.as_of is not None
            and self.horizon_end is not None
            and self.horizon_end < self.as_of
        ):
            raise ValueError("The planning horizon must end on or after the snapshot date")
        return self


ScopeField = Literal[
    "product_code",
    "area",
    "location",
    "as_of",
    "horizon_end",
    "safety_stock",
    "production_lead_days",
]
# buyer: stated in the request (matched to the evidence's spelling); evidence: settled by
# code from located evidence (an assumption); default: neither (lead days 0, or the
# sources' own safety-stock policy).
ScopeSource = Literal["buyer", "evidence", "default"]


class ScopeOption(BaseModel):
    # What to put in the scope field when the buyer picks this option.
    value: str
    label: str
    evidence: list[SourceRef] = Field(default_factory=list[SourceRef])


class ScopeAssumption(BaseModel):
    """A boundary code settled from the evidence; the copilot discloses it."""

    code: str
    field: ScopeField
    # The value code settled on; None when a boundary was left out of the scope.
    value: str | None = None
    message: str
    evidence: list[SourceRef] = Field(default_factory=list[SourceRef])


class ScopeQuestion(BaseModel):
    """A boundary the evidence cannot settle without guessing: ask the buyer."""

    code: str
    field: ScopeField
    message: str
    options: list[ScopeOption] = Field(default_factory=list[ScopeOption])


class ScopeProvenance(BaseModel):
    """Where one value of the resolved scope came from."""

    field: ScopeField
    source: ScopeSource
    evidence: list[SourceRef] = Field(default_factory=list[SourceRef])


class ScopeResolution(BaseModel):
    request: ScopeRequest
    assumptions: list[ScopeAssumption] = Field(default_factory=list[ScopeAssumption])
    questions: list[ScopeQuestion] = Field(default_factory=list[ScopeQuestion])
    # One entry per value of the resolved scope; empty while questions are open.
    provenance: list[ScopeProvenance] = Field(default_factory=list[ScopeProvenance])


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
    # The order's supplier, when the order evidence names one.
    supplier_code: str | None = None
    # The date an MRP list (e.g. SAP MD04 exception 10) asks to bring the order forward to.
    reschedule_to: date | None = None
    evidence: list[SourceRef]


class NettingStep(BaseModel):
    need_by: date
    demand: Decimal
    incoming: Decimal
    purchase_quantity: Decimal
    projected_balance: Decimal


class Assumption(BaseModel):
    """A reading or default the assessment settled on instead of stopping: disclosed with
    the reply, the RFQ draft and the approval, never hidden. `alternative` states what
    changes if the assumption is wrong."""

    code: str
    message: str
    alternative: str | None = None
    # low: a real ambiguity was settled by the most plausible reading; medium: a known
    # convention or a default the sources did not state.
    confidence: Literal["medium", "low"] = "medium"
    # The items it concerns; empty = every line of the assessment.
    part_numbers: list[str] = Field(default_factory=list[str])
    evidence: list[SourceRef] = Field(default_factory=list[SourceRef])


class RecognizedReport(BaseModel):
    """A table read by a deterministic report pack instead of the source mapper."""

    source_id: str
    table: str
    pack: str
    report: str
    rows: int


# agreement: the purchasing info record / price agreement; history: the average of the
# item's completed orders; reported: the source's own supplier lead time; material_master:
# the material's planned delivery time.
LeadTimeSource = Literal["agreement", "history", "reported", "material_master"]
# meets: arrives by the first shortage date (the projected balance, without new orders,
# falls below the safety stock); meets_consuming_safety_stock: after it, but by the first
# stockout date (the balance falls below zero), so the safety stock is used meanwhile;
# does_not_meet: after the first stockout date.
ArrivalStatus = Literal["meets", "meets_consuming_safety_stock", "does_not_meet"]


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
    # ISO 3166-1 alpha-2, when the buyer's supplier master names one recognizable country.
    country: str | None = None
    # The buyer's supplier master blocks purchasing from it: never invited.
    purchase_blocked: bool = False
    # The source's own evaluation scores (e.g. an ERP vendor evaluation), as reported.
    score_overall: Decimal | None = None
    score_price: Decimal | None = None
    score_quality: Decimal | None = None
    score_delivery: Decimal | None = None
    score_service: Decimal | None = None
    # A purchasing info record / price agreement for the item, per base unit.
    contract_price: Decimal | None = None
    contract_currency: str | None = None
    contract_valid_to: date | None = None
    # The agreement ended before the reference date: price history only, not a price.
    contract_expired: bool | None = None
    contract_lead_time_days: Decimal | None = None
    min_order_quantity: Decimal | None = None
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
    # When a new order placed on the reference date would arrive (`TimingCheck`): the
    # lead time used and where it came from, the arrival, and how the arrival meets the
    # line's projected balance. None: no lead time is known, arrival not checked.
    planned_lead_time_days: int | None = None
    lead_time_source: LeadTimeSource | None = None
    estimated_arrival: date | None = None
    arrival_status: ArrivalStatus | None = None
    # Days after the first shortage date it would arrive (0 when by then).
    days_late: int | None = None
    # How much of the purchase needs fall on or after its arrival.
    coverable_quantity: Decimal | None = None
    # The source the timing check picks for this line (`TimingCheck.recommended_supplier`).
    recommended: bool = False


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


class PriceOutlier(BaseModel):
    supplier: str | None = None
    ordered_at: date
    unit_price: Decimal
    # The median of the item's other price references it was compared with.
    reference_price: Decimal
    deviation_pct: Decimal
    # Whether the outlier is part of the benchmark's average and maximum (it is: flagged,
    # never dropped), and the target and maximum the benchmark would have without it.
    in_benchmark: bool
    target_without: Decimal | None = None
    max_without: Decimal | None = None
    # Its order-to-receipt days and the median of the item's other completed orders; an
    # abnormally short lead time suggests an emergency purchase (never a stated cause).
    lead_time_days: int | None = None
    reference_lead_time_days: Decimal | None = None
    possible_emergency: bool = False
    evidence: list[SourceRef] = Field(default_factory=list[SourceRef])


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
    # Completed-purchase prices far from the item's other price references (other
    # purchases and current agreements): shown, never silently used as a reference.
    outliers: list[PriceOutlier] = Field(default_factory=list[PriceOutlier])
    evidence: list[SourceRef] = Field(default_factory=list[SourceRef])


class SpendTotal(BaseModel):
    currency: str
    lines: int
    total_at_target: Decimal
    total_at_last_price: Decimal


Involved = Literal["production_planning"]


class DatedQuantity(BaseModel):
    need_by: date
    quantity: Decimal


class AffectedDemand(BaseModel):
    """A demand the earliest new order cannot cover in time."""

    reference: str
    # The product the material is needed for (a production order's product), when known.
    product_code: str | None = None
    product_description: str | None = None
    # Other materials the same order needs, as the evidence names them: they identify a
    # product the evidence does not describe.
    product_components: list[str] = Field(default_factory=list[str])
    need_by: date
    quantity: Decimal


class ProjectedBalance(BaseModel):
    on: date
    balance: Decimal


class TimingCheck(BaseModel):
    """Whether a new order can arrive before the line runs short, from lead times.

    Computed by code over the projected balance without new orders (stock plus open
    orders as counted, minus dated demand); the quantity to buy is unchanged by it.
    """

    # Orders are placed on the reference date (the stock snapshot's date).
    order_date: date
    safety_stock: Decimal = Decimal(0)
    # The projected balance per need date, without new orders.
    projected: list[ProjectedBalance] = Field(default_factory=list[ProjectedBalance])
    # The first date the balance falls below the safety stock, and below zero.
    first_shortage_date: date | None = None
    first_stockout_date: date | None = None
    # meets / meets_consuming_safety_stock: the recommended source's arrival status;
    # unavoidable_stockout: no usable source arrives by the first stockout date;
    # unknown: no usable source with a known lead time.
    outcome: Literal["meets", "meets_consuming_safety_stock", "unavoidable_stockout", "unknown"]
    recommended_supplier: str | None = None
    recommended_arrival: date | None = None
    # The best-ranked source when the recommendation switched away from it for timing.
    switched_from: str | None = None
    # The deepest shortfall below zero before the recommended arrival, without and with
    # the expedite actions honored.
    uncovered_quantity: Decimal = Decimal(0)
    uncovered_after_expedite: Decimal | None = None
    # The demands dated while the balance is below zero before that arrival (e.g. the
    # production orders the material feeds, from the MRP element data).
    affected_demand: list[AffectedDemand] = Field(default_factory=list[AffectedDemand])
    # Who must act besides purchasing: production planning re-sequences or reschedules
    # the orders a late material holds up.
    involve: list[Involved] = Field(default_factory=list[Involved])


ExpediteReason = Literal["mrp_reschedule_in", "mrp_reschedule_out", "due_after_shortage", "overdue"]


class ExpediteAction(BaseModel):
    """Ask a supplier to bring an open order forward: before buying more, a late open
    order may cover the earliest needs."""

    po_number: str
    po_line: str
    supplier_code: str | None = None
    supplier_name: str | None = None
    quantity: Decimal
    current_date: date
    requested_date: date
    # mrp_reschedule_in / mrp_reschedule_out: the MRP list asks to move it earlier or
    # later (e.g. SAP exceptions 10 and 15); due_after_shortage: it is due after the
    # line's first stockout date; overdue: its date has passed and it is still open, yet
    # counted as arriving now (e.g. SAP exception 07): confirm it with the supplier.
    reason: ExpediteReason
    # How much of the shortfall below the safety stock dated before its current date it
    # would cover.
    would_cover: Decimal = Decimal(0)
    evidence: list[SourceRef] = Field(default_factory=list[SourceRef])


class SourcingProposal(BaseModel):
    """No known supplier can be used: find one before buying."""

    reason: Literal["no_usable_supplier"] = "no_usable_supplier"
    # Each known supplier and why it cannot be used.
    excluded: list[str] = Field(default_factory=list[str])
    # The material's merchandise group (e.g. SAP MM60 "Grupo de mercadorias").
    material_group: str | None = None
    # Other suppliers in the evidence of items of that same group, not yet sources of
    # it. Never suppliers of other categories; empty when there are none.
    candidates: list[str] = Field(default_factory=list[str])
    action: Literal["rfq_new_suppliers"] = "rfq_new_suppliers"


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
    # The net requirement before the item's lot-sizing rules (minimum lot, rounding);
    # calculated_to_buy is the order quantity after them. None: no rule applied.
    net_requirement: Decimal | None = None
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
    # What the quantity rests on besides stated evidence; shown before any RFQ.
    assumptions: list[Assumption] = Field(default_factory=list[Assumption])
    # high: stated evidence only; medium: known conventions or defaults (assumptions);
    # low: an inconsistency or ambiguity was settled by the most plausible reading.
    confidence: Literal["high", "medium", "low"] = "high"
    # The quantity to buy per need date (the netting's purchase steps): a schedule, not
    # one lump on the first date.
    need_schedule: list[DatedQuantity] = Field(default_factory=list[DatedQuantity])
    # The gross demand per date, as the requirements give it.
    demand_schedule: list[DatedQuantity] = Field(default_factory=list[DatedQuantity])
    timing: TimingCheck | None = None
    expedite: list[ExpediteAction] = Field(default_factory=list[ExpediteAction])
    sourcing: SourcingProposal | None = None


class Assessment(BaseModel):
    lines: list[AssessmentLine] = Field(default_factory=list[AssessmentLine])
    issues: list[Issue] = Field(default_factory=list[Issue])
    # Assessment-wide readings and defaults; each line also lists those that concern it.
    assumptions: list[Assumption] = Field(default_factory=list[Assumption])
    record_counts: dict[str, int] = Field(default_factory=dict[str, int])


class TimingAlert(BaseModel):
    """A line that runs short before a new order can arrive, worded by code (the model
    only translates it), with what to do in order: expedites first."""

    part_number: str
    description: str | None = None
    outcome: Literal["unavoidable_stockout", "meets_consuming_safety_stock", "unknown"]
    summary: str
    actions: list[str] = Field(default_factory=list[str])


class EvidenceTable(BaseModel):
    source_id: str
    name: str
    # Every value retains its source spelling; adapters preserve formatted identifiers.
    rows: list[list[str | None]]
    layout: Literal["table", "text"] = "table"
    # 1-based (row, column) of cells the file stored as numbers, not text: their
    # rendering has no locale, so they neither need nor prove a decimal separator.
    numbers: set[tuple[int, int]] = Field(default_factory=set[tuple[int, int]])


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
    # The resolved planning scope the quantities were calculated with.
    scope: PlanningScope | None = None
    # How the buyer's request became that scope: assumptions and open questions.
    scope_resolution: ScopeResolution | None = None
    planning_candidates: list[PlanningCandidate] = Field(default_factory=list[PlanningCandidate])
    snapshot_dates: list[date] = Field(default_factory=list[date])
    coverage: list[Coverage] = Field(default_factory=list[Coverage])
    # Tables read by a report pack (no model call) and the conventions applied.
    reports: list[RecognizedReport] = Field(default_factory=list[RecognizedReport])
    status: Literal["assessed", "needs_review"] = "needs_review"
    persisted: bool = False
    assessment_id: UUID | None = None
    rfq_id: UUID | None = None
    # Spend per currency of the lines to buy, at the internal target and last prices.
    # Amounts in different currencies are never added up or converted.
    totals: list[SpendTotal] = Field(default_factory=list[SpendTotal])
    # Every line that runs short before a new order arrives: stockouts first, then lines
    # that consume their safety stock until the purchase arrives.
    timing_alerts: list[TimingAlert] = Field(default_factory=list[TimingAlert])


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
    # The header cell's text at that column, copied verbatim: code rejects the section
    # when it does not match (an off-by-one column would map the wrong values).
    header_text: str | None = None
    # Repeat the value of the nearest row above when the source leaves a grouping key
    # blank under its first row (identifier columns only; disclosed).
    fill_down: bool = False


DateOrder = Literal["day_first", "month_first"]


class ConstantMapping(BaseModel):
    """A report-level value (a title or header cell) that applies to a whole section.

    `ref` may lie anywhere in the same table outside the section's data rows. `text`,
    when given, is the exact part of that cell holding the value; code finds it there.
    """

    field: str
    ref: CellRef
    text: str | None = None


VocabularyTerm = Literal[
    "open", "closed", "cancelled", "pending", "received", "planned", "released", "approved",
    "blocked", "receipt", "reversal", "return", "gross", "net", "available", "count", "kg",
    "g", "t", "lb", "l", "ml", "m", "cm", "mm", "m2", "m3", "kit", "set", "pair", "box",
    "pack", "roll", "requirement", "proposal", "stock", "safety_stock", "negative",
    "positive",
]  # fmt: skip


class ValueMapping(BaseModel):
    """The meaning of one distinct source spelling of a categorical column.

    `source` must occur in the section; `canonical` must be a term of the task's
    vocabulary for that kind and field. Code copies the spelling, never this text.
    """

    field: str
    source: str
    canonical: VocabularyTerm


class RecordProposal(BaseModel):
    kind: RecordKind
    fields: list[FieldMapping]
    # How the record's numeric dates are written, when the source's language establishes it.
    date_order: DateOrder | None = None


class TableProposal(BaseModel):
    kind: RecordKind
    header_row: int = Field(ge=1)
    first_row: int = Field(ge=1)
    last_row: int = Field(ge=1)
    # Lists keep the model's strict output schema closed while fields vary by kind.
    # Each assignment names a 1-based column; values are always copied by code.
    columns: list[ColumnMapping]
    constants: list[ConstantMapping] = Field(default_factory=list[ConstantMapping])
    value_maps: list[ValueMapping] = Field(default_factory=list[ValueMapping])
    date_order: DateOrder | None = None
    # Fields every record of the section has. A row without them (a group heading, a
    # note) is not a record; a value it holds in a fill_down column still applies to the
    # rows below it.
    required_fields: list[str] = Field(default_factory=list[str])


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


class ShownRow(BaseModel):
    row: int
    cells: list[str | None]


class RowBand(BaseModel):
    """Consecutive rows with content; blank rows separate bands."""

    first_row: int
    last_row: int


class RowShape(BaseModel):
    """Rows whose cells hold the same kinds of values in the same columns.

    `pattern` has one letter per column, from the first: t text, n number, d date and
    . blank (trailing blanks left out).
    """

    pattern: str
    rows: int
    examples: list[int]


class SpellingCount(BaseModel):
    value: str
    rows: int


class ColumnProfile(BaseModel):
    """What one column holds over every row of the table."""

    column: int
    filled: int
    numbers: int
    dates: int
    # Distinct spellings, ignoring case and spacing.
    distinct: int
    # A text column's spellings, most frequent first (all of them when they are few); a
    # number or date column's most frequent ones, to show how it writes them.
    values: list[SpellingCount]


class TableSummary(BaseModel):
    """A table too large to read whole: what code counted over every row, and the rows it
    chose to show (by their row numbers)."""

    rows: int
    columns: int
    shown: list[ShownRow]
    bands: list[RowBand]
    bands_left_out: int = 0
    shapes: list[RowShape]
    # Rows in shapes not listed.
    shapes_left_out: int = 0
    profiles: list[ColumnProfile]


class MappingTask(BaseModel):
    # What every call of an analysis shares comes first, so the prompt prefix is cached.
    catalog: dict[str, list[str]]
    # Closed canonical terms for value_maps: "<kind>.<field>" keys, and "unit" for units.
    vocabulary: dict[str, list[str]] = Field(default_factory=dict[str, list[str]])
    manifest: list[TableManifest]
    objective: str
    source_id: str
    table: str
    layout: Literal["table", "text"]
    # The rows the task covers, 1-based and inclusive: the whole table, or a text page.
    first_row: int
    last_row: int
    # Every row from first_row to last_row; empty when `summary` stands for a table.
    rows: list[list[str | None]] = Field(default_factory=list[list[str | None]])
    summary: TableSummary | None = None
    # A text page's previous page's sections.
    previous_sections: list[TableProposal] = Field(default_factory=list[TableProposal])
    # Set on a second attempt for the same table or page: why parts of the first answer
    # were not read (code-checked problems, never source content). Empty at first.
    retry_feedback: list[str] = Field(default_factory=list[str])
