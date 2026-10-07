"""Purchase analysis services: import company data, analyze a build, draft the RFQ.

Every function takes the caller's `Principal` and works in one
`tenant_session` for that tenant (forced RLS). The numbers come from
`bido_back.purchasing.analysis` (code); no model is involved here.

Imports read a file uploaded with `POST /v1/onboarding/uploads` (the same
upload store; only its uploader may import it). Each import is one
transaction: rows with errors are reported and skipped, the others are
written, and a dry run writes nothing.

* items: upserted by part number.
* bom: each product in the file has its BOM replaced by the file's rows.
* inventory: upserted per item and location.
* purchase_history: suppliers matched by name (case-insensitive) or created;
  orders upserted per (supplier, PO number), lines per (order, line number).
* production_plan: upserted per (product, need-by date).

Unknown part numbers are created as items (description = the row's, or the
part number) so a file is never rejected for that.
"""

import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.catalog.contracts import LineCategory
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import (
    Analysis,
    AnalysisLine,
    BomLine,
    Contact,
    InventoryRow,
    Item,
    PoLine,
    ProductionPlanRow,
    PurchaseOrder,
    Supplier,
    SupplierContact,
    TemplateUpload,
)
from bido_back.purchasing import analysis as calc
from bido_back.purchasing.importers import (
    ImportFileError,
    ImportType,
    ParsedTable,
    parse_rows,
    read_table,
)
from bido_back.purchasing.schemas import (
    AnalysisLineView,
    AnalysisRequest,
    AnalysisSummary,
    AnalysisView,
    FlagView,
    ImportReport,
    ImportRequest,
    ImportRowError,
    InvitedSupplier,
    ProductView,
    RfqFromAnalysis,
    RfqFromAnalysisRequest,
    SkippedSupplier,
    SupplierStatView,
)
from bido_back.quotes.parsing import DecimalSeparator
from bido_back.rfqs import service as rfq_service
from bido_back.storage import BlobStorage

logger = logging.getLogger(__name__)

Db = AsyncEngine | async_sessionmaker[AsyncSession]

DEFAULT_CURRENCY: Final = "EUR"
_DRAWING_CATEGORIES: Final = frozenset(
    {"drawing", "desenho", "disegno", "custom", "usinado", "usinagem", "machined", "casting",
     "fundido", "fusione", "lavorazione", "mechanical", "mecanico", "meccanica"}
)  # fmt: skip
_MATERIAL_CATEGORIES: Final = frozenset(
    {"material", "raw material", "materia prima", "materiale", "materie prime", "chemical",
     "quimico", "chimico"}
)  # fmt: skip


class PurchasingError(Exception):
    """A refusal a caller can act on. Messages carry ids and field names only."""


class NotFound(PurchasingError):
    pass


class InvalidRequest(PurchasingError):
    pass


class Conflict(PurchasingError):
    pass


# --- Imports ------------------------------------------------------------------------------


@dataclass(slots=True)
class _Counts:
    created: dict[str, int]
    updated: dict[str, int]

    def add(self, kind: str, *, created: bool) -> None:
        target = self.created if created else self.updated
        target[kind] = target.get(kind, 0) + 1


async def run_import(
    db: Db, storage: BlobStorage, principal: Principal, request: ImportRequest
) -> ImportReport:
    async with tenant_session(db, principal.tenant_id) as session:
        upload = await session.scalar(
            select(TemplateUpload).where(
                TemplateUpload.id == request.upload_id,
                TemplateUpload.user_id == principal.user_id,
            )
        )
        if upload is None:
            raise NotFound(f"upload {request.upload_id} not found")
        key, filename = upload.storage_key, upload.filename

    data = await storage.get(key)
    try:
        raw_rows = read_table(data, filename, request.sheet)
    except ImportFileError as exc:
        raise InvalidRequest(str(exc)) from exc
    return await import_rows(
        db,
        principal,
        request.type,
        raw_rows,
        dry_run=request.dry_run,
        decimal=request.decimal_separator,
    )


