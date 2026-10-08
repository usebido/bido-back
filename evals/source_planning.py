"""Evaluate semantic mapping and deterministic planning on synthetic ERP evidence.

uv run python -m evals.source_planning --mock
uv run python -m evals.source_planning --live --repeats 3

The live mode needs OPENAI_API_KEY. It sends only generated fixtures, never uploads,
and uses no database, mailbox, importer, approval or purchase-order execution path.
"""

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from uuid import uuid4

from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.purchasing.sources.contracts import (
    ColumnMapping,
    EvidenceTable,
    MappingProposal,
    MappingTask,
    RecordKind,
    TableProposal,
)
from bido_back.purchasing.sources.mapping import default_mapper, map_sources
from bido_back.purchasing.sources.planning import assess_plan
from evals.source_fixtures import EXPECTED, SCOPE, motor_records

CASES = ("complete", "permuted", "conflicting_stock", "missing_bom")


@dataclass(frozen=True)
class EvalResult:
    case: str
    passed: bool
    false_ready: bool
    mapped_records: int
    inspected_tables: int
    issue_codes: list[str]


def layout(case: str) -> tuple[list[EvidenceTable], dict[str, list[TableProposal]]]:
    rows = motor_records()
    if case == "missing_bom":
        rows = [r for r in rows if r.kind != "bom"]
    elif case == "conflicting_stock":
        copy = next(r for r in rows if r.kind == "inventory").model_copy(deep=True)
        copy.values["total"] = "26"
        rows.append(copy)
    if case == "permuted":
        rows.reverse()
    kinds: list[RecordKind] = list(dict.fromkeys(r.kind for r in rows))
    grids: dict[str, list[list[str | None]]] = {}
    sections: dict[str, list[TableProposal]] = {}
    for index, kind in enumerate(kinds):
        name = f"ERP-{index % 3}"
        grid = grids.setdefault(name, [["ERP report"], [], ["As of", "2026-10-08"], []])
        matching = [r for r in rows if r.kind == kind]
        fields = list(dict.fromkeys(field for r in matching for field in r.values))
        if case == "permuted":
            fields.reverse()
        header = len(grid) + 1
        # Natural header labels; opaque sheet names and separate table blocks.
        labels = {
            "bom": "Bill of materials",
            "inventory": "Physical inventory snapshot",
            "receipt": "Physical goods receipts",
            "delivery_schedule": "Pending deliveries",
            "production_plan": "Production orders",
            "item": "Material master",
            "purchase": "Purchase order lines",
            "supplier": "Supplier register",
            "material_requirement": "Internal material requisitions",
        }
        grid.extend([[labels.get(kind, kind)], []])
        header += 2
        grid.append([field.replace("_", " ").title() for field in fields])
        grid.extend([[r.values.get(field) for field in fields] for r in matching])
        sections.setdefault(name, []).append(
            TableProposal(
                kind=kind,
                header_row=header,
                first_row=header + 1,
                last_row=len(grid),
                columns=[ColumnMapping(field=f, column=i) for i, f in enumerate(fields, 1)],
            )
        )
        grid.append([])
    return [
        EvidenceTable(source_id="synthetic-erp", name=name, rows=grid)
        for name, grid in grids.items()
    ], sections


async def evaluate(case: str, *, live: bool = False) -> EvalResult:
    tables, sections = layout(case)

    async def scripted(task: MappingTask, _: ToolContext) -> MappingProposal:
        return MappingProposal(
            sections=[
                s.model_copy(
                    update={
                        "first_row": max(s.first_row, task.first_row),
                        "last_row": min(s.last_row, task.last_row),
                    }
                )
                for s in sections[task.table]
                if s.last_row >= task.first_row and s.first_row <= task.last_row
            ]
        )

    mapper = default_mapper(RunnerConfig()) if live else scripted
    records, problems, coverage = await map_sources(
        tables,
        "Analyze MTR-100 in Plant A, 8 to 22 October 2026, safety stock zero. "
        "Do not contact suppliers. Preserve every planning, stock, order and receipt section.",
        ToolContext(tenant_id=uuid4(), user_id=uuid4(), run_id=uuid4()),
        mapper,
    )
    result = assess_plan(records, SCOPE)
    issues = [*problems, *result.issues, *(i for line in result.lines for i in line.issues)]
    quantities = {line.part_number: line.calculated_to_buy for line in result.lines}
    complete = all(row.inspected for row in coverage) and not any(
        i.severity == "blocker" for i in issues
    )
    if case in {"complete", "permuted"}:
        correct_history = all(
            len(line.suppliers) == 1
            and line.suppliers[0].last_price == 10.5
            and line.suppliers[0].last_price_basis == "received_order"
            for line in result.lines
        )
        passed = complete and quantities == EXPECTED and correct_history
        false_ready = any(
            line.ready and line.calculated_to_buy != EXPECTED.get(line.part_number)
            for line in result.lines
        )
    else:
        affected = [line for line in result.lines if line.part_number == "000101"]
        false_ready = (
            any(line.ready for line in affected)
            if case == "conflicting_stock"
            else any(line.ready for line in result.lines)
        )
        required_code = (
            "conflicting_inventory_snapshot"
            if case == "conflicting_stock"
            else "missing_effective_bom"
        )
        passed = not false_ready and any(i.code == required_code for i in issues)
    return EvalResult(
        case,
        passed,
        false_ready,
        len(records),
        sum(row.inspected for row in coverage),
        sorted({i.code for i in issues}),
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--mock", action="store_true")
    modes.add_argument("--live", action="store_true")
    parser.add_argument("--repeats", type=int, choices=range(1, 11), default=1)
    args = parser.parse_args()
    if args.live and not os.environ.get("OPENAI_API_KEY"):
        parser.error("Live evaluation requires OPENAI_API_KEY; only synthetic fixtures are sent")
    results = [
        await evaluate(case, live=bool(args.live)) for _ in range(args.repeats) for case in CASES
    ]
    print(
        json.dumps(
            {
                "mode": "live" if args.live else "mock",
                "cases": len(results),
                "passed": sum(r.passed for r in results),
                "false_ready": sum(r.false_ready for r in results),
                "failures": [
                    {"case": r.case, "issue_codes": r.issue_codes} for r in results if not r.passed
                ],
            },
            indent=2,
        )
    )
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
