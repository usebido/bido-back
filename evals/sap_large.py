"""A large synthetic SAP export (three plants) and the purchase plan it means.

Mirrors the CEO's large-scale test: eight standard reports in Portuguese, as SAP GUI
exports them, for three plants and roughly 500 material x plant pairs (about 10k MRP
list lines, 6k order-history lines). All data is invented; e-mails use the reserved
example.test domain. It carries the traps that test found:

- open purchase orders with several schedule lines: MD04 names them
  document/item/schedule line ("4500102721/00010/0001"), ME2N lists one row per schedule
  line with the item as a plain number (10);
- overdue schedule lines (exception 07) and reschedule-in requests (exception 10);
- the safety stock, minimum lot and rounding differ per plant (MM60 has one row per
  material x plant), and stock is spread over several storage locations;
- per-1,000 price units, 18 L buckets, supplier minimum order quantities;
- expired info records, a purchase-blocked supplier, and one supplier with two codes of
  the same name and tax id where only one is blocked;
- consumption-based materials (MRP type ND) that have no place in the plan.

The expected plan is computed here from the generated facts by an independent reference
of the CEO's rules (see `reference`), never by Bido's code:

    saldo = free stock of the plant - safety stock of (material, plant)
    events = every open schedule line (overdue ones counted as arriving at once)
             + dependent requirements, in date order (receipts first on a day)
    after each requirement: first_shortage = first date with saldo < 0;
    first_stockout = first date with saldo < -safety stock
    shortfall = max(0, -lowest saldo)
    a source is valid when its info record is valid on the reference date and its
    supplier code is not purchase-blocked; arrival = reference date + planned lead time;
    meets: arrival <= first_shortage; meets_consuming_safety_stock: <= first_stockout
    recommend the cheapest that meets, else the cheapest that meets consuming safety
    stock, else the fastest (unavoidable stockout); none valid: no valid source
    quantity = max(shortfall, minimum lot, supplier minimum) -> rounding value ->
               whole order units

    uv run python -m evals.sap_large /tmp/sap_large   # writes the eight .xlsx files
"""

import io
import math
import random
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal

AS_OF: Final = date(2026, 10, 8)
PLANTS: Final = ("1000", "2000", "3000")
FOREVER: Final = date(9999, 12, 31)

Status = Literal["buy", "covered", "no_valid_source", "excluded_nd"]
Outcome = Literal["meets", "meets_consuming_safety_stock", "unavoidable_stockout"]


@dataclass(frozen=True, slots=True)
class Vendor:
    code: str
    name: str
    tax_id: str
    blocked: bool = False


@dataclass(frozen=True, slots=True)
class Source:
    vendor: str
    price: Decimal  # per price unit of the order unit
    price_unit: int  # 1, or 1000 for screws
    lead_days: int
    valid_to: date
    order_unit: str
    conversion: int  # base units per order unit (1 when the same unit)
    moq: int  # in order units

    def per_base_unit(self) -> Decimal:
        return self.price / self.price_unit / self.conversion


@dataclass(frozen=True, slots=True)
class ScheduleLine:
    po: str
    item: int
    line: int  # 1-based schedule line
    delivery: date
    open_quantity: int
    vendor: str
    price: Decimal
    reschedule: date | None = None


@dataclass(slots=True)
class Pair:
    material: str
    text: str
    plant: str
    unit: str
    group: str
    mrp_type: str
    stock_locations: list[int]
    quality: int
    blocked_stock: int
    safety: int
    min_lot: int
    rounding: int
    planned_days: int
    requirements: list[tuple[date, str, int]]  # (date, order/product data, quantity)
    lines: list[ScheduleLine]
    sources: list[Source]

    @property
    def stock(self) -> int:
        return sum(self.stock_locations)


@dataclass(frozen=True, slots=True)
class Expected:
    status: Status
    shortfall: Decimal = Decimal(0)
    quantity: Decimal = Decimal(0)
    outcome: Outcome | None = None
    supplier: str | None = None
    first_shortage: date | None = None
    first_stockout: date | None = None


