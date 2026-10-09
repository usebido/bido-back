"""The negotiation scenarios in mock mode (scripted models): every scenario runs and is graded.

Checks that fail on the current code are listed in `EXPECTED_MOCK_FAILURES`; each is a
non-strict xfail here, so the suite is green now and reports XPASS once a fix lands
(then remove it from the list). Any other failing check fails the test.

The live run (`uv run python -m evals.negotiation_scenarios --live`) is not part of the
test run.
"""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.catalog.contracts import (
    FollowupEmailInput,
    FollowupItem,
    NegotiationEmailInput,
    NegotiationLineAsk,
)
from bido_back.agents.config import RunnerConfig
from bido_back.quotes.parsing import decimal_separator_for
from bido_back.quotes.validation import QuoteSources, RequestedLine, validate_quote
from evals.harness import Env, MemoryStorage
from evals.negotiation_scenarios import (
    EXPECTED_MOCK_FAILURES,
    NAMES,
    SCENARIOS,
    Say,
    ScenarioRun,
    add_business_days,
    diagnosis,
    report_markdown,
    run_suite,
)
from evals.negotiation_text import (
    amounts,
    commitment,
    language_of,
    signature_problem,
    text_items,
    written_dates,
)
from evals.negotiation_threads import Thread, followup_writer, missing_quotes, negotiation_writer
from tests.db.conftest import app_engine

__all__ = ["app_engine"]

START = date(2026, 10, 9)


def test_suppliers_write_to_test_domains_only() -> None:
    for scenario in SCENARIOS:
        for supplier in scenario.suppliers:
            assert supplier.email.endswith(".example.test"), supplier.email


def test_scripted_extractions_are_grounded_in_each_email() -> None:
    """What mock mode scripts is what a correct model returns: every value quoted from the
    email, and nothing the validation would hold (prompt v5 states RFQ-6's business days
    and relative validity as data; code converts them)."""
    for scenario in SCENARIOS:
        requested = [RequestedLine(id=uuid4(), position=p.position) for p in scenario.parts]
        for step in scenario.steps:
            for action in step.actions:
                if not isinstance(action, Say):
                    continue
                spec = scenario.supplier(action.supplier)
                thread = Thread("RFQ-1", START, scenario.parts, spec.name, spec.contact)
                email = action.write(thread)
                where = f"{scenario.name}/{step.key}"
                assert not missing_quotes(email), (where, missing_quotes(email))
                language = "en" if spec.created == "copilot" else spec.language
                validated = validate_quote(
                    email.truth,
                    requested=requested,
                    sources=QuoteSources(email_subject="Re: [RFQ-1]", email_body=email.body),
                    today=START,
                    decimal=decimal_separator_for(language),
                )
                issues = {i for line in validated.lines for i in line.issues}
                issues |= {
                    i
                    for check in [
                        *validated.header.values(),
                        *(f for line in validated.lines for f in line.fields.values()),
                    ]
                    for i in check.issues
                }
                issues |= set(validated.issues)
                assert not issues, (where, issues)


def test_language_signature_dates_and_amounts() -> None:
    assert language_of("Prezados, segue nossa proposta para os itens abaixo. Atenciosamente") == (
        "pt"
    )
    assert language_of("Thank you for your offer. Please send a revised offer by Monday.") == "en"
    assert language_of("Sehr geehrte Damen und Herren, vielen Dank für Ihr Angebot.") == "de"
    # A table of the buyer's Portuguese item names does not make an English email Portuguese.
    table = "1 | Rolamento motor MTR-100 | 70 | pcs | 2026-10-22 | 000101\n"
    assert language_of(f"Hello, please quote the items below.\n{table}Best regards") == "en"

    assert signature_problem("Hi,\n\nBest regards,\nBiduuuuu\nBiduuuuu", ["Biduuuuu"])
    assert signature_problem("Olá\n\nAtenciosamente,\nAna\n\nAtenciosamente,\nAna", ["Ana"])
    assert signature_problem("Olá,\n\nAtenciosamente,\nAna Souza\nAcme Ltda", ["Ana Souza"]) is None

    around = date(2026, 10, 9)
    assert date(2026, 10, 20) in written_dates("Pedimos retorno até 20 de outubro de 2026.", around)
    assert date(2026, 10, 20) in written_dates("até 20/10/2026", around)
    assert date(2026, 10, 20) in written_dates("bis zum 20.10.2026", around)
    assert date(2026, 10, 20) in written_dates("by 2026-10-20", around)
    assert date(2026, 10, 20) in written_dates("até 20/10", around)
    assert not written_dates("12.5 kg a R$ 95,00", around)

    found = amounts("preço unitário de R$ 45,67 por pcs, para 70 pcs; 42,00 EUR")
    assert [Decimal("45.67") in r for r in found] == [True, False]
    assert any(Decimal(42) in r for r in found)

    assert commitment("Aceitamos a sua proposta e vamos emitir o pedido.")
    assert commitment("We accept your offer.")
    assert not commitment("Lead time counted from purchase order confirmation.")