async def import_rows(
    db: Db,
    principal: Principal,
    import_type: ImportType,
    raw_rows: Sequence[Sequence[object]],
    *,
    dry_run: bool = False,
    decimal: DecimalSeparator | None = None,
) -> ImportReport:
    """Import rows already read from a file (header first). Used by `run_import`."""
    try:
        table = parse_rows(import_type, raw_rows, decimal=decimal)
    except ImportFileError as exc:
        raise InvalidRequest(str(exc)) from exc

    counts = _Counts(created={}, updated={})
    if not table.missing_required and table.rows and not dry_run:
        async with tenant_session(db, principal.tenant_id) as session:
            await _apply(session, principal.tenant_id, table, counts)
    logger.info(
        "data import tenant=%s type=%s rows=%d ok=%d dry_run=%s",
        principal.tenant_id,
        import_type,
        table.total_rows,
        len(table.rows),
        dry_run,
    )
    return _report(import_type, dry_run, table, counts)


def _report(
    import_type: ImportType, dry_run: bool, table: ParsedTable, counts: _Counts
) -> ImportReport:
    failed = table.total_rows - len(table.rows)
    return ImportReport(
        type=import_type,
        dry_run=dry_run,
        rows_total=table.total_rows,
        rows_imported=0 if (dry_run or table.missing_required) else len(table.rows),
        rows_failed=failed,
        columns=table.columns,
        missing_required=table.missing_required,
        unknown_columns=table.unknown_columns,
        errors=[ImportRowError(row=e.row, field=e.field, message=e.message) for e in table.errors],
        created=counts.created,
        updated=counts.updated,
    )


async def _apply(
    session: AsyncSession, tenant_id: UUID, table: ParsedTable, counts: _Counts
) -> None:
    items = _ItemCache(session, tenant_id, counts)
    match table.import_type:
        case "items":
            for _, row in table.rows:
                await items.upsert(row, overwrite=True)
        case "bom":
            await _apply_bom(session, tenant_id, table, items, counts)
        case "inventory":
            await _apply_inventory(session, tenant_id, table, items, counts)
        case "purchase_history":
            await _apply_purchases(session, tenant_id, table, items, counts)
        case "production_plan":
            await _apply_plan(session, tenant_id, table, counts)


class _ItemCache:
    def __init__(self, session: AsyncSession, tenant_id: UUID, counts: _Counts) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.counts = counts
        self.by_number: dict[str, Item] = {}

    async def upsert(self, row: dict[str, object], *, overwrite: bool) -> Item:
        number = str(row["part_number"])
        item = self.by_number.get(number)
        if item is None:
            item = await self.session.scalar(select(Item).where(Item.part_number == number))
        if item is None:
            item = Item(
                tenant_id=self.tenant_id,
                part_number=number,
                description=str(row.get("description") or number),
                mpn=_opt(row.get("mpn")),
                manufacturer=_opt(row.get("manufacturer")),
                category=_opt(row.get("category")),
                unit=str(row.get("unit") or "pcs"),
            )
            self.session.add(item)
            await self.session.flush()
            self.counts.add("items", created=True)
        elif overwrite or any(row.get(k) for k in ("mpn", "manufacturer", "category")):
            for name in ("description", "mpn", "manufacturer", "category", "unit"):
                value = row.get(name)
                if value is not None and (overwrite or getattr(item, name) in (None, "")):
                    setattr(item, name, str(value))
            if overwrite:
                self.counts.add("items", created=False)
        self.by_number[number] = item
        return item


def _opt(value: object) -> str | None:
    return None if value is None else str(value)


def _dec(value: object, default: Decimal | None = None) -> Decimal | None:
    return value if isinstance(value, Decimal) else default


