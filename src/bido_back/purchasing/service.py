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

import difflib
import logging
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.agents.catalog.contracts import LineCategory
from bido_back.auth import Principal
from bido_back.comparison.loader import load_rates
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
    ParsedTable,
    TableImportType,
    parse_rows,
    read_table,
    supplier_type,
)
from bido_back.purchasing.schemas import (
    AnalysisLineView,
    AnalysisRequest,
    AnalysisSummary,
    AnalysisView,
    CalculationView,
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
from bido_back.rfqs.currency import fx_factor, resolve_currency
from bido_back.rfqs.errors import RfqError
from bido_back.rfqs.language import DEFAULT_LANGUAGE, country_language
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
    # Rows left out on purpose (e.g. an exact duplicate purchase line).
    skipped: dict[str, int] = field(default_factory=dict[str, int])

    def add(self, kind: str, *, created: bool) -> None:
        target = self.created if created else self.updated
        target[kind] = target.get(kind, 0) + 1

    def skip(self, kind: str) -> None:
        self.skipped[kind] = self.skipped.get(kind, 0) + 1


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
    import_type: TableImportType,
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
    import_type: TableImportType, dry_run: bool, table: ParsedTable, counts: _Counts
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
        skipped=counts.skipped,
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
        case "supplier_contacts":
            await _apply_contacts(session, tenant_id, table, counts)


async def apply_parsed(
    session: AsyncSession, tenant_id: UUID, table: ParsedTable
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    """Write one validated table in the caller's transaction: (created, updated, skipped)."""
    counts = _Counts(created={}, updated={})
    if not table.missing_required and table.rows:
        await _apply(session, tenant_id, table, counts)
    return counts.created, counts.updated, counts.skipped


async def _apply_contacts(
    session: AsyncSession, tenant_id: UUID, table: ParsedTable, counts: _Counts
) -> None:
    """Supplier contacts: the supplier by name (created when new), one contact per email."""
    suppliers: dict[str, Supplier] = {}
    for _, row in table.rows:
        name = str(row["supplier"]).strip()
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
        country = row.get("country")
        if isinstance(country, str) and country.strip() and not supplier.country:
            supplier.country = country.strip()[:60]
        language = row.get("language")
        if isinstance(language, str) and len(language.strip()) == 2:
            supplier.language = language.strip().lower()
        elif supplier.language in (None, DEFAULT_LANGUAGE):
            # No language column: write to them in their country's language.
            spoken = country_language(supplier.country)
            if spoken is not None:
                supplier.language = spoken
        kind = supplier_type(row.get("type"))
        if kind is not None:
            supplier.type = kind  # the buyer's supplier master says what it is
        email = str(row["email"]).strip().lower()
        existing = await session.scalar(
            select(Contact)
            .join(SupplierContact, SupplierContact.contact_id == Contact.id)
            .where(
                SupplierContact.supplier_id == supplier.id,
                func.lower(Contact.email) == email,
                Contact.pseudonymized_at.is_(None),
            )
            .limit(1)
        )
        if existing is not None:
            for field_name, value in (
                ("name", row.get("contact_name")),
                ("phone", row.get("phone")),
            ):
                if isinstance(value, str) and value.strip():
                    setattr(existing, field_name, value.strip())
            counts.add("contacts", created=False)
            continue
        contact = Contact(
            tenant_id=tenant_id,
            kind="supplier",
            name=_opt(row.get("contact_name")),
            email=email,
            phone=_opt(row.get("phone")),
        )
        session.add(contact)
        await session.flush()
        session.add(
            SupplierContact(tenant_id=tenant_id, supplier_id=supplier.id, contact_id=contact.id)
        )
        counts.add("contacts", created=True)
    await session.flush()


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
        as_of = row.get("as_of")
        if (
            not created
            and stock.as_of is not None
            and (not isinstance(as_of, date) or as_of < stock.as_of)
        ):
            counts.skip("inventory_older_snapshot")
            continue
        stock.on_hand = _dec(row.get("on_hand"), Decimal(0)) or Decimal(0)
        stock.on_order = _dec(row.get("on_order"), Decimal(0)) or Decimal(0)
        stock.reserved_qty = _dec(row.get("reserved_qty"), Decimal(0)) or Decimal(0)
        stock.blocked_qty = _dec(row.get("blocked_qty"), Decimal(0)) or Decimal(0)
        due = row.get("on_order_due")
        stock.on_order_due = due if isinstance(due, date) else None
        stock.as_of = as_of if isinstance(as_of, date) else None
        counts.add("inventory", created=created)
    await session.flush()


async def _apply_purchases(
    session: AsyncSession, tenant_id: UUID, table: ParsedTable, items: _ItemCache, counts: _Counts
) -> None:
    suppliers: dict[str, Supplier] = {}
    orders: dict[tuple[UUID, str], PurchaseOrder] = {}
    next_line: dict[UUID, int] = defaultdict(int)
    # Lines already written by this file, per order, by content. A row with no
    # line number that repeats one of them exactly is a duplicate (spreadsheets
    # exported twice, copy-paste): importing it would double-count the order.
    seen: dict[UUID, set[tuple[object, ...]]] = defaultdict(set)
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

        line_no = row.get("line_no")
        if not isinstance(line_no, int):
            signature = _purchase_signature(row)
            if signature in seen[order.id]:
                counts.skip("po_lines_duplicate")
                continue
            seen[order.id].add(signature)
        item = await items.upsert(row, overwrite=False)
        if not isinstance(line_no, int):
            next_line[order.id] += 1
            line_no = next_line[order.id]
        quantity = _dec(row["quantity"], Decimal(0)) or Decimal(0)
        received_qty = _dec(row.get("received_qty"))
        received_at = row.get("received_at")
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


def _purchase_signature(row: Mapping[str, Any]) -> tuple[object, ...]:
    """What makes two purchase rows of the same order the same line."""
    return (
        str(row["part_number"]).strip().lower(),
        _dec(row["quantity"]),
        _dec(row["unit_price"]),
        str(row.get("currency") or "").upper(),
        row.get("promised_at"),
        row.get("received_at"),
        _dec(row.get("received_qty")),
        _dec(row.get("rejected_qty")),
        row.get("quality_ok"),
    )


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


# Words that only point at "the product" without naming it ("esse produto",
# "this product", "il prodotto"): with one product imported, it is that one.
_POINTER_WORDS: Final = frozenset(
    {
        "a", "o", "os", "as", "do", "da", "de", "the", "this", "that", "it", "my", "our",
        "esse", "este", "essa", "esta", "isso", "isto", "meu", "minha", "nosso", "nossa",
        "il", "lo", "la", "questo", "questa", "quel", "mio", "nostro",
        "produto", "product", "prodotto", "item", "build", "montagem",
    }
)  # fmt: skip


def match_product(products: Sequence[str], wanted: str) -> str | None:
    """The one product `wanted` refers to, or None when it is missing or ambiguous.

    Exact name (case-insensitive) first; then, with a single product, any
    pointer phrase ("esse produto") or partial name; then a unique partial or
    close match ("motor" -> "Motor EV-120", "motor ev120" -> "Motor EV-120").
    """
    if not products:
        return None
    needle = " ".join(wanted.strip().lower().split())
    exact = [p for p in products if p.lower() == needle]
    if exact:
        return exact[0]
    words = [w for w in re.split(r"[^\w-]+", needle) if w]
    pointer_only = all(w in _POINTER_WORDS for w in words)
    if len(products) == 1 and pointer_only:
        return products[0]
    if not needle or pointer_only:
        return None
    partial = [p for p in products if needle in p.lower() or p.lower() in needle]
    if len(partial) == 1:
        return partial[0]
    if partial:
        return None
    squashed = {p: re.sub(r"[^a-z0-9]", "", p.lower()) for p in products}
    target = re.sub(r"[^a-z0-9]", "", needle)
    squashed_hits = [p for p, q in squashed.items() if target and (target in q or q in target)]
    if len(squashed_hits) == 1:
        return squashed_hits[0]
    close = difflib.get_close_matches(needle, [p.lower() for p in products], n=2, cutoff=0.75)
    if len(close) == 1:
        return next(p for p in products if p.lower() == close[0])
    return None


async def _resolve_product(session: AsyncSession, wanted: str) -> str:
    products = sorted(set(await session.scalars(select(BomLine.product).distinct())))
    if not products:
        raise NotFound("no bill of materials imported yet")
    found = match_product(products, wanted)
    if found is not None:
        return found
    raise NotFound(f"no single product matches {wanted!r}; products: {', '.join(products[:20])}")


# --- Analyses -----------------------------------------------------------------------------


async def create_analysis(
    db: Db, principal: Principal, request: AnalysisRequest, today: date | None = None
) -> AnalysisView:
    today = today or datetime.now(UTC).date()
    params = calc.Params(
        safety_stock_pct=request.safety_stock_pct,
        lookback_days=request.lookback_days,
        location=request.location,
    )
    planning_start = request.planning_start or today
    planning_end = request.planning_end or request.need_by
    if planning_end is not None and planning_end < planning_start:
        raise InvalidRequest("planning_end must not precede planning_start")
    stock_as_of = request.stock_as_of or today
    if stock_as_of > today:
        raise InvalidRequest("stock_as_of must not be in the future")
    async with tenant_session(db, principal.tenant_id) as session:
        product = await _resolve_product(session, request.product)
        quantity, need_by = request.quantity, request.need_by or request.planning_end
        demands: list[calc.Demand] | None = None
        if quantity is None:
            query = select(ProductionPlanRow).where(
                ProductionPlanRow.product == product,
                ProductionPlanRow.need_by >= planning_start,
            )
            if planning_end is not None:
                query = query.where(ProductionPlanRow.need_by <= planning_end)
            planned = list(await session.scalars(query.order_by(ProductionPlanRow.need_by)))
            if not planned:
                raise InvalidRequest(
                    "no production plan in the selected demand window; give a quantity"
                )
            demands = [calc.Demand(row.quantity, row.need_by) for row in planned]
            quantity = sum((d.quantity for d in demands), Decimal(0))
            need_by = planned[0].need_by

        bom = await _load_bom(session, product)
        stock = await _load_stock(
            session,
            [entry.item.id for entry in bom],
            location=request.location,
            as_of=stock_as_of,
            max_age_days=request.max_stock_age_days,
        )
        competing = (
            select(BomLine.item_id)
            .join(
                ProductionPlanRow,
                ProductionPlanRow.product == BomLine.product,
            )
            .where(
                BomLine.item_id.in_([entry.item.id for entry in bom]),
                ProductionPlanRow.need_by >= today,
                or_(
                    ProductionPlanRow.product != product, ProductionPlanRow.need_by < planning_start
                ),
            )
        )
        coverage_end = demands[-1].need_by if demands else need_by
        if coverage_end is not None:
            competing = competing.where(ProductionPlanRow.need_by <= coverage_end)
        for item_id in set(await session.scalars(competing)):
            held = stock.get(item_id, calc.Stock(known=False, issues=("stock_unknown",)))
            stock[item_id] = replace(
                held,
                issues=(*held.issues, "shared_stock_competing_demand"),
            )
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
            demands=demands,
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
                "planning_start": planning_start.isoformat(),
                "planning_end": planning_end.isoformat() if planning_end else None,
                "location": request.location,
                "stock_as_of": stock_as_of.isoformat(),
                "max_stock_age_days": request.max_stock_age_days,
                "demand_source": "production_plan"
                if request.quantity is None
                else "explicit_quantity",
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


async def adjust_analysis(
    db: Db,
    principal: Principal,
    analysis_id: UUID,
    *,
    exclude_parts: Sequence[str] = (),
    quantities: Mapping[str, Decimal] | None = None,
) -> AnalysisView:
    """The buyer's edits to a recommendation: drop parts, or set how much to buy.

    Only before an RFQ was created from it. Totals are recomputed from the
    stored target and last prices; nothing else changes.
    """
    wanted = {p.strip().lower() for p in exclude_parts}
    overrides = {k.strip().lower(): v for k, v in (quantities or {}).items()}
    if any(v < 0 for v in overrides.values()):
        raise InvalidRequest("quantities must not be negative")
    async with tenant_session(db, principal.tenant_id) as session:
        record = await session.scalar(
            select(Analysis).where(Analysis.id == analysis_id).with_for_update()
        )
        if record is None:
            raise NotFound(f"analysis {analysis_id} not found")
        if record.rfq_id is not None:
            raise Conflict("this analysis already has an RFQ; run a new analysis to change it")
        rows = list(
            await session.execute(
                select(AnalysisLine, Item)
                .join(Item, Item.id == AnalysisLine.item_id)
                .where(AnalysisLine.analysis_id == analysis_id)
                .with_for_update(of=AnalysisLine)
            )
        )
        known = {item.part_number.lower() for _, item in rows}
        unknown = sorted((wanted | set(overrides)) - known)
        if unknown:
            raise InvalidRequest(f"not in this analysis: {', '.join(unknown)}")
        for line, item in rows:
            key = item.part_number.lower()
            if key in wanted:
                line.to_buy = Decimal(0)
            elif key in overrides:
                line.to_buy = overrides[key]
                line.calculation = {
                    **line.calculation,
                    "buyer_quantity_override": str(line.to_buy),
                }
        summary = dict(record.summary)
        buying = [line for line, _ in rows if line.to_buy > 0]
        summary["items_to_buy"] = len(buying)
        totals = calc.currency_totals(
            (line.currency, line.to_buy, line.target_price, line.last_price) for line in buying
        )
        single = totals[0] if len(totals) == 1 else None
        summary["currency"] = single.currency if single else None
        summary["total_at_target"] = _num(single.total_at_target if single else None)
        summary["total_at_last_price"] = _num(single.total_at_last_price if single else None)
        summary["totals"] = _totals_json(totals)
        record.summary = summary
        await session.flush()
        logger.info(
            "analysis adjusted tenant=%s analysis=%s excluded=%d set=%d",
            principal.tenant_id,
            analysis_id,
            len(wanted),
            len(overrides),
        )
        return await _view(session, record)


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


async def _load_stock(
    session: AsyncSession,
    item_ids: Sequence[UUID],
    *,
    location: str | None = None,
    as_of: date | None = None,
    max_age_days: int | None = None,
) -> dict[UUID, calc.Stock]:
    if not item_ids:
        return {}
    query = select(InventoryRow).where(InventoryRow.item_id.in_(item_ids))
    if location is not None:
        query = query.where(InventoryRow.location == location)
    grouped: dict[UUID, list[InventoryRow]] = defaultdict(list)
    for row in await session.scalars(query):
        grouped[row.item_id].append(row)
    cutoff = as_of or datetime.now(UTC).date()
    result: dict[UUID, calc.Stock] = {}
    for item_id, rows in grouped.items():
        eligible: list[InventoryRow] = []
        issues: list[str] = []
        for row in rows:
            if row.as_of is not None and row.as_of > cutoff:
                issues.append("stock_after_cutoff")
                continue
            if row.as_of is None:
                issues.append("stock_undated")
                if max_age_days is not None:
                    continue
            if (
                max_age_days is not None
                and row.as_of is not None
                and row.as_of < cutoff - timedelta(days=max_age_days)
            ):
                issues.append("stock_stale")
                continue
            if row.reserved_qty + row.blocked_qty > row.on_hand:
                issues.append("stock_reservations_exceed_on_hand")
            eligible.append(row)
        if len(eligible) != len(rows):
            issues.append("stock_unknown")
        result[item_id] = calc.Stock(
            on_hand=sum(
                (
                    max(row.on_hand - row.reserved_qty - row.blocked_qty, Decimal(0))
                    for row in eligible
                ),
                Decimal(0),
            ),
            on_order=sum((row.on_order for row in eligible), Decimal(0)),
            known=len(eligible) == len(rows) and "stock_reservations_exceed_on_hand" not in issues,
            issues=tuple(dict.fromkeys(issues)),
            gross_on_hand=sum((row.on_hand for row in eligible), Decimal(0)),
            reserved_qty=sum((row.reserved_qty for row in eligible), Decimal(0)),
            blocked_qty=sum((row.blocked_qty for row in eligible), Decimal(0)),
            inbound=tuple((row.on_order_due, row.on_order) for row in eligible if row.on_order),
        )
    return result


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
        "totals": _totals_json(summary.totals),
    }


def _totals_json(totals: Sequence[calc.CurrencyTotal]) -> list[dict[str, Any]]:
    return [
        {
            "currency": t.currency,
            "items_to_buy": t.items_to_buy,
            "total_at_target": str(t.total_at_target),
            "total_at_last_price": str(t.total_at_last_price),
        }
        for t in totals
    ]


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
            "delivery_observations": s.delivery_observations,
            "quality_observations": s.quality_observations,
            "last_received_at": s.last_received_at.isoformat() if s.last_received_at else None,
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
        calculation=CalculationView.model_validate(asdict(line.calculation)).model_dump(
            mode="json"
        ),
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
        planning_start=params.get("planning_start"),
        planning_end=params.get("planning_end"),
        location=params.get("location"),
        stock_as_of=params.get("stock_as_of"),
        max_stock_age_days=params.get("max_stock_age_days"),
        demand_source=params.get("demand_source", "explicit_quantity"),
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
                calculation=CalculationView.model_validate(line.calculation),
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
        for line, _ in rows:
            calculation = CalculationView.model_validate(line.calculation)
            uncertain = (
                not calculation.stock_known
                or "shared_stock_competing_demand" in calculation.stock_issues
                or "inbound_date_unknown" in calculation.stock_issues
                or "inbound_location_unknown" in calculation.stock_issues
                or "partial_receipt_status_unknown" in calculation.stock_issues
            )
            if uncertain and calculation.buyer_quantity_override != line.to_buy:
                raise InvalidRequest(
                    "confirm purchase quantities for lines with uncertain supply "
                    "or competing demand before creating an RFQ"
                )

        title = request.title or f"{record.product}: {record.quantity.normalize():f} un"
        try:
            resolved = await resolve_currency(
                session, stated=request.currency, analysis=[line.currency for line, _ in rows]
            )
        except RfqError as exc:
            raise InvalidRequest(str(exc)) from None
        currency = resolved.code
        rates = await load_rates(session, datetime.now(UTC).date())
        rfq, rfq_lines = await rfq_service.create_draft_rfq(
            session,
            principal,
            title=title,
            lines=[
                rfq_service.NewRfqLine(
                    description=item.description,
                    quantity=line.to_buy,
                    unit=line.unit,
                    part_number=item.part_number,
                    mpn=item.mpn,
                    manufacturer=item.manufacturer,
                    category=rfq_category(item.category),
                    need_by=line.need_by,
                )
                for line, item in rows
            ],
            source_text=f"Purchase analysis {analysis_id}",
            response_due=request.response_due,
            base_currency=currency,
        )
        wanted: dict[UUID, str] = {}
        without_limits: list[int] = []
        for (line, _), rfq_line in zip(rows, rfq_lines, strict=True):
            line.rfq_line_id = rfq_line.id
            rfq_line.source_evidence = {
                "kind": "purchase_analysis",
                "analysis_id": str(record.id),
                "analysis_line_id": str(line.id),
                "item_id": str(line.item_id),
                "product": record.product,
                "calculation": line.calculation,
                "planning_start": record.params.get("planning_start"),
                "planning_end": record.params.get("planning_end"),
                "location": record.params.get("location"),
                "stock_as_of": record.params.get("stock_as_of"),
            }
            # The analysis prices become the negotiation's default limits (the buyer
            # can still change them), in the RFQ's currency: the strategy compares
            # offers in base currency. A line bought in another currency has its
            # limits converted at the latest reference rate; without a rate it has none.
            factor = fx_factor(line.currency, currency, rates)
            if factor is not None and line.target_price is not None and line.max_price is not None:
                rfq_line.target_unit_price = (line.target_price * factor).quantize(
                    calc.PRICE_PLACES
                )
                rfq_line.max_unit_price = (line.max_price * factor).quantize(calc.PRICE_PLACES)
            else:
                without_limits.append(rfq_line.position)
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
        "rfq from analysis tenant=%s analysis=%s rfq=%s invites=%d currency_source=%s",
        principal.tenant_id,
        analysis_id,
        rfq.id,
        len(invited),
        resolved.source,
    )
    return RfqFromAnalysis(
        analysis_id=analysis_id,
        rfq=detail,
        currency_source=resolved.source,
        invited=invited,
        skipped=skipped,
        lines_without_limits=without_limits,
    )
