"""Typed tools the agent runner can offer to a model.

A tool is a plain async function over our own types: it takes the run's
`ToolContext` and a validated Pydantic input, and returns a Pydantic output.
Nothing here knows about any LLM provider.

Every tool declares a `kind`:

- `read`  - no side effects. The runner executes it freely.
- `draft` - creates a draft record that a person reviews later. Allowed.
- `act`   - sends, accepts, pays, or otherwise commits. The runner NEVER
  executes it. It emits `ApprovalRequested` and the run ends with that
  proposal; execution happens elsewhere, after a person approves.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, JsonValue

from bido_back.agents.context import ToolContext

ToolKind = Literal["read", "draft", "act"]

# Reserved for the runner's final-output tool; see runner.py.
SUBMIT_OUTPUT_TOOL = "submit_output"


class ToolError(Exception):
    """A failure whose message is safe to show the model.

    Raise this from a handler when the model can do something about it
    ("no supplier with that id"). Any other exception is logged and reported
    to the model as a generic internal error, so internals never leak into
    the conversation.
    """


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

    def __post_init__(self) -> None:
        if not self.name or self.name == SUBMIT_OUTPUT_TOOL:
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


# Heterogeneous tool lists hold tools of different input/output types.
AnyTool = Tool[Any, Any]


def json_schema(model: type[BaseModel]) -> dict[str, JsonValue]:
    schema: dict[str, JsonValue] = model.model_json_schema()
    # The tool name already says what this is; the class name is noise.
    schema.pop("title", None)
    return schema
