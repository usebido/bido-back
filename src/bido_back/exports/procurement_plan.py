"""The full purchase plan of a source assessment: one row per material and plant.

A large plan does not fit an answer: the Copilot writes a summary and the buyer gets
every row here, as a workbook (`/v1/procurement-assessments/{id}/plan.xlsx`) or page
by page (the Copilot's `procurement_plan_lines`). Both are built by code from the same
assessment, so the file, the pages and the counts always agree.

Nothing is stored: the workbook is rendered on request from the assessment snapshot.
The status of a row is derived here, in this order: a line with something to buy and
no usable supplier needs sourcing (`no_valid_source`); otherwise its timing check says
whether no source arrives before it runs out (`unavoidable_stockout`) or the purchase
arrives only after it starts consuming its safety stock (`consumes_safety_stock`); any
other line with something to buy is a `purchase`, and the rest are `covered`.
"""

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Literal, get_args

from bido_back.exports.render import Table, Value, to_xlsx_sheets
from bido_back.purchasing.sources.contracts import (
    AssessmentLine,
    SourceAssessment,
    SupplierEvidence,
)

PlanStatus = Literal[
    "purchase", "unavoidable_stockout", "consumes_safety_stock", "no_valid_source", "covered"
]
PLAN_STATUSES: Final[tuple[PlanStatus, ...]] = get_args(PlanStatus)
PlanLanguage = Literal["pt", "en"]

MAX_PAGE: Final = 100
DEFAULT_PAGE: Final = 50
# One sheet per plant up to this many plants; past it, the plan sheet's plant column.
MAX_PLANT_SHEETS: Final = 20
_NO_PLANT: Final = ""

# Money totals in the plan are rounded to cents; reference unit prices to 6 decimals
# (prices per 1,000 units divide down to fractions of a cent).
_CENTS: Final = Decimal("0.01")
_PRICE_PLACES: Final = Decimal("0.000001")

# Assumption codes about lot sizing, shown in the plan's "lot rules" column.
_LOT_CODES: Final = ("lot", "moq", "rounding", "order_unit", "minimum_order")


@dataclass(frozen=True, slots=True)
class PlanRow:
    part_number: str
    description: str | None
    plant: str | None
    unit: str | None
    free_stock: Decimal | None
    safety_stock: Decimal | None
    open_receipts: Decimal | None
    net_shortfall: Decimal | None
    quantity: Decimal | None
    lot_rules: str | None
    first_shortage: date | None
    first_stockout: date | None
    status: PlanStatus
    supplier: str | None
    supplier_code: str | None
    arrival: date | None
    unit_price: Decimal | None
    currency: str | None
    total: Decimal | None
    actions: list[str]
    affected_orders: list[str]
    assumptions: list[str]
    confidence: str


def _positive(value: Decimal | None) -> bool:
    return value is not None and value > 0


def line_status(line: AssessmentLine) -> PlanStatus:
    if not _positive(line.calculated_to_buy):
        return "covered"
    if line.sourcing is not None:
        return "no_valid_source"
    outcome = line.timing.outcome if line.timing is not None else None
    if outcome == "unavoidable_stockout":
        return "unavoidable_stockout"
    if outcome == "meets_consuming_safety_stock":
        return "consumes_safety_stock"
    return "purchase"


def _recommended(line: AssessmentLine) -> SupplierEvidence | None:
    """The timing check's pick, else the first suggested source; None without one."""
    for supplier in line.suppliers:
        if supplier.recommended:
            return supplier
    for supplier in line.suppliers:
        if supplier.suggested:
            return supplier
    return None


def _price(
    line: AssessmentLine, supplier: SupplierEvidence | None
) -> tuple[Decimal | None, str | None]:
    """A unit price to plan with: the source's valid agreement, else its last price, else
    the line's internal target. A reference, never a quotation."""
    if supplier is not None:
        if supplier.contract_price is not None and not supplier.contract_expired:
            return supplier.contract_price, supplier.contract_currency or supplier.currency
        if supplier.last_price is not None:
            return supplier.last_price, supplier.currency
    if line.price is not None:
        return line.price.target_price, line.price.currency
    return None, line.currency


def _action(action_text: str) -> str:
    return " ".join(action_text.split())


