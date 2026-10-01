"""The LLM seam: neutral request/response types plus the Anthropic adapter.

Everything outside this module speaks the neutral types below. Anthropic SDK
types are imported and used ONLY inside `AnthropicLlmClient`, so swapping the
provider (or putting a framework behind the runner) touches one file.

Reasoning blocks are carried through verbatim (text + signature, or redacted
data). Current Claude models require thinking blocks to be echoed back
unchanged when a tool loop continues, and the runner keeps the history
append-only, so do not rewrite or drop `ReasoningPart`s between turns.
"""

import logging
from collections.abc import Sequence
from typing import Annotated, Literal, Protocol

import anthropic
from anthropic.types import (
    Message as AnthropicMessage,
)
from anthropic.types import (
    MessageParam,
    OutputConfigParam,
    RedactedThinkingBlockParam,
    TextBlockParam,
    ThinkingBlockParam,
    ThinkingConfigParam,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlockParam,
)
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

logger = logging.getLogger(__name__)


# --- Neutral message types -------------------------------------------------


class _Part(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TextPart(_Part):
    type: Literal["text"] = "text"
    text: str


class ReasoningPart(_Part):
    """Model reasoning. Opaque to us: round-trip it exactly as received."""

    type: Literal["reasoning"] = "reasoning"
    text: str = ""
    signature: str | None = None
    # Set instead of text when the provider returned encrypted reasoning.
    redacted_data: str | None = None


class ToolCallPart(_Part):
    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    args: dict[str, JsonValue]


class ToolResultPart(_Part):
    type: Literal["tool_result"] = "tool_result"
    call_id: str
    content: str
    is_error: bool = False


class UserMessage(_Part):
    role: Literal["user"] = "user"
    content: list[Annotated[TextPart | ToolResultPart, Field(discriminator="type")]]


class AssistantMessage(_Part):
    role: Literal["assistant"] = "assistant"
    content: list[Annotated[TextPart | ReasoningPart | ToolCallPart, Field(discriminator="type")]]


Message = Annotated[UserMessage | AssistantMessage, Field(discriminator="role")]


class ToolSpec(_Part):
    name: str
    description: str
    input_schema: dict[str, JsonValue]


class Usage(_Part):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


# end_turn: done talking. tool_use: wants tools run. max_tokens: truncated.
# refusal: the provider declined. other: anything we do not act on.
StopReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "other"]


class LlmRequest(_Part):
    model: str
    system: str
    messages: Sequence[Message]
    tools: Sequence[ToolSpec] = ()
    max_tokens: int = 16_000
    thinking: bool = True
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None


class LlmResponse(_Part):
    message: AssistantMessage
    stop_reason: StopReason
    usage: Usage = Usage()

    @property
    def tool_calls(self) -> list[ToolCallPart]:
        return [p for p in self.message.content if isinstance(p, ToolCallPart)]


class LlmError(Exception):
    """A provider call failed. `retryable` is a hint for the caller (the
    runner itself does not retry; the SDK already retried transient errors)."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class LlmClient(Protocol):
    async def complete(self, request: LlmRequest) -> LlmResponse: ...


# --- Anthropic adapter -----------------------------------------------------

_ARGS = TypeAdapter(dict[str, JsonValue])


class AnthropicLlmClient:
    """`LlmClient` over the Anthropic Messages API (non-streaming)."""

    def __init__(
        self,
        client: anthropic.AsyncAnthropic | None = None,
        *,
        api_key: str | None = None,
        timeout: float = 120.0,
        max_retries: int = 2,
    ) -> None:
        # api_key=None lets the SDK resolve credentials from the environment.
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout, max_retries=max_retries
        )

    async def complete(self, request: LlmRequest) -> LlmResponse:
        thinking: ThinkingConfigParam | anthropic.Omit = (
            {"type": "adaptive", "display": "summarized"} if request.thinking else anthropic.omit
        )
        output_config: OutputConfigParam | anthropic.Omit = (
            {"effort": request.effort} if request.effort else anthropic.omit
        )
        try:
            response = await self._client.messages.create(
                model=request.model,
                max_tokens=request.max_tokens,
                system=request.system,
                messages=[_to_message_param(m) for m in request.messages],
                tools=[_to_tool_param(t) for t in request.tools],
                thinking=thinking,
                output_config=output_config,
            )
        except anthropic.RateLimitError as exc:
            raise LlmError("rate_limited", str(exc), retryable=True) from exc
        except anthropic.APIStatusError as exc:
            raise LlmError(
                f"http_{exc.status_code}", str(exc), retryable=exc.status_code >= 500
            ) from exc
        except anthropic.APIConnectionError as exc:  # includes timeouts
            raise LlmError("connection_error", str(exc), retryable=True) from exc
        return _from_response(response)


def _to_tool_param(spec: ToolSpec) -> ToolParam:
    schema: dict[str, object] = dict(spec.input_schema)
    return {"name": spec.name, "description": spec.description, "input_schema": schema}


def _to_message_param(message: UserMessage | AssistantMessage) -> MessageParam:
    if isinstance(message, UserMessage):
        user_blocks: list[TextBlockParam | ToolResultBlockParam] = []
        for part in message.content:
            if isinstance(part, TextPart):
                user_blocks.append({"type": "text", "text": part.text})
            else:
                user_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": part.call_id,
                        "content": part.content,
                        "is_error": part.is_error,
                    }
                )
        return {"role": "user", "content": user_blocks}

    blocks: list[
        TextBlockParam | ThinkingBlockParam | RedactedThinkingBlockParam | ToolUseBlockParam
    ] = []
    for part in message.content:
        if isinstance(part, TextPart):
            blocks.append({"type": "text", "text": part.text})
        elif isinstance(part, ToolCallPart):
            args: dict[str, object] = dict(part.args)
            blocks.append({"type": "tool_use", "id": part.id, "name": part.name, "input": args})
        elif part.redacted_data is not None:
            blocks.append({"type": "redacted_thinking", "data": part.redacted_data})
        elif part.signature is not None:
            blocks.append({"type": "thinking", "thinking": part.text, "signature": part.signature})
        # A reasoning part without a signature did not come from this provider
        # and cannot be replayed; it is dropped.
    return {"role": "assistant", "content": blocks}


def _from_response(response: AnthropicMessage) -> LlmResponse:
    parts: list[TextPart | ReasoningPart | ToolCallPart] = []
    for block in response.content:
        if block.type == "text":
            parts.append(TextPart(text=block.text))
        elif block.type == "thinking":
            parts.append(ReasoningPart(text=block.thinking, signature=block.signature))
        elif block.type == "redacted_thinking":
            parts.append(ReasoningPart(redacted_data=block.data))
        elif block.type == "tool_use":
            parts.append(
                ToolCallPart(id=block.id, name=block.name, args=_ARGS.validate_python(block.input))
            )
        else:
            # Server-tool blocks: we do not enable server tools, so this is unexpected.
            logger.warning("ignoring unsupported content block type %s", block.type)

    stop: StopReason
    match response.stop_reason:
        case "end_turn" | "stop_sequence":
            stop = "end_turn"
        case "tool_use":
            stop = "tool_use"
        case "max_tokens" | "model_context_window_exceeded":
            stop = "max_tokens"
        case "refusal":
            stop = "refusal"
        case _:
            stop = "other"

    usage = response.usage
    return LlmResponse(
        message=AssistantMessage(content=parts),
        stop_reason=stop,
        usage=Usage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens or 0,
            cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
        ),
    )
