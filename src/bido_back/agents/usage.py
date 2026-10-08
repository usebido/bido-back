"""Model token usage per run, and its estimated cost in USD.

The adapter records every model request's usage, as the provider reported it,
on the run's `UsageMeter` and on the meters of the runs it is nested in: a
sheet-mapper run started by a Copilot tool counts toward the Copilot turn. The
runner writes a run's totals on its `agent run` log line and the Copilot writes
the turn's on `copilot turn finished`, so CloudWatch can sum tokens and spend
per agent, model, tenant and day.

Token counts follow OpenAI's Responses API: `input_tokens` includes the cached
ones and `output_tokens` includes the reasoning ones. Cache writes are not
reported separately by the adapter and are priced as ordinary input.

Cost is an estimate from OpenAI's standard list prices for short-context
requests (up to 272K input tokens), per million tokens, as published at
https://developers.openai.com/api/docs/pricing on 2026-10-08. A model missing
from `PRICES` has no cost (None), and is logged once per process. Update the
table when prices or models change; the invoice stays the source of truth.
"""

import logging
import re
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD per million tokens."""

    input: float
    cached_input: float
    output: float


PRICES: Final[dict[str, ModelPrice]] = {
    "gpt-6-luna": ModelPrice(input=0.10, cached_input=0.01, output=0.50),
    "gpt-6.1-sol": ModelPrice(input=2.00, cached_input=0.10, output=10.00),
}

# A dated snapshot ("gpt-6-luna-2026-09-22") is priced as its model.
_SNAPSHOT = re.compile(r"-\d{4}-\d{2}-\d{2}$")
_unpriced_logged: set[str] = set()


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def __add__(self, other: "ModelUsage") -> "ModelUsage":
        return ModelUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )


def price_of(model: str) -> ModelPrice | None:
    return PRICES.get(model) or PRICES.get(_SNAPSHOT.sub("", model))


def estimate_cost(model: str, usage: ModelUsage) -> float | None:
    """The request's estimated cost in USD; None for a model without a price."""
    price = price_of(model)
    if price is None:
        if model not in _unpriced_logged:
            _unpriced_logged.add(model)
            logger.warning("no price for model %s; its cost is not estimated", model)
        return None
    cached = min(usage.cached_input_tokens, usage.input_tokens)
    uncached = usage.input_tokens - cached
    return (
        uncached * price.input + cached * price.cached_input + usage.output_tokens * price.output
    ) / 1_000_000


class UsageMeter:
    """Running totals for one run, also added to every enclosing run's meter."""

    def __init__(self, parent: "UsageMeter | None" = None) -> None:
        self.parent = parent
        # Totals: this run's requests and those of the runs nested in it.
        self.model_calls = 0
        self.unpriced_calls = 0
        self.usage = ModelUsage()
        self.cost_usd = 0.0
        # This run's own requests only.
        self.own = UsageTotals()

    def record(self, model: str, usage: ModelUsage) -> float | None:
        """Add one model request; returns its estimated cost."""
        cost = estimate_cost(model, usage)
        self.own.add(usage, cost)
        meter: UsageMeter | None = self
        while meter is not None:
            meter.model_calls += 1
            meter.usage = meter.usage + usage
            if cost is None:
                meter.unpriced_calls += 1
            else:
                meter.cost_usd += cost
            meter = meter.parent
        return cost

    def log_fields(self) -> dict[str, object]:
        """The totals, nested runs included, as log fields (see `_fields`)."""
        return _fields(self.model_calls, self.unpriced_calls, self.usage, self.cost_usd)


@dataclass(slots=True)
class UsageTotals:
    model_calls: int = 0
    unpriced_calls: int = 0
    usage: ModelUsage = ModelUsage()
    cost_usd: float = 0.0

    def add(self, usage: ModelUsage, cost: float | None) -> None:
        self.model_calls += 1
        self.usage = self.usage + usage
        if cost is None:
            self.unpriced_calls += 1
        else:
            self.cost_usd += cost

    def log_fields(self) -> dict[str, object]:
        return _fields(self.model_calls, self.unpriced_calls, self.usage, self.cost_usd)


def _fields(calls: int, unpriced: int, usage: ModelUsage, cost: float) -> dict[str, object]:
    """Usage as log fields. `cost_usd` covers the priced calls only and is None
    when no call had a price; `unpriced_calls` says how many had none."""
    fields: dict[str, object] = {
        "model_calls": calls,
        "input_tokens": usage.input_tokens,
        "cached_input_tokens": usage.cached_input_tokens,
        "output_tokens": usage.output_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "cost_usd": round(cost, 6) if calls > unpriced or calls == 0 else None,
    }
    if unpriced:
        fields["unpriced_calls"] = unpriced
    return fields
