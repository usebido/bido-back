"""Typed tools the agent runner can offer to a model.

A tool is a plain async function over our own types: it takes the run's
`ToolContext` and a validated Pydantic input, and returns a Pydantic output.
Nothing here knows about any LLM provider or agent framework.

Every tool declares a `kind`:

- `read`  - no side effects. The runner executes it freely.
- `draft` - creates a draft record that a person reviews later. Allowed.
- `act`   - sends, accepts, pays, or otherwise commits. The runner NEVER
  executes it. It emits `ApprovalRequested` and the run ends with that
  proposal; execution happens elsewhere, after a person approves.
"""

import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, JsonValue

from bido_back.agents.context import ToolContext

ToolKind = Literal["read", "draft", "act"]

# What model providers accept as a tool name.
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Valid values a validation error lists for the model, at most.
MAX_VALID_VALUES = 30


class ToolError(Exception):
    """A failure whose message is safe to show the model.

    Raise this from a handler when the model can do something about it
    ("no supplier with that id"). Any other exception is logged and reported
    to the model as a generic internal error, so internals never leak into
    the conversation.
    """


class ToolValidationError(ToolError):
    """An argument that does not match the data the tool works on, with what would.

    Raise it when the model's parameter names something the data does not hold (a
    plant, a product, a column value) and the data says what it holds instead. The
    runner feeds `payload()` back to the model as JSON and lets it correct the
    parameters and call again, a bounded number of times, before it must ask the
    buyer. An empty result is never how an invalid parameter surfaces.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        field: str,
        value: str | None,
        valid_values: Sequence[str],
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.value = value
        self.valid_values = list(valid_values)[:MAX_VALID_VALUES]
        self.valid_values_left_out = max(0, len(valid_values) - MAX_VALID_VALUES)

    def payload(self) -> dict[str, JsonValue]:
        found: dict[str, JsonValue] = {
            "error": "validation_error",
            "code": self.code,
            "field": self.field,
            "value": self.value,
            "valid_values": list(self.valid_values),
            "message": str(self),
        }
        if self.valid_values_left_out:
            found["valid_values_left_out"] = self.valid_values_left_out
        return found


@dataclass(frozen=True, slots=True)
class Tool[InT: BaseModel, OutT: BaseModel]:
    name: str
    description: str
    input_model: type[InT]
    output_model: type[OutT]
    kind: ToolKind
    handler: Callable[[ToolContext, InT], Awaitable[OutT]]
    # When set, every successful output is also emitted as an Artifact event
    # of this kind, so the frontend can render it as a card.
    artifact_kind: str | None = None
    # Which successful outputs make that card (the JSON output); None = all. A
    # proposal awaiting the buyer's confirmation, for one, is not an RFQ yet.
    artifact_if: Callable[[Mapping[str, JsonValue]], bool] | None = None
    # What the model reads of a successful output; None = the whole output. Only
    # the model's context changes (and the history that replays it): the
    # ToolResult event, the artifact and traces keep the whole output.
    model_view: Callable[[OutT], JsonValue] | None = None

    def __post_init__(self) -> None:
        if not _TOOL_NAME.match(self.name):
            raise ValueError(f"invalid tool name: {self.name!r}")

    def input_schema(self) -> dict[str, JsonValue]:
        """The JSON schema the model sees for this tool's arguments."""
        return json_schema(self.input_model)

    def parse_input(self, args: Mapping[str, object]) -> InT:
        """Validate model-supplied arguments. Raises pydantic.ValidationError."""
        return self.input_model.model_validate(dict(args))

    async def invoke(self, ctx: ToolContext, args: InT) -> OutT:
        output = await self.handler(ctx, args)
        if not isinstance(output, self.output_model):
            raise TypeError(
                f"tool {self.name!r} returned {type(output).__name__}, "
                f"expected {self.output_model.__name__}"
            )
        return output

    def for_model(self, output: OutT) -> str:
        """The JSON text the model reads for a successful output."""
        if self.model_view is None:
            return output.model_dump_json()
        return json.dumps(self.model_view(output), ensure_ascii=False, separators=(",", ":"))


# Heterogeneous tool lists hold tools of different input/output types.
AnyTool = Tool[Any, Any]


def json_schema(model: type[BaseModel]) -> dict[str, JsonValue]:
    schema: dict[str, JsonValue] = model.model_json_schema()
    # The tool name already says what this is; the class name is noise.
    schema.pop("title", None)
    return schema