@dataclass(slots=True)
class Scenario:
    vendors: dict[str, Vendor]
    pairs: list[Pair]
    expected: dict[tuple[str, str], Expected] = field(
        default_factory=dict[tuple[str, str], Expected]
    )


# --- Generation -------------------------------------------------------------------------

_GROUPS: Final = (
    ("FUND", "UN", "CARCACA FUNDIDA"),
    ("USIN", "UN", "EIXO USINADO"),
    ("ROLA", "UN", "ROLAMENTO"),
    ("FIXA", "UN", "PARAFUSO"),
    ("QUIM", "L", "TINTA EPOXI"),
    ("ACOS", "KG", "CHAPA ACO"),
    ("VEDA", "UN", "ANEL VEDACAO"),
    ("ELET", "UN", "MOTOR ELETRICO"),
)


def _vendors() -> dict[str, Vendor]:
    vendors: dict[str, Vendor] = {}
    for i in range(30):
        code = f"{100101 + i}"
        vendors[code] = Vendor(
            code, f"FORNECEDOR EXEMPLO {i + 1:02d} LTDA", f"{10000000 + i:08d}0001{i % 90:02d}"
        )
    vendors["100140"] = Vendor("100140", "BLOQUEADA EXEMPLO LTDA", "11111111000111", blocked=True)
    # One company, two supplier codes: the old one blocked, the active one valid.
    same = "22222222000122"
    vendors["100150"] = Vendor("100150", "METALURGICA DUPLA EXEMPLO LTDA", same, blocked=True)
    vendors["100151"] = Vendor("100151", "METALURGICA DUPLA EXEMPLO LTDA", same)
    return vendors


