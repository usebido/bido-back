"""The CEO's cold test of a SAP export (plant 1000), as a regression set for timing.

Eight reports for seven materials, with the dated balances, lead times and MRP
exceptions of the review that found the plan recommending a supplier that arrives after
every need. The numbers reproduce that test. Company names are invented and e-mails use
the reserved example.test domain. The review's own gold file was not available: the
expected outcomes below are the review's acceptance criteria.

    uv run python -m evals.sap_review_case /tmp/review8   # writes the eight .xlsx files
"""

import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

from evals.sap_reports import _xlsx  # pyright: ignore[reportPrivateUsage]

AS_OF: Final = date(2026, 10, 8)
PLANT: Final = "1000"

# code: (name, payment terms, blocked)
VENDORS: Final = {
    "100101": ("FUNDICAO EXEMPLO AZUL LTDA", ""),
    "100102": ("METALURGICA EXEMPLO TIETE LTDA", ""),
    "100103": ("INOX EXEMPLO COMPONENTES LTDA", ""),
    "100104": ("USINAGEM EXEMPLO PRECISA LTDA", ""),
    "100107": ("VEDACOES EXEMPLO SUL LTDA", "X"),  # purchase-blocked
    "100108": ("ELETROMOTORES EXEMPLO SA", ""),
    "100109": ("MOTORES EXEMPLO SUL LTDA", ""),
    "100113": ("TINTAS EXEMPLO INDUSTRIAIS LTDA", ""),
}


@dataclass(frozen=True, slots=True)
class Material:
    code: str
    text: str
    unit: str
    group: str
    stock: int
    safety: int
    planned_days: int
    rounding: int
    # MD04 elements after stock and safety stock: (date, element, data, reschedule date,
    # exception, signed quantity).
    mrp: tuple[tuple[date, str, str, date | None, str | None, Decimal], ...]
    # Info records: (vendor, price, lead days, valid from, valid to, order unit, conversion).
    info: tuple[tuple[str, Decimal, int, date, date, str, str | None], ...]
    # Open orders: (po, ordered, vendor, quantity, price, delivery).
    orders: tuple[tuple[str, date, str, int, Decimal, date], ...] = ()
    # Order history: (po, ordered, vendor, quantity, unit, price, delivery, received).
    history: tuple[tuple[str, date, str, int, str, Decimal, date, date], ...] = ()


def _d(text: str) -> date:
    return date.fromisoformat(text)


def _req(
    day: str, order: int, product: str, quantity: str
) -> tuple[date, str, str, date | None, str | None, Decimal]:
    return (_d(day), "NecDep", f"000000{order:04d}/{product}", None, None, -Decimal(quantity))


def _po(day: str, po: str, quantity: int, moved: str | None = None, exception: str | None = None):
    return (
        _d(day), "PedCpa", f"{po}/00010", _d(moved) if moved else None, exception,
        Decimal(quantity),
    )  # fmt: skip


BC50, BC80, BC125 = "300000010", "300000020", "300000030"
FOREVER: Final = date(9999, 12, 31)
JAN: Final = date(2026, 1, 1)

