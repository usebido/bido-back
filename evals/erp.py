"""An ERP export (SAP list reports saved to Excel) for an EV traction motor.

Messy the way real exports are: report title and selection rows above every
table, Portuguese SAP field names (MB52, ME2M, CS12, MD04, XK03), numbers as
text with a decimal comma and three-decimal quantities ("888,000"), dd.mm.yyyy
text dates, a price unit column ("Por" 100), purchases in BRL and EUR, partial
deliveries and returned lots, a totals row, a three-level BOM whose product is
named only in the title, and a supplier master keyed by vendor code. Supplier
e-mails use reserved test domains only (example.test).

`sap_motor()` writes the workbook and returns the dataset it means (the BOM
exploded to the parts bought), which the golden expectations come from. Run as
a script to write the workbook for a manual test:

    uv run python -m evals.erp ~/Downloads/bido-teste-erp-sap-motor.xlsx
"""

import io
import random
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Final, cast

from evals.data import Dataset, Part, PlanRow, PurchaseRow, SupplierSpec

PRODUCT: Final = "MT-120-ASM"
FILENAME: Final = "bido-teste-erp-sap-motor.xlsx"


@dataclass(frozen=True, slots=True)
class _Vendor:
    code: str
    name: str
    country: str
    contact: str
    email: str
    classification: str  # as the supplier master writes it
    type: str  # what Bido stores


