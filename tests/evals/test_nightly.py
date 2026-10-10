"""The nightly wrapper: spend from the runner's log lines, the exit policy, the summary.

The suites themselves are covered by their own mock tests; these stub them.
"""

import json
import logging
from pathlib import Path

import pytest

from evals import nightly


def _agent_run(model: str, cost: float, outcome: str = "completed") -> None:
    logging.getLogger("bido_back.agents.runner").info(
        "agent run",
        extra={
            "model": model,
            "outcome": outcome,
            "model_calls": 2,
            "input_tokens": 1000,
            "cached_input_tokens": 400,
            "output_tokens": 100,
            "cost_usd": cost,
        },
    )


def _report(folder: Path, *, passing: bool) -> None:
    (folder / "20261010T060000Z-live").mkdir(parents=True)
    row = {
        "scenario": "copilot",
        "case": "motor_tidy_en",
        "runs": 3,
        "passed": 3 if passing else 2,
        "pass_all": passing,
        "skipped": None,
        "avg_seconds": 12.5,
        "failures": {} if passing else {"rfq_created": 1},
        "details": {},
    }
    (folder / "20261010T060000Z-live" / "report.json").write_text(
        json.dumps({"mode": "live", "k": 3, "summary": [row]})
    )


def _args(out: Path, *, fail: bool = False) -> list[str]:
    return ["run", "--out", str(out), "-k", "3", *(["--fail-on-failures"] if fail else [])]


def test_spend_sums_agent_run_lines_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def suite(name: str, folder: Path, **_: object) -> int:
        _agent_run("gpt-6-luna", 0.01)
        _agent_run("gpt-6.1-sol", 0.25, outcome="failed")
        logging.getLogger("bido_back.agents.runner").info("something else", extra={"cost_usd": 9})
        _report(folder, passing=True)
        return 0

    monkeypatch.setattr(nightly, "run_suite", suite)
    assert nightly.main(_args(tmp_path)) == 0

    usage = json.loads((tmp_path / "run" / "usage.json").read_text())
    assert usage["model_calls"] == 4
    assert usage["failed_runs"] == 1
    assert usage["input_tokens"] == 2000
    assert usage["cached_input_tokens"] == 800
    assert usage["cost_usd"] == pytest.approx(0.26)
    assert usage["by_model"] == {"gpt-6-luna": 0.01, "gpt-6.1-sol": 0.25}


def test_failing_cases_are_report_only_unless_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def suite(name: str, folder: Path, **_: object) -> int:
        _report(folder, passing=False)
        return 0

    monkeypatch.setattr(nightly, "run_suite", suite)
    assert nightly.main(_args(tmp_path / "a")) == 0
    assert nightly.main(_args(tmp_path / "b", fail=True)) == 1


def test_a_crash_or_a_missing_report_is_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crashing(name: str, folder: Path, **_: object) -> int:
        raise RuntimeError("database unreachable")

    monkeypatch.setattr(nightly, "run_suite", crashing)
    assert nightly.main(_args(tmp_path / "a")) == nightly.CRASH
    assert json.loads((tmp_path / "a" / "run" / "usage.json").read_text())["exit_code"] == 3

    def no_report(name: str, folder: Path, **_: object) -> int:
        return 0

    monkeypatch.setattr(nightly, "run_suite", no_report)
    assert nightly.main(_args(tmp_path / "b")) == nightly.CRASH


def test_missing_key_keeps_its_own_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_key(name: str, folder: Path, **_: object) -> int:
        return 2

    monkeypatch.setattr(nightly, "run_suite", no_key)
    assert nightly.main(_args(tmp_path)) == 2


def test_summary_lists_failing_cases_first_with_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def suite(name: str, folder: Path, **_: object) -> int:
        _agent_run("gpt-6-luna", 0.5)
        _report(folder, passing=False)
        return 0

    monkeypatch.setattr(nightly, "run_suite", suite)
    nightly.main(_args(tmp_path))
    text = nightly.summary(tmp_path)

    assert "## run (live, exit 0)" in text
    assert "pass^k (k=3): **0/1 cases**" in text
    assert "| copilot | motor_tidy_en | ❌ | 2/3 | 12.5 | rfq_created ×1 |" in text
    assert "$0.50" in text
