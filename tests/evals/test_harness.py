"""The eval harness itself, in mock mode (scripted model): it must run and report.

The live suite (`uv run python -m evals.run`) is not part of the test run.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.config import RunnerConfig
from bido_back.purchasing.workbook import read_workbook
from evals.golden import golden_analysis
from evals.harness import Check, Env, MemoryStorage, RunResult
from evals.layouts import fixtures
from evals.run import markdown, run_suite, summarize
from tests.db.conftest import app_engine

__all__ = ["app_engine"]

TODAY = date(2026, 10, 7)


def test_fixtures_are_deterministic_and_use_test_domains_only() -> None:
    first = fixtures(TODAY)
    second = fixtures(TODAY)
    # Compare cell values: the .xlsx bytes carry a creation timestamp.
    assert [[(g.name, g.rows) for g in read_workbook(f.data, f.filename)] for f in first] == [
        [(g.name, g.rows) for g in read_workbook(f.data, f.filename)] for f in second
    ]
    assert len(first) == 13
    for fixture in first:
        for supplier in fixture.dataset.suppliers:
            assert supplier.email.endswith((".example.test", ".example.com"))


def test_golden_analysis_needs_a_plan_or_a_quantity() -> None:
    by_name = {f.name: f for f in fixtures(TODAY)}
    no_plan = by_name["inverter_no_plan"]
    assert golden_analysis(no_plan.dataset, TODAY, None) is None
    golden = golden_analysis(no_plan.dataset, TODAY, Decimal(250))
    assert golden is not None and golden["quantity"] == "250"
    assert golden_analysis(by_name["motor_tidy_en"].dataset, TODAY, None) is not None


def test_summary_counts_pass_k_and_failures() -> None:
    ok = RunResult("import", "a", 1, [Check("x", True)])
    bad = RunResult("import", "a", 2, [Check("x", False, "boom")])
    skipped = RunResult("extraction", "b", 1, skipped="live only")
    rows = summarize([ok, bad, skipped])
    a = next(r for r in rows if r["case"] == "a")
    assert (a["runs"], a["passed"], a["pass_all"], a["failures"]) == (2, 1, False, {"x": 1})
    assert next(r for r in rows if r["case"] == "b")["skipped"] == "live only"
    assert "| import | a | ❌ | 1/2 |" in markdown(rows, "mock", 2, TODAY)


@pytest.mark.db
async def test_mock_suite_passes_on_the_baseline_cases(app_engine: AsyncEngine) -> None:
    env = Env(
        engine=app_engine,
        storage=MemoryStorage(),
        runner_config=RunnerConfig(),
        mode="mock",
        today=TODAY,
    )
    results = await run_suite(
        env, k=1, only=("motor_tidy_en", "battery_no_bom", "it_body_prices", "en_decline")
    )
    assert {r.scenario for r in results} == {"import", "analysis", "copilot", "extraction"}
    failed = [
        (r.scenario, r.case, r.error, [c for c in r.checks if not c.ok])
        for r in results
        if not r.ok
    ]
    assert not failed


@pytest.mark.eval
@pytest.mark.db
async def test_live_suite_writes_a_report(app_engine: AsyncEngine, tmp_path: object) -> None:
    """`pytest -m eval`: one live pass over everything (needs OPENAI_API_KEY)."""
    import os
    from pathlib import Path

    from bido_back.settings import get_settings
    from evals.run import write_report

    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("live evals need OPENAI_API_KEY")
    env = Env(
        engine=app_engine,
        storage=MemoryStorage(),
        runner_config=RunnerConfig.from_settings(get_settings()),
        mode="live",
        today=date.today(),
    )
    results = await run_suite(env, k=1)
    folder = write_report(results, env, 1, Path(str(tmp_path)) / "report")
    assert (folder / "report.md").exists()
