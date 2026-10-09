"""The buyer journey eval in mock mode (scripted models): every step runs and is graded.

The live run (`uv run python -m evals.journey --live`) is not part of the test run.
"""

from datetime import date, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.purchasing.sources.contracts import PlanningScope
from bido_back.purchasing.sources.readers import SourceDocument
from bido_back.purchasing.sources.service import analyze_documents
from bido_back.quotes.validation import QuoteSources, RequestedLine, validate_quote
from evals.harness import Env, MemoryStorage
from evals.journey import (
    PENDING,
    STEPS,
    cannot_conclude,
    golden_rounds,
    journey_markdown,
    language_of,
    quantity_shown,
    run_journeys,
    segments,
)
from evals.journey_suppliers import (
    SCRIPT,
    Offer,
    Requested,
    expected_recommendations,
    expected_states,
    write,
)
from evals.journey_workbook import REAL_PARTS, SUPPLIERS, XLSX, mock_case
from tests.db.conftest import app_engine

__all__ = ["app_engine"]

EXPORT = date(2026, 10, 8)


def test_suppliers_write_to_test_domains_only() -> None:
    assert all(s.email.endswith(".example.test") for s in SUPPLIERS)


async def test_mock_workbook_has_the_golden_answer() -> None:
    case = mock_case(EXPORT)
    assert case.mapper is not None
    assessment = await analyze_documents(
        [SourceDocument("mock", case.filename, XLSX, case.data)],
        "Motor MTR-100, Planta A",
        ToolContext(tenant_id=uuid4(), user_id=uuid4(), run_id=uuid4()),
        case.mapper,
        scope=PlanningScope.model_validate(case.scope()),
    )
    assert assessment.status == "assessed"
    got = {line.part_number: (line.calculated_to_buy, line.unit) for line in assessment.lines}
    assert got == {p.part: (p.to_buy, p.unit) for p in case.parts}
    for line in assessment.lines:
        prices = {s.name: s.last_price for s in line.suppliers}
        part = case.part(line.part_number)
        assert {
            s.name: part.last_prices[s.key] for s in SUPPLIERS if s.key in part.last_prices
        } == (prices)


def test_supplier_script_follows_the_business_rules() -> None:
    states = expected_states()
    # A lead-time-only amendment keeps every price.
    assert states["metalnova.r2"]["000104"] == Offer(Decimal("112.00"), 15)
    assert states["metalnova.r2"]["000101"].price == Decimal("45.60")
    # One price lowered, the rest kept.
    assert states["precisaosul.r1"]["000105"].price == Decimal("20.70")
    assert states["precisaosul.r1"]["000101"].price == Decimal("48.50")
    # An older revision delivered late changes nothing.
    assert states["multipecas.rev1"]["000102"].price == Decimal("238.00")
    assert "000104" not in states["multipecas.r0"]

    targets = {p.part: p.target for p in REAL_PARTS if p.buy}
    maxima = {p.part: p.maximum for p in REAL_PARTS if p.buy}
    # MultiPecas left 000104 unquoted: the first request asks it too and negotiates the
    # other parts (one email per answer); its unchanged repeats get nothing until its
    # revision moves a price: then the best-and-final request.
    multipecas = golden_rounds(targets, 2, maxima=maxima)["multipecas"]
    assert [a for a, _ in multipecas] == ["counter", "bafo"]
    assert all("000104" not in asks for _, asks in multipecas)
    # Two rounds: one request, then the best-and-final request once the supplier moved.
    two = golden_rounds(targets, 2, maxima=maxima)
    assert [a for a, _ in two["metalnova"]] == ["counter", "bafo"]
    assert [a for a, _ in two["precisaosul"]] == ["counter", "bafo"]
    # Three rounds: MetalNova's lead-time-only change moves nothing that was asked (no
    # request), and its own final offer ends the requests.
    three = golden_rounds(targets, 3, maxima=maxima)
    assert [a for a, _ in three["metalnova"]] == ["counter", "counter"]
    first, second = (asks for _, asks in three["metalnova"])
    assert second["000102"] > first["000102"]  # it moved: a labeled concession

    best = expected_recommendations(REAL_PARTS, 10, maxima={p.part: p.maximum for p in REAL_PARTS})
    assert {p: e.supplier for p, e in best.items()} == {
        "000101": "metalnova",
        "000102": "metalnova",
        "000104": "precisaosul",  # MetalNova is cheaper but late
        "000105": "precisaosul",
        "000106": "metalnova",
    }


def test_scripted_extraction_is_grounded_in_each_email() -> None:
    lines = [Requested(i, p) for i, p in enumerate((p for p in REAL_PARTS if p.buy), 1)]
    for email in SCRIPT:
        written = write(
            email,
            rfq_ref="RFQ-1",
            thread_subject="[RFQ-1] Componentes",
            requested=lines,
            valid_until=EXPORT + timedelta(days=30),
        )
        validated = validate_quote(
            written.truth,
            requested=[RequestedLine(id=uuid4(), position=r.position) for r in lines],
            sources=QuoteSources(email_subject=written.subject, email_body=written.body),
            today=EXPORT,
            decimal=",",
        )
        assert not validated.needs_review, email.key


def test_answer_checks_read_tables_and_sentences() -> None:
    part = next(p for p in REAL_PARTS if p.part == "000104")
    covered = next(p for p in REAL_PARTS if p.part == "000103")
    answer = (
        "| 000101 | Rolamento | 50 pcs | MetalNova R$ 40,59 |\n"
        "| 000104 | Fio de cobre | 22 kg | PrecisaoSul R$ 128,86 |\n"
        "Coberto pelo estoque, nada a comprar: 000103 Carcaça."
    )
    parts = segments(answer, ["000101", "000103", "000104"])
    assert quantity_shown(parts["000104"], part)
    assert quantity_shown(parts["000103"], covered)
    assert "128,86" not in parts["000101"]
    assert cannot_conclude("Não foi possível calcular a quantidade a comprar do motor.")
    assert not cannot_conclude("Não consegui confirmar o frete.")
    assert (
        language_of("Não há estoque para essa compra; o fornecedor tem prazo de entrega.") == "pt"
    )


@pytest.mark.db
async def test_mock_journey_grades_every_step(app_engine: AsyncEngine) -> None:
    env = Env(
        engine=app_engine,
        storage=MemoryStorage(),
        runner_config=RunnerConfig(),
        mode="mock",
        today=EXPORT,
    )
    runs = await run_journeys(env, k=1)
    run = runs[0]
    assert [s.key for s in run.steps] == [key for key, _ in STEPS]
    assert not [(s.key, s.error, s.skipped) for s in run.steps if s.error or s.skipped]
    # Only what other workstreams add (or an unassigned product gap) may still fail.
    unexpected = [
        (s.key, c.name, c.detail)
        for s in run.steps
        for c in s.checks
        if not c.ok and c.name not in PENDING
    ]
    assert not unexpected
    assert run.notes["non_test_recipients"] == []
    assert "5. Negotiation rounds under the mandate | ✅" in journey_markdown(runs, "mock", 1)
