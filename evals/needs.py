"""Needs first: an ERP procurement export whose MRP sheet says what to buy.

Shaped after a CEO's test workbook (2026-10-08), with its own synthetic data:
a stock sheet (MMBE: free, blocked, reserved stock, on order, safety stock, a
base unit "UN" next to a price unit KG / M / EA), the purchase history, a
supplier master with material groups ("Grupo de mercadoria") and e-mails on
reserved test domains, the buyer's supplier evaluation (OTIF, quality, rating,
lead time), the MRP's needs per material and date, and a consolidated view that
already holds the right answer: "Necessidade de compra" = need + safety stock -
free stock - on order, never below 0.

The scenarios check that Bido reaches those numbers material by material (no
BOM involved), reads the stock in the price unit when the units differ, and
groups the RFQs by material group with that group's supplier invited:

* `needs_import`: import + `create_requirements_analysis` + RFQs per group.
* `needs_copilot`: upload, "o que preciso comprar?", "pode criar as RFQs"
  through the real copilot (live only).
* `needs_typed`: the same data without the MRP sheet; the buyer types one need
  in the chat (live only).

Run as a script to write the workbook for a manual test:

    uv run python -m evals.needs ~/Downloads/bido-teste-necessidades.xlsx
"""

import io
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, cast
from uuid import UUID

from sqlalchemy import select

from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.db.models import Invite, Rfq, RfqLine, Supplier
from bido_back.onboarding.service import store_upload
from bido_back.purchasing import service as purchasing
from bido_back.purchasing.schemas import RequirementsAnalysisRequest, RfqFromAnalysisRequest
from bido_back.purchasing.workbook import SheetMapper, default_sheet_mapper, import_workbook
from evals.harness import XLSX, Env, RunResult, Turn, called, copilot_turn, new_company

FILENAME: Final = "ERP_Export_Compras_Necessidades.xlsx"
PLANT: Final = "BR02"


@dataclass(frozen=True, slots=True)
class Group:
    name: str
    code: str
    supplier: str
    email: str
    otif: int
    quality: int
    rating: Decimal
    lead_time: int


@dataclass(frozen=True, slots=True)
class Material:
    code: str
    description: str
    price_unit: str  # the unit it is bought and priced in (MRP and prices)
    group: str
    price: Decimal
    free: int
    blocked: int
    reserved: int
    on_order: int
    safety: int
    need: int
    need_days: int  # need-by date = today + this

    @property
    def to_buy(self) -> int:
        return max(0, self.need + self.safety - self.free - self.on_order)


GROUPS: Final = (
    Group("Chapas de Aço", "20002001", "Siderúrgica Vale do Aço Ltda.",
          "vendas@valedoaco.example.test", 94, 93, Decimal("4.3"), 8),
    Group("Perfis e Tubos", "20002002", "Tubotec Perfis Metálicos S.A.",
          "comercial@tubotec.example.test", 92, 95, Decimal("4.4"), 11),
    Group("Fixação", "20002003", "Parafusos Prime Indústria Ltda.",
          "vendas@parafusosprime.example.test", 90, 98, Decimal("4.2"), 6),
    Group("Transmissão", "20002004", "Movimentar Rolamentos e Correias Ltda.",
          "cotacoes@movimentar.example.test", 97, 96, Decimal("4.8"), 7),
    Group("Vedação", "20002005", "Vedabras Componentes de Vedação Ltda.",
          "vendas@vedabras.example.test", 93, 91, Decimal("4.0"), 13),
)  # fmt: skip
# A general distributor that sold a bit of everything once: invited for what it sold.
DISTRIBUTOR: Final = Group(
    "MRO Geral", "20002009", "Central MRO Distribuidora Ltda.",
    "compras@centralmro.example.test", 86, 88, Decimal("3.8"), 5,
)  # fmt: skip

