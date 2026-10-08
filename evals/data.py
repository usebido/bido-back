"""Synthetic purchasing datasets, the truth every eval is checked against.

A dataset is what a buyer's spreadsheet *means*: a product's bill of materials,
stock, a year of purchase orders with their suppliers (and their e-mails) and a
production plan. `evals.layouts` writes the same dataset in different shapes
(what ChatGPT produces when asked for "fake purchasing data"); the golden
expectations come from the dataset, never from the layout.

Everything is deterministic for a given `today`: a seeded RNG, dates relative
to `today`. Supplier e-mails use reserved test domains only (example.test,
example.com) so nothing could ever reach a real company.
"""

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Literal

ProductKind = Literal["motor", "battery", "inverter"]

PRICE_STEP: Final = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class Part:
    number: str
    description: str
    mpn: str | None
    manufacturer: str | None
    category: str
    unit: str
    qty_per_unit: Decimal
    base_price: Decimal


@dataclass(frozen=True, slots=True)
class SupplierSpec:
    name: str
    email: str
    contact: str
    country: str
    parts: tuple[str, ...]
    price_factor: Decimal
    late_every: int  # every Nth received order arrives late; 0 = never
    reject_every: int  # every Nth received order has rejects; 0 = never


@dataclass(frozen=True, slots=True)
class PurchaseRow:
    po_number: str
    supplier: str
    part: str
    description: str
    quantity: Decimal
    unit_price: Decimal
    currency: str
    ordered_at: date
    promised_at: date
    received_at: date | None
    rejected_qty: Decimal


@dataclass(frozen=True, slots=True)
class PlanRow:
    product: str
    quantity: Decimal
    need_by: date


@dataclass(frozen=True, slots=True)
class Dataset:
    key: str
    product: str
    parts: tuple[Part, ...]
    suppliers: tuple[SupplierSpec, ...]
    stock: dict[str, tuple[Decimal, Decimal]]  # part -> (on hand, on order)
    purchases: tuple[PurchaseRow, ...]
    plan: tuple[PlanRow, ...]
    # Extra products sharing the catalog (BOM part number -> qty per unit).
    other_boms: dict[str, dict[str, Decimal]] = field(default_factory=dict[str, dict[str, Decimal]])

    def part(self, number: str) -> Part:
        return next(p for p in self.parts if p.number == number)

    def supplier(self, name: str) -> SupplierSpec:
        return next(s for s in self.suppliers if s.name == name)


def _d(value: str | int | float) -> Decimal:
    return Decimal(str(value))


_PARTS: Final[dict[ProductKind, tuple[Part, ...]]] = {
    "motor": (
        Part("MT-LAM-01", "Electrical steel laminations M270-35A", "M270-35A", "Steelworks Demo",
             "material", "kg", _d(18.5), _d("2.85")),
        Part("MT-CU-02", "Copper magnet wire 1.0 mm grade 2", "CW-10-G2", "Wire Demo Co",
             "material", "kg", _d(6.2), _d("11.40")),
        Part("MT-MAG-03", "NdFeB magnet N42SH 30x10x5", "N42SH-30105", "Magnet Demo GmbH",
             "catalog", "pcs", _d(24), _d("1.18")),
        Part("MT-BRG-04", "Deep groove ball bearing 6206-2RS", "6206-2RS", "Bearing Demo",
             "catalog", "pcs", _d(2), _d("4.90")),
        Part("MT-SHF-05", "Rotor shaft 42CrMo4 machined", None, None,
             "drawing", "pcs", _d(1), _d("38.00")),
        Part("MT-HSG-06", "Aluminium housing die cast", None, None,
             "drawing", "pcs", _d(1), _d("42.80")),
        Part("MT-RES-07", "Resolver sensor 2-pole", "RS-2P-50", "Sensor Demo",
             "catalog", "pcs", _d(1), _d("27.50")),
        Part("MT-CON-08", "HV connector 3-pin 300A", "HVC-3P-300", "Connect Demo",
             "catalog", "pcs", _d(1), _d("15.20")),
    ),
    "battery": (
        Part("BP-CEL-01", "Li-ion cell 21700 5000mAh", "INR21700-50E", "Cell Demo",
             "catalog", "pcs", _d(208), _d("3.10")),
        Part("BP-BMS-02", "BMS board 16S 200A", "BMS-16S-200", "BMS Demo",
             "catalog", "pcs", _d(1), _d("86.00")),
        Part("BP-BUS-03", "Nickel-plated copper busbar", None, None,
             "drawing", "pcs", _d(32), _d("0.95")),
        Part("BP-ENC-04", "Aluminium enclosure IP67", None, None,
             "drawing", "pcs", _d(1), _d("64.00")),
        Part("BP-THM-05", "NTC thermistor 10k", "NTC-10K-3950", "Sensor Demo",
             "catalog", "pcs", _d(8), _d("0.42")),
        Part("BP-PAD-06", "Thermal pad 2 mm 300x300", "TP-2-300", "Thermal Demo",
             "material", "pcs", _d(2), _d("7.80")),
    ),
    "inverter": (
        Part("IV-IGB-01", "IGBT module 1200V 400A", "IGBT-1200-400", "Power Demo",
             "catalog", "pcs", _d(3), _d("118.00")),
        Part("IV-CAP-02", "DC-link film capacitor 450uF 800V", "FC-450-800", "Cap Demo",
             "catalog", "pcs", _d(2), _d("36.50")),
        Part("IV-DRV-03", "Gate driver board", "GDB-6CH", "Driver Demo",
             "catalog", "pcs", _d(1), _d("54.00")),
        Part("IV-CPL-04", "Liquid cold plate", None, None,
             "drawing", "pcs", _d(1), _d("72.00")),
        Part("IV-MCU-05", "Control MCU automotive", "MCU-AUTO-32", "Chip Demo",
             "catalog", "pcs", _d(1), _d("9.80")),
        Part("IV-CUR-06", "Current sensor 600A", "CS-600-HALL", "Sensor Demo",
             "catalog", "pcs", _d(3), _d("12.40")),
        Part("IV-THP-07", "Thermal paste 1 kg", None, "Thermal Demo",
             "material", "kg", _d("0.05"), _d("48.00")),
    ),
}  # fmt: skip

