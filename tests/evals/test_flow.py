"""The flow evals in mock mode (scripted model): the whole RFQ journey must pass.

The live run (`uv run python -m evals.flow`) is not part of the test run.
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.config import RunnerConfig
from evals.flow import SCENARIOS, flow_markdown, golden_actions, guess_language, run_flow
from evals.harness import Env, MemoryStorage
from evals.personas import CHEAP_LATE, DECLINER, EAGER, NEGOTIATION_CAST, TOUGH
from tests.db.conftest import app_engine

__all__ = ["app_engine"]

TODAY = date(2026, 10, 7)


def test_personas_write_to_test_domains_only() -> None:
    for persona in NEGOTIATION_CAST:
        assert persona.email.endswith(".example.test")


def test_golden_negotiation_follows_the_buyer_rules() -> None:
    # An unchanged reply stops the automatic requests and hands the supplier to the buyer.
    assert golden_actions(TOUGH) == [
        ("counter", {1: (Decimal("0.040"), None), 2: (Decimal("1.00"), None)}),
    ]
    # Cheap but late: only the delivery date is asked, once.
    assert golden_actions(CHEAP_LATE) == [("counter", {1: (None, 42), 2: (None, 42)})]
    # Line 1 accepted after the first counter; line 2 moved: a quarter-step concession.
    assert golden_actions(EAGER)[1] == ("counter", {2: (Decimal("1.01"), None)})
    # A line left unquoted is an open question first: no negotiation email yet.
    assert golden_actions(DECLINER) == []


def test_language_guess_reads_supplier_languages() -> None:
    assert guess_language("Sehr geehrte Damen und Herren, vielen Dank für Ihr Angebot.") == "de"
    assert guess_language("Buongiorno, grazie per la vostra offerta.") == "it"
    assert guess_language("Bonjour, merci pour votre offre.") == "fr"
    assert guess_language("Hello, thank you for your quotation.") == "en"
    assert guess_language("Prezados, solicitamos cotação para os itens abaixo.") == "pt"


@pytest.mark.db
async def test_mock_flow_passes_every_scenario(app_engine: AsyncEngine) -> None:
    env = Env(
        engine=app_engine,
        storage=MemoryStorage(),
        runner_config=RunnerConfig(),
        mode="mock",
        today=TODAY,
    )
    results = await run_flow(env, k=1)
    assert {r.scenario for r in results} == set(SCENARIOS)
    failed = [
        (r.scenario, r.case, r.error, [c for c in r.checks if not c.ok])
        for r in results
        if not r.ok
    ]
    assert not failed
    report = flow_markdown(results, "mock", 1, TODAY)
    assert "| negotiation | mandate_five | ✅ | 1/1 |" in report
