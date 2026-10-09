"""A synthetic SAP export of eight standard reports, and the purchase plan it means.

The set a buyer exports for one plant before an RFQ round: MB52 (stocks), MD04 (the
stock/requirements list, requirements signed negative), MM60 (MRP parameters), ME1M
(info records, one per 1000 and one in buckets of 18 L), ME2N (open orders, one
overdue), ME80FN (order history), ME6H (vendor evaluation) and BP (suppliers, one
purchase-blocked). Portuguese headers, as SAP GUI exports them. All data is invented;
e-mails use the reserved example.test domain.

The golden quantity per material comes from the report itself, not from Bido's code:
MD04's running available quantity already subtracts the safety stock, so the purchase
need is the deepest shortfall, max(0, -min(running available)), then the material
master's minimum lot and rounding value.

    uv run python -m evals.sap_reports /tmp/sap8   # writes the eight .xlsx files
"""

import io
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

AS_OF: Final = date(2026, 10, 8)
PLANT: Final = "1000"


@dataclass(frozen=True, slots=True)
class Material:
    code: str
    text: str
    unit: str
    stock: int
    safety: int
    # (days after AS_OF, quantity) of dependent requirements.
    needs: tuple[tuple[int, int], ...]
    # (po, days after AS_OF, open quantity, ordered quantity, vendor) open orders.
    orders: tuple[tuple[str, int, int, int, str], ...] = ()
    min_lot: int = 0
    rounding: int = 0
    vendor: str = "100101"
    price: Decimal = Decimal("10")
    price_unit: int = 1
    order_unit: str | None = None  # an alternative order unit ("BD")
    conversion: int | None = None  # base units per order unit


MATERIALS: Final = (
    Material("200000101", "CARCACA ALUMINIO MOTOR M-10", "UN", 80, 20,
             ((4, 60), (18, 70), (32, 50)), (("4500030001", 15, 100, 100, "100101"),),
             rounding=10, price=Decimal("412")),
    Material("200000110", "ROTOR FUNDIDO M-10", "UN", 40, 10,
             ((4, 60), (18, 70)), (("4500030002", -12, 50, 80, "100102"),),
             price=Decimal("230"), vendor="100102"),
    Material("200000130", "ROLAMENTO 6204-2RS", "UN", 900, 100,
             ((4, 300), (18, 320), (32, 300)), (("4500030003", 40, 4000, 4000, "100103"),),
             price=Decimal("9.8"), vendor="100103"),
    Material("200000160", "PARAFUSO M8X25 ZINCADO", "UN", 5000, 1000,
             ((4, 1500), (18, 1500), (32, 1500)), min_lot=5000, rounding=1000,
             price=Decimal("210"), price_unit=1000, vendor="100104"),
    Material("200000180", "VERNIZ ISOLANTE CLASSE H", "L", 60, 18,
             ((4, 30), (18, 40), (32, 35)), rounding=18, price=Decimal("540"),
             order_unit="BD", conversion=18, vendor="100105"),
    Material("200000190", "ANEL O-RING 40X3 NBR", "UN", 500, 50,
             ((4, 300), (32, 300)), price=Decimal("1.2"), vendor="100106"),
)  # fmt: skip
VENDORS: Final = {
    "100101": ("FUNDICAO EXEMPLO LTDA", ""),
    "100102": ("ROTORES EXEMPLO LTDA", ""),
    "100103": ("ROLAMENTOS EXEMPLO LTDA", ""),
    "100104": ("FIXADORES EXEMPLO LTDA", ""),
    "100105": ("QUIMICA EXEMPLO LTDA", ""),
    "100106": ("VEDACOES EXEMPLO LTDA", "X"),  # purchase-blocked
}


def _mrp_rows(m: Material) -> list[tuple[date, str, str, Decimal]]:
    """MD04 elements in date order: (date, element, data, signed quantity)."""
    rows: list[tuple[date, str, str, Decimal]] = [
        (AS_OF, "Est.Cen", "", Decimal(m.stock)),
        (AS_OF, "EstSeg", "Estoque de segurança", Decimal(-m.safety)),
    ]
    events: list[tuple[date, str, str, Decimal]] = []
    for i, (days, quantity) in enumerate(m.needs, start=1):
        events.append((AS_OF + timedelta(days), "NecDep", f"000000{i:04d}", Decimal(-quantity)))
    for po, days, open_quantity, _, _ in m.orders:
        events.append((AS_OF + timedelta(days), "PedCpa", f"{po}/00010", Decimal(open_quantity)))
    return rows + sorted(events, key=lambda e: e[0])