async def _apply_bom(
    session: AsyncSession, tenant_id: UUID, table: ParsedTable, items: _ItemCache, counts: _Counts
) -> None:
    by_product: dict[str, list[dict[str, object]]] = defaultdict(list)
    for _, row in table.rows:
        by_product[str(row["product"])].append(row)
    for product, rows in by_product.items():
        await session.execute(delete(BomLine).where(BomLine.product == product))
        quantities: dict[UUID, Decimal] = {}
        for row in rows:
            item = await items.upsert(row, overwrite=False)
            qty = _dec(row["qty_per_unit"], Decimal(0)) or Decimal(0)
            quantities[item.id] = quantities.get(item.id, Decimal(0)) + qty
        for item_id, qty in quantities.items():
            session.add(
                BomLine(tenant_id=tenant_id, product=product, item_id=item_id, qty_per_unit=qty)
            )
            counts.add("bom_lines", created=True)
    await session.flush()


async def _apply_inventory(
    session: AsyncSession, tenant_id: UUID, table: ParsedTable, items: _ItemCache, counts: _Counts
) -> None:
    for _, row in table.rows:
        item = await items.upsert(row, overwrite=False)
        location = str(row.get("location") or "")
        stock = await session.scalar(
            select(InventoryRow).where(
                InventoryRow.item_id == item.id, InventoryRow.location == location
            )
        )
        created = stock is None
        if stock is None:
            stock = InventoryRow(tenant_id=tenant_id, item_id=item.id, location=location)
            session.add(stock)
        stock.on_hand = _dec(row.get("on_hand"), Decimal(0)) or Decimal(0)
        stock.on_order = _dec(row.get("on_order"), Decimal(0)) or Decimal(0)
        as_of = row.get("as_of")
        stock.as_of = as_of if isinstance(as_of, date) else None
        counts.add("inventory", created=created)
    await session.flush()


async def _apply_purchases(
    session: AsyncSession, tenant_id: UUID, table: ParsedTable, items: _ItemCache, counts: _Counts
) -> None:
    suppliers: dict[str, Supplier] = {}
    orders: dict[tuple[UUID, str], PurchaseOrder] = {}
    next_line: dict[UUID, int] = defaultdict(int)
    for _, row in table.rows:
        name = str(row["supplier"])
        supplier = suppliers.get(name.lower())
        if supplier is None:
            supplier = await session.scalar(
                select(Supplier).where(func.lower(Supplier.name) == name.lower()).limit(1)
            )
            if supplier is None:
                supplier = Supplier(tenant_id=tenant_id, name=name, type="distributor")
                session.add(supplier)
                await session.flush()
                counts.add("suppliers", created=True)
            suppliers[name.lower()] = supplier

        number = str(row["po_number"])
        currency = str(row.get("currency") or DEFAULT_CURRENCY).upper()
        ordered_at = row["ordered_at"]
        assert isinstance(ordered_at, date)
        order = orders.get((supplier.id, number))
        if order is None:
            order = await session.scalar(
                select(PurchaseOrder).where(
                    PurchaseOrder.supplier_id == supplier.id, PurchaseOrder.po_number == number
                )
            )
            if order is None:
                order = PurchaseOrder(
                    tenant_id=tenant_id,
                    po_number=number,
                    supplier_id=supplier.id,
                    currency=currency,
                    ordered_at=ordered_at,
                )
                session.add(order)
                await session.flush()
                counts.add("purchase_orders", created=True)
            else:
                order.currency = currency
                order.ordered_at = ordered_at
            orders[(supplier.id, number)] = order

        item = await items.upsert(row, overwrite=False)
        line_no = row.get("line_no")
        if not isinstance(line_no, int):
            next_line[order.id] += 1
            line_no = next_line[order.id]
        quantity = _dec(row["quantity"], Decimal(0)) or Decimal(0)
        received_qty = _dec(row.get("received_qty"))
        received_at = row.get("received_at")
        if received_qty is None and isinstance(received_at, date):
            received_qty = quantity
        rejected = _dec(row.get("rejected_qty"))
        if rejected is None:
            rejected = (
                (received_qty or Decimal(0)) if row.get("quality_ok") is False else Decimal(0)
            )

        line = await session.scalar(
            select(PoLine).where(PoLine.purchase_order_id == order.id, PoLine.line_no == line_no)
        )
        created = line is None
        if line is None:
            line = PoLine(tenant_id=tenant_id, purchase_order_id=order.id, line_no=line_no)
            session.add(line)
        line.item_id = item.id
        line.quantity = quantity
        line.unit_price = _dec(row["unit_price"], Decimal(0)) or Decimal(0)
        promised = row.get("promised_at")
        line.promised_at = promised if isinstance(promised, date) else None
        line.received_at = received_at if isinstance(received_at, date) else None
        line.received_qty = received_qty
        line.rejected_qty = rejected
        counts.add("po_lines", created=created)
    await session.flush()