def _actions(line: AssessmentLine, lang: PlanLanguage) -> list[str]:
    actions: list[str] = []
    for expedite in line.expedite:
        who = expedite.supplier_name or expedite.supplier_code or ""
        order = f"{expedite.po_number}/{expedite.po_line}"
        if expedite.reason == "overdue":
            text = (
                f"Cobrar {order} ({who}), vencido em {expedite.current_date:%d/%m/%Y}"
                if lang == "pt"
                else f"Chase {order} ({who}), due {expedite.current_date.isoformat()}"
            )
        else:
            text = (
                f"Antecipar {order} ({who}): {expedite.current_date:%d/%m/%Y} → "
                f"{expedite.requested_date:%d/%m/%Y}"
                if lang == "pt"
                else f"Expedite {order} ({who}): {expedite.current_date.isoformat()} → "
                f"{expedite.requested_date.isoformat()}"
            )
        actions.append(_action(text))
    if line.sourcing is not None:
        actions.append(
            "Buscar fornecedor (sem fonte válida)"
            if lang == "pt"
            else "Find a supplier (no valid source)"
        )
    return actions


def _affected(line: AssessmentLine) -> list[str]:
    if line.timing is None:
        return []
    affected: list[str] = []
    for demand in line.timing.affected_demand:
        parts = [demand.reference or "", demand.product_code or ""]
        label = " ".join(p for p in parts if p)
        when = demand.need_by.isoformat()
        affected.append(f"{label} ({when})" if label else when)
    return affected


def _lot_rules(line: AssessmentLine) -> str | None:
    rules = [a.message for a in line.assumptions if any(code in a.code for code in _LOT_CODES)]
    return " ".join(rules) or None


def plan_rows(assessment: SourceAssessment, lang: PlanLanguage = "pt") -> list[PlanRow]:
    """Every line of the assessment as a plan row, in the assessment's order."""
    rows: list[PlanRow] = []
    for line in assessment.lines:
        supplier = _recommended(line)
        unit_price, currency = _price(line, supplier)
        quantity = line.calculated_to_buy
        status = line_status(line)
        if unit_price is not None:
            exponent = unit_price.as_tuple().exponent
            if isinstance(exponent, int) and exponent < -6:
                unit_price = unit_price.quantize(_PRICE_PLACES, rounding=ROUND_HALF_UP)
        total = (
            (unit_price * quantity).quantize(_CENTS, rounding=ROUND_HALF_UP)
            if unit_price is not None and quantity is not None and quantity > 0
            else None
        )
        rows.append(
            PlanRow(
                part_number=line.part_number,
                description=line.description,
                plant=line.location,
                unit=line.unit,
                free_stock=line.available,
                safety_stock=line.safety_stock,
                open_receipts=line.on_order,
                net_shortfall=(
                    line.net_requirement if line.net_requirement is not None else quantity
                ),
                quantity=quantity,
                lot_rules=_lot_rules(line),
                first_shortage=line.timing.first_shortage_date if line.timing else None,
                first_stockout=line.timing.first_stockout_date if line.timing else None,
                status=status,
                supplier=supplier.name if supplier is not None and status != "covered" else None,
                supplier_code=(
                    supplier.supplier_code if supplier is not None and status != "covered" else None
                ),
                arrival=(
                    supplier.estimated_arrival
                    if supplier is not None and status != "covered"
                    else None
                ),
                unit_price=unit_price if status != "covered" else None,
                currency=currency if status != "covered" else None,
                total=total,
                actions=_actions(line, lang),
                affected_orders=_affected(line),
                assumptions=[a.message for a in line.assumptions],
                confidence=line.confidence,
            )
        )
    return rows


# --- Counts --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CurrencyTotal:
    currency: str
    lines: int
    total: Decimal


@dataclass(frozen=True, slots=True)
class PlanCounts:
    rows: int
    by_status: dict[PlanStatus, int]
    by_plant: dict[str, int]
    to_buy_by_plant: dict[str, int]
    # Amounts in different currencies are never added up or converted.
    totals: list[CurrencyTotal]
    priced_lines_without_currency: int


def plan_counts(rows: Sequence[PlanRow]) -> PlanCounts:
    by_status: Counter[PlanStatus] = Counter(row.status for row in rows)
    by_plant: Counter[str] = Counter(row.plant or _NO_PLANT for row in rows)
    to_buy: Counter[str] = Counter(
        row.plant or _NO_PLANT for row in rows if row.status != "covered"
    )
    sums: dict[str, Decimal] = {}
    lines: Counter[str] = Counter()
    unknown = 0
    for row in rows:
        if row.total is None:
            continue
        if not row.currency:
            unknown += 1
            continue
        sums[row.currency] = sums.get(row.currency, Decimal(0)) + row.total
        lines[row.currency] += 1
    return PlanCounts(
        rows=len(rows),
        by_status={status: by_status.get(status, 0) for status in PLAN_STATUSES},
        by_plant=dict(sorted(by_plant.items())),
        to_buy_by_plant=dict(sorted(to_buy.items())),
        totals=[CurrencyTotal(currency=c, lines=lines[c], total=sums[c]) for c in sorted(sums)],
        priced_lines_without_currency=unknown,
    )