MATERIALS: Final = (
    Material("300000301", "CHAPA AÇO SAE 1045 4MM", "KG", "Chapas de Aço",
             Decimal("8.40"), 9200, 1500, 2600, 1200, 5000, 9800, 20),
    Material("300000302", "CHAPA AÇO INOX 304 2MM", "KG", "Chapas de Aço",
             Decimal("31.70"), 2100, 300, 900, 400, 1500, 2600, 24),
    Material("300000303", "PERFIL U 100X50X3MM", "M", "Perfis e Tubos",
             Decimal("22.10"), 4300, 700, 1900, 900, 3500, 5200, 17),
    Material("300000304", "TUBO REDONDO 1 1/2 X 2MM", "M", "Perfis e Tubos",
             Decimal("14.90"), 6100, 900, 2400, 1500, 4000, 3800, 17),
    Material("300000305", "PARAFUSO ALLEN M10X40 8.8", "EA", "Fixação",
             Decimal("0.42"), 64000, 9000, 31000, 15000, 70000, 58000, 14),
    Material("300000306", "PORCA AUTOTRAVANTE M10", "EA", "Fixação",
             Decimal("0.18"), 81000, 12000, 40000, 20000, 60000, 45000, 14),
    Material("300000307", "ROLAMENTO 6305-2Z", "EA", "Transmissão",
             Decimal("27.30"), 1300, 200, 600, 300, 1500, 1700, 28),
    Material("300000308", "CORREIA SINCRONIZADORA HTD 8M", "EA", "Transmissão",
             Decimal("96.00"), 420, 50, 120, 80, 300, 380, 28),
    Material("300000309", "ANEL O-RING NBR 70 50X3", "EA", "Vedação",
             Decimal("0.95"), 22000, 3000, 9000, 4000, 15000, 12000, 31),
    # Free stock and open orders cover the need and the buffer: nothing to buy.
    Material("300000310", "RETENTOR 45X62X8 NBR", "EA", "Vedação",
             Decimal("3.40"), 9000, 500, 2000, 1500, 2500, 4000, 31),
)  # fmt: skip


def group(name: str) -> Group:
    return next(g for g in GROUPS if g.name == name)


def golden() -> dict[str, dict[str, Any]]:
    """Per material: what to buy (the consolidated view's answer), its unit and group."""
    return {
        m.code: {
            "to_buy": Decimal(m.to_buy),
            "unit": m.price_unit,
            "group": m.group,
            "specialist": group(m.group).supplier,
        }
        for m in MATERIALS
    }


