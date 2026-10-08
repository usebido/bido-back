"""One synthetic purchasing scenario behind every layout of the source corpus.

A motor (MTR-100) is built at Planta A from a six-component BOM, next to two
other areas' products (a hydraulic pump at Planta B, a maintenance kit at
Planta A). The export holds production orders for three areas and two plants
(one motor order inside the planning horizon, one after it, one at the other
plant), current and older stock snapshots with gross/reserved/blocked
quantities, open purchase orders with pending deliveries (one arriving too
late, one partly received), about seventy closed purchase orders over 180 days
from three suppliers with distinct price, punctuality and rejection profiles,
their physical receipts (some split), a cancelled order, supplier codes, names
and e-mails in different tables, an approved-source list and archived
quotation e-mails (one with an extra offer for another department).

Everything is generated in code from a fixed seed; nothing comes from a
customer file. Supplier e-mails use the reserved `.test` domain. `expected()`
derives the answers a correct assessment must give with plain arithmetic,
independently of the production planner the corpus grades.
"""

import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from typing import Final, Literal

AS_OF: Final = date(2026, 10, 8)
HORIZON_END: Final = date(2026, 10, 22)
PRODUCT: Final = "MTR-100"
CURRENCY: Final = "BRL"
SEED: Final = 20261008
# Closed history: ordered between these days, so every one is received before AS_OF.
HISTORY_START: Final = AS_OF - timedelta(days=180)
HISTORY_END: Final = AS_OF - timedelta(days=28)

Plant = Literal["A", "B"]
Area = Literal["motors", "pumps", "maintenance"]
Unit = Literal["pcs", "kg", "L", "kits"]
OrderStatus = Literal["closed", "open", "cancelled"]
PlanStatus = Literal["planned", "released"]

_CENT: Final = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class Material:
    code: str
    unit: Unit
    area: Area


@dataclass(frozen=True, slots=True)
class Product:
    code: str
    unit: Unit
    area: Area


@dataclass(frozen=True, slots=True)
class BomLine:
    product: str
    material: str
    quantity_per: Decimal
    revision: str
    valid_from: date


@dataclass(frozen=True, slots=True)
class ProductionOrder:
    reference: str
    plant: Plant
    area: Area
    product: str
    quantity: Decimal
    unit: Unit
    need_by: date
    status: PlanStatus
    internal: bool  # an internal (maintenance) order rather than a manufacturing one


@dataclass(frozen=True, slots=True)
class Requisition:
    reference: str
    plant: Plant
    area: Area
    material: str
    quantity: Decimal
    unit: Unit
    need_by: date


@dataclass(frozen=True, slots=True)
class StockRow:
    as_of: date
    plant: Plant
    material: str
    total: Decimal
    reserved: Decimal
    blocked: Decimal

    @property
    def free(self) -> Decimal:
        return self.total - self.reserved - self.blocked


@dataclass(frozen=True, slots=True)
class Supplier:
    code: str
    name: str
    country: str
    email: str
    registered: date
    # Generation profile: price factor, chance of a late delivery and its days,
    # chance of rejected quantity and its percentage range.
    price_factor: Decimal
    late_chance: float
    late_days: tuple[int, int]
    reject_chance: float
    reject_pct: tuple[int, int]


@dataclass(frozen=True, slots=True)
class Approval:
    material: str
    supplier: str
    valid_from: date


@dataclass(frozen=True, slots=True)
class PurchaseLine:
    po_number: str
    po_line: str
    plant: Plant
    material: str
    supplier: str
    quantity: Decimal
    unit_price: Decimal
    ordered_at: date
    promised_at: date
    status: OrderStatus


@dataclass(frozen=True, slots=True)
class Receipt:
    po_number: str
    po_line: str
    plant: Plant
    received_at: date
    quantity: Decimal
    rejected: Decimal
    receipt_id: str


@dataclass(frozen=True, slots=True)
class Delivery:
    """A supplier confirmation still pending physical receipt."""

    po_number: str
    po_line: str
    plant: Plant
    material: str
    quantity: Decimal
    expected_at: date


