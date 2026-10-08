"""The synthetic semantic-mapping evaluation harness also runs deterministically in CI."""

import pytest

from evals.source_planning import CASES, evaluate


@pytest.mark.parametrize("case", CASES)
async def test_source_planning_evaluation_harness(case: str) -> None:
    result = await evaluate(case)
    assert result.passed, result
    assert not result.false_ready
    assert result.inspected_tables == 3
