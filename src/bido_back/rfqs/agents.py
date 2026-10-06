"""The two agents the RFQ services call, behind a seam tests can replace.

`RfqAgents` holds one callable per agent: input contract in, output contract
out (see `bido_back.agents.catalog.contracts`). `default_rfq_agents()` runs
the real definitions (`bido_back.agents.catalog.definitions.RFQ_INTAKE` and
`RFQ_EMAIL_WRITER`) through `runner.run_to_completion`. The definitions and
the runner (and so Agno) are imported on first use, so importing the api
does not load the agent framework.

Neither agent has an `act` tool: a run that ends in an approval request is
treated as a failure. The send approval is recorded by the RFQ service from
the writer's output, never by the model.
"""

import importlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from pydantic import BaseModel

from bido_back.agents.catalog.contracts import (
    RfqEmailInput,
    RfqEmailOutput,
    RfqIntakeInput,
    RfqIntakeOutput,
)
from bido_back.agents.context import ToolContext
from bido_back.agents.definition import AgentDefinition
from bido_back.rfqs.errors import AgentFailed

logger = logging.getLogger(__name__)

DEFINITIONS_MODULE = "bido_back.agents.catalog.definitions"

IntakeAgent = Callable[[RfqIntakeInput, ToolContext], Awaitable[RfqIntakeOutput]]
EmailWriterAgent = Callable[[RfqEmailInput, ToolContext], Awaitable[RfqEmailOutput]]


@dataclass(frozen=True, slots=True)
class RfqAgents:
    intake: IntakeAgent
    email_writer: EmailWriterAgent


def _definition[OutT: BaseModel](name: str, output_model: type[OutT]) -> AgentDefinition[OutT]:
    module = importlib.import_module(DEFINITIONS_MODULE)
    found: object = getattr(module, name, None)
    if not isinstance(found, AgentDefinition):
        raise RuntimeError(f"{DEFINITIONS_MODULE}.{name} is not an AgentDefinition")
    agent = cast(AgentDefinition[OutT], found)
    if agent.output_model is not output_model:
        raise RuntimeError(f"{name} does not produce {output_model.__name__}")
    return agent


async def _run[OutT: BaseModel](
    name: str, output_model: type[OutT], input: BaseModel, context: ToolContext
) -> OutT:
    from bido_back.agents.runner import Completed, Failed, run_to_completion

    result = await run_to_completion(_definition(name, output_model), input, context)
    if isinstance(result, Completed):
        return result.output
    code = result.failure.code if isinstance(result, Failed) else "unexpected_approval"
    logger.error("agent %s run %s failed: %s", name, context.run_id, code)
    raise AgentFailed(f"agent {name} failed ({code})")


async def run_intake(input: RfqIntakeInput, context: ToolContext) -> RfqIntakeOutput:
    return await _run("RFQ_INTAKE", RfqIntakeOutput, input, context)


async def run_email_writer(input: RfqEmailInput, context: ToolContext) -> RfqEmailOutput:
    return await _run("RFQ_EMAIL_WRITER", RfqEmailOutput, input, context)


def default_rfq_agents() -> RfqAgents:
    return RfqAgents(intake=run_intake, email_writer=run_email_writer)