MATERIALS: Final = (
    Material(
        "100000101",
        "CARCACA FERRO FUNDIDO GG20 BOMBA BC-50",
        "UN",
        "FUND",
        95,
        30,
        45,
        10,
        (
            _req("2026-10-12", 4101, BC50, "80"),
            _po("2026-10-23", "4500019725", 150, "2026-10-12", "10"),
            _req("2026-10-26", 4102, BC50, "85"),
            _req("2026-11-09", 4103, BC50, "95"),
            _req("2026-11-23", 4104, BC50, "100"),
            _req("2026-12-07", 4105, BC50, "95"),
            _req("2026-12-21", 4106, BC50, "50"),
        ),
        (
            ("100101", Decimal("378"), 45, JAN, FOREVER, "UN", None),
            ("100102", Decimal("366.5"), 30, JAN, FOREVER, "UN", None),
        ),
        (("4500019725", _d("2026-09-08"), "100101", 150, Decimal("378"), _d("2026-10-23")),),
        (
            (
                "4500018059",
                _d("2025-01-15"),
                "100102",
                180,
                "UN",
                Decimal("339.43"),
                _d("2025-02-14"),
                _d("2025-02-15"),
            ),
            (
                "4500018086",
                _d("2025-04-14"),
                "100101",
                180,
                "UN",
                Decimal("344.91"),
                _d("2025-05-29"),
                _d("2025-05-28"),
            ),
            (
                "4500018169",
                _d("2025-11-10"),
                "100101",
                140,
                "UN",
                Decimal("362.42"),
                _d("2025-12-25"),
                _d("2025-12-23"),
            ),
            (
                "4500018226",
                _d("2026-04-26"),
                "100102",
                120,
                "UN",
                Decimal("358.28"),
                _d("2026-05-26"),
                _d("2026-05-26"),
            ),
        ),
    ),
    Material(
        "100000103",
        "CARCACA FERRO FUNDIDO GG20 BOMBA BC-125",
        "UN",
        "FUND",
        95,
        8,
        50,
        5,
        (
            _req("2026-10-12", 4113, BC125, "10"),
            _req("2026-10-26", 4114, BC125, "10"),
            _req("2026-11-09", 4115, BC125, "12"),
            _po("2026-11-13", "4500019733", 40),
            _req("2026-11-23", 4116, BC125, "12"),
            _req("2026-12-07", 4117, BC125, "10"),
            _req("2026-12-21", 4118, BC125, "6"),
        ),
        (("100101", Decimal("872"), 50, JAN, FOREVER, "UN", None),),
        (("4500019733", _d("2026-09-24"), "100101", 40, Decimal("872"), _d("2026-11-13")),),
        (
            (
                "4500018149",
                _d("2025-10-12"),
                "100101",
                50,
                "UN",
                Decimal("830.31"),
                _d("2025-12-01"),
                _d("2025-12-01"),
            ),
            (
                "4500018250",
                _d("2026-06-23"),
                "100101",
                49,
                "UN",
                Decimal("874.97"),
                _d("2026-08-12"),
                _d("2026-08-11"),
            ),
        ),
    ),
    Material(
        "100000110",
        "ROTOR FECHADO INOX 304 D160MM",
        "UN",
        "USIN",
        60,
        25,
        30,
        0,
        (
            _po("2026-09-25", "4500019706", 120, None, "07"),
            _req("2026-10-12", 4101, BC50, "80"),
            _req("2026-10-26", 4102, BC50, "85"),
            _req("2026-11-09", 4103, BC50, "95"),
            _req("2026-11-23", 4104, BC50, "100"),
            _req("2026-12-07", 4105, BC50, "95"),
            _req("2026-12-21", 4106, BC50, "50"),
        ),
        (
            ("100103", Decimal("262"), 30, JAN, FOREVER, "UN", None),
            ("100104", Decimal("248"), 21, JAN, FOREVER, "UN", None),
        ),
        (("4500019706", _d("2026-09-01"), "100104", 150, Decimal("248"), _d("2026-09-25")),),
        (
            (
                "4500018189",
                _d("2025-12-17"),
                "100103",
                190,
                "UN",
                Decimal("250.55"),
                _d("2026-01-16"),
                _d("2026-02-04"),
            ),
            (
                "4500018254",
                _d("2026-07-09"),
                "100104",
                110,
                "UN",
                Decimal("238.94"),
                _d("2026-07-30"),
                _d("2026-07-30"),
            ),
        ),
    ),
    Material(
        "100000120",
        "EIXO ACO SAE 4140 D25X420MM RETIFICADO",
        "UN",
        "USIN",
        260,
        40,
        25,
        0,
        (
            _req("2026-10-12", 4101, BC50, "80"),
            _req("2026-10-12", 4107, BC80, "50"),
            _req("2026-10-26", 4102, BC50, "85"),
            _req("2026-10-26", 4108, BC80, "50"),
            _po("2026-10-30", "4500019744", 200, "2026-10-26", "10"),
            _req("2026-11-09", 4103, BC50, "95"),
            _req("2026-11-09", 4109, BC80, "60"),
            _req("2026-11-23", 4104, BC50, "100"),
            _req("2026-11-23", 4110, BC80, "60"),
            _req("2026-12-07", 4105, BC50, "95"),
            _req("2026-12-07", 4111, BC80, "55"),
            _req("2026-12-21", 4106, BC50, "50"),
            _req("2026-12-21", 4112, BC80, "30"),
        ),
        (
            ("100104", Decimal("139"), 25, JAN, FOREVER, "UN", None),
            ("100102", Decimal("133.5"), 20, JAN, FOREVER, "UN", None),
        ),
        (("4500019744", _d("2026-10-05"), "100104", 200, Decimal("139"), _d("2026-10-30")),),
        (
            (
                "4500018021",
                _d("2024-11-03"),
                "100102",
                200,
                "UN",
                Decimal("116.56"),
                _d("2024-11-23"),
                _d("2024-11-21"),
            ),
            (
                "4500018119",
                _d("2025-06-11"),
                "100102",
                210,
                "UN",
                Decimal("122.9"),
                _d("2025-07-01"),
                _d("2025-06-30"),
            ),
            # The outlier: +32% and delivered in 8 days against 18-24 for the others.
            (
                "4500018182",
                _d("2025-12-05"),
                "100102",
                150,
                "UN",
                Decimal("179.25"),
                _d("2025-12-12"),
                _d("2025-12-13"),
            ),
            (
                "4500018271",
                _d("2026-08-07"),
                "100104",
                240,
                "UN",
                Decimal("135.59"),
                _d("2026-09-01"),
                _d("2026-08-31"),
            ),
        ),
    ),
    Material(
        "100000141",
        "SELO MECANICO TIPO 21 EIXO 35MM",
        "UN",
        "VEDA",
        22,
        10,
        20,
        0,
        (
            _req("2026-10-12", 4113, BC125, "10"),
            _req("2026-10-26", 4114, BC125, "10"),
            _req("2026-11-09", 4115, BC125, "12"),
            _req("2026-11-23", 4116, BC125, "12"),
            _req("2026-12-07", 4117, BC125, "10"),
            _req("2026-12-21", 4118, BC125, "6"),
        ),
        (("100107", Decimal("139"), 20, date(2024, 1, 1), date(2025, 6, 30), "UN", None),),
        (),
        (
            (
                "4500018002",
                _d("2024-10-14"),
                "100107",
                31,
                "UN",
                Decimal("128.38"),
                _d("2024-11-03"),
                _d("2024-11-01"),
            ),
            (
                "4500018082",
                _d("2025-03-10"),
                "100107",
                43,
                "UN",
                Decimal("129.64"),
                _d("2025-03-30"),
                _d("2025-03-28"),
            ),
        ),
    ),
    Material(
        "100000152",
        "MOTOR ELETRICO TRIF 25CV 4P 380/660V IP55",
        "UN",
        "MOTO",
        9,
        2,
        75,
        0,
        (
            _req("2026-10-12", 4113, BC125, "10"),
            _req("2026-10-26", 4114, BC125, "10"),
            _req("2026-11-09", 4115, BC125, "12"),
            _req("2026-11-23", 4116, BC125, "12"),
            _req("2026-12-07", 4117, BC125, "10"),
            _po("2026-12-11", "4500019736", 20, "2026-10-12", "10"),
            _req("2026-12-21", 4118, BC125, "6"),
        ),
        (
            ("100109", Decimal("7520"), 75, JAN, FOREVER, "UN", None),
            ("100108", Decimal("7890"), 45, JAN, FOREVER, "UN", None),
        ),
        (("4500019736", _d("2026-09-28"), "100109", 20, Decimal("7520"), _d("2026-12-11")),),
        (
            (
                "4500018116",
                _d("2025-06-07"),
                "100109",
                24,
                "UN",
                Decimal("6996.17"),
                _d("2025-08-21"),
                _d("2025-08-20"),
            ),
            (
                "4500018207",
                _d("2026-01-31"),
                "100109",
                12,
                "UN",
                Decimal("7600.33"),
                _d("2026-04-16"),
                _d("2026-04-14"),
            ),
            (
                "4500018211",
                _d("2026-04-03"),
                "100109",
                10,
                "UN",
                Decimal("7512.98"),
                _d("2026-06-17"),
                _d("2026-06-26"),
            ),
        ),
    ),
    Material(
        "100000180",
        "TINTA EPOXI CINZA RAL7035",
        "L",
        "QUIM",
        126,
        36,
        10,
        18,
        (
            _req("2026-10-12", 4101, BC50, "48"),
            _req("2026-10-12", 4107, BC80, "45"),
            _req("2026-10-12", 4113, BC125, "15"),
            _req("2026-10-26", 4102, BC50, "51"),
            _req("2026-10-26", 4108, BC80, "45"),
            _req("2026-10-26", 4114, BC125, "15"),
            _req("2026-11-09", 4103, BC50, "57"),
            _req("2026-11-09", 4109, BC80, "54"),
            _req("2026-11-09", 4115, BC125, "18"),
            _req("2026-11-23", 4104, BC50, "60"),
            _req("2026-11-23", 4110, BC80, "54"),
            _req("2026-11-23", 4116, BC125, "18"),
            _req("2026-12-07", 4105, BC50, "57"),
            _req("2026-12-07", 4111, BC80, "49.5"),
            _req("2026-12-07", 4117, BC125, "15"),
            _req("2026-12-21", 4106, BC50, "30"),
            _req("2026-12-21", 4112, BC80, "27"),
            _req("2026-12-21", 4118, BC125, "9"),
        ),
        (("100113", Decimal("862"), 10, JAN, FOREVER, "BD", "18 L"),),
        (),
        (
            (
                "4500018161",
                _d("2025-10-28"),
                "100113",
                19,
                "BD",
                Decimal("822.32"),
                _d("2025-11-07"),
                _d("2025-11-05"),
            ),
            (
                "4500018235",
                _d("2026-06-17"),
                "100113",
                13,
                "BD",
                Decimal("886.66"),
                _d("2026-06-27"),
                _d("2026-06-26"),
            ),
        ),
    ),
)