def test_clarification_text_is_read_when_no_task_matches() -> None:
    lines = [(1, "Rolamento motor MTR-100", "000101"), (2, "Eixo motor MTR-100", "000102")]
    body = (
        "Dear MetalNova,\n\nCould you please provide the following?\n"
        "Offer: how long it is valid.\n"
        "Position 1 – Rolamento motor MTR-100: the quantity or quantity tier the price applies "
        "to.\nPrazo de entrega do eixo 000102, por favor.\n\nBest regards,\nAna"
    )
    assert text_items(body, lines) == [(None, "valid_until"), (1, "quantity"), (2, "lead_time")]


def test_business_days_skip_brazilian_holidays() -> None:
    # 2026-10-12 (Monday) is a national holiday in Brazil.
    start = datetime(2026, 10, 9, 9, tzinfo=UTC)
    assert add_business_days(start, 2, "BR").date() == date(2026, 10, 14)
    assert add_business_days(start, 2, None).date() == date(2026, 10, 13)


@pytest.mark.parametrize("language", ["pt", "de", "en"])
def test_mock_writers_follow_the_v2_prompts(language: str) -> None:
    """Code renders every email; the writers may only add a note, empty by default, and the
    scripted writers (like a correct model) add none."""
    followup = FollowupEmailInput(
        kind="clarification",
        rfq_ref="RFQ-1",
        rfq_title="Componentes",
        buyer_company="Acme",
        buyer_name="Ana",
        supplier_name="Fornecedor",
        supplier_language=language,
        chase_number=0,
        open_items=[
            FollowupItem(position=None, line=None, field="valid_until", problem="missing"),
            FollowupItem(position=2, line="Eixo", field="lead_time", problem="unclear"),
        ],
    )
    assert followup_writer(followup).note == ""
    negotiation = NegotiationEmailInput(
        action="bafo",
        rfq_ref="RFQ-1",
        rfq_title="Componentes",
        buyer_company="Acme",
        buyer_name="Ana",
        supplier_name="Fornecedor",
        supplier_language=language,
        round=3,
        reply_by=date(2026, 10, 20),
        mention_competition=True,
        lines=[
            NegotiationLineAsk(
                position=1,
                line="Rolamento",
                quantity=Decimal(70),
                unit="pcs",
                currency="BRL",
                offered_unit_price=Decimal("48.90"),
                target_unit_price=Decimal("45.67"),
            ),
            NegotiationLineAsk(
                position=2,
                line="Eixo",
                quantity=Decimal(15),
                unit="pcs",
                currency="BRL",
                offered_unit_price=Decimal("229"),
                target_unit_price=None,
                offered_lead_time_days=35,
                required_lead_time_days=21,
            ),
        ],
    )
    assert negotiation_writer(negotiation).note == ""


def test_every_expected_failure_is_diagnosed() -> None:
    for scenario, _, check in EXPECTED_MOCK_FAILURES:
        assert scenario in NAMES
        assert diagnosis(scenario, check), (scenario, check)


# --- The suite in mock mode ---------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def mock_runs(app_engine: AsyncEngine) -> AsyncIterator[dict[str, ScenarioRun]]:
    env = Env(
        engine=app_engine,
        storage=MemoryStorage(),
        runner_config=RunnerConfig(),
        mode="mock",
        today=date.today(),
    )
    runs = await run_suite(env, k=1)
    yield {run.scenario: run for run in runs}


def _failing(run: ScenarioRun) -> set[tuple[str, str, str]]:
    return {(run.scenario, s.key, c.name) for s in run.steps for c in s.failing()}


@pytest.mark.db
@pytest.mark.parametrize("name", NAMES)
async def test_mock_scenario_runs_and_fails_only_where_expected(
    name: str, mock_runs: dict[str, ScenarioRun]
) -> None:
    run = mock_runs[name]
    scenario = next(s for s in SCENARIOS if s.name == name)
    assert run.error is None
    assert [s.key for s in run.steps] == ["rfq", *(s.key for s in scenario.steps)]
    assert not [(s.key, s.error) for s in run.steps if s.error]
    assert run.notes["unscripted_extractions"] == []
    unexpected = _failing(run) - set(EXPECTED_MOCK_FAILURES)
    assert not unexpected, [
        (step, check, c.detail)
        for _, step, check in sorted(unexpected)
        for s in run.steps
        if s.key == step
        for c in s.checks
        if c.name == check
    ]
    assert f"| {name} |" in report_markdown([run], "mock", 1)


@pytest.mark.db
@pytest.mark.parametrize(
    "expected",
    [
        pytest.param(
            key,
            id="/".join(key),
            marks=pytest.mark.xfail(strict=False, reason=", ".join(diagnosis(key[0], key[2]))),
        )
        for key in sorted(EXPECTED_MOCK_FAILURES)
    ],
)
async def test_mock_known_gap_now_passes(
    expected: tuple[str, str, str], mock_runs: dict[str, ScenarioRun]
) -> None:
    """A gap the baseline found: XPASS once the product handles it."""
    scenario, _, _ = expected
    assert expected not in _failing(mock_runs[scenario])