@dataclass(frozen=True, slots=True)
class QuoteMessage:
    """One archived quotation e-mail (never a purchase or an accepted price)."""

    reference: str
    supplier: str
    sent_at: date
    kind: Literal["offer", "revision", "decline"]
    # (material, quantity, unit price); quantity is None for a price revision.
    items: tuple[tuple[str, Decimal | None, Decimal | None], ...]
    # Offers for another department's material in the same e-mail.
    extra: tuple[tuple[str, Decimal, Decimal], ...] = ()
    lead_days: int = 0


@dataclass(frozen=True, slots=True)
class Scenario:
    materials: tuple[Material, ...]
    products: tuple[Product, ...]
    bom: tuple[BomLine, ...]
    production: tuple[ProductionOrder, ...]
    requisitions: tuple[Requisition, ...]
    stock: tuple[StockRow, ...]
    suppliers: tuple[Supplier, ...]
    approvals: tuple[Approval, ...]
    # In register order (shuffled, as ERP exports are).
    purchases: tuple[PurchaseLine, ...]
    receipts: tuple[Receipt, ...]
    deliveries: tuple[Delivery, ...]
    quotes: tuple[QuoteMessage, ...]

    def material(self, code: str) -> Material:
        return next(m for m in self.materials if m.code == code)

    def supplier(self, code: str) -> Supplier:
        return next(s for s in self.suppliers if s.code == code)

    def unit_of(self, code: str) -> Unit:
        for item in (*self.materials, *self.products):
            if item.code == code:
                return item.unit
        raise KeyError(code)

    def received(self, po_number: str, po_line: str) -> list[Receipt]:
        return [r for r in self.receipts if (r.po_number, r.po_line) == (po_number, po_line)]


@dataclass(frozen=True, slots=True)
class LastPurchase:
    price: Decimal
    supplier: str
    ordered_at: date
    po_number: str


@dataclass(frozen=True, slots=True)
class Expected:
    """What a correct assessment of MTR-100 at Planta A for AS_OF..HORIZON_END says."""

    to_buy: dict[str, Decimal]
    demand: dict[str, Decimal]
    available: dict[str, Decimal]
    on_order: dict[str, Decimal]
    # The most recent completed (closed, fully received) purchase of each part.
    last: dict[str, LastPurchase]
    # Codes that must never appear as lines: other areas' materials and the products.
    out_of_scope: frozenset[str]


def _d(value: str | int) -> Decimal:
    return Decimal(value)