VENDORS: Final = (
    _Vendor("100231", "Aços Elétricos Paulista Ltda.", "BR", "Renata Moura",
            "vendas@acos-paulista.example.test", "Fabricante", "manufacturer"),
    _Vendor("100245", "Cobre Sul Fios Esmaltados S.A.", "BR", "Diego Ramos",
            "comercial@cobresul.example.test", "Fabricante", "manufacturer"),
    _Vendor("200118", "Magnetica Europe GmbH", "DE", "Stefan Krause",
            "sales@magnetica-eu.example.test", "Fabricante", "manufacturer"),
    _Vendor("100377", "Rolamentos Precisão Distribuidora", "BR", "Carla Nunes",
            "cotacao@rolprecisao.example.test", "Distribuidor autorizado",
            "authorized_distributor"),
    _Vendor("300902", "Global Parts Brokers Ltd", "GB", "Tom Hughes",
            "quotes@gpbrokers.example.test", "Revendedor", "reseller"),
    _Vendor("100412", "Fundição Vale do Aço Ltda.", "BR", "Marcos Teixeira",
            "orcamentos@fundicaovale.example.test", "Fabricante", "manufacturer"),
    _Vendor("200455", "Sensortec Resolver Europe", "DE", "Julia Weber",
            "rfq@sensortec-eu.example.test", "Fabricante", "manufacturer"),
    _Vendor("100518", "Conectores Brasil Ind. e Com.", "BR", "Paulo Lima",
            "vendas@conectoresbr.example.test", "Distribuidor autorizado",
            "authorized_distributor"),
    _Vendor("100533", "Isolantes Técnicos Ltda.", "BR", "Bianca Rocha",
            "comercial@isotecnicos.example.test", "Fabricante", "manufacturer"),
    _Vendor("100601", "Fixadores Paraná Ltda.", "BR", "Hugo Alves",
            "pedidos@fixpr.example.test", "Distribuidor autorizado", "authorized_distributor"),
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class _Node:
    """One BOM row: an assembly (no vendor) or a part bought from `vendor`."""

    level: int
    number: str
    description: str
    unit: str
    qty: Decimal  # per one unit of the row above
    mpn: str = ""
    manufacturer: str = ""
    vendor: str | None = None
    price: Decimal | None = None  # BRL per unit
    category: str = "catalog"


def _d(value: str) -> Decimal:
    return Decimal(value)


# The CS12 explosion, in order. Rotor magnets sit three levels down: 8 packs of 8.
BOM: Final = (
    _Node(1, "ST-120-ASM", "ESTATOR EV-120 BOBINADO", "UN", _d("1")),
    _Node(2, "CH-M270-35", "CHAPA ACO SILICIO M270-35A ESTAMPADA", "KG", _d("18.5"),
          "M270-35A", "Aços Paulista", "100231", _d("14.20"), "material"),
    _Node(2, "FI-CU-180", "FIO COBRE ESMALTADO 1,80MM CLASSE H", "KG", _d("6.4"),
          "CuL-H 1.80", "Cobre Sul", "100245", _d("72.50"), "material"),
    _Node(2, "PP-NMX-022", "PAPEL ISOLANTE NOMEX 0,22MM", "M2", _d("2.8"),
          "NMX-410-0.22", "Isolantes Técnicos", "100533", _d("38.90"), "material"),
    _Node(2, "VZ-H-5L", "VERNIZ IMPREGNACAO CLASSE H", "L", _d("1.2"),
          "ELAN-H 5L", "Isolantes Técnicos", "100533", _d("96.00"), "material"),
    _Node(1, "RT-120-ASM", "ROTOR EV-120 COM IMAS", "UN", _d("1")),
    _Node(2, "PK-MAG-08", "PACOTE IMAS ROTOR 8 PECAS", "UN", _d("8")),
    _Node(3, "IM-N48H-40", "IMA NDFEB N48H 40X12X5", "UN", _d("8"),
          "N48H-40125", "Magnetica Europe", "200118", _d("6.85")),
    _Node(2, "EX-120-42", "EIXO ROTOR AÇO 42CRMO4 USINADO", "UN", _d("1"),
          "SH-42CM-120", "Fundição Vale", "100412", _d("410.00"), "drawing"),
    _Node(1, "RL-6206-2RS", "ROLAMENTO 6206-2RS C3 HT", "UN", _d("1"),
          "6206-2RSH/C3", "SKF", "100377", _d("58.40")),
    _Node(1, "RL-6205-2RS", "ROLAMENTO 6205-2RS C3 HT", "UN", _d("1"),
          "6205-2RSH/C3", "SKF", "100377", _d("49.90")),
    _Node(1, "CA-120-AL", "CARCACA ALUMINIO A356 USINADA", "UN", _d("1"),
          "HS-A356-120", "Fundição Vale", "100412", _d("1280.00"), "drawing"),
    _Node(1, "TP-120-DE", "TAMPA LADO ACIONAMENTO", "UN", _d("1"),
          "EC-DE-120", "Fundição Vale", "100412", _d("245.00"), "drawing"),
    _Node(1, "TP-120-NDE", "TAMPA LADO OPOSTO", "UN", _d("1"),
          "EC-NDE-120", "Fundição Vale", "100412", _d("228.00"), "drawing"),
    _Node(1, "RS-AU-12", "RESOLVER 12 POLOS AUTOMOTIVO", "UN", _d("1"),
          "TS2620N21E11", "Tamagawa", "200455", _d("690.00")),
    _Node(1, "NT-10K-0603", "TERMISTOR NTC 10K AEC-Q200", "UN", _d("3"),
          "NCP18XH103F03RB", "Murata", "100518", _d("1.95")),
    _Node(1, "CN-HV-3P", "CONECTOR ALTA TENSAO 3 VIAS", "UN", _d("1"),
          "HVA280-3P", "TE Connectivity", "100518", _d("185.00")),
    _Node(1, "RT-SEAL-90", "RETENTOR FKM 40X62X7", "UN", _d("2"),
          "BABSL 40-62-7 FKM", "Freudenberg", "100377", _d("27.80")),
    _Node(1, "PF-M6-KIT", "KIT PARAFUSOS M6 CLASSE 10.9", "KIT", _d("1"),
          "M6x20-10.9-ZN", "", "100601", _d("18.60")),
)  # fmt: skip

# Second sources: a broker for magnets and bearings, two copper wire offers.
_SECOND: Final = {"IM-N48H-40": "300902", "RL-6206-2RS": "300902"}
# Price unit 100: SAP prices for cheap parts are per hundred.
_PER_100: Final = frozenset({"NT-10K-0603"})
# Materials in stock that are not in the motor's BOM (the gaps variant writes
# them with "n/d" as their stock: rows that cannot be read).
_OTHER_STOCK: Final = (
    ("EB-CX-120", "CAIXA EMBALAGEM MOTOR EV-120"),
    ("EB-PL-STD", "PALETE MADEIRA PADRAO"),
    ("LB-GR-01", "GRAXA ROLAMENTO ALTA TEMPERATURA"),
    ("ET-ID-120", "ETIQUETA IDENTIFICACAO MOTOR"),
    ("FT-ISO-05", "FITA ISOLANTE CLASSE H 19MM"),
)
_BRL_PER_EUR: Final = Decimal("6.1")
_CENT: Final = Decimal("0.01")
_MILLI: Final = Decimal("0.001")


def _br(value: Decimal, places: int) -> str:
    """Brazilian text: 1.234,560."""
    text = f"{value:,.{places}f}"
    return text.replace(",", "§").replace(".", ",").replace("§", ".")


def _dt(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _leaves() -> list[tuple[_Node, Decimal]]:
    """The parts bought and their quantity per motor (multiplied through levels)."""
    out: list[tuple[_Node, Decimal]] = []
    stack: list[tuple[int, Decimal]] = []
    for position, node in enumerate(BOM):
        while stack and stack[-1][0] >= node.level:
            stack.pop()
        quantity = node.qty * (stack[-1][1] if stack else Decimal(1))
        stack.append((node.level, quantity))
        has_children = position + 1 < len(BOM) and BOM[position + 1].level > node.level
        if not has_children:
            out.append((node, quantity))
    return out


def _vendor(code: str) -> _Vendor:
    return next(v for v in VENDORS if v.code == code)


def sap_motor(today: date, *, gaps: bool = False, seed: int = 42) -> tuple[bytes, Dataset]:
    """The workbook and the dataset it means. `gaps` adds unreadable stock rows."""
    rng = random.Random(seed)  # noqa: S311 - synthetic data, not security
    leaves = _leaves()

    # MB52: unrestricted, quality inspection and blocked stock per material.
    stock: dict[str, tuple[Decimal, Decimal]] = {}
    stock_rows: list[list[object]] = []
    for node, per_motor in leaves:
        free = (per_motor * rng.randint(20, 160)).quantize(Decimal("0.1"))
        qc = (per_motor * rng.choice((0, 0, 5, 12))).quantize(Decimal("0.1"))
        stock[node.number] = (free, Decimal(0))
        stock_rows.append([node.number, node.description, "1100", "MP01", _br(free, 3),
                           _br(qc, 3), "0,000", node.unit])  # fmt: skip
    if gaps:
        for number, description in _OTHER_STOCK:
            stock_rows.append([number, description, "1100", "EXP1", "n/d", "", "", "UN"])
    stock_rows.append(["", "", "", "", "", "", "", ""])
    stock_rows.append(["*", "Total", "", "", "", "", "", ""])

    # ME2M: purchase order lines with their deliveries.
    purchases: list[PurchaseRow] = []
    po_rows: list[list[object]] = []
    po = 4500021300
    for node, per_motor in leaves:
        assert node.vendor is not None and node.price is not None
        vendors = [node.vendor, *([_SECOND[node.number]] if node.number in _SECOND else [])]
        for item in range(rng.randint(3, 6)):
            code = vendors[0] if rng.random() < 0.7 else vendors[-1]  # second source: 30%
            vendor = _vendor(code)
            ordered = today - timedelta(days=rng.randint(20, 330))
            drift = Decimal(1) + Decimal(rng.randint(-40, 50)) / 1000
            if node.number == "FI-CU-180" and (today - ordered).days < 90:
                drift += Decimal("0.12")  # copper went up
            price = node.price * drift * (Decimal("0.88") if code == "300902" else 1)
            currency = "EUR" if code.startswith(("2", "3")) else "BRL"
            if currency == "EUR":
                price /= _BRL_PER_EUR
            per = Decimal(100) if node.number in _PER_100 else Decimal(1)
            written = (price * per).quantize(_CENT, rounding=ROUND_HALF_UP)
            quantity = (per_motor * rng.choice((100, 150, 200, 300, 400))).quantize(_MILLI)
            promised = ordered + timedelta(days=rng.choice((21, 30, 45, 60, 75)))
            late = rng.choice((10, 18, 30)) if code == "100412" else rng.choice((0, 0, 0, 5, 12))
            received = promised + timedelta(days=late)
            delivered = quantity if received < today else Decimal(0)
            if delivered and rng.random() < 0.15:
                delivered = (quantity * Decimal("0.6")).quantize(_MILLI)
            rejected = Decimal(0)
            if code == "300902" and delivered and rng.random() < 0.5:
                rejected = (delivered * Decimal("0.15")).quantize(_MILLI)
            po += rng.randint(3, 40)
            purchases.append(
                PurchaseRow(
                    po_number=str(po),
                    supplier=vendor.name,
                    part=node.number,
                    description=node.description,
                    quantity=quantity,
                    unit_price=written / per,
                    currency=currency,
                    ordered_at=ordered,
                    promised_at=promised,
                    received_at=received if delivered else None,
                    rejected_qty=rejected,
                    received_qty=delivered,
                )
            )
            po_rows.append([
                str(po), str(10 * (item + 1)).zfill(5), code, vendor.name, node.number,
                node.description, node.mpn, _br(quantity, 3), node.unit, _br(written, 2),
                currency, str(per), _dt(ordered), _dt(promised),
                _dt(received) if delivered else "", _br(delivered, 3), _br(rejected, 3), "1100",
            ])  # fmt: skip
    po_rows.sort(key=lambda r: str(r[0]))
    purchases.sort(key=lambda p: (p.ordered_at, p.po_number))

    # CS12: the explosion, level 0 the motor itself.
    bom_rows: list[list[object]] = [["0", PRODUCT, "MOTOR TRACAO EV-120 120KW MONTADO",
                                     "1,000", "UN", "", ""]]  # fmt: skip
    for node in BOM:
        bom_rows.append([f"{'.' * node.level}{node.level}", node.number, node.description,
                         _br(node.qty, 3), node.unit, node.mpn, node.manufacturer])  # fmt: skip

    plan = (
        PlanRow(PRODUCT, Decimal(250), today + timedelta(days=35)),
        PlanRow(PRODUCT, Decimal(300), today + timedelta(days=66)),
    )
    plan_rows: list[list[object]] = [
        [p.product, "MOTOR TRACAO EV-120 120KW MONTADO", _br(p.quantity, 0), "UN",
         _dt(p.need_by), "Ordem planejada"]
        for p in plan
    ]  # fmt: skip

    sheets: list[tuple[str, list[list[object]], list[object], list[list[object]]]] = [
        (
            "MB52 Estoque",
            [["Lista de estoque em depósito: material"],
             [f"Data: {_dt(today)}   Centro: 1100 (Joinville)"], []],
            ["Material", "Txt.brv.material", "Centro", "Depósito", "Utilização livre",
             "Em contr.qualidade", "Estoque bloqueado", "UM básica"],
            stock_rows,
        ),
        (
            "ME2M Pedidos",
            [["Documentos de compras por material"],
             ["Org.compras: 1000   Centro: 1100   Período: últimos 12 meses"], []],
            ["Doc.compras", "Item", "Fornecedor", "Nome do fornecedor", "Material",
             "Texto breve", "Nº peça fabricante", "Qtd.do pedido", "UMP", "Preço líquido",
             "Moeda", "Por", "Data doc.", "Data remessa", "Data EM", "Qtd.fornecida",
             "Qtd.devolvida", "Centro"],
            po_rows,
        ),
        (
            "CS12 Lista técnica",
            [["Explosão de lista técnica multinível"],
             [f"Material: {PRODUCT}   Centro: 1100   Utilização: 1 (Produção)   Qtd.base: 1 UN"],
             []],
            ["Nível", "Componente", "Texto do objeto", "Quantidade", "UM",
             "Nº peça fabricante", "Fabricante"],
            bom_rows,
        ),
        (
            "Plano de produção",
            [["Planejamento de produção (MD04)"], []],
            ["Material", "Texto breve", "Qtd.planejada", "UM", "Data necessidade",
             "Elemento MRP"],
            plan_rows,
        ),
        (
            "XK03 Fornecedores",
            [["Mestre de fornecedores (exportação)"], []],
            ["Fornecedor", "Nome", "País", "Pessoa de contato", "E-mail", "Classificação"],
            [[v.code, v.name, v.country, v.contact, v.email, v.classification]
             for v in VENDORS],
        ),
    ]  # fmt: skip

    from openpyxl import Workbook  # pyright: ignore[reportMissingTypeStubs]
    from openpyxl.styles import Font, PatternFill  # pyright: ignore[reportMissingTypeStubs]

    workbook = cast(Any, Workbook())
    workbook.remove(workbook.active)
    for title, titles, header, rows in sheets:
        sheet = workbook.create_sheet(title)
        for row in titles:
            sheet.append(row)
        sheet.append(header)
        for cell in sheet[sheet.max_row]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="D9E1F2")
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)

    bought = {v for node, _ in leaves for v in (node.vendor, _SECOND.get(node.number)) if v}
    dataset = Dataset(
        key="erp_sap_motor_gaps" if gaps else "erp_sap_motor",
        product=PRODUCT,
        parts=tuple(
            Part(
                number=node.number,
                description=node.description,
                mpn=node.mpn or None,
                manufacturer=node.manufacturer or None,
                category=node.category,
                unit=node.unit,
                qty_per_unit=per_motor,
                base_price=node.price or Decimal(0),
            )
            for node, per_motor in leaves
        ),
        suppliers=tuple(
            SupplierSpec(
                name=v.name,
                email=v.email,
                contact=v.contact,
                country=v.country,
                parts=tuple(
                    n.number for n, _ in leaves if v.code in (n.vendor, _SECOND.get(n.number))
                ),
                price_factor=Decimal(1),
                late_every=0,
                reject_every=0,
                type=v.type,
            )
            for v in VENDORS
            if v.code in bought
        ),
        stock=stock,
        purchases=tuple(purchases),
        plan=plan,
    )
    return buffer.getvalue(), dataset


def stock_rows_written(gaps: bool) -> int:
    """Stock rows in MB52 besides the totals (the gaps variant adds unreadable ones)."""
    return len(_leaves()) + (len(_OTHER_STOCK) if gaps else 0)


if __name__ == "__main__":
    out = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path(FILENAME)
    data, ds = sap_motor(date.today())
    out.write_bytes(data)
    print(f"{out}: {len(ds.parts)} parts, {len(ds.purchases)} PO lines, product {ds.product}")