def golden() -> dict[str, Decimal]:
    """The quantity to buy per material, read from MD04's own running availability."""
    result: dict[str, Decimal] = {}
    for m in MATERIALS:
        running, lowest = Decimal(0), Decimal(0)
        for day, _, _, quantity in _mrp_rows(m):
            # MRP counts an overdue order as available at once.
            running += quantity
            if day >= AS_OF:
                lowest = min(lowest, running)
        need = -lowest
        if need > 0 and m.min_lot and need < m.min_lot:
            need = Decimal(m.min_lot)
        if need > 0 and m.rounding and need % m.rounding:
            need = (need // m.rounding + 1) * m.rounding
        result[m.code] = need
    return result


def _xlsx(header: list[str], rows: list[list[object]]) -> bytes:
    from openpyxl import Workbook  # pyright: ignore[reportMissingTypeStubs]

    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def reports() -> dict[str, bytes]:
    """File name -> workbook bytes, one per report."""
    files: dict[str, bytes] = {}
    files["MB52.xlsx"] = _xlsx(
        ["Material", "Texto breve de material", "Centro", "Depósito", "UM básica",
         "Utilização livre", "Trânsito e transf.", "Em controle qualidade", "Bloqueado"],
        [[m.code, m.text, PLANT, "0001", m.unit, m.stock, 0, 5 if i == 0 else 0, 0]
         for i, m in enumerate(MATERIALS)],
    )  # fmt: skip
    md04: list[list[object]] = []
    for m in MATERIALS:
        running = Decimal(0)
        for day, element, data, quantity in _mrp_rows(m):
            running += quantity
            md04.append(
                [m.code, m.text, PLANT, m.unit, day, element, data, None, None,
                 float(quantity), float(running)]
            )  # fmt: skip
    files["MD04.xlsx"] = _xlsx(
        ["Material", "Texto breve de material", "Centro", "UM básica", "Data", "Elemento MRP",
         "Dados do elemento MRP", "Data reprogramação", "Exceção", "Entrada/necessidade",
         "Qtd.disponível"],
        md04,
    )  # fmt: skip
    files["MM60.xlsx"] = _xlsx(
        ["Material", "Texto breve de material", "UM básica", "Centro", "Tipo de MRP",
         "Tamanho do lote", "Tamanho mínimo do lote", "Valor de arredondamento",
         "Estoque de segurança", "Prazo entrega previsto"],
        [[m.code, m.text, m.unit, PLANT, "PD", "EX", m.min_lot, m.rounding, m.safety, 20]
         for m in MATERIALS],
    )  # fmt: skip
    files["ME1M.xlsx"] = _xlsx(
        ["Registro info", "Material", "Fornecedor", "Nome do fornecedor", "Centro",
         "Preço líquido", "Moeda", "Unidade de preço", "Unidade de medida do pedido",
         "Conversão p/ UM básica", "Prazo entrega planejado", "Quantidade mínima",
         "Validade de", "Validade até"],
        [[f"53000{i:05d}", m.code, m.vendor, VENDORS[m.vendor][0], PLANT, float(m.price),
          "BRL", m.price_unit, m.order_unit or m.unit,
          f"{m.conversion} {m.unit}" if m.conversion else None, 20, 0,
          date(2026, 1, 1), date(9999, 12, 31)]
         for i, m in enumerate(MATERIALS)],
    )  # fmt: skip
    files["ME2N.xlsx"] = _xlsx(
        ["Documento de compra", "Item", "Data do documento", "Fornecedor/centro fornecedor",
         "Material", "Texto breve", "Centro", "Qtd.do pedido", "Unidade de medida do pedido",
         "Preço líquido", "Moeda", "Unidade de preço", "Data de remessa",
         "A fornecer (quantidade)"],
        [[po, 10, AS_OF - timedelta(30), f"{vendor} {VENDORS[vendor][0]}", m.code, m.text,
          PLANT, ordered, m.unit, float(m.price), "BRL", m.price_unit,
          AS_OF + timedelta(days), open_quantity]
         for m in MATERIALS for po, days, open_quantity, ordered, vendor in m.orders],
    )  # fmt: skip
    history: list[list[object]] = []
    for i, m in enumerate(MATERIALS):
        for k in range(3):
            ordered = AS_OF - timedelta(days=60 + 70 * k)
            quantity = 10 * (k + 2) if m.order_unit else 100 * (k + 1)
            price = m.price * (Decimal(1) - Decimal(k) / 50)
            history.append(
                [f"45000{i:02d}{k:03d}", 10, ordered, m.vendor, VENDORS[m.vendor][0], m.code,
                 m.text, PLANT, quantity, m.order_unit or m.unit, float(price), "BRL",
                 m.price_unit, ordered + timedelta(15), ordered + timedelta(14), quantity]
            )  # fmt: skip
    files["ME80FN.xlsx"] = _xlsx(
        ["Documento de compra", "Item", "Data do documento", "Fornecedor",
         "Nome do fornecedor", "Material", "Texto breve", "Centro", "Qtd.do pedido",
         "Unidade de medida do pedido", "Preço líquido", "Moeda", "Unidade de preço",
         "Data de remessa", "Data da última entrada de mercadorias",
         "Qtd. entrada de mercadorias"],
        history,
    )  # fmt: skip
    files["ME6H.xlsx"] = _xlsx(
        ["Fornecedor", "Nome do fornecedor", "Nota geral", "Nota Preço", "Nota Qualidade",
         "Nota Entrega", "Nota Serviço", "Data da avaliação"],
        [[code, name, 85, 80, 90, 85, 80, date(2026, 9, 30)]
         for code, (name, _) in VENDORS.items()],
    )  # fmt: skip
    files["BP.xlsx"] = _xlsx(
        ["Fornecedor", "Nome 1", "País", "Endereço de e-mail", "Bloqueio de compras"],
        [[code, name, "BR", f"vendas@f{code}.example.test", block or None]
         for code, (name, block) in VENDORS.items()],
    )  # fmt: skip
    return files


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "sap8")
    out.mkdir(parents=True, exist_ok=True)
    for name, data in reports().items():
        (out / name).write_bytes(data)
    print({code: str(quantity) for code, quantity in golden().items()})
