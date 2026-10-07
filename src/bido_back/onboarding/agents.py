"""The two onboarding agents, behind a seam tests can replace.

`OnboardingAgents` holds one callable per agent: input contract in, output
contract out. `default_onboarding_agents()` runs the real definitions
(`RFQ_CANDIDATE_CLASSIFIER`, `RFQ_TEMPLATE_BUILDER`) through
`runner.run_to_completion`, imported on first use so the api does not load the
agent framework. Neither agent has tools; a run that does not complete raises
`AgentFailed`.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic import BaseModel

from bido_back.agents.catalog.contracts import (
    RfqCandidateClassifierInput,
    RfqCandidateClassifierOutput,
    RfqTemplateBuilderInput,
    RfqTemplateBuilderOutput,
)
from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.agents.definition import AgentDefinition

logger = logging.getLogger(__name__)

ClassifyAgent = Callable[
    [RfqCandidateClassifierInput, ToolContext], Awaitable[RfqCandidateClassifierOutput]
]
BuildAgent = Callable[[RfqTemplateBuilderInput, ToolContext], Awaitable[RfqTemplateBuilderOutput]]


class AgentFailed(Exception):
    """An onboarding agent run ended without a usable output. The message is a code."""


@dataclass(frozen=True, slots=True)
class OnboardingAgents:
    classify: ClassifyAgent
    build: BuildAgent


async def _run[OutT: BaseModel](
    agent: AgentDefinition[OutT], input: BaseModel, context: ToolContext, config: RunnerConfig
) -> OutT:
    from bido_back.agents.runner import Completed, Failed, run_to_completion

    result = await run_to_completion(agent, input, context, config=config)
    if isinstance(result, Completed):
        return result.output
    code = result.failure.code if isinstance(result, Failed) else "unexpected_approval"
    logger.warning("agent %s run %s failed: %s", agent.name, context.run_id, code)
    raise AgentFailed(code)


def default_onboarding_agents(config: RunnerConfig) -> OnboardingAgents:
    from bido_back.agents.catalog import RFQ_CANDIDATE_CLASSIFIER, RFQ_TEMPLATE_BUILDER

    async def classify(
        input: RfqCandidateClassifierInput, context: ToolContext
    ) -> RfqCandidateClassifierOutput:
        return await _run(RFQ_CANDIDATE_CLASSIFIER, input, context, config)

    async def build(
        input: RfqTemplateBuilderInput, context: ToolContext
    ) -> RfqTemplateBuilderOutput:
        return await _run(RFQ_TEMPLATE_BUILDER, input, context, config)

    return OnboardingAgents(classify=classify, build=build)
