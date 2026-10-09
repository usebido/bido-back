"""Does source understanding work with any spreadsheet? One scenario, many layouts, exact answers.

    uv run python -m evals.source_corpus --mock
    uv run python -m evals.source_corpus --live --repeats 3
    uv run python -m evals.source_corpus --live --variant csv_bundle --variant german
    uv run python -m evals.source_corpus --mock --write-dir /tmp/corpus  # files to upload

Every variant writes the same business scenario (`evals.source_scenario`) as a
differently shaped export (`evals.source_variants`): Portuguese SAP-like with
scattered tables, tidy English, one stacked sheet, latin-1 CSVs, a stock date
only in a title, inline receipts, German/Italian/Spanish vocabularies,
mismatched identifiers, formulas with cached results, a prompt injection and a
time-phased MRP grid. Each run goes through `analyze_documents` (readers,
mapping, deterministic planning) and is graded on exact quantities per
in-scope part, no out-of-scope parts, the last received price and supplier per
part, the assessed/needs_review status, and the critical false-ready flag (a
line marked ready with a wrong quantity). pass^k means all k repeats passed.

`--mock` injects the correct mapping of every table (no network): it checks the
harness and the deterministic code, and shows what readers and planning do with
a perfect mapping. `--live` uses the production mapper (`default_mapper`) and
needs OPENAI_API_KEY; it sends only these generated, synthetic files and needs
no database or mailbox. Failing runs are diagnosed: blocking rules with their
evidence, per-part demand/stock/inbound against the expected values, and the
mapper's proposals against the correct sections (missing, mislabelled or
misassigned columns).
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.purchasing.sources.contracts import (
    AssessmentLine,
    EvidenceTable,
    Issue,
    MappingProposal,
    MappingTask,
    ScopeRequest,
    SourceAssessment,
    TableProposal,
)
from bido_back.purchasing.sources.mapping import Mapper, default_mapper
from bido_back.purchasing.sources.readers import read_source
from bido_back.purchasing.sources.service import analyze_documents
from evals.source_scenario import AS_OF, CURRENCY, HORIZON_END, PRODUCT, expected, scenario
from evals.source_variants import VARIANT, VARIANTS, Corpus, Variant, render

# exact: assessed and right; review: needs_review, nothing wrong marked ready;
# inexact: assessed but something wrong or missing; error: the run raised or timed out.
Outcome = Literal["exact", "review", "inexact", "false_ready", "error"]

# Fields without which the deterministic code cannot use a record of that kind.
CRITICAL: Final[dict[str, frozenset[str]]] = {
    kind: frozenset(fields.split())
    for kind, fields in {
        "inventory": "part_number location as_of unit",
        "purchase": "part_number po_number po_line location status ordered_at quantity unit "
        "unit_price currency supplier_code promised_at",
        "receipt": "po_number po_line location received_at quantity unit movement receipt_id",
        "delivery_schedule": "po_number po_line location part_number quantity unit expected_at "
        "status",
        "production_plan": "reference product_code quantity unit location need_by status",
        "bom": "product_code part_number quantity_per unit valid_from",
        "item": "part_number unit",
        "supplier": "supplier_code",
        "supplier_item": "part_number supplier_code valid_from",
        "material_requirement": "part_number quantity location need_by",
    }.items()
}


def build_scope(variant: Variant) -> ScopeRequest:
    """The buyer's planning boundaries as a buyer states them: the one place they are built.

    Only the buyer's words (product, plant, a horizon in days, no safety stock); code
    resolves the exact code and the snapshot date from the evidence, as the copilot does.
    """
    return ScopeRequest(
        product=f"Motor {PRODUCT}",
        location=variant.locale.plants["A"],
        horizon_days=(HORIZON_END - AS_OF).days,
        safety_stock=Decimal(0),
    )


def scripted_mapper(corpus: Corpus) -> Mapper:
    """The correct reading of every page with today's catalog (mock mode)."""

    async def call(task: MappingTask, _: ToolContext) -> MappingProposal:
        key = (task.source_id, task.table)
        sections: list[TableProposal] = []
        for section in corpus.sections.get(key, []):
            if section.last_row < task.first_row or section.first_row > task.last_row:
                continue
            allowed = set(task.catalog.get(section.kind, []))
            sections.append(
                section.model_copy(
                    update={
                        "first_row": max(section.first_row, task.first_row),
                        "last_row": min(section.last_row, task.last_row),
                        "columns": [c for c in section.columns if c.field in allowed],
                    }
                )
            )
        issues: list[Issue] = []
        unsupported = corpus.unsupported.get(key)
        if unsupported is not None and task.first_row <= unsupported.header_row <= task.last_row:
            issues.append(
                Issue(code="unsupported_layout", severity="blocker", message=unsupported.reason)
            )
        return MappingProposal(sections=sections, issues=issues)

    return call


