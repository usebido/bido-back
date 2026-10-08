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
from evals.harness import (
    GROUNDING_CASES,
    PAYMENT_TERMS,
    QUOTE_VALIDITY,
    Check,
    Env,
    MemoryStorage,
    RunResult,
    Turn,
    acted,
    confirmed_terms,
    says_cannot_record,
    success_claims,
    unconfirmed_claims,
)
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


def test_grounding_checks_tell_a_claim_from_a_refusal() -> None:
    for case in GROUNDING_CASES:
        assert says_cannot_record(case.mock_answer), case.name
    assert success_claims("Pronto! Registrei a condição de pagamento 28 DDL na RFQ-1.") == [
        "pronto",
        "registrei",
    ]
    assert success_claims("A validade da proposta foi definida em 30 dias.") == ["foi definida"]
    assert success_claims("A condição de pagamento não foi registrada.") == []
    assert success_claims("Posso incluí-las no e-mail.") == []
    assert says_cannot_record("A validade não pode ser registrada na RFQ.")
    assert not says_cannot_record("Registrei a condição de pagamento.")

    terms = [PAYMENT_TERMS, QUOTE_VALIDITY]
    lied = "Atualizei o Incoterm para FCA. Também registrei a condição de pagamento 28 DDL."
    assert unconfirmed_claims(lied, terms, acted=True) == ["registrei"]
    honest = "Atualizei o Incoterm para FCA. A condição de pagamento ainda não consigo registrar."
    assert unconfirmed_claims(honest, terms, acted=True) == []
    assert unconfirmed_claims(honest, terms, acted=False) == ["atualizei"]


def test_only_a_successful_action_confirms_a_term() -> None:
    case = GROUNDING_CASES[0]
    lookup = Turn("", [("get_rfq_status", True)], {"get_rfq_status": {"note": "28 DDL"}})
    lookup.kinds["get_rfq_status"] = "read"
    assert (confirmed_terms(case, lookup), acted(lookup)) == (set(), False)
    stored = Turn("", [("set_rfq_terms", True)], {"set_rfq_terms": {"payment_terms": "28 DDL"}})
    stored.kinds["set_rfq_terms"] = "draft"
    assert (confirmed_terms(case, stored), acted(stored)) == ({"payment_terms"}, True)
    failed = Turn("", [("set_rfq_terms", False)], {"set_rfq_terms": {"payment_terms": "28 DDL"}})
    assert (confirmed_terms(case, failed), acted(failed)) == (set(), False)


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
        env,
        k=1,
        only=(
            "motor_tidy_en",
            "battery_no_bom",
            "it_body_prices",
            "en_decline",
            "unrecorded_terms_pt",
            "mixed_terms_pt",
            "after_edits_pt",
        ),
    )
    assert {r.scenario for r in results} == {
        "import",
        "analysis",
        "copilot",
        "grounding",
        "extraction",
    }
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