# --- Pages ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlanPage:
    rows: list[PlanRow]
    matched: int
    page: int
    pages: int


def _matches(value: str | None, wanted: str) -> bool:
    return value is not None and value.strip().casefold() == wanted.strip().casefold()


def page_rows(
    rows: Sequence[PlanRow],
    *,
    plant: str | None = None,
    statuses: Iterable[PlanStatus] = (),
    material: str | None = None,
    page: int = 1,
    limit: int = DEFAULT_PAGE,
) -> PlanPage:
    """The rows matching every filter given, `limit` per page (pages count from 1)."""
    wanted = set(statuses)
    matched = [
        row
        for row in rows
        if (plant is None or _matches(row.plant, plant))
        and (not wanted or row.status in wanted)
        and (
            material is None
            or _matches(row.part_number, material)
            or (row.description or "").casefold().find(material.strip().casefold()) >= 0
        )
    ]
    limit = max(1, min(limit, MAX_PAGE))
    pages = max(1, -(-len(matched) // limit))
    page = max(1, min(page, pages))
    start = (page - 1) * limit
    return PlanPage(
        rows=matched[start : start + limit], matched=len(matched), page=page, pages=pages
    )


# --- Workbook ------------------------------------------------------------------------

_HEADERS: Final[dict[PlanLanguage, tuple[str, ...]]] = {
    "pt": (
        "Material",
        "Descrição",
        "Centro",
        "Unidade",
        "Estoque livre",
        "Estoque de segurança",
        "Pedidos abertos abatidos",
        "Falta líquida",
        "Quantidade a comprar",
        "Regras de lote",
        "Primeira falta",
        "Primeira ruptura",
        "Status",
        "Fornecedor recomendado",
        "Código do fornecedor",
        "Chegada estimada",
        "Preço unitário (referência)",
        "Moeda",
        "Total",
        "Ações",
        "Ordens afetadas",
        "Premissas",
        "Confiança",
    ),
    "en": (
        "Material",
        "Description",
        "Plant",
        "Unit",
        "Free stock",
        "Safety stock",
        "Open receipts netted",
        "Net shortfall",
        "Quantity to buy",
        "Lot rules",
        "First shortage",
        "First stockout",
        "Status",
        "Recommended supplier",
        "Supplier code",
        "Estimated arrival",
        "Unit price (reference)",
        "Currency",
        "Total",
        "Actions",
        "Affected orders",
        "Assumptions",
        "Confidence",
    ),
}
STATUS_LABELS: Final[dict[PlanLanguage, dict[PlanStatus, str]]] = {
    "pt": {
        "purchase": "Comprar",
        "unavoidable_stockout": "Ruptura inevitável",
        "consumes_safety_stock": "Consome estoque de segurança até a chegada",
        "no_valid_source": "Sem fonte válida",
        "covered": "Coberto, sem compra",
    },
    "en": {
        "purchase": "Buy",
        "unavoidable_stockout": "Unavoidable stockout",
        "consumes_safety_stock": "Consumes safety stock until arrival",
        "no_valid_source": "No valid source",
        "covered": "Covered, nothing to buy",
    },
}
_CONFIDENCE: Final[dict[PlanLanguage, dict[str, str]]] = {
    "pt": {"high": "Alta", "medium": "Média", "low": "Baixa"},
    "en": {"high": "High", "medium": "Medium", "low": "Low"},
}
_SHEETS: Final[dict[PlanLanguage, dict[str, str]]] = {
    "pt": {
        "summary": "Resumo",
        "plan": "Plano",
        "plant": "Centro",
        "actions": "Ações",
        "assumptions": "Premissas",
    },
    "en": {
        "summary": "Summary",
        "plan": "Plan",
        "plant": "Plant",
        "actions": "Actions",
        "assumptions": "Assumptions",
    },
}


def _row_values(row: PlanRow, lang: PlanLanguage) -> list[Value]:
    return [
        row.part_number,
        row.description,
        row.plant,
        row.unit,
        row.free_stock,
        row.safety_stock,
        row.open_receipts,
        row.net_shortfall,
        row.quantity,
        row.lot_rules,
        row.first_shortage,
        row.first_stockout,
        STATUS_LABELS[lang][row.status],
        row.supplier,
        row.supplier_code,
        row.arrival,
        row.unit_price,
        row.currency,
        row.total,
        "; ".join(row.actions) or None,
        "; ".join(row.affected_orders) or None,
        " ".join(row.assumptions) or None,
        _CONFIDENCE[lang].get(row.confidence, row.confidence),
    ]


def _plan_table(rows: Sequence[PlanRow], lang: PlanLanguage) -> Table:
    return Table(header=_HEADERS[lang], rows=[_row_values(row, lang) for row in rows])


def _summary_table(assessment: SourceAssessment, counts: PlanCounts, lang: PlanLanguage) -> Table:
    pt = lang == "pt"
    rows: list[list[Value]] = [
        ["Linhas (material × centro)" if pt else "Rows (material × plant)", counts.rows, None],
    ]
    for status in PLAN_STATUSES:
        rows.append([STATUS_LABELS[lang][status], counts.by_status[status], None])
    for plant, total in counts.by_plant.items():
        to_buy = counts.to_buy_by_plant.get(plant, 0)
        label = f"{'Centro' if pt else 'Plant'} {plant or '—'}"
        rows.append([label, total, f"{to_buy} {'com compra' if pt else 'to buy'}"])
    for total in counts.totals:
        rows.append(
            [
                f"{'Total estimado' if pt else 'Estimated total'} ({total.currency})",
                total.total,
                f"{total.lines} {'linhas' if pt else 'lines'}",
            ]
        )
    if counts.priced_lines_without_currency:
        rows.append(
            [
                "Linhas com preço sem moeda" if pt else "Priced lines without a currency",
                counts.priced_lines_without_currency,
                None,
            ]
        )
    if assessment.scope is not None:
        rows.append(
            ["Data de referência" if pt else "Reference date", assessment.scope.as_of, None]
        )
    rows.append(
        [
            "Moedas não são somadas nem convertidas"
            if pt
            else "Currencies are never added up or converted",
            None,
            None,
        ]
    )
    return Table(header=("Item", "Valor" if pt else "Value", "Nota" if pt else "Note"), rows=rows)


def _actions_table(rows: Sequence[PlanRow], lang: PlanLanguage) -> Table:
    pt = lang == "pt"
    return Table(
        header=("Material", "Centro" if pt else "Plant", "Status", "Ação" if pt else "Action"),
        rows=[
            [row.part_number, row.plant, STATUS_LABELS[lang][row.status], action]
            for row in rows
            for action in row.actions
        ],
    )


def _assumptions_table(assessment: SourceAssessment, lang: PlanLanguage) -> Table:
    pt = lang == "pt"
    rows: list[list[Value]] = []
    if assessment.scope_resolution is not None:
        rows.extend([a.message, None] for a in assessment.scope_resolution.assumptions)
    for assumption in assessment.assumptions:
        rows.append([assumption.message, ", ".join(assumption.part_numbers) or None])
    return Table(
        header=("Premissa" if pt else "Assumption", "Materiais" if pt else "Materials"),
        rows=rows,
    )


def plan_workbook(assessment: SourceAssessment, lang: PlanLanguage = "pt") -> bytes:
    """The whole plan as .xlsx: summary, every row, one sheet per plant, actions,
    assumptions."""
    rows = plan_rows(assessment, lang)
    counts = plan_counts(rows)
    names = _SHEETS[lang]
    sheets: list[tuple[str, Table]] = [
        (names["summary"], _summary_table(assessment, counts, lang)),
        (names["plan"], _plan_table(rows, lang)),
    ]
    plants = [p for p in counts.by_plant if p != _NO_PLANT]
    if 1 < len(plants) <= MAX_PLANT_SHEETS:
        for plant in plants:
            plant_rows = [row for row in rows if (row.plant or _NO_PLANT) == plant]
            sheets.append((f"{names['plant']} {plant}", _plan_table(plant_rows, lang)))
    sheets.append((names["actions"], _actions_table(rows, lang)))
    sheets.append((names["assumptions"], _assumptions_table(assessment, lang)))
    return to_xlsx_sheets(sheets)


def plan_filename(assessment: SourceAssessment, lang: PlanLanguage = "pt") -> str:
    stem = "plano-de-compras" if lang == "pt" else "purchase-plan"
    if assessment.scope is None:
        return f"{stem}.xlsx"
    return f"{stem}-{assessment.scope.as_of.isoformat()}.xlsx"