@dataclass(frozen=True, slots=True)
class MapperCall:
    source_id: str
    table: str
    first_row: int
    last_row: int
    proposal: MappingProposal | None  # None: the mapper raised


class Recorder:
    """Keeps every page's proposal so a failure can be traced back to the mapping."""

    def __init__(self) -> None:
        self.calls: list[MapperCall] = []

    def wrap(self, mapper: Mapper) -> Mapper:
        async def call(task: MappingTask, context: ToolContext) -> MappingProposal:
            page = (task.source_id, task.table, task.first_row, task.last_row)
            try:
                proposal = await mapper(task, context)
            except Exception:
                self.calls.append(MapperCall(*page, None))
                raise
            self.calls.append(MapperCall(*page, proposal))
            return proposal

        return call


# --- Usage ---------------------------------------------------------------------------------


@dataclass(slots=True)
class Usage:
    calls: int = 0
    failed: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    model_seconds: float = 0.0

    def add(self, other: "Usage") -> None:
        self.calls += other.calls
        self.failed += other.failed
        self.input_tokens += other.input_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.output_tokens += other.output_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.cost_usd += other.cost_usd
        self.model_seconds += other.model_seconds


def _number_attr(record: logging.LogRecord, name: str) -> float:
    value: object = getattr(record, name, None)
    return float(value) if isinstance(value, int | float) else 0.0