def generate(materials: int = 165, seed: int = 7) -> Scenario:
    """`materials` x 3 plants pairs; about 21 MRP lines and 12 history lines per pair."""
    rng = random.Random(seed)  # noqa: S311 - a reproducible fixture, not security.
    vendors = _vendors()
    regular = [v for v in vendors if not vendors[v].blocked and v != "100151"]
    pairs: list[Pair] = []
    po_number = 4500102700
    for index in range(materials):
        group, unit, label = _GROUPS[index % len(_GROUPS)]
        code = f"{110000000 + index}"
        text = f"{label} MODELO {index:03d}"
        nd = index % 19 == 7  # consumption-based material, every plant
        for plant in PLANTS:
            weekly = rng.randint(5, 60) * (100 if group == "FIXA" else 1)
            safety = rng.choice((0, weekly // 2, weekly, 2 * weekly))
            stock_total = rng.randint(1, 10) * weekly
            locations = [stock_total // 2, stock_total - stock_total // 2] if stock_total else [0]
            requirements: list[tuple[date, str, int]] = []
            for k in range(rng.randint(14, 20)):
                day = AS_OF + timedelta(days=2 + 4 * k)  # never on a receipt day (odd offsets)
                order = 4100 + (index * 3 + PLANTS.index(plant)) * 25 + k
                product = f"3{(index % 40):08d}"
                requirements.append(
                    (day, f"{order:010d}/{product}", max(1, int(weekly * rng.uniform(0.2, 0.7))))
                )
            # Sources: one to three info records, some expired, some blocked suppliers.
            sources: list[Source] = []
            n_sources = rng.choice((1, 2, 2, 3))
            chosen = rng.sample(regular, n_sources)
            if index % 23 == 5:
                chosen = ["100151", *chosen[:1]]  # the active code of the duplicated company
            if index % 29 == 11:
                chosen = ["100150", *chosen[:1]]  # its blocked code
            if index % 31 == 13:
                chosen = ["100140"]  # only a blocked supplier: no valid source
            base_price = Decimal(rng.randint(5, 900))
            for k, vendor in enumerate(chosen):
                expired = (index * 7 + k) % 13 == 4
                if index % 37 == 17:
                    expired = True  # every record expired: no valid source
                if group == "FIXA":
                    order_unit, conversion, price_unit, moq = (
                        "UN",
                        1,
                        1000,
                        rng.choice((5000, 10000, 20000)),
                    )
                    price = base_price  # per 1,000 screws
                elif group == "QUIM":
                    order_unit, conversion, price_unit, moq = "BD", 18, 1, rng.choice((0, 2, 5))
                    price = base_price * 18 / 10
                else:
                    order_unit, conversion, price_unit, moq = (
                        unit,
                        1,
                        1,
                        rng.choice((0, 0, 50, 200)),
                    )
                    price = base_price
                sources.append(
                    Source(
                        vendor=vendor,
                        price=(price * Decimal(str(round(rng.uniform(0.85, 1.25), 2)))).quantize(
                            Decimal("0.01")
                        ),
                        price_unit=price_unit,
                        lead_days=rng.choice((5, 9, 15, 21, 21, 30, 30, 45, 60)),
                        valid_to=date(2025, 6, 30) if expired else FOREVER,
                        order_unit=order_unit,
                        conversion=conversion,
                        moq=moq,
                    )
                )
            # Open orders: none, one single-line, or one with two or three schedule lines.
            lines: list[ScheduleLine] = []
            kind = rng.choice(("none", "single", "multi", "multi", "overdue"))
            if kind != "none":
                po_number += 1
                vendor = next(
                    (s.vendor for s in sources if not vendors[s.vendor].blocked), regular[0]
                )
                count = 1 if kind in ("single", "overdue") else rng.randint(2, 3)
                for line in range(1, count + 1):
                    offset = rng.choice((5, 13, 21, 29, 37, 45))  # odd: never a requirement day
                    if kind == "overdue" and line == 1:
                        offset = -rng.choice((3, 9, 15))
                    delivery = AS_OF + timedelta(days=offset)
                    quantity = max(1, int(weekly * rng.uniform(0.5, 2.0)))
                    reschedule = None
                    if offset > 9 and rng.random() < 0.25:
                        reschedule = delivery - timedelta(days=rng.choice((4, 8)))
                    lines.append(
                        ScheduleLine(po=str(po_number), item=10, line=line, delivery=delivery,
                                     open_quantity=quantity, vendor=vendor, price=sources[0].price,
                                     reschedule=reschedule)
                    )  # fmt: skip
            pairs.append(
                Pair(
                    material=code,
                    text=text,
                    plant=plant,
                    unit=unit,
                    group=group,
                    mrp_type="ND" if nd else "PD",
                    stock_locations=locations,
                    quality=rng.choice((0, 0, weekly // 4)),
                    blocked_stock=rng.choice((0, 0, weekly // 5)),
                    safety=safety,
                    min_lot=rng.choice((0, 0, weekly * 2)),
                    rounding=rng.choice((0, 10, 25))
                    if group not in ("FIXA", "QUIM")
                    else (1000 if group == "FIXA" else 0),
                    planned_days=rng.choice((10, 20, 30)),
                    requirements=requirements,
                    lines=lines,
                    sources=sources,
                )
            )
    scenario = Scenario(vendors=vendors, pairs=pairs)
    scenario.expected = reference(scenario)
    return scenario


# --- The reference plan -----------------------------------------------------------------


def _round_up(quantity: Decimal, step: int) -> Decimal:
    if step <= 0:
        return quantity
    return Decimal(math.ceil(quantity / step) * step)


def reference(scenario: Scenario) -> dict[tuple[str, str], Expected]:
    """The CEO's rules, applied to the generated facts (not to the reports)."""
    out: dict[tuple[str, str], Expected] = {}
    for pair in scenario.pairs:
        key = (pair.material, pair.plant)
        if pair.mrp_type == "ND":
            out[key] = Expected(status="excluded_nd")
            continue
        events: list[tuple[date, int, int]] = []  # (date, receipts first, signed quantity)
        for line in pair.lines:
            events.append((max(line.delivery, AS_OF), 0, line.open_quantity))
        for day, _, quantity in pair.requirements:
            events.append((day, 1, -quantity))
        events.sort()
        # The balance is judged where it can fall: after each requirement (receipts dated
        # on or before it, overdue ones at once, are in by then).
        saldo = Decimal(pair.stock - pair.safety)
        lowest = Decimal(0)
        first_shortage: date | None = None
        first_stockout: date | None = None
        for day, kind, quantity in events:
            saldo += quantity
            if kind == 0:
                continue
            lowest = min(lowest, saldo)
            if saldo < 0 and first_shortage is None:
                first_shortage = day
            if saldo < -pair.safety and first_stockout is None:
                first_stockout = day
        shortfall = -lowest
        if shortfall <= 0:
            out[key] = Expected(status="covered")
            continue
        valid = [
            s
            for s in pair.sources
            if s.valid_to >= AS_OF and not scenario.vendors[s.vendor].blocked
        ]
        if not valid:
            out[key] = Expected(
                status="no_valid_source", shortfall=shortfall,
                first_shortage=first_shortage, first_stockout=first_stockout,
            )  # fmt: skip
            continue

        def arrival(source: Source) -> date:
            return AS_OF + timedelta(days=source.lead_days)

        assert first_shortage is not None
        meets = [s for s in valid if arrival(s) <= first_shortage]
        consuming = [
            s for s in valid
            if first_stockout is None or arrival(s) <= first_stockout
        ]  # fmt: skip
        outcome: Outcome
        if meets:
            chosen = min(meets, key=lambda s: (s.per_base_unit(), arrival(s), s.vendor))
            outcome = "meets"
        elif consuming:
            chosen = min(consuming, key=lambda s: (s.per_base_unit(), arrival(s), s.vendor))
            outcome = "meets_consuming_safety_stock"
        else:
            chosen = min(valid, key=lambda s: (arrival(s), s.per_base_unit(), s.vendor))
            outcome = "unavoidable_stockout"
        quantity = max(shortfall, Decimal(pair.min_lot), Decimal(chosen.moq * chosen.conversion))
        quantity = _round_up(quantity, pair.rounding)
        quantity = _round_up(quantity, chosen.conversion)
        out[key] = Expected(
            status="buy",
            shortfall=shortfall,
            quantity=quantity,
            outcome=outcome,
            supplier=chosen.vendor,
            first_shortage=first_shortage,
            first_stockout=first_stockout,
        )
    return out


def counts(expected: dict[tuple[str, str], Expected]) -> dict[str, int]:
    tally = Counter(e.status for e in expected.values())
    tally.update(e.outcome for e in expected.values() if e.outcome == "unavoidable_stockout")
    return dict(tally)


# --- The eight reports ------------------------------------------------------------------


def _xlsx(header: list[str], rows: list[list[object]]) -> bytes:
    from openpyxl import Workbook  # pyright: ignore[reportMissingTypeStubs]

    book = Workbook(write_only=True)
    sheet = book.create_sheet()
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def reports(scenario: Scenario, *, planned_orders: bool = False) -> dict[str, bytes]:
    """File name -> workbook bytes, one per report. `planned_orders` adds MD16, the
    planned orders the MRP list's dependent requirements come from: it must not change
    the plan (MD04 already holds their component demand)."""
    vendors = scenario.vendors
    files: dict[str, bytes] = {}
    mb52: list[list[object]] = []
    for p in scenario.pairs:
        for n, quantity in enumerate(p.stock_locations, start=1):
            mb52.append([p.material, p.text, p.plant, f"{n:04d}", "ROH", p.group, p.unit,
                         quantity, 0, p.quality if n == 1 else 0,
                         p.blocked_stock if n == 1 else 0, "BRL"])  # fmt: skip
    files["MB52.xlsx"] = _xlsx(
        ["Material", "Texto breve de material", "Centro", "Depósito", "Tipo de material",
         "Grupo de mercadorias", "UM básica", "Utilização livre", "Trânsito e transf.",
         "Em controle qualidade", "Bloqueado", "Moeda"],
        mb52,
    )  # fmt: skip
    md04: list[list[object]] = []
    for p in scenario.pairs:
        running = Decimal(p.stock)
        md04.append([p.material, p.text, p.plant, p.unit, AS_OF, "Est.Cen", None, None, None,
                     p.stock, float(running)])  # fmt: skip
        running -= p.safety
        md04.append([p.material, p.text, p.plant, p.unit, AS_OF, "EstSeg",
                     "Estoque de segurança", None, None, -p.safety, float(running)])  # fmt: skip
        events: list[tuple[date, int, tuple[date, str, str, date | None, str | None, int]]] = []
        for line in p.lines:
            exception = "07" if line.delivery < AS_OF else ("10" if line.reschedule else None)
            data = (
                f"{line.po}/{line.item:05d}/{line.line:04d}"
                if len(p.lines) > 1 or line.line == 1 and int(line.po) % 2 == 0
                else f"{line.po}/{line.item:05d}"
            )
            events.append((line.delivery, 0, (line.delivery, "PedCpa", data, line.reschedule,
                                               exception, line.open_quantity)))  # fmt: skip
        for day, data, quantity in p.requirements:
            events.append((day, 1, (day, "NecDep", data, None, None, -quantity)))
        events.sort(key=lambda e: (e[0], e[1]))
        for _, _, (day, element, data, moved, exception, quantity) in events:
            running += quantity
            md04.append([p.material, p.text, p.plant, p.unit, day, element, data, moved,
                         exception, quantity, float(running)])  # fmt: skip
    files["MD04.xlsx"] = _xlsx(
        ["Material", "Texto breve de material", "Centro", "UM básica", "Data", "Elemento MRP",
         "Dados do elemento MRP", "Data reprogramação", "Exceção", "Entrada/necessidade",
         "Qtd.disponível"],
        md04,
    )  # fmt: skip
    files["MM60.xlsx"] = _xlsx(
        ["Material", "Texto breve de material", "Tipo de material", "Grupo de mercadorias",
         "UM básica", "Centro", "Tipo de MRP", "Tamanho do lote", "Tamanho mínimo do lote",
         "Valor de arredondamento", "Estoque de segurança", "Prazo entrega previsto"],
        [[p.material, p.text, "ROH", p.group, p.unit, p.plant, p.mrp_type, "EX", p.min_lot,
          p.rounding, p.safety, p.planned_days] for p in scenario.pairs],
    )  # fmt: skip
    me1m: list[list[object]] = []
    for i, p in enumerate(scenario.pairs):
        for k, s in enumerate(p.sources):
            me1m.append([f"53{i:05d}{k:02d}", p.material, p.text, s.vendor,
                         vendors[s.vendor].name, p.plant, float(s.price), "BRL", s.price_unit,
                         s.order_unit, f"{s.conversion} {p.unit}" if s.conversion > 1 else None,
                         s.lead_days, s.moq, date(2024, 1, 1), s.valid_to])  # fmt: skip
    files["ME1M.xlsx"] = _xlsx(
        ["Registro info", "Material", "Texto breve de material", "Fornecedor",
         "Nome do fornecedor", "Centro", "Preço líquido", "Moeda", "Unidade de preço",
         "Unidade de medida do pedido", "Conversão p/ UM básica", "Prazo entrega planejado",
         "Quantidade mínima", "Validade de", "Validade até"],
        me1m,
    )  # fmt: skip
    me2n: list[list[object]] = []
    for p in scenario.pairs:
        ordered_total = sum(line.open_quantity for line in p.lines)
        for line in p.lines:
            source = next(s for s in p.sources)  # price unit and order unit of the item
            me2n.append([line.po, line.item, AS_OF - timedelta(45),
                         f"{line.vendor} {vendors[line.vendor].name}", p.material, p.text,
                         p.plant, ordered_total, p.unit, float(line.price), "BRL",
                         source.price_unit, line.delivery, line.open_quantity])  # fmt: skip
    files["ME2N.xlsx"] = _xlsx(
        ["Documento de compra", "Item", "Data do documento", "Fornecedor/centro fornecedor",
         "Material", "Texto breve", "Centro", "Qtd.do pedido", "Unidade de medida do pedido",
         "Preço líquido", "Moeda", "Unidade de preço", "Data de remessa",
         "A fornecer (quantidade)"],
        me2n,
    )  # fmt: skip
    history: list[list[object]] = []
    rng = random.Random(11)  # noqa: S311 - a reproducible fixture, not security.
    for i, p in enumerate(scenario.pairs):
        if not p.sources:
            continue
        for k in range(12):
            s = p.sources[k % len(p.sources)]
            ordered = AS_OF - timedelta(days=30 + 25 * k)
            quantity = max(1, s.moq) * (2 if s.order_unit == "BD" else 1) or 100
            price = (s.price * Decimal(str(round(rng.uniform(0.9, 1.05), 3)))).quantize(
                Decimal("0.01")
            )
            history.append([f"44{i:05d}{k:03d}", 10, ordered, s.vendor, vendors[s.vendor].name,
                            p.material, p.text, p.plant, quantity, s.order_unit, float(price),
                            "BRL", s.price_unit, ordered + timedelta(s.lead_days),
                            ordered + timedelta(s.lead_days + rng.choice((-1, 0, 0, 2))),
                            quantity])  # fmt: skip
    files["ME80FN.xlsx"] = _xlsx(
        ["Documento de compra", "Item", "Data do documento", "Fornecedor",
         "Nome do fornecedor", "Material", "Texto breve", "Centro", "Qtd.do pedido",
         "Unidade de medida do pedido", "Preço líquido", "Moeda", "Unidade de preço",
         "Data de remessa", "Data da última entrada de mercadorias",
         "Qtd. entrada de mercadorias"],
        history,
    )  # fmt: skip
    if planned_orders:
        orders: dict[tuple[str, str], tuple[str, str, date, int]] = {}
        for p in scenario.pairs:
            for day, data, quantity in p.requirements:
                order, product = data.split("/")
                known = orders.get((order, p.plant))
                total = (known[3] if known else 0) + quantity
                orders[order, p.plant] = (
                    order,
                    product,
                    min(day, known[2]) if known else day,
                    total,
                )
        files["MD16.xlsx"] = _xlsx(
            ["Ordem planejada", "Material", "Texto breve de material", "Centro",
             "Tipo de ordem planejada", "Quantidade total da ordem", "UM básica",
             "Data de início", "Data de fim", "Firmada"],
            [[order, product, f"PRODUTO ACABADO {product}", plant, "LA", max(1, total // 10),
              "UN", start, start + timedelta(days=5), "X" if int(order) % 3 == 0 else None]
             for (_, plant), (order, product, start, total) in sorted(orders.items())],
        )  # fmt: skip
    files["ME6H.xlsx"] = _xlsx(
        ["Fornecedor", "Nome do fornecedor", "Nota geral", "Nota Preço", "Nota Qualidade",
         "Nota Entrega", "Nota Serviço", "Data da avaliação"],
        [[v.code, v.name, 85, 80, 90, 85, 80, date(2026, 9, 30)] for v in vendors.values()],
    )  # fmt: skip
    files["BP.xlsx"] = _xlsx(
        ["Fornecedor", "Nome 1", "CNPJ", "País", "Endereço de e-mail", "Bloqueio de compras"],
        [[v.code, v.name, v.tax_id, "BR", f"vendas@f{v.code}.example.test",
          "X" if v.blocked else None] for v in vendors.values()],
    )  # fmt: skip
    return files


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "sap_large")
    out.mkdir(parents=True, exist_ok=True)
    scenario = generate()
    for name, data in reports(scenario).items():
        (out / name).write_bytes(data)
    print(counts(scenario.expected))
