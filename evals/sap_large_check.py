"""Bido's plan for the large synthetic SAP export, against the reference plan.

    uv run python -m evals.sap_large_check [materials] [seed] [--md16]

Prints the counts in the shape of the CEO's acceptance criteria and every mismatch.
No model is called: known SAP reports are read by code.
"""

import asyncio
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from uuid import uuid4

from bido_back.agents.context import ToolContext
from bido_back.purchasing.sources.contracts import (
    AssessmentLine,
    MappingProposal,
    MappingTask,
    SourceAssessment,
)
from bido_back.purchasing.sources.readers import SourceDocument
from bido_back.purchasing.sources.service import analyze_documents
from evals.sap_large import Expected, Scenario, generate, reports

TODAY = date(2026, 10, 9)


async def _no_model(task: MappingTask, context: ToolContext) -> MappingProposal:
    raise AssertionError(f"the mapper was called for {task.table}")


async def assess(scenario: Scenario, *, planned_orders: bool = False) -> SourceAssessment:
    documents = [
        SourceDocument(name, name, "application/vnd.ms-excel", data)
        for name, data in reports(scenario, planned_orders=planned_orders).items()
    ]
    return await analyze_documents(
        documents,
        "O que comprar, quanto e de quem, em todos os centros",
        ToolContext(tenant_id=uuid4(), user_id=uuid4(), run_id=uuid4()),
        _no_model,
        today=TODAY,
    )


@dataclass(frozen=True, slots=True)
class Got:
    status: str
    shortfall: Decimal | None
    quantity: Decimal | None
    outcome: str | None
    supplier: str | None


def observed(line: AssessmentLine) -> Got:
    quantity = line.calculated_to_buy
    timing = line.timing
    if line.sourcing is not None and (quantity or 0) > 0:
        status = "no_valid_source"
    elif quantity is None or quantity <= 0:
        status = "covered"
    else:
        status = "buy"
    supplier = next((s.supplier_code for s in line.suppliers if s.recommended), None)
    return Got(
        status=status,
        shortfall=line.net_requirement if line.net_requirement is not None else quantity,
        quantity=quantity,
        outcome=timing.outcome if timing is not None else None,
        supplier=supplier,
    )


@dataclass(slots=True)
class Report:
    counts: Counter[str] = field(default_factory=Counter[str])
    mismatches: list[str] = field(default_factory=list[str])


def compare(scenario: Scenario, result: SourceAssessment) -> Report:
    report = Report()
    by_key: dict[tuple[str, str], AssessmentLine] = {}
    for line in result.lines:
        key = (line.part_number, line.location or "")
        if key in by_key:
            report.mismatches.append(f"duplicate line {key}")
        by_key[key] = line
    blocked = {code for code, v in scenario.vendors.items() if v.blocked}
    for key, want in sorted(scenario.expected.items()):
        line = by_key.get(key)
        if want.status == "excluded_nd":
            report.counts[
                "nd_excluded" if line is None or not line.calculated_to_buy else "nd_planned"
            ] += 1
            if line is not None and line.calculated_to_buy:
                report.mismatches.append(f"{key}: ND material planned ({line.calculated_to_buy})")
            continue
        if line is None:
            got = Got("covered", None, None, None, None)
        else:
            got = observed(line)
        report.counts[f"want_{want.status}"] += 1
        report.counts[f"got_{got.status}"] += 1
        if got.supplier in blocked:
            report.counts["blocked_recommended"] += 1
        if line is not None and got.supplier is not None:
            chosen = next((s for s in line.suppliers if s.supplier_code == got.supplier), None)
            if chosen is not None and chosen.contract_expired:
                report.counts["expired_recommended"] += 1
        if line is not None and any(a.code == "open_orders_not_counted" for a in line.assumptions):
            report.counts["open_orders_not_netted"] += 1
        _check(key, want, got, report)
    return report


def _check(key: tuple[str, str], want: Expected, got: Got, report: Report) -> None:
    problems: list[str] = []
    if want.status != got.status:
        problems.append(f"status {got.status} != {want.status}")
    if want.status == "buy" and got.status == "buy":
        if got.quantity != want.quantity:
            problems.append(
                f"quantity {got.quantity} != {want.quantity} (shortfall {want.shortfall})"
            )
            report.counts["quantity_wrong"] += 1
        if got.supplier != want.supplier:
            problems.append(f"supplier {got.supplier} != {want.supplier}")
            report.counts["supplier_wrong"] += 1
        if got.outcome != want.outcome:
            problems.append(f"outcome {got.outcome} != {want.outcome}")
            report.counts["outcome_wrong"] += 1
        if want.outcome == "unavoidable_stockout":
            report.counts["want_unavoidable"] += 1
        if got.outcome == "unavoidable_stockout":
            report.counts["got_unavoidable"] += 1
    if problems:
        report.mismatches.append(f"{key}: " + "; ".join(problems))


async def main() -> int:
    materials = int(sys.argv[1]) if len(sys.argv) > 1 else 165
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    planned = "--md16" in sys.argv
    scenario = generate(materials, seed)
    started = time.perf_counter()
    result = await assess(scenario, planned_orders=planned)
    elapsed = time.perf_counter() - started
    report = compare(scenario, result)
    print(f"analysis {elapsed:.1f}s, {len(result.lines)} lines, status {result.status}")
    for name, value in sorted(report.counts.items()):
        print(f"  {name}: {value}")
    print(f"mismatches: {len(report.mismatches)}")
    for m in report.mismatches[:60]:
        print("  " + m)
    blockers = [i for i in result.issues if i.severity == "blocker"]
    for issue in [*blockers, *result.issues][:10]:
        print(f"issue {issue.severity} {issue.code}: {issue.message[:200]}")
    return 0 if not report.mismatches else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
