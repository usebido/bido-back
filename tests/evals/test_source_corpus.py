"""The source-understanding corpus in mock mode: the correct mapping injected, no network.

It checks the generated scenario and layouts, the grader (including the critical
false-ready flag) and what the deterministic pipeline does with a perfect mapping.
Variants whose ideal outcome the current readers/contract/planner cannot reach are
expected failures until that gap closes. The live run is
`uv run python -m evals.source_corpus --live --repeats 3` (not part of the test run).
"""

import json
import sys
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bido_back.purchasing.sources.contracts import MappingProposal
from bido_back.purchasing.sources.readers import read_source
from evals.source_corpus import (
    MapperCall,
    diagnose_mapping,
    evaluate,
    main,
    scripted_mapper,
    write_corpus,
)
from evals.source_scenario import AS_OF, expected, scenario
from evals.source_variants import VARIANT, VARIANTS, render


def test_the_scenario_answers_the_buyers_motor_case() -> None:
    s, truth = scenario(), expected()
    assert truth.to_buy == {
        "000101": Decimal(50),
        "000102": Decimal(30),  # the 20 pending pieces arrive on 26/10, after the need
        "000103": Decimal(0),
        "000104": Decimal(22),
        "000105": Decimal(220),
        "000106": Decimal(60),
    }
    closed = [p for p in s.purchases if p.status == "closed"]
    assert 65 <= len(closed) <= 75
    assert {p.supplier for p in closed} == {"F001", "F002", "F003"}
    assert min(p.ordered_at for p in closed) >= AS_OF - timedelta(days=180)
    assert {p.status for p in s.purchases} == {"closed", "open", "cancelled"}
    assert all(v.email.endswith(".example.test") for v in s.suppliers)
    # Open orders, the cancelled one and archived offers must never pass for a paid price.
    decoys = {p.unit_price for p in s.purchases if p.status != "closed"}
    decoys |= {price for q in s.quotes for _, _, price in q.items if price is not None}
    assert not decoys & {last.price for last in truth.last.values()}
    assert {"000201", "000302", "000303", "MTR-100"} <= truth.out_of_scope


def test_generation_is_deterministic() -> None:
    first = scenario()
    scenario.cache_clear()
    assert scenario() == first
    for name in ("scattered_pt", "csv_bundle", "formula_cache"):
        again = render.__wrapped__(name)
        # Compare the cells read back: workbook bytes carry a creation timestamp.
        assert [read_source(d)[0] for d in again.documents] == [
            read_source(d)[0] for d in render(name).documents
        ]
        assert again.sections == render(name).sections


@pytest.mark.parametrize("name", [v.name for v in VARIANTS])
async def test_a_correct_mapping_is_never_false_ready(name: str) -> None:
    result = await evaluate(name)
    assert not result.false_ready, result.failures
    assert not result.false_complete, result.failures
    assert not result.infra_error
    # The injected proposals are the expected ones: the diagnosis finds nothing to blame.
    assert all(note.startswith("mapper issue") for note in result.mapping), result.mapping


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(v.name, marks=pytest.mark.xfail(reason=v.known_gap, strict=False))
        if v.known_gap
        else v.name
        for v in VARIANTS
    ],
)
async def test_a_correct_mapping_reaches_the_expected_outcome(name: str) -> None:
    result = await evaluate(name)
    assert result.passed, (result.outcome, result.failures, result.blockers)


async def test_the_layouts_without_a_known_gap_are_exact() -> None:
    for name in ("scattered_pt", "tidy_en", "single_sheet", "prompt_injection"):
        result = await evaluate(name)
        assert result.outcome == "exact", (name, result.failures, result.blockers)
        assert all(part.ready and part.price_ok for part in result.parts)
    grid = await evaluate("mrp_grid")
    assert grid.outcome == "review" and grid.passed


async def test_gross_stock_read_as_available_is_flagged_false_ready() -> None:
    corpus = render("scattered_pt")
    wrong = {
        key: [
            s.model_copy(
                update={
                    "columns": [
                        c.model_copy(update={"field": "available"}) if c.field == "total" else c
                        for c in s.columns
                    ]
                }
            )
            for s in sections
        ]
        for key, sections in corpus.sections.items()
    }
    result = await evaluate("scattered_pt", mapper=scripted_mapper(replace(corpus, sections=wrong)))
    assert result.false_ready and result.outcome == "false_ready" and not result.passed
    # Free stock 25 instead of 20 for 000101: 45 to buy instead of 50, marked ready.
    bearing = next(p for p in result.parts if p.part == "000101")
    assert bearing.ready and bearing.to_buy == Decimal(45) and bearing.available == Decimal(25)
    assert any("extra available -> col 5 'Físico bruto'" in note for note in result.mapping)


def test_the_diagnosis_names_missing_and_mislabelled_sections() -> None:
    corpus = render("scattered_pt")
    key = ("upload-1", "Recebimentos")
    receipts, schedule = corpus.sections[key]
    (table,) = [t for t in read_source(corpus.documents[0])[0] if t.name == key[1]]
    proposal = MappingProposal(
        sections=[
            receipts.model_copy(
                update={"columns": [c for c in receipts.columns if c.field != "movement"]}
            ),
            schedule.model_copy(update={"kind": "receipt"}),
        ]
    )
    notes = diagnose_mapping(
        replace(corpus, sections={key: [receipts, schedule]}),
        [MapperCall(key[0], key[1], 1, 93, proposal)],
        {key: table},
    )
    assert any("receipt: movement missing" in note for note in notes)
    assert any("delivery_schedule: not mapped (read as receipt)" in note for note in notes)


def test_formula_cells_keep_their_cached_results() -> None:
    (document,) = render("formula_cache").documents
    stock = next(t for t in read_source(document)[0] if t.name == "Saldos_01")
    header = stock.rows[5]
    free = header.index("Livre utilização")
    values = {row[2]: row[free] for row in stock.rows[6:20]}
    assert values["000101"] == "20" and values["000104"] == "10"


def test_write_dir_holds_the_files_and_the_answers(tmp_path: Path) -> None:
    write_corpus(tmp_path, [VARIANT["csv_bundle"], VARIANT["scattered_pt"]])
    answer = json.loads((tmp_path / "csv_bundle" / "expected.json").read_text())
    assert answer["to_buy"]["000105"] == "220"
    assert answer["scope"]["location"] == "Planta A"
    assert answer["last_received"]["000106"]["supplier"] == expected().last["000106"].supplier
    schedule = (tmp_path / "csv_bundle" / "agenda_entregas.csv").read_bytes()
    assert "Não recebido".encode("latin-1") in schedule and b";" in schedule
    orders = (tmp_path / "csv_bundle" / "ZMM042_pedidos.csv").read_bytes().decode("latin-1")
    assert "1.700" in orders  # screws are bought by the thousand, thousands dot
    assert (tmp_path / "scattered_pt" / "Exportacao_ERP.xlsx").stat().st_size > 0


def test_the_cli_runs_in_mock_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report = tmp_path / "report.json"
    argv = ["source_corpus", "--mock", "--variant", "tidy_en", "--variant", "mrp_grid"]
    monkeypatch.setattr(sys, "argv", [*argv, "--json", str(report)])
    assert main() == 0
    data = json.loads(report.read_text())
    assert data["summary"]["runs"] == 2 and data["summary"]["false_ready_runs"] == 0
    assert [run["outcome"] for run in data["runs"]] == ["exact", "review"]
