"""The nightly eval run: one suite per call, its spend, and a summary of every report.

    uv run python -m evals.nightly run --out evals/reports/nightly --mock
    uv run python -m evals.nightly flow --out evals/reports/nightly -k 3
    uv run python -m evals.nightly sources --out evals/reports/nightly -k 3
    uv run python -m evals.nightly summary --out evals/reports/nightly >> "$GITHUB_STEP_SUMMARY"

`run`, `flow` and `sources` call `evals.run`, `evals.flow` and `evals.source_corpus`
unchanged, with their reports under `--out/<suite>/`, and add `usage.json`: the model
calls, tokens and estimated cost summed from the runner's `agent run` log lines (counts
and ids only, never content).

Exit codes are the failure policy. A suite that ran exits 0 even when cases fail
(report-only), unless `--fail-on-failures` is given; then it exits 1 when a case did not
pass. A harness crash exits 3, a missing OPENAI_API_KEY in live mode exits 2, so the
workflow turns red only when the run itself is broken.
"""

import argparse
import asyncio
import json
import logging
import sys
import time
import traceback
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final, cast

SUITES: Final = ("run", "flow", "sources")
CRASH: Final = 3


@dataclass(slots=True)
class Spend:
    model_calls: int = 0
    failed_runs: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    by_model: dict[str, float] = field(default_factory=lambda: defaultdict(float))


class SpendLog(logging.Handler):
    """Sums the runner's `agent run` lines. Each line carries that run's own usage, so
    summing them never counts a nested run twice (see `bido_back.agents.runner`)."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.spend = Spend()

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() != "agent run":
            return
        spend = self.spend
        spend.model_calls += int(_number(record, "model_calls"))
        spend.failed_runs += getattr(record, "outcome", "") == "failed"
        spend.input_tokens += int(_number(record, "input_tokens"))
        spend.cached_input_tokens += int(_number(record, "cached_input_tokens"))
        spend.output_tokens += int(_number(record, "output_tokens"))
        cost = _number(record, "cost_usd")
        spend.cost_usd += cost
        spend.by_model[str(getattr(record, "model", "?"))] += cost


def _number(record: logging.LogRecord, name: str) -> float:
    value: object = getattr(record, name, None)
    return float(value) if isinstance(value, int | float) else 0.0


def run_suite(suite: str, folder: Path, *, mock: bool, k: int | None, extra: list[str]) -> int:
    """Run one suite with its reports in `folder`; returns the suite's own exit code."""
    folder.mkdir(parents=True, exist_ok=True)
    mode = ["--mock"] if mock else []
    runs = ["-k", str(k)] if k else []
    if suite == "run":
        import evals.run as module

        module.REPORTS = folder
        return asyncio.run(module.main([*mode, *runs, *extra]))
    if suite == "flow":
        import evals.flow as module

        module.REPORTS = folder
        return asyncio.run(module.main([*mode, *runs, *extra]))
    import evals.source_corpus as corpus

    repeats = ["--repeats", str(k)] if k else []
    sys.argv = [
        "evals.source_corpus",
        *(mode or ["--live"]),
        *repeats,
        "--json",
        str(folder / "report.json"),
        *extra,
    ]
    return corpus.main()


def passed_all(suite: str, folder: Path) -> bool:
    """Whether every case of the suite's latest report passed (pass^k)."""
    for report in sorted(folder.glob("**/report.json"), reverse=True):
        data = json.loads(report.read_text())
        if suite == "sources":
            summary = data["summary"]
            return all(row["pass_k"] or row["known_gap"] for row in summary["per_variant"])
        return all(row["pass_all"] or row["skipped"] for row in data["summary"])
    return False


def run_with_spend(args: argparse.Namespace) -> int:
    folder = Path(args.out) / args.suite
    handler = SpendLog()
    logger = logging.getLogger("bido_back.agents.runner")
    logger.addHandler(handler)
    if logger.getEffectiveLevel() > logging.INFO:
        logger.setLevel(logging.INFO)
    started = time.monotonic()
    try:
        code = run_suite(args.suite, folder, mock=args.mock, k=args.k, extra=args.extra)
    except SystemExit as exit_:  # argparse inside a suite
        code = exit_.code if isinstance(exit_.code, int) else CRASH
    except Exception:
        traceback.print_exc()
        code = CRASH
    finally:
        logger.removeHandler(handler)
    spend = asdict(handler.spend)
    spend["wall_seconds"] = round(time.monotonic() - started, 1)
    spend["mode"] = "mock" if args.mock else "live"
    spend["exit_code"] = code
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "usage.json").write_text(json.dumps(spend, indent=1))
    if code == 2:
        return 2  # live mode without OPENAI_API_KEY, or bad arguments
    if code == CRASH or not any(folder.glob("**/report.json")):
        print(f"{args.suite}: the harness did not finish (exit {code})", file=sys.stderr)
        return CRASH
    if args.fail_on_failures and not passed_all(args.suite, folder):
        return 1
    return 0


# --- Summary --------------------------------------------------------------------------------


