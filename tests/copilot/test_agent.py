"""The copilot definition: prompt, tool set, and schemas OpenAI accepts."""

import json

from sqlalchemy.ext.asyncio import create_async_engine

from bido_back.agents.catalog.definitions import DEFAULT_MODEL
from bido_back.copilot.agent import copilot_agent
from bido_back.copilot.tools import CopilotToolDeps, copilot_tools
from tests.rfqs.conftest import FakeMailboxes, fake_agents

EXPECTED_TOOLS = {
    "list_rfqs": "read",
    "get_rfq_status": "read",
    "who_responded": "read",
    "what_is_missing": "read",
    "list_proposals": "read",
    "get_comparison": "read",
    "price_history": "read",
    "suggest_suppliers": "read",
    "get_rfq_template": "read",
    "update_rfq_template": "draft",
    "create_rfq": "draft",
    "invite_suppliers": "draft",
    "prepare_send": "draft",
    "get_negotiation_status": "read",
    "propose_counteroffers": "draft",
}


def test_definition_and_tools() -> None:
    engine = create_async_engine("postgresql+asyncpg://app@localhost:5432/unused")
    tools = copilot_tools(
        CopilotToolDeps(db=engine, agents=fake_agents(), mailboxes=FakeMailboxes())
    )
    agent = copilot_agent(tools)

    assert agent.model == DEFAULT_MODEL
    assert len(agent.load_prompt()) > 500
    assert {tool.name: tool.kind for tool in agent.tools} == EXPECTED_TOOLS
    assert agent.history_runs > 0
    # OpenAI's strict structured output rejects regex lookarounds.
    assert "(?" not in json.dumps(agent.output_model.model_json_schema())
    for tool in agent.tools:
        assert "(?" not in json.dumps(tool.input_schema()), tool.name