async def _apply_plan(
    session: AsyncSession, tenant_id: UUID, table: ParsedTable, counts: _Counts
) -> None:
    for _, row in table.rows:
        product = str(row["product"])
        need_by = row["need_by"]
        assert isinstance(need_by, date)
        plan = await session.scalar(
            select(ProductionPlanRow).where(
                ProductionPlanRow.product == product, ProductionPlanRow.need_by == need_by
            )
        )
        created = plan is None
        if plan is None:
            plan = ProductionPlanRow(tenant_id=tenant_id, product=product, need_by=need_by)
            session.add(plan)
        plan.quantity = _dec(row["quantity"], Decimal(0)) or Decimal(0)
        counts.add("production_plan", created=created)
    await session.flush()


# --- Products -----------------------------------------------------------------------------


async def list_products(
    db: Db, principal: Principal, today: date | None = None
) -> list[ProductView]:
    today = today or datetime.now(UTC).date()
    async with tenant_session(db, principal.tenant_id) as session:
        boms = dict(
            (await session.execute(
                select(BomLine.product, func.count()).group_by(BomLine.product)
            )).all()
        )  # fmt: skip
        plans = (
            await session.execute(
                select(
                    ProductionPlanRow.product,
                    func.sum(ProductionPlanRow.quantity),
                    func.min(ProductionPlanRow.need_by),
                )
                .where(ProductionPlanRow.need_by >= today)
                .group_by(ProductionPlanRow.product)
            )
        ).all()
        planned = {product: (qty, need_by) for product, qty, need_by in plans}
    names = sorted(set(boms) | set(planned))
    return [
        ProductView(
            product=name,
            bom_items=boms.get(name, 0),
            planned_quantity=planned[name][0] if name in planned else None,
            next_need_by=planned[name][1] if name in planned else None,
        )
        for name in names
    ]


async def _resolve_product(session: AsyncSession, wanted: str) -> str:
    products = list(await session.scalars(select(BomLine.product).distinct()))
    if not products:
        raise NotFound("no bill of materials imported yet")
    exact = [p for p in products if p.lower() == wanted.strip().lower()]
    if exact:
        return exact[0]
    needle = wanted.strip().lower()
    partial = [p for p in products if needle in p.lower()]
    if len(partial) == 1:
        return partial[0]
    raise NotFound(
        f"no single product matches {wanted!r}; products: {', '.join(sorted(products)[:20])}"
    )


# --- Analyses -----------------------------------------------------------------------------