def summary(out: Path) -> str:
    lines = ["# Nightly evals", ""]
    total_cost, total_wall = 0.0, 0.0
    sections: list[str] = []
    for suite in SUITES:
        folder = out / suite
        if not folder.exists():
            continue
        usage = _load(folder / "usage.json")
        reports = sorted(folder.glob("**/report.json"), reverse=True)
        data = _load(reports[0]) if reports else {}
        total_cost += float(usage.get("cost_usd", 0.0))
        total_wall += float(usage.get("wall_seconds", 0.0))
        render: Callable[[dict[str, Any]], list[str]] = (
            _sources_section if suite == "sources" else _cases_section
        )
        sections += [
            f"## {suite} ({usage.get('mode', '?')}, exit {usage.get('exit_code', '?')})",
            "",
            _spend_line(usage),
            "",
            *(render(data) if data else ["No report: the harness did not finish."]),
            "",
        ]
    lines += [f"Total: ${total_cost:.2f} estimated model spend, {total_wall / 60:.0f} min.", ""]
    return "\n".join([*lines, *sections])


def _spend_line(usage: dict[str, Any]) -> str:
    models = ", ".join(f"{m} ${c:.2f}" for m, c in usage.get("by_model", {}).items())
    return (
        f"{usage.get('model_calls', 0)} model calls, "
        f"{usage.get('input_tokens', 0):,} input tokens "
        f"({usage.get('cached_input_tokens', 0):,} cached), "
        f"{usage.get('output_tokens', 0):,} output, "
        f"${usage.get('cost_usd', 0.0):.2f} ({models or 'none'}), "
        f"{usage.get('wall_seconds', 0) / 60:.1f} min"
    )


def _cases_section(data: dict[str, Any]) -> list[str]:
    rows: list[dict[str, Any]] = data.get("summary", [])
    live = [r for r in rows if not r.get("skipped")]
    passing = sum(bool(r["pass_all"]) for r in live)
    out = [
        f"pass^k (k={data.get('k')}): **{passing}/{len(live)} cases**",
        "",
        "| Scenario | Case | pass^k | Runs passed | Avg s | Failing checks |",
        "|---|---|---|---|---|---|",
    ]
    # Failing cases first, then the rest, so the table starts with what needs a look.
    for row in sorted(live, key=lambda r: (bool(r["pass_all"]), r["scenario"], r["case"])):
        failing = ", ".join(f"{name} ×{n}" for name, n in row["failures"].items()) or "—"
        out.append(
            f"| {row['scenario']} | {row['case']} | {'✅' if row['pass_all'] else '❌'} "
            f"| {row['passed']}/{row['runs']} | {row['avg_seconds']} | {failing} |"
        )
    rates: dict[str, list[int]] = data.get("check_rates", {})
    if rates:
        out += ["", "| Scenario | Checks passed | Rate |", "|---|---|---|"]
        for scenario, (ok, total) in rates.items():
            rate = f"{ok / total:.0%}" if total else "—"
            out.append(f"| {scenario} | {ok}/{total} | {rate} |")
    top: list[list[Any]] = data.get("top_failures", [])
    if top:
        out += ["", "Top failures:", ""]
        out += [f"{i}. **{key}** ×{n}: {detail}" for i, (key, n, detail) in enumerate(top[:10], 1)]
    return out


def _sources_section(data: dict[str, Any]) -> list[str]:
    summary_: dict[str, Any] = data.get("summary", {})
    rows: list[dict[str, Any]] = summary_.get("per_variant", [])
    out = [
        f"pass^k (k={data.get('repeats')}): **{summary_.get('variants_pass_k')}/"
        f"{summary_.get('variants')} variants**, false-ready runs "
        f"{summary_.get('false_ready_runs')}, infra errors {summary_.get('infra_error_runs')}",
        "",
        "| Variant | pass^k | Runs passed | False-ready | Mean s | Known gap |",
        "|---|---|---|---|---|---|",
    ]
    for row in sorted(rows, key=lambda r: (bool(r["pass_k"]), r["variant"])):
        out.append(
            f"| {row['variant']} | {'✅' if row['pass_k'] else '❌'} "
            f"| {row['passed']}/{row['runs']} | {row['false_ready']} | {row['mean_seconds']} "
            f"| {row['known_gap'] or '—'} |"
        )
    reasons = [
        f"- **{row['variant']}**: {reason} ({count})"
        for row in rows
        if not row["pass_k"]
        for reason, count in list(row["failure_reasons"].items())[:3]
    ]
    if reasons:
        out += ["", "Why runs failed (first reasons per variant):", "", *reasons]
    return out


def _load(path: Path) -> dict[str, Any]:
    try:
        loaded: Any = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return cast(dict[str, Any], loaded)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nightly evals: one suite, or the summary.")
    parser.add_argument("suite", choices=[*SUITES, "summary"])
    parser.add_argument("--out", required=True, help="the nightly report folder")
    parser.add_argument("--mock", action="store_true", help="scripted model outputs")
    parser.add_argument("-k", type=int, default=None, help="runs per case (suite default)")
    parser.add_argument(
        "--fail-on-failures", action="store_true", help="exit 1 when a case did not pass"
    )
    parser.add_argument("extra", nargs="*", help="passed to the suite, after --")
    args = parser.parse_args(argv)
    if args.suite == "summary":
        print(summary(Path(args.out)))
        return 0
    return run_with_spend(args)


if __name__ == "__main__":
    sys.exit(main())