# The review's acceptance criteria, per material.
QUANTITIES: Final = {
    "100000101": Decimal(290),
    "100000103": Decimal(0),
    "100000110": Decimal(350),
    "100000120": Decimal(390),
    "100000141": Decimal(48),
    "100000152": Decimal(47),
    "100000180": Decimal(594),
}
OUTCOMES: Final = {
    "100000101": "meets_consuming_safety_stock",
    "100000110": "meets_consuming_safety_stock",
    "100000120": "unavoidable_stockout",
    "100000152": "unavoidable_stockout",
    "100000180": "meets_consuming_safety_stock",
}


def reports() -> dict[str, bytes]:
    """File name -> workbook bytes, one per report."""
    files: dict[str, bytes] = {}
    files["MB52.xlsx"] = _xlsx(
        ["Material", "Texto breve de material", "Centro", "Depósito", "Tipo de material",
         "Grupo de mercadorias", "UM básica", "Utilização livre", "Trânsito e transf.",
         "Em controle qualidade", "Bloqueado", "Moeda"],
        [[m.code, m.text, PLANT, "0001", "ROH", m.group, m.unit, m.stock, 0, 0, 0, "BRL"]
         for m in MATERIALS],
    )  # fmt: skip
    md04: list[list[object]] = []
    for m in MATERIALS:
        running = Decimal(m.stock)
        md04.append([m.code, m.text, PLANT, m.unit, AS_OF, "Est.Cen", None, None, None,
                     m.stock, float(running)])  # fmt: skip
        running -= m.safety
        md04.append([m.code, m.text, PLANT, m.unit, AS_OF, "EstSeg", "Estoque de segurança",
                     None, None, -m.safety, float(running)])  # fmt: skip
        for day, element, data, moved, exception, quantity in m.mrp:
            running += quantity
            md04.append([m.code, m.text, PLANT, m.unit, day, element, data, moved, exception,
                         float(quantity), float(running)])  # fmt: skip
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
        [[m.code, m.text, "ROH", m.group, m.unit, PLANT, "PD", "EX", 0, m.rounding, m.safety,
          m.planned_days] for m in MATERIALS],
    )  # fmt: skip
    files["ME1M.xlsx"] = _xlsx(
        ["Registro info", "Material", "Texto breve de material", "Fornecedor",
         "Nome do fornecedor", "Centro", "Preço líquido", "Moeda", "Unidade de preço",
         "Unidade de medida do pedido", "Conversão p/ UM básica", "Prazo entrega planejado",
         "Quantidade mínima", "Validade de", "Validade até"],
        [[f"53000{i:02d}{k:03d}", m.code, m.text, vendor, VENDORS[vendor][0], PLANT,
          float(price), "BRL", 1, unit, conversion, days, 0, start, end]
         for i, m in enumerate(MATERIALS)
         for k, (vendor, price, days, start, end, unit, conversion) in enumerate(m.info)],
    )  # fmt: skip
    files["ME2N.xlsx"] = _xlsx(
        ["Documento de compra", "Item", "Data do documento", "Fornecedor/centro fornecedor",
         "Material", "Texto breve", "Centro", "Qtd.do pedido", "Unidade de medida do pedido",
         "Preço líquido", "Moeda", "Unidade de preço", "Data de remessa",
         "A fornecer (quantidade)"],
        [[po, 10, ordered, f"{vendor} {VENDORS[vendor][0]}", m.code, m.text, PLANT, quantity,
          m.unit, float(price), "BRL", 1, delivery,
          120 if po == "4500019706" else quantity]
         for m in MATERIALS for po, ordered, vendor, quantity, price, delivery in m.orders],
    )  # fmt: skip
    files["ME80FN.xlsx"] = _xlsx(
        ["Documento de compra", "Item", "Data do documento", "Fornecedor",
         "Nome do fornecedor", "Material", "Texto breve", "Centro", "Qtd.do pedido",
         "Unidade de medida do pedido", "Preço líquido", "Moeda", "Unidade de preço",
         "Data de remessa", "Data da última entrada de mercadorias",
         "Qtd. entrada de mercadorias"],
        [[po, 10, ordered, vendor, VENDORS[vendor][0], m.code, m.text, PLANT, quantity, unit,
          float(price), "BRL", 1, delivery, received, quantity]
         for m in MATERIALS
         for po, ordered, vendor, quantity, unit, price, delivery, received in m.history],
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
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "review8")
    out.mkdir(parents=True, exist_ok=True)
    for name, data in reports().items():
        (out / name).write_bytes(data)
    print({code: str(quantity) for code, quantity in QUANTITIES.items()})
