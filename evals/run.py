"""Run the evals and write a report.

    uv run python -m evals.run                    # live model, k=3
    uv run python -m evals.run --mock             # scripted model (harness check)
    uv run python -m evals.run -k 1 --scenario import --fixture motor_tidy_en
    uv run python -m evals.run --scenario import --mapper-model gpt-6.1-sol
    uv run python -m evals.run --scenario grounding --fixture unrecorded_terms_pt

Needs DATABASE_URL (and AGNO_DATABASE_URL for the copilot) pointing at a
migrated database; live mode needs OPENAI_API_KEY. Every run creates its own
company, so it is safe to re-run on the same database. Reports go to
evals/reports/<timestamp>/ (report.md, report.json, the generated fixtures).
"""

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final

from bido_back.agents.config import RunnerConfig
from bido_back.db import create_engine
from bido_back.settings import get_settings
from evals.golden import golden_analysis
from evals.harness import (
    GROUNDING_CASES,
    Env,
    MemoryStorage,
    Mode,
    RunResult,
    live_extractor,
    run_timed,
    scenario_analysis,
    scenario_copilot,
    scenario_extraction,
    scenario_grounding,
    scenario_import,
)
from evals.layouts import Fixture, fixtures
from evals.mock import mock_extractor
from evals.quotes import cases

SCENARIOS: Final = ("import", "analysis", "copilot", "grounding", "extraction")
# The copilot conversation is the expensive one: a representative subset.
COPILOT_FIXTURES: Final = (
    "motor_tidy_en",
    "battery_titles_it",
    "inverter_no_plan",
    "battery_no_bom",
    "erp_sap_motor",
    "erp_sap_motor_gaps",
    "erp_wide_export",
)
REPORTS: Final = Path(__file__).parent / "reports"


async def run_suite(
    env: Env,
    *,
    k: int,
    scenarios: Sequence[str] = SCENARIOS,
    only: Sequence[str] = (),
) -> list[RunResult]:
    selected = [f for f in fixtures(env.today) if not only or f.name in only]
    quote_cases = [c for c in cases(env.today) if not only or c.name in only]
    grounding_cases = [c for c in GROUNDING_CASES if not only or c.name in only]
    extractor = (
        mock_extractor(env.today) if env.mode == "mock" else live_extractor(env.runner_config)
    )
    results: list[RunResult] = []
    for run in range(1, k + 1):
        for fixture in selected:
            if "import" in scenarios:
                results.append(await _one(env, "import", fixture, run))
            if "analysis" in scenarios:
                results.append(await _one(env, "analysis", fixture, run))
            if "copilot" in scenarios and (only or fixture.name in COPILOT_FIXTURES):
                results.append(await _one(env, "copilot", fixture, run))
        if "grounding" in scenarios:
            for grounding in grounding_cases:
                result = RunResult("grounding", grounding.name, run)
                results.append(
                    await run_timed(
                        lambda c=grounding, r=result: scenario_grounding(env, c, r), result
                    )
                )
        if "extraction" in scenarios:
            for case in quote_cases:
                result = RunResult("extraction", case.name, run)
                if env.mode == "mock" and not case.body_only:
                    result.skipped = "live only (values sit in attachments)"
                    results.append(result)
                    continue
                results.append(
                    await run_timed(
                        lambda c=case, r=result: scenario_extraction(env, c, r, extractor), result
                    )
                )
    return results


async def _one(env: Env, scenario: str, fixture: Fixture, run: int) -> RunResult:
    result = RunResult(scenario, fixture.name, run)
    if scenario == "import":

        async def go() -> None:
            await scenario_import(env, fixture, result)
    elif scenario == "analysis":

        async def go() -> None:
            await scenario_analysis(env, fixture, result)
    else:

        async def go() -> None:
            await scenario_copilot(env, fixture, result)

    return await run_timed(go, result)


# --- Report -----------------------------------------------------------------------------


def summarize(results: Sequence[RunResult]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[RunResult]] = defaultdict(list)
    for r in results:
        groups[(r.scenario, r.case)].append(r)
    rows: list[dict[str, Any]] = []
    for (scenario, case), runs in groups.items():
        live = [r for r in runs if r.skipped is None]
        failures: dict[str, int] = defaultdict(int)
        details: dict[str, str] = {}
        for r in live:
            if r.error:
                failures["error"] += 1
                details.setdefault("error", r.error)
            for c in r.checks:
                if not c.ok:
                    failures[c.name] += 1
                    details.setdefault(c.name, c.detail)
        rows.append(
            {
                "scenario": scenario,
                "case": case,
                "runs": len(live),
                "passed": sum(r.ok for r in live),
                "pass_all": bool(live) and all(r.ok for r in live),
                "skipped": runs[0].skipped if not live else None,
                "avg_seconds": round(sum(r.seconds for r in live) / len(live), 1) if live else 0.0,
                "failures": dict(failures),
                "details": details,
            }
        )
    return rows