async def create_analysis(
    db: Db, principal: Principal, request: AnalysisRequest, today: date | None = None
) -> AnalysisView:
    today = today or datetime.now(UTC).date()
    params = calc.Params(
        safety_stock_pct=request.safety_stock_pct, lookback_days=request.lookback_days
    )
    async with tenant_session(db, principal.tenant_id) as session:
        product = await _resolve_product(session, request.product)
        quantity, need_by = request.quantity, request.need_by
        if quantity is None:
            planned: Decimal | None = await session.scalar(
                select(func.sum(ProductionPlanRow.quantity)).where(
                    ProductionPlanRow.product == product, ProductionPlanRow.need_by >= today
                )
            )
            if planned is None:
                raise InvalidRequest(
                    f"no production plan for {product!r} from today; give a quantity"
                )
            first: date | None = await session.scalar(
                select(func.min(ProductionPlanRow.need_by)).where(
                    ProductionPlanRow.product == product, ProductionPlanRow.need_by >= today
                )
            )
            quantity, need_by = Decimal(planned), need_by or first

        bom = await _load_bom(session, product)
        stock = await _load_stock(session, [entry.item.id for entry in bom])
        purchases = await _load_purchases(session, [entry.item.id for entry in bom])
        result = calc.analyze(
            product=product,
            quantity=quantity,
            need_by=need_by,
            bom=bom,
            stock=stock,
            purchases=purchases,
            today=today,
            params=params,
        )
        record = Analysis(
            tenant_id=principal.tenant_id,
            product=product,
            quantity=quantity,
            need_by=need_by,
            params={
                "safety_stock_pct": str(params.safety_stock_pct),
                "lookback_days": params.lookback_days,
                "max_over_pct": str(params.max_over_pct),
                "today": today.isoformat(),
            },
            summary=_summary_json(result.summary),
            created_by=principal.user_id,
        )
        session.add(record)
        await session.flush()
        for line in result.lines:
            session.add(_line_row(principal.tenant_id, record.id, line))
        await session.flush()
        await session.refresh(record)
        view = await _view(session, record)
    logger.info(
        "purchase analysis tenant=%s analysis=%s items=%d",
        principal.tenant_id,
        record.id,
        len(result.lines),
    )
    return view


async def get_analysis(db: Db, principal: Principal, analysis_id: UUID) -> AnalysisView:
    async with tenant_session(db, principal.tenant_id) as session:
        record = await session.get(Analysis, analysis_id)
        if record is None:
            raise NotFound(f"analysis {analysis_id} not found")
        return await _view(session, record)


async def _load_bom(session: AsyncSession, product: str) -> list[calc.BomEntry]:
    rows = await session.execute(
        select(BomLine, Item)
        .join(Item, Item.id == BomLine.item_id)
        .where(BomLine.product == product)
        .order_by(Item.part_number)
    )
    return [
        calc.BomEntry(item=_item_info(item), qty_per_unit=bom.qty_per_unit) for bom, item in rows
    ]


def _item_info(item: Item) -> calc.ItemInfo:
    return calc.ItemInfo(
        id=item.id,
        part_number=item.part_number,
        description=item.description,
        mpn=item.mpn,
        manufacturer=item.manufacturer,
        category=item.category,
        unit=item.unit,
    )


async def _load_stock(session: AsyncSession, item_ids: Sequence[UUID]) -> dict[UUID, calc.Stock]:
    if not item_ids:
        return {}
    rows = await session.execute(
        select(
            InventoryRow.item_id, func.sum(InventoryRow.on_hand), func.sum(InventoryRow.on_order)
        )
        .where(InventoryRow.item_id.in_(item_ids))
        .group_by(InventoryRow.item_id)
    )
    return {
        item_id: calc.Stock(on_hand=Decimal(on_hand), on_order=Decimal(on_order))
        for item_id, on_hand, on_order in rows
    }


async def _load_purchases(session: AsyncSession, item_ids: Sequence[UUID]) -> list[calc.Purchase]:
    if not item_ids:
        return []
    rows = await session.execute(
        select(PoLine, PurchaseOrder, Supplier)
        .join(PurchaseOrder, PurchaseOrder.id == PoLine.purchase_order_id)
        .join(Supplier, Supplier.id == PurchaseOrder.supplier_id)
        .where(PoLine.item_id.in_(item_ids))
        .order_by(PurchaseOrder.ordered_at, PurchaseOrder.po_number, PoLine.line_no)
    )
    return [
        calc.Purchase(
            item_id=line.item_id,
            supplier_id=supplier.id,
            supplier_name=supplier.name,
            supplier_type=supplier.type,
            currency=order.currency,
            ordered_at=order.ordered_at,
            quantity=line.quantity,
            unit_price=line.unit_price,
            promised_at=line.promised_at,
            received_at=line.received_at,
            received_qty=line.received_qty,
            rejected_qty=line.rejected_qty,
        )
        for line, order, supplier in rows
    ]


