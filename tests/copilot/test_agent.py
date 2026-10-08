"""The copilot definition: prompt, tool set, and schemas OpenAI accepts."""

import json
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

from bido_back.agents.catalog.definitions import DEFAULT_MODEL
from bido_back.copilot.agent import copilot_agent
from bido_back.copilot.tools import CopilotToolDeps, copilot_tools
from tests.rfqs.conftest import FakeMailboxes, fake_agents

PROMPTS = Path(__file__).parents[2] / "prompts"

EXPECTED_TOOLS = {
    "list_rfqs": "read",
    "get_rfq_status": "read",
    "who_responded": "read",
    "what_is_missing": "read",
    "list_proposals": "read",
    "get_comparison": "read",
    "price_history": "read",
    "list_suppliers": "read",
    "purchase_history": "read",
    "suggest_suppliers": "read",
    "get_rfq_template": "read",
    "update_rfq_template": "draft",
    "run_purchase_analysis": "draft",
    "create_rfq_from_analysis": "draft",
    "create_rfq": "draft",
    "update_rfq_line": "draft",
    "add_rfq_line": "draft",
    "remove_rfq_line": "draft",
    "set_rfq_terms": "draft",
    "invite_suppliers": "draft",
    "prepare_send": "draft",
    "get_negotiation_status": "read",
    "propose_counteroffers": "draft",
    "get_award_status": "read",
    "list_open_pos": "read",
    "prepare_award": "draft",
    "import_spreadsheet": "draft",
    "analyze_procurement_sources": "read",
    "save_procurement_assessment": "draft",
    "get_procurement_assessment": "read",
    "list_procurement_assessments": "read",
    "create_rfq_from_assessment": "draft",
    "set_comparison_policy": "draft",
    "add_supplier_contact": "draft",
    "refine_analysis": "draft",
    "propose_negotiation_mandate": "draft",
    "pause_negotiation": "draft",
    "resume_negotiation": "draft",
    "close_negotiation": "draft",
    "get_negotiation_outcome": "read",
    "list_extra_offers": "read",
}


def test_definition_and_tools() -> None:
    engine = create_async_engine("postgresql+asyncpg://app@localhost:5432/unused")
    tools = copilot_tools(
        CopilotToolDeps(db=engine, agents=fake_agents(), mailboxes=FakeMailboxes())
    )
    agent = copilot_agent(tools)

    assert agent.model == DEFAULT_MODEL
    assert (agent.prompt_file, agent.prompt_version) == ("copilot.v22.md", 22)
    # The copilot runs its newest prompt: a new version is never left unused.
    newest = max(int(p.stem.removeprefix("copilot.v")) for p in PROMPTS.glob("copilot.v*.md"))
    assert agent.prompt_version == newest
    assert len(agent.load_prompt()) > 500
    assert {tool.name: tool.kind for tool in agent.tools} == EXPECTED_TOOLS
    assert agent.history_runs > 0
    # OpenAI's strict structured output rejects regex lookarounds.
    assert "(?" not in json.dumps(agent.output_model.model_json_schema())
    for tool in agent.tools:
        assert "(?" not in json.dumps(tool.input_schema()), tool.name