_PRODUCTS: Final[dict[ProductKind, str]] = {
    "motor": "Motor EV-150",
    "battery": "Battery Pack BP-48",
    "inverter": "Inverter INV-80",
}

# name, contact, country, part indexes, price factor, late every, reject every
_SUPPLIERS: Final[
    dict[ProductKind, tuple[tuple[str, str, str, tuple[int, ...], str, int, int], ...]]
] = {
    "motor": (
        ("Alpha Steel Demo", "Paolo Ferri", "IT", (0,), "1.00", 0, 0),
        ("Beta Wire Demo", "Elena Galli", "IT", (1,), "0.97", 2, 0),
        ("Gamma Magnets Demo", "Stefan Krause", "DE", (2,), "1.02", 0, 4),
        ("Delta Bearings Demo", "Luca Moretti", "IT", (3, 7), "1.00", 0, 0),
        ("Epsilon Castings Demo", "Anna Zanetti", "IT", (4, 5), "1.05", 1, 0),
        ("Zeta Sensors Demo", "Jana Vogel", "DE", (6,), "1.00", 0, 0),
        ("Eta Parts Demo", "Tom Hughes", "GB", (1, 2, 3), "0.92", 3, 2),
    ),
    "battery": (
        ("Cella Demo S.r.l.", "Marco Conti", "IT", (0,), "1.00", 0, 0),
        ("Circuiti Demo S.p.A.", "Sara Ricci", "IT", (1, 4), "1.03", 3, 0),
        ("Metalli Demo S.r.l.", "Giorgio Neri", "IT", (2, 3), "0.98", 2, 0),
        ("Termico Demo S.r.l.", "Laura Bianchi", "IT", (5, 4), "1.00", 0, 3),
        ("Ricambi Demo Ltd", "Peter Smith", "GB", (0, 2), "0.94", 2, 2),
    ),
    "inverter": (
        ("Potencia Demo Ltda", "Rafael Souza", "BR", (0,), "1.00", 0, 0),
        ("Capacitores Demo Ltda", "Juliana Lima", "BR", (1,), "0.99", 4, 0),
        ("Placas Demo SA", "Bruno Alves", "BR", (2, 4), "1.04", 0, 0),
        ("Usinagem Demo Ltda", "Carla Dias", "BR", (3, 6), "1.02", 2, 0),
        ("Sensores Demo SA", "Pedro Rocha", "BR", (5, 4), "1.00", 0, 4),
        ("Distribuidora Demo Ltda", "Ana Costa", "BR", (0, 1, 5), "0.95", 3, 3),
    ),
}

