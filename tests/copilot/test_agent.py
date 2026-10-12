"""The copilot definition: prompt, tool set, and schemas OpenAI accepts."""

import json
from datetime import date
from pathlib import Path
from typing import Any, cast

from sqlalchemy.ext.asyncio import create_async_engine

from bido_back.agents.catalog.definitions import DEFAULT_MODEL
from bido_back.agents.definition import HandoffCall
from bido_back.copilot.agent import (
    ANALYSIS_REPLY_EFFORT,
    ANALYSIS_REPLY_MODEL,
    ANALYSIS_TOOLS,
    AnalysisReplyInput,
    analysis_handoff,
    copilot_agent,
)
from bido_back.copilot.model_views import assessment_view, cell_free_view
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
    "register_quote": "draft",
    "prepare_send": "draft",
    "get_negotiation_status": "read",
    "propose_counteroffers": "draft",
    "get_award_status": "read",
    "list_open_pos": "read",
    "prepare_award": "draft",
    "list_purchase_orders": "read",
    "send_purchase_order": "draft",
    "export_procurement_plan": "draft",
    "procurement_plan_lines": "read",
    "acknowledge_purchase_order": "draft",
    "receive_purchase_order": "draft",
    "close_purchase_order": "draft",
    "cancel_purchase_order": "draft",
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
    "answer_supplier_question": "draft",
    "reopen_supplier_question": "draft",
    "use_current_offer": "draft",
    "release_negotiation_hold": "draft",
}


def test_rfq_titles_are_asked_for_in_the_buyers_language() -> None:
    # RFQ-3 (2026-10-08): a Portuguese conversation got an English RFQ title. The title goes
    # on the RFQ and in supplier emails, so every field the title is written from says so.
    engine = create_async_engine("postgresql+asyncpg://app@localhost:5432/unused")
    tools = {
        tool.name: tool
        for tool in copilot_tools(
            CopilotToolDeps(db=engine, agents=fake_agents(), mailboxes=FakeMailboxes())
        )
    }

    for name, field in (
        ("create_rfq", "text"),
        ("create_rfq_from_assessment", "title"),
        ("set_rfq_terms", "title"),
    ):
        properties = cast(dict[str, dict[str, Any]], tools[name].input_schema()["properties"])
        assert "buyer's language" in str(properties[field]["description"]), (name, field)


def test_definition_and_tools() -> None:
    engine = create_async_engine("postgresql+asyncpg://app@localhost:5432/unused")
    tools = copilot_tools(
        CopilotToolDeps(db=engine, agents=fake_agents(), mailboxes=FakeMailboxes())
    )
    agent = copilot_agent(tools)

    assert agent.model == DEFAULT_MODEL
    assert (agent.prompt_file, agent.prompt_version) == ("copilot.v35.md", 35)
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


def test_the_model_reads_tool_outputs_without_cell_references() -> None:
    engine = create_async_engine("postgresql+asyncpg://app@localhost:5432/unused")
    agent = copilot_agent(
        copilot_tools(CopilotToolDeps(db=engine, agents=fake_agents(), mailboxes=FakeMailboxes()))
    )

    views = {tool.name: tool.model_view for tool in agent.tools}
    for name in (
        "analyze_procurement_sources",
        "save_procurement_assessment",
        "get_procurement_assessment",
    ):
        assert views[name] is assessment_view, name
    assert views["get_comparison"] is cell_free_view
    assert all(view is not None for view in views.values())


async def test_analysis_turns_hand_their_reply_to_the_writer_on_the_stronger_model() -> None:
    engine = create_async_engine("postgresql+asyncpg://app@localhost:5432/unused")
    tools = copilot_tools(
        CopilotToolDeps(db=engine, agents=fake_agents(), mailboxes=FakeMailboxes())
    )

    async def earlier() -> list[str]:
        return ["analisa o motor"]

    handoff = analysis_handoff("analisa o motor", date(2026, 10, 9), ["mb52.xlsx"], earlier)
    agent = copilot_agent(tools, handoff=handoff)

    # Luna stays the Copilot; only the analysis tools hand the reply off.
    assert agent.model == DEFAULT_MODEL
    assert agent.handoff is not None
    assert agent.handoff.tools == ANALYSIS_TOOLS
    assert ANALYSIS_TOOLS <= {tool.name for tool in agent.tools}
    call = HandoffCall(tool="analyze_procurement_sources", args={"objective": "x"}, output={"a": 1})
    writer, writer_input = await agent.handoff.writer([call])
    assert (writer.model, writer.effort) == (ANALYSIS_REPLY_MODEL, ANALYSIS_REPLY_EFFORT)
    assert (writer.prompt_file, writer.prompt_version) == ("analysis_reply_writer.v3.md", 3)
    assert writer.model != agent.model
    assert writer.tools == () and writer.history_runs == 0 and writer.stream_text
    assert isinstance(writer_input, AnalysisReplyInput)
    assert writer_input.buyer_messages == ["analisa o motor"]
    assert writer_input.tool_calls[0].output == {"a": 1}
    # The writer's own rules first, then the Copilot's presentation rules, whole.
    prompt = writer.load_prompt()
    assert prompt.startswith("You write Bido's reply to the buyer")
    assert prompt.endswith(agent.load_prompt())


def test_without_analysis_tools_there_is_no_handoff() -> None:
    async def earlier() -> list[str]:
        return []

    handoff = analysis_handoff("oi", date(2026, 10, 9), [], earlier)

    assert copilot_agent((), handoff=handoff).handoff is None