class UsageLog(logging.Handler):
    """Sums the runner's `agent run` lines (counts and ids only) per tenant id."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.by_tenant: dict[str, Usage] = defaultdict(Usage)

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() != "agent run":
            return
        tenant: object = getattr(record, "tenant_id", "")
        outcome: object = getattr(record, "outcome", "")
        usage = self.by_tenant[str(tenant)]
        usage.calls += 1
        usage.failed += outcome != "completed"
        usage.input_tokens += int(_number_attr(record, "input_tokens"))
        usage.cached_input_tokens += int(_number_attr(record, "cached_input_tokens"))
        usage.output_tokens += int(_number_attr(record, "output_tokens"))
        usage.reasoning_tokens += int(_number_attr(record, "reasoning_tokens"))
        usage.cost_usd += _number_attr(record, "cost_usd")
        usage.model_seconds += _number_attr(record, "duration_ms") / 1000


@contextmanager
def usage_log() -> Generator[UsageLog]:
    logger = logging.getLogger("bido_back.agents.runner")
    handler, level = UsageLog(), logger.level
    logger.addHandler(handler)
    if logger.getEffectiveLevel() > logging.INFO:
        logger.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


# --- Grading -------------------------------------------------------------------------------


class _PriceView(BaseModel):
    model_config = ConfigDict(extra="ignore")
    last_price: Decimal | None = None
    last_supplier: str | None = None
    last_ordered_at: date | None = None


class _LineView(BaseModel):
    """Reads the price benchmark when the contract has one (workstream B)."""

    model_config = ConfigDict(extra="ignore")
    price: _PriceView | None = None


def last_received(line: AssessmentLine) -> tuple[Decimal | None, set[str], date | None]:
    """The line's most recent completed purchase: price, supplier identities, order date.

    The price benchmark when present; otherwise the newest received-order supplier
    price. A tie between suppliers on the newest date is no answer.
    """
    view = _LineView.model_validate(line.model_dump())
    if view.price is not None and view.price.last_price is not None:
        who = {view.price.last_supplier} if view.price.last_supplier else set[str]()
        return view.price.last_price, who, view.price.last_ordered_at
    received = [
        s
        for s in line.suppliers
        if s.last_price_basis == "received_order" and s.last_price is not None and s.ordered_at
    ]
    newest = max((s.ordered_at for s in received if s.ordered_at), default=None)
    top = [s for s in received if s.ordered_at == newest]
    if len(top) != 1:
        return None, set(), newest
    who = {v for v in (top[0].supplier_code, top[0].name, top[0].email) if v}
    return top[0].last_price, who, newest


def _key(code: str) -> str:
    """Identity of a part code regardless of padding or leading zeros."""
    stripped = code.strip()
    return stripped.lstrip("0") or stripped


@dataclass(frozen=True, slots=True)
class PartResult:
    part: str
    in_scope: bool
    ready: bool
    to_buy: Decimal | None
    expected_to_buy: Decimal | None
    demand: Decimal | None
    available: Decimal | None
    on_order: Decimal | None
    last_price: Decimal | None
    last_supplier: list[str]
    price_ok: bool
    issues: list[str]


@dataclass(frozen=True, slots=True)
class RunResult:
    variant: str
    repeat: int
    live: bool
    passed: bool
    outcome: Outcome
    false_ready: bool
    # Assessed although an in-scope part has no line at all.
    false_complete: bool
    status: str
    checks: dict[str, bool]
    failures: list[str]
    parts: list[PartResult]
    blockers: list[str]
    warnings: dict[str, int]
    record_counts: dict[str, int]
    coverage: list[str]
    mapping: list[str]
    infra_error: bool
    seconds: float
    usage: Usage | None = None


def _refs(issues: Sequence[Issue]) -> str:
    refs = sorted({(r.table, r.row) for i in issues for r in i.evidence})
    shown = ", ".join(f"{table} r{row}" for table, row in refs[:4])
    return f" ({shown}{', ...' if len(refs) > 4 else ''})" if refs else ""


def _blockers(assessment: SourceAssessment) -> list[str]:
    """Blocking rules by code, with the parts they hold back and the cells they cite."""
    grouped: dict[str, list[Issue]] = defaultdict(list)
    parts: dict[str, set[str]] = defaultdict(set)
    for issue in assessment.issues:
        if issue.severity == "blocker":
            grouped[issue.code].append(issue)
    for line in assessment.lines:
        for issue in line.issues:
            if issue.severity == "blocker":
                grouped[issue.code].append(issue)
                parts[issue.code].add(line.part_number)
    found: list[str] = []
    for code, issues in sorted(grouped.items()):
        held = f" on {' '.join(sorted(parts[code]))}" if parts[code] else ""
        count = f" x{len(issues)}" if len(issues) > 1 and not held else ""
        found.append(f"{code}{held}{count}{_refs(issues)}")
    return found


def _overlap(a: TableProposal, b: TableProposal) -> bool:
    return a.first_row <= b.last_row and b.first_row <= a.last_row


def diagnose_mapping(
    corpus: Corpus, calls: Sequence[MapperCall], tables: dict[tuple[str, str], EvidenceTable]
) -> list[str]:
    """Compare the mapper's sections with the correct reading of every table."""
    notes: list[str] = []
    proposed: dict[tuple[str, str], list[TableProposal]] = defaultdict(list)
    records: Counter[tuple[str, str]] = Counter()
    mapper_issues: Counter[tuple[str, str]] = Counter()
    for call in calls:
        where = f"{call.table} r{call.first_row}-{call.last_row}"
        if call.proposal is None:
            notes.append(f"{where}: the mapper raised")
            continue
        if any(i.code == "mapping_failed" for i in call.proposal.issues):
            notes.append(f"{where}: mapping_failed (the model run failed)")
        proposed[call.source_id, call.table].extend(call.proposal.sections)
        for record in call.proposal.records:
            records[call.table, record.kind] += 1
        for issue in call.proposal.issues:
            if issue.code != "mapping_failed":
                mapper_issues[issue.severity, issue.code] += 1

    def label(key: tuple[str, str], header: int, column: int) -> str:
        table = tables.get(key)
        if table is None or header > len(table.rows) or column > len(table.rows[header - 1]):
            return f"col {column}"
        return f"col {column} '{table.rows[header - 1][column - 1] or ''}'"

    for key in sorted(set(corpus.sections) | set(proposed)):
        name = key[1]
        wanted = corpus.sections.get(key, [])
        got = proposed.get(key, [])
        for want in wanted:
            where = f"{name} r{want.first_row}-{want.last_row} {want.kind}"
            same = [p for p in got if p.kind == want.kind and _overlap(p, want)]
            if not same:
                others = sorted({p.kind for p in got if _overlap(p, want)})
                notes.append(
                    f"{where}: not mapped" + (f" (read as {', '.join(others)})" if others else "")
                )
                continue
            rows = {
                row
                for p in same
                for row in range(
                    max(p.first_row, want.first_row), min(p.last_row, want.last_row) + 1
                )
            }
            size = want.last_row - want.first_row + 1
            if len(rows) < size:
                notes.append(f"{where}: {size - len(rows)} of {size} rows not mapped")
            columns = {c.field: c.column for c in want.columns}
            problems: set[str] = set()
            for p in same:
                have = {c.field: c.column for c in p.columns}
                for fld, col in columns.items():
                    if fld not in have and fld in CRITICAL.get(want.kind, frozenset()):
                        problems.add(f"{fld} missing")
                    elif fld in have and have[fld] != col:
                        problems.add(
                            f"{fld} -> {label(key, p.header_row, have[fld])} "
                            f"(expected {label(key, want.header_row, col)})"
                        )
                for fld, col in have.items():
                    if fld not in columns:
                        problems.add(f"extra {fld} -> {label(key, p.header_row, col)}")
            notes.extend(f"{where}: {problem}" for problem in sorted(problems))
        for p in got:
            if not any(w.kind == p.kind and _overlap(p, w) for w in wanted):
                notes.append(f"{name} r{p.first_row}-{p.last_row}: unexpected {p.kind} section")
    notes.extend(f"{table}: {n} text record(s) as {kind}" for (table, kind), n in records.items())
    notes.extend(
        f"mapper issue {severity} {code} x{n}"
        for (severity, code), n in sorted(mapper_issues.items())
    )
    return notes