_DOMAINS: Final = ("example.test", "example.com")


def _slug(name: str) -> str:
    return "".join(c for c in name.lower().split()[0] if c.isalnum())


def _price(value: Decimal) -> Decimal:
    return value.quantize(PRICE_STEP, rounding=ROUND_HALF_UP)


def build(
    key: str,
    kind: ProductKind,
    today: date,
    *,
    seed: int,
    with_plan: bool = True,
    second_product: bool = False,
) -> Dataset:
    """A dataset for `kind`: 12 months of purchases, stock and (optionally) a plan."""
    rng = random.Random(seed)  # noqa: S311 - synthetic data, not security
    parts = _PARTS[kind]
    suppliers: list[SupplierSpec] = []
    for index, (name, contact, country, idx, factor, late, reject) in enumerate(_SUPPLIERS[kind]):
        domain = _DOMAINS[index % len(_DOMAINS)]
        suppliers.append(
            SupplierSpec(
                name=name,
                email=f"sales.{_slug(name)}@{_slug(name)}-{kind}.{domain}",
                contact=contact,
                country=country,
                parts=tuple(parts[i].number for i in idx),
                price_factor=Decimal(factor),
                late_every=late,
                reject_every=reject,
            )
        )

    purchases: list[PurchaseRow] = []
    po = 4100 + seed * 10
    for supplier in suppliers:
        received = 0
        for number in supplier.parts:
            part = next(p for p in parts if p.number == number)
            orders = rng.randint(2, 4)
            for _ in range(orders):
                po += 1
                days_ago = rng.randint(15, 350)
                ordered = today - timedelta(days=days_ago)
                # A recent price increase for the first material (copper-like trend).
                drift = Decimal(1)
                if part.category == "material" and days_ago < 90:
                    drift = Decimal("1.08")
                noise = Decimal(rng.randint(-30, 30)) / Decimal(1000)
                unit_price = _price(part.base_price * supplier.price_factor * drift * (1 + noise))
                per_build = part.qty_per_unit * rng.choice((50, 100, 150, 200))
                quantity = per_build.quantize(Decimal(1)) if per_build >= 1 else Decimal(1)
                promised = ordered + timedelta(days=rng.randint(20, 45))
                received_at: date | None = None
                rejected = Decimal(0)
                if promised <= today:
                    received += 1
                    late = supplier.late_every and received % supplier.late_every == 0
                    received_at = promised + timedelta(days=rng.randint(5, 20) if late else -2)
                    if supplier.reject_every and received % supplier.reject_every == 0:
                        rejected = (quantity * Decimal("0.05")).quantize(Decimal(1))
                purchases.append(
                    PurchaseRow(
                        po_number=f"PO-{po}",
                        supplier=supplier.name,
                        part=part.number,
                        description=part.description,
                        quantity=quantity,
                        unit_price=unit_price,
                        currency="EUR",
                        ordered_at=ordered,
                        promised_at=promised,
                        received_at=received_at,
                        rejected_qty=rejected,
                    )
                )
    purchases.sort(key=lambda p: (p.ordered_at, p.po_number))

    stock: dict[str, tuple[Decimal, Decimal]] = {}
    for part in parts:
        build_qty = part.qty_per_unit * 100
        on_hand = (build_qty * Decimal(rng.choice((0, 0.2, 0.5, 1.4)))).quantize(Decimal(1))
        on_order = (build_qty * Decimal(rng.choice((0, 0, 0.3)))).quantize(Decimal(1))
        stock[part.number] = (on_hand, on_order)

    product = _PRODUCTS[kind]
    plan: tuple[PlanRow, ...] = ()
    if with_plan:
        plan = (
            PlanRow(product, _d(rng.choice((120, 180, 240))), today + timedelta(days=30)),
            PlanRow(product, _d(rng.choice((60, 90))), today + timedelta(days=60)),
        )
    other_boms: dict[str, dict[str, Decimal]] = {}
    if second_product:
        other = f"{product}-LITE"
        other_boms[other] = {p.number: p.qty_per_unit for p in parts[: len(parts) - 2]}
        plan = (*plan, PlanRow(other, _d(300), today + timedelta(days=45)))
    return Dataset(
        key=key,
        product=product,
        parts=parts,
        suppliers=tuple(suppliers),
        stock=stock,
        purchases=tuple(purchases),
        plan=plan,
        other_boms=other_boms,
    )