def _history(today: date) -> list[list[object]]:
    rows: list[list[object]] = []
    number = 4500002000
    for i, m in enumerate(MATERIALS):
        specialist = group(m.group)
        for k, days in enumerate((95, 63, 31)):
            number += 1
            price = m.price * (Decimal("0.97") + Decimal(k) * Decimal("0.015"))
            rows.append(
                [str(number), (today - timedelta(days=days + i)).isoformat(), PLANT, "0001",
                 specialist.code, m.code, max(m.need // 3, 1), float(round(price, 2)),
                 float(round(price * max(m.need // 3, 1), 2)), "BRL", "WE", "Recebido"]
            )  # fmt: skip
        number += 1
        price = m.price * Decimal("1.06")
        rows.append(
            [str(number), (today - timedelta(days=48 + i)).isoformat(), PLANT, "0001",
             DISTRIBUTOR.code, m.code, max(m.need // 6, 1), float(round(price, 2)),
             float(round(price * max(m.need // 6, 1), 2)), "BRL", "WE", "Recebido"]
        )  # fmt: skip
    return rows


def needs_export(today: date, *, with_mrp: bool = True) -> bytes:
    """The workbook (with or without its MRP sheet)."""
    suppliers = (*GROUPS, DISTRIBUTOR)
    sheets: list[tuple[str, list[object], list[list[object]]]] = [
        ("MMBE_Estoque",
         ["Material", "Descrição do material", "Unidade de medida", "Unidade de preço",
          "Centro", "Depósito", "Estoque total", "Estoque bloqueado", "Estoque disponível",
          "Quantidade reservada", "Quantidade em pedido", "Estoque de segurança",
          "Preço médio móvel"],
         [[m.code, m.description, "UN", m.price_unit, PLANT, "0001", m.free + m.blocked,
           m.blocked, m.free, m.reserved, m.on_order, m.safety, float(m.price)]
          for m in MATERIALS]),
        ("Historico_Compras",
         ["Pedido", "Data do pedido", "Centro", "Depósito", "Fornecedor", "Material",
          "Quantidade pedida", "Preço líquido", "Valor líquido", "Moeda",
          "Tipo de movimento", "Status"],
         _history(today)),
        ("Fornecedores",
         ["Fornecedor", "Nome do fornecedor", "Grupo de mercadoria", "País", "UF", "Cidade",
          "E-mail"],
         [[s.code, s.supplier, s.name, "BR", "SP", "São Paulo", s.email] for s in suppliers]),
        ("Avaliacao_Fornecedores",
         ["Fornecedor", "Nome do fornecedor", "OTIF %", "Qualidade %", "Avaliação média",
          "Lead time médio (dias)", "Grupo de mercadoria", "UF"],
         [[s.code, s.supplier, s.otif, s.quality, float(s.rating), s.lead_time, s.name, "SP"]
          for s in suppliers]),
    ]  # fmt: skip
    if with_mrp:
        sheets.append(
            (
                "MRP_Necessidades",
                ["Requisição", "Data início", "Data necessidade", "Material", "Centro",
                 "Quantidade necessária", "Unidade", "Elemento PEP / Projeto",
                 "Tipo de necessidade"],
                [[f"{(i + 1) * 10:06d}", (today + timedelta(days=m.need_days - 14)).isoformat(),
                  (today + timedelta(days=m.need_days)).isoformat(), m.code, PLANT, m.need,
                  m.price_unit, "Projeto Linha Beta", "Produção"]
                 for i, m in enumerate(MATERIALS)],
            )
        )  # fmt: skip
    sheets.append(
        (
            "Visao_Consolidada",
            ["Material", "Descrição do material", "Unidade de medida", "Centro", "Depósito",
             "Estoque disponível", "Quantidade em pedido", "Estoque de segurança",
             "Quantidade necessária", "Estoque projetado após necessidade",
             "Necessidade de compra", "Preço médio móvel", "Valor estimado da compra"],
            [[m.code, m.description, "UN", PLANT, "0001", m.free, m.on_order, m.safety, m.need,
              m.free + m.on_order - m.need, m.to_buy, float(m.price),
              float(m.price * m.to_buy)]
             for m in MATERIALS],
        )
    )  # fmt: skip

    from openpyxl import Workbook  # pyright: ignore[reportMissingTypeStubs]

    workbook = cast(Any, Workbook())
    workbook.remove(workbook.active)
    for title, header, rows in sheets:
        sheet = workbook.create_sheet(title)
        sheet.append(header)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


EXPECTED_SECTIONS: Final = {
    ("MMBE_Estoque", "inventory"),
    ("Historico_Compras", "purchase_history"),
    ("Fornecedores", "supplier_contacts"),
    ("Avaliacao_Fornecedores", "supplier_evaluation"),
    ("MRP_Necessidades", "requirements"),
    ("Visao_Consolidada", "inventory"),
}


# --- Scenarios ------------------------------------------------------------------------------


def _mapper(env: Env) -> SheetMapper | None:
    # Every sheet reads by its headers; mock mode never needs the model.
    return None if env.mode == "mock" else default_sheet_mapper(env.runner_config)


async def _upload(env: Env, principal: Principal, data: bytes) -> UUID:
    view = await store_upload(
        env.engine, env.storage, principal, filename=FILENAME, content_type=XLSX, data=data
    )
    return view.id


async def _rfq_groups(env: Env, principal: Principal) -> list[tuple[set[str], set[str]]]:
    """Per RFQ: its part numbers and its invited suppliers."""
    out: list[tuple[set[str], set[str]]] = []
    async with tenant_session(env.engine, principal.tenant_id) as session:
        rfqs = list(await session.scalars(select(Rfq).order_by(Rfq.created_at)))
        for rfq in rfqs:
            parts = {
                p
                for p in await session.scalars(
                    select(RfqLine.part_number).where(RfqLine.rfq_id == rfq.id)
                )
                if p
            }
            invited = set(
                await session.scalars(
                    select(Supplier.name)
                    .join(Invite, Invite.supplier_id == Supplier.id)
                    .where(Invite.rfq_id == rfq.id)
                )
            )
            out.append((parts, invited))
    return out


def _check_rfqs(result: RunResult, rfqs: Sequence[tuple[set[str], set[str]]]) -> None:
    want = golden()
    to_buy = {code for code, g in want.items() if g["to_buy"] > 0}
    got_parts: set[str] = set()
    for parts, _ in rfqs:
        got_parts |= parts
    result.check(
        "rfq_parts",
        got_parts == to_buy,
        f"missing={sorted(to_buy - got_parts)} extra={sorted(got_parts - to_buy)}",
    )
    wrong: list[str] = []
    for parts, invited in rfqs:
        groups = {want[p]["group"] for p in parts if p in want}
        if len(groups) != 1:
            wrong.append(f"mixed groups {sorted(groups)}")
            continue
        specialist = group(next(iter(groups))).supplier
        if specialist not in invited:
            wrong.append(f"{specialist} not invited for {sorted(parts)}")
    groups_to_buy = {want[p]["group"] for p in to_buy}
    result.check(
        "rfqs_per_group",
        not wrong and len(rfqs) == len(groups_to_buy),
        "; ".join(wrong[:3]) or f"{len(rfqs)} RFQs for {len(groups_to_buy)} groups",
    )


def _compare(result: RunResult, lines: dict[str, tuple[Decimal, str, list[str]]]) -> None:
    """lines: part -> (to_buy, unit, flag codes)."""
    want = golden()
    qty_wrong = [
        f"{code} {lines[code][0] if code in lines else 'missing'} vs {g['to_buy']}"
        for code, g in want.items()
        if code not in lines or lines[code][0] != g["to_buy"]
    ]
    result.check("to_buy_matches_golden", not qty_wrong, "; ".join(qty_wrong[:4]))
    unit_wrong = [
        code
        for code, (_, unit, flags) in lines.items()
        if code in want and (unit.upper() != want[code]["unit"] or "unit_mismatch" in flags)
    ]
    result.check("units", not unit_wrong, f"wrong unit or mismatch on {unit_wrong[:4]}")
    assumed = sorted(code for code, (_, _, flags) in lines.items() if "unit_assumed" in flags)
    expected = sorted(c for c, g in want.items() if g["unit"] in ("KG", "M"))
    result.check("unit_assumed_flags", assumed == expected, f"{assumed} vs {expected}")


async def scenario_needs_import(env: Env, result: RunResult) -> None:
    principal = await new_company(env, "needs_import")
    upload_id = await _upload(env, principal, needs_export(env.today))
    report = await import_workbook(
        env.engine, env.storage, principal, upload_id, mapper=_mapper(env), confirmed=True
    )
    result.check("imported", report.status == "imported", f"status={report.status}")
    found = {(s.sheet, str(s.type)) for s in [*report.sections, *report.extra_sections]}
    result.check(
        "sections",
        found == EXPECTED_SECTIONS,
        f"missing={sorted(EXPECTED_SECTIONS - found)} extra={sorted(found - EXPECTED_SECTIONS)}",
    )
    view = await purchasing.create_requirements_analysis(
        env.engine, principal, RequirementsAnalysisRequest(), today=env.today
    )
    result.check("source", view.source == "requirements", view.source)
    _compare(
        result,
        {
            line.part_number: (line.to_buy, line.unit, [f.code for f in line.flags])
            for line in view.lines
        },
    )
    want = golden()
    not_suggested = [
        line.part_number
        for line in view.lines
        if line.part_number in want
        and want[line.part_number]["specialist"]
        not in {x.name for x in line.suppliers if x.suggested}
    ]
    result.check("specialist_suggested", not not_suggested, f"{not_suggested[:4]}")
    await purchasing.create_rfqs_from_analysis(
        env.engine, principal, view.id, RfqFromAnalysisRequest()
    )
    _check_rfqs(result, await _rfq_groups(env, principal))
    again = await purchasing.list_requirements(env.engine, principal)
    still_open = sorted(r.part_number for r in again if want[r.part_number]["to_buy"] > 0)
    result.check("requirements_marked_quoted", not still_open, f"still open: {still_open[:4]}")


def _last_output(turns: Sequence[Turn], tool: str) -> object:
    for turn in reversed(turns):
        if tool in turn.outputs:
            return turn.outputs[tool]
    return None


def _tool_lines(output: object) -> dict[str, tuple[Decimal, str, list[str]]]:
    if not isinstance(output, dict):
        return {}
    lines: dict[str, tuple[Decimal, str, list[str]]] = {}
    for raw in cast(dict[str, Any], output).get("lines", []):
        line = cast(dict[str, Any], raw)
        flags = [str(cast(dict[str, Any], f).get("code")) for f in line.get("flags", [])]
        lines[str(line["part_number"])] = (
            Decimal(str(line["to_buy"])),
            str(line.get("unit") or ""),
            flags,
        )
    return lines


async def scenario_needs_copilot(env: Env, result: RunResult) -> None:
    if env.mode == "mock":
        result.skipped = "live only (the copilot's choices are the point)"
        return
    principal = await new_company(env, "needs_copilot")
    upload_id = await _upload(env, principal, needs_export(env.today))
    turns: list[Turn] = []
    conversation, turn = await copilot_turn(
        env,
        principal,
        None,
        "Segue o export do nosso ERP com estoque, compras, fornecedores e o MRP. "
        "O que eu preciso comprar?",
        [upload_id],
    )
    turns.append(turn)
    if not called(turns, "analyze_requirements"):
        conversation, turn = await copilot_turn(
            env, principal, conversation, "Sim, pode importar e me diz o que comprar.", []
        )
        turns.append(turn)
    result.check(
        "import_called", called(turns, "import_spreadsheet"), "import_spreadsheet not called"
    )
    result.check(
        "analyze_requirements_called",
        called(turns, "analyze_requirements"),
        f"tools: {[t.tools for t in turns]}",
    )
    result.check(
        "no_bom_analysis",
        not called(turns, "run_purchase_analysis"),
        "ran a BOM analysis although the company has requirements",
    )
    asked_bom = any(
        "bom" in t.answer.lower() or "lista de materiais" in t.answer.lower() for t in turns
    )
    result.check("did_not_ask_for_bom", not asked_bom, "asked for a BOM")
    analysis = _last_output(turns, "analyze_requirements")
    _compare(result, _tool_lines(analysis))
    answer = " ".join(t.answer for t in turns).lower()
    top = sorted(MATERIALS, key=lambda m: m.price * m.to_buy, reverse=True)[:3]
    mentioned = [m.code for m in top if m.code in answer]
    result.check("mentions_top_items", len(mentioned) >= 2, f"mentioned {mentioned}")
    conversation, turn = await copilot_turn(env, principal, conversation, "Pode criar as RFQs.", [])
    turns.append(turn)
    result.check(
        "rfq_tool_called",
        called(turns, "create_rfq_from_analysis"),
        f"tools: {turn.tools}",
    )
    _check_rfqs(result, await _rfq_groups(env, principal))
    result.notes["answers"] = [t.answer[:600] for t in turns]
    result.notes["tools"] = [t.tools for t in turns]


async def scenario_needs_typed(env: Env, result: RunResult) -> None:
    if env.mode == "mock":
        result.skipped = "live only (the copilot's choices are the point)"
        return
    principal = await new_company(env, "needs_typed")
    upload_id = await _upload(env, principal, needs_export(env.today, with_mrp=False))
    report = await import_workbook(
        env.engine, env.storage, principal, upload_id, mapper=_mapper(env), confirmed=True
    )
    result.check("imported", report.status == "imported", f"status={report.status}")
    material = MATERIALS[0]
    need_by = env.today + timedelta(days=material.need_days)
    message = (
        f"Preciso de {material.need:,} kg do {material.code} até "
        f"{need_by.strftime('%d/%m/%Y')}. O que tenho que comprar?"
    ).replace(",", ".")
    conversation, turn = await copilot_turn(env, principal, None, message, [])
    turns = [turn]
    if not called(turns, "analyze_requirements"):
        conversation, turn = await copilot_turn(
            env, principal, conversation, "Sim, pode calcular.", []
        )
        turns.append(turn)
    result.check("add_requirements_called", called(turns, "add_requirements"), f"{turn.tools}")
    result.check(
        "analyze_requirements_called",
        called(turns, "analyze_requirements"),
        f"tools: {[t.tools for t in turns]}",
    )
    added = _last_output(turns, "add_requirements")
    reqs: list[dict[str, Any]] = (
        [cast(dict[str, Any], r) for r in cast(dict[str, Any], added).get("requirements", [])]
        if isinstance(added, dict)
        else []
    )
    exact = [
        r
        for r in reqs
        if str(r.get("part_number")) == material.code
        and Decimal(str(r.get("quantity"))) == material.need
        and str(r.get("need_by")) == need_by.isoformat()
    ]
    result.check("requirement_as_said", len(exact) == 1, f"saved: {reqs}")
    analysis = _last_output(turns, "analyze_requirements")
    lines = _tool_lines(analysis)
    got = lines.get(material.code)
    result.check(
        "to_buy_matches_golden",
        got is not None and got[0] == material.to_buy,
        f"{got[0] if got else None} vs {material.to_buy}",
    )
    result.notes["answers"] = [t.answer[:600] for t in turns]
    result.notes["tools"] = [t.tools for t in turns]


NEEDS_CASES: Final = ("needs_import", "needs_copilot", "needs_typed")


if __name__ == "__main__":
    out = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path(FILENAME)
    out.write_bytes(needs_export(date.today()))
    print(f"{out}: {len(MATERIALS)} materials, {len(GROUPS) + 1} suppliers")