def grade(
    variant: Variant,
    corpus: Corpus,
    assessment: SourceAssessment,
    calls: Sequence[MapperCall],
    *,
    repeat: int = 1,
    live: bool = False,
    seconds: float = 0.0,
    usage: Usage | None = None,
) -> RunResult:
    truth, s = expected(), scenario()
    scope = {_key(part): part for part in truth.to_buy}
    lines: dict[str, list[AssessmentLine]] = defaultdict(list)
    for line in assessment.lines:
        lines[_key(line.part_number)].append(line)
    failures: list[str] = []
    parts: list[PartResult] = []
    quantities_ok = prices_ok = True
    for key, part in scope.items():
        found = lines.get(key, [])
        want = truth.to_buy[part]
        if len(found) != 1:
            quantities_ok = prices_ok = False
            failures.append(f"{part}: {len(found)} lines (expected 1)")
            continue
        line = found[0]
        price, who, ordered = last_received(line)
        last = truth.last[part]
        supplier = s.supplier(last.supplier)
        identities = {supplier.code, supplier.name, supplier.email}
        price_ok = price == last.price and bool(who & identities)
        if line.calculated_to_buy != want:
            quantities_ok = False
            failures.append(f"{part}: to buy {line.calculated_to_buy} (expected {want})")
        if not price_ok:
            prices_ok = False
            failures.append(
                f"{part}: last received {price} from {sorted(who) or None} on {ordered} "
                f"(expected {last.price} {CURRENCY} from {last.supplier} on {last.ordered_at})"
            )
        parts.append(
            PartResult(
                part=part,
                in_scope=True,
                ready=line.ready,
                to_buy=line.calculated_to_buy,
                expected_to_buy=want,
                demand=line.demand,
                available=line.available,
                on_order=line.on_order,
                last_price=price,
                last_supplier=sorted(who),
                price_ok=price_ok,
                issues=sorted({f"{i.severity}:{i.code}" for i in line.issues}),
            )
        )
    extra = sorted(key for key in lines if key not in scope)
    for key in extra:
        for line in lines[key]:
            failures.append(f"{line.part_number}: listed but out of scope")
            parts.append(
                PartResult(
                    part=line.part_number,
                    in_scope=False,
                    ready=line.ready,
                    to_buy=line.calculated_to_buy,
                    expected_to_buy=None,
                    demand=line.demand,
                    available=line.available,
                    on_order=line.on_order,
                    last_price=None,
                    last_supplier=[],
                    price_ok=False,
                    issues=sorted({f"{i.severity}:{i.code}" for i in line.issues}),
                )
            )
    false_ready = any(
        line.ready and (key not in scope or line.calculated_to_buy != truth.to_buy[scope[key]])
        for key, found in lines.items()
        for line in found
    )
    false_complete = assessment.status == "assessed" and any(k not in lines for k in scope)
    if assessment.status != "assessed":
        failures.insert(0, f"status {assessment.status}")
    checks = {
        "quantities": quantities_ok,
        "scope": not extra,
        "prices": prices_ok,
        "status": assessment.status == "assessed",
    }
    outcome: Outcome
    if false_ready:
        outcome = "false_ready"
    elif all(checks.values()):
        outcome = "exact"
    elif assessment.status == "needs_review":
        outcome = "review"
    else:
        outcome = "inexact"
    tables: dict[tuple[str, str], EvidenceTable] = {}
    for document in corpus.documents:
        for table in read_source(document)[0]:
            tables[document.source_id, table.name] = table
    all_issues = [*assessment.issues, *(i for line in assessment.lines for i in line.issues)]
    return RunResult(
        variant=variant.name,
        repeat=repeat,
        live=live,
        passed=outcome in variant.accepts,
        outcome=outcome,
        false_ready=false_ready,
        false_complete=false_complete,
        status=assessment.status,
        checks=checks,
        failures=failures,
        parts=parts,
        blockers=_blockers(assessment),
        warnings=dict(Counter(i.code for i in all_issues if i.severity == "warning")),
        record_counts=dict(sorted(assessment.record_counts.items())),
        coverage=[
            f"{c.table} {c.mapped_rows}/{c.rows}{'' if c.inspected else ' NOT inspected'}"
            for c in assessment.coverage
        ],
        mapping=diagnose_mapping(corpus, calls, tables),
        infra_error=any(i.code == "mapping_failed" for i in assessment.issues),
        seconds=round(seconds, 1),
        usage=usage,
    )