def markdown(rows: Sequence[dict[str, Any]], mode: Mode, k: int, today: date) -> str:
    out = [
        f"# Eval report ({mode}, k={k}, {today.isoformat()})",
        "",
        "pass^k = every one of the k runs passed every check.",
        "",
        "| Scenario | Case | pass^k | Runs passed | Avg s | Failing checks |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        if row["skipped"]:
            out.append(
                f"| {row['scenario']} | {row['case']} | skipped | — | — | {row['skipped']} |"
            )
            continue
        failing = ", ".join(f"{name} ×{n}" for name, n in row["failures"].items()) or "—"
        mark = "✅" if row["pass_all"] else "❌"
        out.append(
            f"| {row['scenario']} | {row['case']} | {mark} | {row['passed']}/{row['runs']} "
            f"| {row['avg_seconds']} | {failing} |"
        )
    out += ["", "## First failure detail per check", ""]
    for row in rows:
        for name, detail in row["details"].items():
            out.append(f"- **{row['scenario']} / {row['case']} / {name}**: {detail}")
    return "\n".join(out) + "\n"


def write_report(
    results: Sequence[RunResult], env: Env, k: int, folder: Path | None = None
) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = folder or REPORTS / f"{stamp}-{env.mode}"
    folder.mkdir(parents=True, exist_ok=True)
    rows = summarize(results)
    (folder / "report.md").write_text(markdown(rows, env.mode, k, env.today))
    (folder / "report.json").write_text(
        json.dumps(
            {
                "mode": env.mode,
                "k": k,
                "today": env.today.isoformat(),
                "summary": rows,
                "runs": [asdict(r) for r in results],
            },
            indent=1,
            default=str,
            ensure_ascii=False,
        )
    )
    fixtures_dir = folder / "fixtures"
    fixtures_dir.mkdir(exist_ok=True)
    for fixture in fixtures(env.today):
        (fixtures_dir / f"{fixture.name}.xlsx").write_bytes(fixture.data)
        golden = golden_analysis(fixture.dataset, env.today, fixture.quantity)
        (fixtures_dir / f"{fixture.name}.golden.json").write_text(
            json.dumps(
                {
                    "description": fixture.description,
                    "sections": fixture.sections,
                    "rows_ok": fixture.rows_ok,
                    "analysis": golden,
                },
                indent=1,
                ensure_ascii=False,
            )
        )
    return folder


async def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the agent evals on synthetic spreadsheets.")
    parser.add_argument("--mock", action="store_true", help="scripted model outputs")
    parser.add_argument("-k", type=int, default=None, help="runs per case (default 3 live, 1 mock)")
    parser.add_argument("--scenario", action="append", choices=SCENARIOS, help="repeatable")
    parser.add_argument("--fixture", action="append", default=[], help="fixture or case name")
    parser.add_argument("--mapper-model", help="model for the sheet mapper, to compare models")
    args = parser.parse_args(argv)
    if args.mapper_model:
        from bido_back.agents.catalog import definitions

        # The mapper is looked up at each run, so the import and the copilot both use it.
        setattr(definitions, "SHEET_MAPPER", definitions.sheet_mapper(args.mapper_model))  # noqa: B010
        print(f"sheet mapper model: {args.mapper_model}")

    mode: Mode = "mock" if args.mock else "live"
    settings = get_settings()
    if mode == "live" and not (settings.openai_api_key or os.environ.get("OPENAI_API_KEY")):
        print("live evals need OPENAI_API_KEY; use --mock to check the harness", file=sys.stderr)
        return 2
    k = args.k or (1 if args.mock else 3)
    engine = create_engine(settings)
    env = Env(
        engine=engine,
        storage=MemoryStorage(),
        runner_config=RunnerConfig.from_settings(settings),
        mode=mode,
        today=date.today(),
    )
    try:
        results = await run_suite(env, k=k, scenarios=args.scenario or SCENARIOS, only=args.fixture)
    finally:
        await engine.dispose()
    folder = write_report(results, env, k)
    print((folder / "report.md").read_text())
    print(f"report: {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