MATERIALS: Final = (
    Material("000101", "pcs", "motors"),
    Material("000102", "pcs", "motors"),
    Material("000103", "pcs", "motors"),
    Material("000104", "kg", "motors"),
    Material("000105", "pcs", "motors"),
    Material("000106", "pcs", "motors"),
    Material("000201", "pcs", "pumps"),
    Material("000202", "pcs", "pumps"),
    Material("000203", "pcs", "pumps"),
    Material("000301", "pcs", "maintenance"),
    Material("000302", "pcs", "maintenance"),
    Material("000303", "L", "maintenance"),
)
PRODUCTS: Final = (
    Product(PRODUCT, "pcs", "motors"),
    Product("BOM-BH40", "pcs", "pumps"),
    Product("MAN-KIT02", "kits", "maintenance"),
)
BOM: Final = (
    *(
        BomLine(PRODUCT, code, _d(per), "A", date(2026, 7, 5))
        for code, per in (
            ("000101", "2"),
            ("000102", "1"),
            ("000103", "1"),
            ("000104", "0.8"),
            ("000105", "8"),
            ("000106", "4"),
        )
    ),
    *(
        BomLine("BOM-BH40", code, _d(per), "B", date(2026, 7, 20))
        for code, per in (("000201", "1"), ("000202", "3"), ("000203", "1"))
    ),
    *(
        BomLine("MAN-KIT02", code, _d(per), "C", date(2026, 8, 9))
        for code, per in (("000301", "2"), ("000302", "1"))
    ),
)
PRODUCTION: Final = (
    ProductionOrder("OP-260911", "A", "motors", PRODUCT, _d(30), "pcs", date(2026, 11, 1),
                    "planned", False),
    ProductionOrder("OP-260875", "A", "maintenance", "MAN-KIT02", _d(8), "kits",
                    date(2026, 11, 7), "planned", True),
    ProductionOrder("OP-260903", "B", "pumps", "BOM-BH40", _d(25), "pcs", date(2026, 11, 3),
                    "planned", False),
    ProductionOrder("OP-260896", "B", "motors", PRODUCT, _d(100), "pcs", date(2026, 10, 15),
                    "planned", False),
    ProductionOrder("OP-260891", "B", "pumps", "BOM-BH40", _d(18), "pcs", date(2026, 10, 17),
                    "planned", False),
    ProductionOrder("OP-260884", "A", "motors", PRODUCT, _d(40), "pcs", date(2026, 10, 18),
                    "planned", False),
    ProductionOrder("OP-260919", "A", "maintenance", "MAN-KIT02", _d(12), "kits",
                    date(2026, 10, 20), "released", True),
)  # fmt: skip
REQUISITIONS: Final = (
    Requisition("RI-40081", "A", "maintenance", "000303", _d(35), "L", date(2026, 10, 16)),
    Requisition("RI-40086", "B", "pumps", "000202", _d(20), "pcs", date(2026, 10, 21)),
)
_PREVIOUS: Final = date(2026, 9, 19)
STOCK: Final = (
    StockRow(AS_OF, "A", "000301", _d(70), _d(10), _d(0)),
    StockRow(AS_OF, "A", "000104", _d("12.5"), _d("2.5"), _d(0)),
    StockRow(AS_OF, "A", "000106", _d(100), _d(0), _d(0)),
    StockRow(AS_OF, "B", "000203", _d(38), _d(4), _d(1)),
    StockRow(AS_OF, "B", "000101", _d(300), _d(0), _d(0)),
    StockRow(AS_OF, "A", "000105", _d(100), _d(20), _d(10)),
    StockRow(AS_OF, "A", "000103", _d(50), _d(0), _d(5)),
    StockRow(AS_OF, "B", "000202", _d(155), _d(25), _d(0)),
    StockRow(AS_OF, "A", "000302", _d(22), _d(2), _d(1)),
    StockRow(AS_OF, "B", "000105", _d(900), _d(50), _d(0)),
    StockRow(AS_OF, "A", "000303", _d(125), _d(15), _d(5)),
    StockRow(AS_OF, "A", "000101", _d(25), _d(5), _d(0)),
    StockRow(AS_OF, "B", "000201", _d(62), _d(8), _d(3)),
    StockRow(AS_OF, "A", "000102", _d(15), _d(5), _d(0)),
    StockRow(_PREVIOUS, "A", "000102", _d(27), _d(5), _d(0)),
    StockRow(_PREVIOUS, "A", "000303", _d(130), _d(18), _d(3)),
    StockRow(_PREVIOUS, "A", "000106", _d(106), _d(4), _d(0)),
    StockRow(_PREVIOUS, "A", "000301", _d(75), _d(12), _d(0)),
    StockRow(_PREVIOUS, "B", "000203", _d(41), _d(7), _d(0)),
    StockRow(_PREVIOUS, "A", "000103", _d(49), _d(0), _d(5)),
    StockRow(_PREVIOUS, "B", "000202", _d(160), _d(28), _d(1)),
    StockRow(_PREVIOUS, "A", "000104", _d("23.5"), _d(4), _d(0)),
    StockRow(_PREVIOUS, "B", "000201", _d(61), _d(12), _d(4)),
    StockRow(_PREVIOUS, "A", "000101", _d(35), _d(2), _d(0)),
    StockRow(_PREVIOUS, "A", "000302", _d(16), _d(0), _d(3)),
    StockRow(_PREVIOUS, "A", "000105", _d(110), _d(22), _d(9)),
)
SUPPLIERS: Final = (
    # Cheapest, mostly punctual, occasional small rejections.
    Supplier("F001", "MetalNova Indústria Ltda.", "BR", "vendas@metalnova.example.test",
             date(2026, 3, 2), _d("0.95"), 0.15, (1, 4), 0.3, (2, 6)),
    # Most expensive, always on time, no rejections.
    Supplier("F002", "PrecisaoSul Componentes S.A.", "BR", "b2b@precisaosul.example.test",
             date(2026, 2, 20), _d("1.10"), 0.0, (0, 0), 0.0, (0, 0)),
    # Mid price, often late, frequent rejections.
    Supplier("F003", "MultiPeças Distribuidora Ltda.", "BR", "vendas@multipecas.example.test",
             date(2026, 2, 10), _d("1.00"), 0.6, (3, 12), 0.5, (5, 12)),
)  # fmt: skip
# The two approved sources of every material, with their validation dates.
SOURCES: Final = {
    "000101": (("F001", date(2025, 12, 29)), ("F002", date(2025, 11, 30))),
    "000102": (("F002", date(2026, 3, 17)), ("F003", date(2025, 12, 11))),
    "000103": (("F001", date(2026, 2, 12)), ("F003", date(2026, 3, 9))),
    "000104": (("F001", date(2026, 4, 15)), ("F002", date(2026, 3, 2))),
    "000105": (("F002", date(2026, 1, 22)), ("F003", date(2025, 11, 22))),
    "000106": (("F001", date(2025, 11, 22)), ("F003", date(2026, 5, 25))),
    "000201": (("F001", date(2026, 3, 10)), ("F003", date(2025, 11, 11))),
    "000202": (("F002", date(2026, 1, 18)), ("F003", date(2025, 11, 14))),
    "000203": (("F001", date(2026, 4, 24)), ("F002", date(2026, 1, 6))),
    "000301": (("F002", date(2025, 11, 5)), ("F003", date(2026, 4, 17))),
    "000302": (("F001", date(2025, 11, 29)), ("F003", date(2025, 11, 3))),
    "000303": (("F002", date(2026, 4, 9)), ("F003", date(2025, 11, 28))),
}
_BASE_PRICE: Final = {
    "000101": _d("44.00"),
    "000102": _d("250.00"),
    "000103": _d("176.00"),
    "000104": _d("115.00"),
    "000105": _d("22.00"),
    "000106": _d("2.45"),
    "000201": _d("310.00"),
    "000202": _d("19.50"),
    "000203": _d("166.00"),
    "000301": _d("48.00"),
    "000302": _d("78.00"),
    "000303": _d("25.50"),
}
# Ten closed orders for every motor component, two for every other material.
_ORDERS_PER_MATERIAL: Final = {m.code: 10 if m.area == "motors" else 2 for m in MATERIALS}
# Orders delivered in two physical receipts (material, index of its order by date).
_SPLIT: Final = frozenset({("000101", 3), ("000105", 2), ("000106", 4)})
# (po, material, supplier, plant, quantity, price, ordered, promised); delivered on time.
_OPEN: Final = (
    ("520101", "000101", "F002", "A", _d(10), _d("49.05"), date(2026, 10, 5), date(2026, 10, 13)),
    ("520102", "000102", "F003", "A", _d(20), _d("240.00"), date(2026, 10, 6),
     date(2026, 10, 26)),
    ("520103", "000105", "F002", "A", _d(50), _d("22.89"), date(2026, 9, 29), date(2026, 10, 13)),
    ("520104", "000303", "F003", "A", _d(40), _d("25.90"), date(2026, 10, 2), date(2026, 10, 14)),
    ("520105", "000202", "F002", "B", _d(60), _d("20.10"), date(2026, 10, 1), date(2026, 10, 20)),
)  # fmt: skip
# 520103 was partly received before the snapshot: 20 in, 30 still pending.
_PARTIAL: Final = ("520103", date(2026, 10, 6), _d(20))
# Cancelled after the last completed purchases, at a price nobody paid.
_CANCELLED: Final = ("000103", "F003", _d(40), _d("158.00"), date(2026, 9, 28), date(2026, 10, 9))
QUOTES: Final = (
    QuoteMessage("COT-ANT-087", "F001", date(2026, 7, 15), "offer",
                 (("000101", _d(60), _d("41.80")), ("000103", _d(25), _d("172.00"))),
                 lead_days=14),
    QuoteMessage("COT-ANT-087", "F001", date(2026, 7, 20), "revision",
                 (("000101", None, _d("40.90")), ("000103", None, _d("168.00")))),
    QuoteMessage("COT-ANT-087", "F002", date(2026, 7, 16), "offer",
                 (("000101", _d(60), _d("48.90")), ("000104", _d(30), _d("122.00"))),
                 lead_days=9),
    QuoteMessage("COT-ANT-087", "F002", date(2026, 7, 18), "decline", (("000102", None, None),)),
    QuoteMessage("COT-ANT-087", "F003", date(2026, 7, 16), "offer",
                 (("000102", _d(25), _d("245.00")),),
                 extra=(("000302", _d(15), _d("77.00")),), lead_days=12),
    QuoteMessage("COT-ANT-087", "F003", date(2026, 7, 19), "revision",
                 (("000102", None, _d("239.00")),)),
)  # fmt: skip