async def evaluate(
    variant: Variant | str,
    *,
    live: bool = False,
    repeat: int = 1,
    config: RunnerConfig | None = None,
    mapper: Mapper | None = None,
    usage: UsageLog | None = None,
    context: ToolContext | None = None,
) -> RunResult:
    """One run of one variant through `analyze_documents`, graded."""
    chosen = VARIANT[variant] if isinstance(variant, str) else variant
    corpus = render(chosen.name)
    recorder = Recorder()
    if mapper is None:
        mapper = default_mapper(config or RunnerConfig()) if live else scripted_mapper(corpus)
    # A fresh tenant per run keys this run's model usage in the runner's log lines.
    context = context or ToolContext(tenant_id=uuid4(), user_id=uuid4(), run_id=uuid4())
    started = time.monotonic()
    assessment = await analyze_documents(
        list(corpus.documents),
        chosen.locale.objective,
        context,
        recorder.wrap(mapper),
        scope=build_scope(chosen),
    )
    spent = usage.by_tenant.get(str(context.tenant_id)) if usage is not None else None
    return grade(
        chosen,
        corpus,
        assessment,
        recorder.calls,
        repeat=repeat,
        live=live,
        seconds=time.monotonic() - started,
        usage=spent,
    )


# --- Report --------------------------------------------------------------------------------