def _num(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _summary_json(summary: calc.Summary) -> dict[str, Any]:
    return {
        "items": summary.items,
        "items_to_buy": summary.items_to_buy,
        "currency": summary.currency,
        "total_at_target": _num(summary.total_at_target),
        "total_at_last_price": _num(summary.total_at_last_price),
        "flagged_items": summary.flagged_items,
    }


def _line_row(tenant_id: UUID, analysis_id: UUID, line: calc.LineResult) -> AnalysisLine:
    suppliers: list[JsonValue] = [
        {
            "supplier_id": str(s.supplier_id),
            "name": s.name,
            "type": s.type,
            "orders": s.orders,
            "quantity": str(s.quantity),
            "last_price": str(s.last_price),
            "last_ordered_at": s.last_ordered_at.isoformat(),
            "avg_price": str(s.avg_price),
            "min_price": str(s.min_price),
            "on_time_rate": _num(s.on_time_rate),
            "quality_rate": _num(s.quality_rate),
            "suggested": s.suggested,
        }
        for s in line.suppliers
    ]
    flags: list[JsonValue] = [
        {"code": f.code, "supplier": f.supplier, "value": _num(f.value)} for f in line.flags
    ]
    return AnalysisLine(
        tenant_id=tenant_id,
        analysis_id=analysis_id,
        item_id=line.item.id,
        position=line.position,
        required=line.required,
        on_hand=line.on_hand,
        on_order=line.on_order,
        to_buy=line.to_buy,
        unit=line.item.unit,
        need_by=line.need_by,
        currency=line.currency,
        last_price=line.last_price,
        avg_price=line.avg_price,
        min_price=line.min_price,
        trend_pct=line.trend_pct,
        target_price=line.target_price,
        max_price=line.max_price,
        suppliers=suppliers,
        flags=flags,
    )


def _qty(value: Decimal) -> Decimal:
    """A quantity without the column's trailing zeros: 20, 12.5 (not 20.000000)."""
    if value == value.to_integral_value():
        return value.quantize(Decimal(1))
    return value.normalize()


def _money(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(calc.PRICE_PLACES)


def _pct(value: Decimal | None) -> Decimal | None:
    return None if value is None else value.quantize(calc.PCT_PLACES)


async def _view(session: AsyncSession, record: Analysis) -> AnalysisView:
    rows = await session.execute(
        select(AnalysisLine, Item)
        .join(Item, Item.id == AnalysisLine.item_id)
        .where(AnalysisLine.analysis_id == record.id)
        .order_by(AnalysisLine.position)
    )
    params: dict[str, Any] = record.params
    return AnalysisView(
        id=record.id,
        product=record.product,
        quantity=_qty(record.quantity),
        need_by=record.need_by,
        safety_stock_pct=Decimal(str(params.get("safety_stock_pct", "0"))),
        lookback_days=int(params.get("lookback_days", 365)),
        summary=AnalysisSummary.model_validate(record.summary),
        lines=[
            AnalysisLineView(
                id=line.id,
                position=line.position,
                item_id=item.id,
                part_number=item.part_number,
                description=item.description,
                mpn=item.mpn,
                manufacturer=item.manufacturer,
                unit=line.unit,
                required=_qty(line.required),
                on_hand=_qty(line.on_hand),
                on_order=_qty(line.on_order),
                to_buy=_qty(line.to_buy),
                need_by=line.need_by,
                currency=line.currency,
                last_price=_money(line.last_price),
                avg_price=_money(line.avg_price),
                min_price=_money(line.min_price),
                trend_pct=_pct(line.trend_pct),
                target_price=_money(line.target_price),
                max_price=_money(line.max_price),
                suppliers=[SupplierStatView.model_validate(s) for s in line.suppliers],
                flags=[FlagView.model_validate(f) for f in line.flags],
                rfq_line_id=line.rfq_line_id,
            )
            for line, item in rows
        ],
        rfq_id=record.rfq_id,
        created_at=record.created_at,
    )


# --- RFQ from an analysis -----------------------------------------------------------------


def rfq_category(item_category: str | None) -> LineCategory:
    """The RFQ line category for an item's free-text category."""
    text = (item_category or "").strip().lower()
    if text in _DRAWING_CATEGORIES:
        return "drawing"
    if text in _MATERIAL_CATEGORIES:
        return "material"
    return "catalog"


async def create_rfq_from_analysis(
    db: Db, principal: Principal, analysis_id: UUID, request: RfqFromAnalysisRequest
) -> RfqFromAnalysis:
    """A DRAFT RFQ with the analysis's lines to buy, its suggested suppliers invited
    as DRAFT invites. Nothing is sent; preparing the send asks for approval."""
    async with tenant_session(db, principal.tenant_id) as session:
        record = await session.get(Analysis, analysis_id, with_for_update=True)
        if record is None:
            raise NotFound(f"analysis {analysis_id} not found")
        if record.rfq_id is not None:
            raise Conflict(f"analysis {analysis_id} already has an RFQ")
        rows = list(
            (
                await session.execute(
                    select(AnalysisLine, Item)
                    .join(Item, Item.id == AnalysisLine.item_id)
                    .where(AnalysisLine.analysis_id == analysis_id, AnalysisLine.to_buy > 0)
                    .order_by(AnalysisLine.position)
                )
            ).all()
        )
        if not rows:
            raise InvalidRequest("nothing to buy: stock and open orders cover the build")

        title = request.title or f"{record.product}: {record.quantity.normalize():f} un"
        rfq, rfq_lines = await rfq_service.create_draft_rfq(
            session,
            principal,
            title=title,
            lines=[
                rfq_service.NewRfqLine(
                    description=item.description,
                    quantity=line.to_buy,
                    unit=line.unit,
                    mpn=item.mpn,
                    manufacturer=item.manufacturer,
                    category=rfq_category(item.category),
                    need_by=line.need_by,
                )
                for line, item in rows
            ],
            source_text=f"Purchase analysis {analysis_id}",
            response_due=request.response_due,
        )
        wanted: dict[UUID, str] = {}
        for (line, _), rfq_line in zip(rows, rfq_lines, strict=True):
            line.rfq_line_id = rfq_line.id
            suggested = [s for s in line.suppliers if s.get("suggested")]
            for supplier in suggested[: request.max_suppliers_per_line]:
                wanted.setdefault(UUID(str(supplier["supplier_id"])), str(supplier["name"]))
        record.rfq_id = rfq.id

        invited: list[InvitedSupplier] = []
        skipped: list[SkippedSupplier] = []
        for supplier_id, name in wanted.items():
            contact_id = await session.scalar(
                select(Contact.id)
                .join(SupplierContact, SupplierContact.contact_id == Contact.id)
                .where(
                    SupplierContact.supplier_id == supplier_id,
                    Contact.email.is_not(None),
                    Contact.pseudonymized_at.is_(None),
                )
                .order_by(Contact.created_at)
                .limit(1)
            )
            if contact_id is None:
                skipped.append(
                    SkippedSupplier(supplier_id=supplier_id, name=name, reason="no contact email")
                )
                continue
            await rfq_service.add_draft_invite(session, principal, rfq, supplier_id, contact_id)
            invited.append(InvitedSupplier(supplier_id=supplier_id, name=name))
        await session.flush()
        detail = await rfq_service.rfq_detail(session, rfq)
    logger.info(
        "rfq from analysis tenant=%s analysis=%s rfq=%s invites=%d",
        principal.tenant_id,
        analysis_id,
        rfq.id,
        len(invited),
    )
    return RfqFromAnalysis(analysis_id=analysis_id, rfq=detail, invited=invited, skipped=skipped)