def _lot(rng: random.Random, material: str) -> Decimal:
    match material:
        case "000104":
            return Decimal(rng.randint(150, 520)) / 10  # kg, one decimal
        case "000105":
            return Decimal(rng.randint(12, 40) * 5)
        case "000106":
            return Decimal(rng.randint(10, 20) * 100)  # screws are bought by the thousand
        case "000303":
            return Decimal(rng.randint(4, 12) * 10)
        case _:
            return Decimal(rng.randint(15, 70))


def _rejected(rng: random.Random, supplier: Supplier, quantity: Decimal, unit: Unit) -> Decimal:
    if rng.random() >= supplier.reject_chance:
        return Decimal(0)
    share = Decimal(rng.randint(*supplier.reject_pct)) / 100
    if unit == "kg":
        return max(Decimal("0.1"), (quantity * share).quantize(Decimal("0.1")))
    return max(Decimal(1), (quantity * share).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _history(
    rng: random.Random,
) -> tuple[list[PurchaseLine], list[Receipt]]:
    """Closed orders over 180 days with their receipts, open and cancelled orders."""
    drafts: list[tuple[date, str, str, Decimal, Decimal, date, list[tuple[date, Decimal]]]] = []
    span = (HISTORY_END - HISTORY_START).days
    for material in MATERIALS:
        count = _ORDERS_PER_MATERIAL[material.code]
        days = sorted(rng.sample(range(span + 1), count))
        first, second = (code for code, _ in SOURCES[material.code])
        vendors = [first, second] * (count // 2)
        rng.shuffle(vendors)
        for index, (day, code) in enumerate(zip(days, vendors, strict=True)):
            supplier = next(s for s in SUPPLIERS if s.code == code)
            ordered = HISTORY_START + timedelta(days=day)
            drift = Decimal(1) + Decimal(rng.randint(-200, 200)) / 10_000
            if material.code == "000104":  # copper climbs about 10% over the period
                drift += Decimal(day) / Decimal(span) / 10
            price = (_BASE_PRICE[material.code] * supplier.price_factor * drift).quantize(
                _CENT, rounding=ROUND_HALF_UP
            )
            quantity = _lot(rng, material.code)
            promised = ordered + timedelta(days=rng.randint(7, 14))
            if rng.random() < supplier.late_chance:
                arrived = promised + timedelta(days=rng.randint(*supplier.late_days))
            else:
                arrived = promised - timedelta(days=rng.choice((0, 0, 1)))
            lots = [(arrived, quantity)]
            if (material.code, index) in _SPLIT:
                first_lot = (quantity * Decimal("0.6")).quantize(Decimal(1))
                lots = [(arrived, first_lot), (arrived + timedelta(days=4), quantity - first_lot)]
            drafts.append((ordered, material.code, code, quantity, price, promised, lots))
    drafts.sort(key=lambda d: (d[0], d[1]))
    purchases: list[PurchaseLine] = []
    receipts: list[Receipt] = []
    for number, (ordered, code, vendor, quantity, price, promised, lots) in enumerate(drafts, 1):
        material = next(m for m in MATERIALS if m.code == code)
        supplier = next(s for s in SUPPLIERS if s.code == vendor)
        plant: Plant = "B" if material.area == "pumps" else "A"
        po = f"{510000 + number}"
        purchases.append(
            PurchaseLine(po, "00010", plant, code, vendor, quantity, price, ordered, promised,
                         "closed")
        )  # fmt: skip
        rejected = _rejected(rng, supplier, quantity, material.unit)
        for lot, (arrived, lot_quantity) in enumerate(lots):
            receipts.append(
                Receipt(po, "00010", plant, arrived, lot_quantity,
                        rejected if lot == len(lots) - 1 else Decimal(0), "")
            )  # fmt: skip
    code, vendor, quantity, price, ordered, promised = _CANCELLED
    purchases.append(
        PurchaseLine(f"{510001 + len(drafts)}", "00010", "A", code, vendor, quantity, price,
                     ordered, promised, "cancelled")
    )  # fmt: skip
    for po, code, vendor, site, quantity, price, ordered, promised in _OPEN:
        purchases.append(
            PurchaseLine(po, "00010", "A" if site == "A" else "B", code, vendor, quantity, price,
                         ordered, promised, "open")
        )  # fmt: skip
    po, arrived, quantity = _PARTIAL
    receipts.append(Receipt(po, "00010", "A", arrived, quantity, Decimal(0), ""))
    # Delivery notes are numbered in posting order.
    receipts.sort(key=lambda r: (r.received_at, r.po_number))
    receipts = [
        Receipt(r.po_number, r.po_line, r.plant, r.received_at, r.quantity, r.rejected,
                f"NF{30001 + i}")
        for i, r in enumerate(receipts)
    ]  # fmt: skip
    rng.shuffle(purchases)
    rng.shuffle(receipts)
    return purchases, receipts


@cache
def scenario() -> Scenario:
    rng = random.Random(SEED)  # noqa: S311 - synthetic data, not security
    purchases, receipts = _history(rng)
    received: dict[str, Decimal] = defaultdict(Decimal)
    for receipt in receipts:
        received[receipt.po_number] += receipt.quantity
    deliveries = tuple(
        Delivery(p.po_number, p.po_line, p.plant, p.material, p.quantity - received[p.po_number],
                 p.promised_at)
        for p in sorted(purchases, key=lambda p: p.po_number, reverse=True)
        if p.status == "open"
    )  # fmt: skip
    approvals = tuple(
        Approval(material, supplier, since)
        for material, sources in SOURCES.items()
        for supplier, since in sources
    )
    shuffled = list(approvals)
    rng.shuffle(shuffled)
    return Scenario(
        materials=MATERIALS,
        products=PRODUCTS,
        bom=BOM,
        production=PRODUCTION,
        requisitions=REQUISITIONS,
        stock=STOCK,
        suppliers=SUPPLIERS,
        approvals=tuple(shuffled),
        purchases=tuple(purchases),
        receipts=tuple(receipts),
        deliveries=deliveries,
        quotes=QUOTES,
    )


def expected(s: Scenario | None = None) -> Expected:
    """The answers, by plain arithmetic over the scenario (not by the planner under test)."""
    s = s or scenario()
    per = {line.material: line.quantity_per for line in s.bom if line.product == PRODUCT}
    orders = [
        o
        for o in s.production
        if o.product == PRODUCT and o.plant == "A" and AS_OF <= o.need_by <= HORIZON_END
    ]
    first_need = min(o.need_by for o in orders)
    demand = {m: sum((o.quantity * q for o in orders), Decimal(0)) for m, q in per.items()}
    available = {r.material: r.free for r in s.stock if r.plant == "A" and r.as_of == AS_OF}
    on_order = {
        m: sum(
            (
                d.quantity
                for d in s.deliveries
                if d.material == m and d.plant == "A" and AS_OF <= d.expected_at <= first_need
            ),
            Decimal(0),
        )
        for m in per
    }
    to_buy = {m: max(Decimal(0), demand[m] - available[m] - on_order[m]) for m in per}
    last: dict[str, LastPurchase] = {}
    for m in per:
        completed = [
            p
            for p in s.purchases
            if p.material == m
            and p.plant == "A"
            and p.status == "closed"
            and sum(
                (r.quantity for r in s.received(p.po_number, p.po_line) if r.received_at <= AS_OF),
                Decimal(0),
            )
            == p.quantity
        ]
        newest = max(p.ordered_at for p in completed)
        (latest,) = [p for p in completed if p.ordered_at == newest]
        last[m] = LastPurchase(latest.unit_price, latest.supplier, latest.ordered_at,
                               latest.po_number)  # fmt: skip
    codes = {m.code for m in s.materials} | {p.code for p in s.products}
    return Expected(
        to_buy=to_buy,
        demand=demand,
        available=available,
        on_order=on_order,
        last=last,
        out_of_scope=frozenset(codes - set(per)),
    )