def write_corpus(directory: Path, variants: Sequence[Variant]) -> None:
    """The generated files of each variant, with what a correct assessment says."""
    truth = expected()
    for variant in variants:
        target = directory / variant.name
        target.mkdir(parents=True, exist_ok=True)
        for document in render(variant.name).documents:
            (target / document.filename).write_bytes(document.data)
        answer = {
            "variant": variant.name,
            "description": variant.description,
            "objective": variant.locale.objective,
            "scope": build_scope(variant).model_dump(mode="json"),
            "to_buy": {part: str(quantity) for part, quantity in truth.to_buy.items()},
            "last_received": {
                part: {
                    "price": str(last.price),
                    "currency": CURRENCY,
                    "supplier": last.supplier,
                    "ordered_at": last.ordered_at.isoformat(),
                    "po_number": last.po_number,
                }
                for part, last in truth.last.items()
            },
            "out_of_scope": sorted(truth.out_of_scope),
            "accepts": sorted(variant.accepts),
            "known_gap": variant.known_gap,
        }
        (target / "expected.json").write_text(
            json.dumps(answer, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )


def _thousands(value: int) -> str:
    return f"{value / 1000:.0f}k" if value >= 1000 else str(value)


@dataclass(frozen=True, slots=True)
class VariantSummary:
    variant: str
    runs: int
    passed: int
    pass_k: bool  # every repeat passed
    false_ready: int
    false_complete: int
    infra_errors: int
    outcomes: dict[str, int]
    mean_seconds: float
    usage: Usage
    accepts: list[str]
    known_gap: str | None
    # Reasons of the failed runs, with the number of failed runs that showed each.
    failure_reasons: dict[str, int]


@dataclass(frozen=True, slots=True)
class Summary:
    runs: int
    passed: int
    pass_at_1: float
    variants: int
    variants_pass_k: int
    false_ready_runs: int
    false_complete_runs: int
    infra_error_runs: int
    usage: Usage
    per_variant: list[VariantSummary]


def summarize(results: Sequence[RunResult], variants: Sequence[Variant]) -> Summary:
    by_variant: dict[str, list[RunResult]] = defaultdict(list)
    for result in results:
        by_variant[result.variant].append(result)
    rows: list[VariantSummary] = []
    total = Usage()
    for variant in variants:
        runs = by_variant[variant.name]
        spent = Usage()
        reasons: Counter[str] = Counter()
        for run in runs:
            if run.usage is not None:
                spent.add(run.usage)
            if not run.passed:
                reasons.update({*run.failures, *(f"blocker {b}" for b in run.blockers)})
                reasons.update({f"mapping: {note}" for note in run.mapping})
        total.add(spent)
        rows.append(
            VariantSummary(
                variant=variant.name,
                runs=len(runs),
                passed=sum(r.passed for r in runs),
                pass_k=bool(runs) and all(r.passed for r in runs),
                false_ready=sum(r.false_ready for r in runs),
                false_complete=sum(r.false_complete for r in runs),
                infra_errors=sum(r.infra_error for r in runs),
                outcomes=dict(sorted(Counter(r.outcome for r in runs).items())),
                mean_seconds=round(sum(r.seconds for r in runs) / max(len(runs), 1), 1),
                usage=spent,
                accepts=sorted(variant.accepts),
                known_gap=variant.known_gap,
                failure_reasons=dict(sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
            )
        )
    return Summary(
        runs=len(results),
        passed=sum(r.passed for r in results),
        pass_at_1=round(sum(r.passed for r in results) / max(len(results), 1), 3),
        variants=len(variants),
        variants_pass_k=sum(row.pass_k for row in rows),
        false_ready_runs=sum(r.false_ready for r in results),
        false_complete_runs=sum(r.false_complete for r in results),
        infra_error_runs=sum(r.infra_error for r in results),
        usage=total,
        per_variant=rows,
    )


def print_report(summary: Summary, *, mode: str, repeats: int, wall: float) -> None:
    print(
        f"Source corpus: {mode}, k={repeats}, {summary.variants} variants, "
        f"{summary.runs} runs, wall {wall:.0f} s\n"
    )
    print(
        f"{'variant':22} {'pass':>5} {'pass^k':>6} {'false-ready':>11}  {'outcomes':28} "
        f"{'mean s':>6} {'in tok':>7} {'out tok':>7} {'cost $':>7}"
    )
    for row in summary.per_variant:
        outcomes = " ".join(f"{k}x{v}" for k, v in row.outcomes.items())
        print(
            f"{row.variant:22} {row.passed:>2}/{row.runs:<2} {'yes' if row.pass_k else 'no':>6} "
            f"{row.false_ready:>11}  {outcomes:28} {row.mean_seconds:>6} "
            f"{_thousands(row.usage.input_tokens):>7} {_thousands(row.usage.output_tokens):>7} "
            f"{row.usage.cost_usd:>7.2f}"
        )
    print(
        f"\npass@1 {summary.pass_at_1}  pass^k {summary.variants_pass_k}/{summary.variants} "
        f"variants  false-ready runs {summary.false_ready_runs}  false-complete runs "
        f"{summary.false_complete_runs}  infra errors {summary.infra_error_runs}  model calls "
        f"{summary.usage.calls}  cost ${summary.usage.cost_usd:.2f}"
    )
    failing = [row for row in summary.per_variant if row.passed != row.runs]
    if failing:
        print("\nWhy runs failed ([failed runs showing it / failed runs] reason):")
    for row in failing:
        gap = f"  [known gap] {row.known_gap}" if row.known_gap else ""
        print(f"\n{row.variant}{gap}")
        for reason, count in list(row.failure_reasons.items())[:30]:
            print(f"  [{count}/{row.runs - row.passed}] {reason}")


def _errored(variant: Variant, repeat: int, live: bool, reason: str, seconds: float) -> RunResult:
    return RunResult(
        variant=variant.name,
        repeat=repeat,
        live=live,
        passed=False,
        outcome="error",
        false_ready=False,
        false_complete=False,
        status="error",
        checks={"quantities": False, "scope": False, "prices": False, "status": False},
        failures=[reason],
        parts=[],
        blockers=[],
        warnings={},
        record_counts={},
        coverage=[],
        mapping=[],
        infra_error=True,
        seconds=round(seconds, 1),
    )


async def run_corpus(
    variants: Sequence[Variant],
    *,
    live: bool,
    repeats: int,
    concurrency: int,
    config: RunnerConfig | None = None,
    run_timeout: float = 900.0,
) -> list[RunResult]:
    """Runs variants concurrently, bounded; each run keeps the mapper's own page concurrency.

    A run that raises or exceeds `run_timeout` seconds is an `error` outcome, not a crash.
    """
    semaphore = asyncio.Semaphore(concurrency)
    with usage_log() as log:

        async def one(variant: Variant, repeat: int) -> RunResult:
            async with semaphore:
                context = ToolContext(tenant_id=uuid4(), user_id=uuid4(), run_id=uuid4())
                started = time.monotonic()
                try:
                    result = await asyncio.wait_for(
                        evaluate(
                            variant,
                            live=live,
                            repeat=repeat,
                            config=config,
                            usage=log,
                            context=context,
                        ),
                        run_timeout,
                    )
                except TimeoutError:
                    reason = f"timed out after {run_timeout:.0f} s"
                    result = _errored(variant, repeat, live, reason, run_timeout)
                except Exception as exc:  # report the run, keep the batch going
                    result = _errored(
                        variant, repeat, live, f"raised {type(exc).__name__}",
                        time.monotonic() - started,
                    )  # fmt: skip
                if result.usage is None:
                    result = replace(result, usage=log.by_tenant.get(str(context.tenant_id)))
                print(
                    f"{variant.name} #{repeat}: {result.outcome} in {result.seconds} s",
                    file=sys.stderr,
                    flush=True,
                )
                return result

        return list(
            await asyncio.gather(*(one(v, r) for r in range(1, repeats + 1) for v in variants))
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--mock", action="store_true", help="inject the correct mapping")
    modes.add_argument("--live", action="store_true", help="the production mapper (OpenAI)")
    parser.add_argument("--repeats", type=int, choices=range(1, 11), default=1)
    parser.add_argument("--variant", action="append", choices=[v.name for v in VARIANTS])
    parser.add_argument("--write-dir", type=Path, help="write each variant's files there")
    parser.add_argument("--concurrency", type=int, default=3, help="variant runs at once")
    parser.add_argument("--json", type=Path, help="write the full report (every run) there")
    parser.add_argument("--timeout", type=float, default=900.0, help="seconds per run")
    args = parser.parse_args()
    live = bool(args.live)
    if live and not os.environ.get("OPENAI_API_KEY"):
        parser.error("--live needs OPENAI_API_KEY; only the generated synthetic files are sent")
    names: list[str] = list(args.variant or [v.name for v in VARIANTS])
    variants = [VARIANT[name] for name in names]
    if args.write_dir is not None:
        write_corpus(Path(args.write_dir), variants)
    repeats = int(args.repeats)
    started = time.monotonic()
    results = asyncio.run(
        run_corpus(
            variants,
            live=live,
            repeats=repeats,
            concurrency=max(1, int(args.concurrency)),
            run_timeout=float(args.timeout),
        )
    )
    summary = summarize(results, variants)
    mode = "live" if live else "mock"
    print_report(summary, mode=mode, repeats=repeats, wall=time.monotonic() - started)
    if args.json is not None:
        report = {
            "mode": mode,
            "repeats": repeats,
            "summary": asdict(summary),
            "runs": [asdict(r) for r in results],
        }
        Path(args.json).write_text(json.dumps(report, indent=2, default=str) + "\n")
    known = {v.name for v in variants if v.known_gap}
    healthy = all(r.passed for r in results if r.variant not in known)
    return 0 if healthy and not any(r.false_ready for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
