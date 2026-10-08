"""Conversation titles: refined over the first turns, milestones, and the buyer's rename."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.config import RunnerConfig
from bido_back.agents.context import ToolContext
from bido_back.auth import Principal
from bido_back.copilot.titles import (
    MAX_TITLED_TURNS,
    ConversationTitlerInput,
    ConversationTitlerOutput,
    TitlerFailed,
    clean_title,
    differs,
    milestone,
    stored_title,
)
from bido_back.db import tenant_session
from bido_back.db.models import CopilotConversation
from tests.agents.test_runner import Script, reply, script, text, tool_use
from tests.approvals.conftest import People
from tests.db.conftest import agno_engine

from .conftest import buyer_of, other_buyer_of
from .test_api import Api, api, parse_sse

__all__ = ["agno_engine", "api", "script"]


# --- Pure helpers --------------------------------------------------------------------------


def test_clean_title_drops_quotes_emails_and_the_final_period() -> None:
    assert clean_title('"Cotação de rolamentos 6204."') == "Cotação de rolamentos 6204"
    assert clean_title("Compra com ana@fornecedor.com.br") == "Compra com"
    assert clean_title("  \n  ") is None
    long = "Compra de chapas de aço carbono e fixadores para a linha Alpha da planta BR01"
    cut = clean_title(long)
    assert cut is not None and len(cut) <= 60 and long.startswith(cut) and not cut.endswith(" ")


def test_stored_title_is_one_line_and_bounded() -> None:
    assert stored_title("RFQ-3 ·\n  Necessidades   BR01") == "RFQ-3 · Necessidades BR01"
    assert len(stored_title("x" * 300)) == 120


def test_differs_ignores_case_accents_and_tiny_edits() -> None:
    assert not differs("Cotação de rolamentos", "cotacao de rolamentos")
    assert not differs("Compra de aço e fixadores", "Compra de aço e fixadores.")
    assert differs("oi", "Cotação de rolamentos 6204")


def test_milestones_prefer_award_then_rfq_then_analysis() -> None:
    analysis = ("run_purchase_analysis", True, {"product": "MT-120"})
    rfq = ("create_rfq", True, {"ref": "RFQ-3", "title": "Necessidades BR01"})
    award = ("prepare_award", True, {"ref": "RFQ-3"})
    failed = ("create_rfq", False, {"ref": "RFQ-9", "title": "x"})

    found = milestone([analysis], language="pt", base_title="oi")
    assert found is not None and (found.kind, found.title) == (
        "analysis",
        "Análise de compra · MT-120",
    )
    found = milestone([analysis, rfq], language="pt", base_title="oi")
    assert found is not None and (found.kind, found.title) == ("rfq", "RFQ-3 · Necessidades BR01")
    found = milestone([rfq, award], language="en", base_title="oi")
    assert found is not None and (found.kind, found.title) == ("award", "RFQ-3 · Award")
    assert milestone([failed], language="pt", base_title="oi") is None
    # An unknown language labels in English.
    found = milestone([analysis], language="ja", base_title="oi")
    assert found is not None and found.title == "Purchase analysis · MT-120"


# --- Over HTTP ------------------------------------------------------------------------------


@dataclass
class FakeTitler:
    """Answers with the next scripted proposal and records what it read."""

    proposals: list[ConversationTitlerOutput | Exception]
    seen: list[ConversationTitlerInput] = field(default_factory=list[ConversationTitlerInput])

    async def __call__(
        self, input: ConversationTitlerInput, context: ToolContext
    ) -> ConversationTitlerOutput:
        self.seen.append(input)
        proposal = self.proposals[min(len(self.seen), len(self.proposals)) - 1]
        if isinstance(proposal, Exception):
            raise proposal
        return proposal


def proposal(title: str, settled: bool, language: str = "pt") -> ConversationTitlerOutput:
    return ConversationTitlerOutput(title=title, settled=settled, language=language)


def with_storage(api: Api, agno: AsyncEngine) -> None:
    api.runner_config = RunnerConfig(
        openai_api_key="test-not-a-real-key",
        agno_database_url=agno.url.render_as_string(hide_password=False),
    )


async def send(api: Api, conversation: str, message: str) -> list[dict[str, object]]:
    response = await api.http.post(
        "/v1/copilot/messages", json={"message": message, "conversation_id": conversation}
    )
    assert response.status_code == 200, response.text
    events = parse_sse(response.text)
    assert events[-1]["type"] == "done" and events[-1]["ok"] is True
    return events


def titles(events: Sequence[dict[str, object]]) -> list[object]:
    return [e["title"] for e in events if e["type"] == "title"]


async def stored(
    engine: AsyncEngine, principal: Principal, conversation: str
) -> CopilotConversation:
    async with tenant_session(engine, principal.tenant_id) as session:
        row = await session.scalar(
            select(CopilotConversation).where(CopilotConversation.id == conversation)
        )
    assert row is not None
    return row


@pytest.mark.db
async def test_a_vague_opener_is_refined_on_the_next_turn_then_stops(
    api: Api,
    app_engine: AsyncEngine,
    agno_engine: AsyncEngine,
    copilot_rows: People,
    script: Script,
) -> None:
    with_storage(api, agno_engine)
    titler = FakeTitler(
        [
            proposal("Conversa sobre compras", settled=False),
            proposal("Cotação de rolamentos 6204", settled=True),
        ]
    )
    api.titler = titler
    conversation = str(uuid4())
    script(
        reply(text("Olá! Em que posso ajudar?")),
        reply(text("Certo, vamos cotar os rolamentos 6204.")),
        reply(text("Os rolamentos 6204 são para a linha Alpha.")),
    )

    first = await send(api, conversation, "oi")
    # The title comes after the answer and before `done`.
    types = [e["type"] for e in first]
    assert types.index("answer") < types.index("title") == len(types) - 2
    assert titles(first) == ["Conversa sobre compras"]
    row = await stored(app_engine, api.principal, conversation)
    assert (row.title_source, row.title_settled, row.title_language) == ("ai", False, "pt")

    second = await send(api, conversation, "preciso cotar 2.600 rolamentos 6204")
    assert titles(second) == ["Cotação de rolamentos 6204"]
    # The titler read the whole conversation so far, the earlier turn from storage.
    assert [t.message for t in titler.seen[1].turns] == [
        "oi",
        "preciso cotar 2.600 rolamentos 6204",
    ]
    assert titler.seen[1].current_title == "Conversa sobre compras"

    third = await send(api, conversation, "para que linha são?")
    assert titles(third) == []
    assert len(titler.seen) == 2
    row = await stored(app_engine, api.principal, conversation)
    assert (row.title, row.title_settled) == ("Cotação de rolamentos 6204", True)

    listed = (await api.http.get("/v1/copilot/conversations")).json()["items"]
    assert [(i["title"], i["title_source"]) for i in listed] == [
        ("Cotação de rolamentos 6204", "ai")
    ]


@pytest.mark.db
async def test_the_titler_stops_after_five_turns(
    api: Api, app_engine: AsyncEngine, copilot_rows: People, script: Script
) -> None:
    subjects = [
        "Ajuda geral",
        "Dúvida sobre estoque",
        "Fornecedores de aço",
        "Prazo de entrega",
        "Condições de pagamento",
        "Frete internacional",
        "Embalagem",
    ]
    titler = FakeTitler([proposal(subject, False) for subject in subjects])
    api.titler = titler
    conversation = str(uuid4())
    script(*[reply(text("Pode explicar melhor?")) for _ in range(MAX_TITLED_TURNS + 2)])

    for n in range(MAX_TITLED_TURNS + 2):
        await send(api, conversation, f"mensagem {n}")

    assert len(titler.seen) == MAX_TITLED_TURNS
    row = await stored(app_engine, api.principal, conversation)
    assert row.title == subjects[MAX_TITLED_TURNS - 1]


@pytest.mark.db
async def test_a_title_that_barely_changes_is_not_sent(
    api: Api, app_engine: AsyncEngine, copilot_rows: People, script: Script
) -> None:
    api.titler = FakeTitler([proposal("cotacao de rolamentos 6204.", settled=True)])
    conversation = str(uuid4())
    script(reply(text("Vamos cotar.")))

    events = await send(api, conversation, "Cotação de rolamentos 6204")

    assert titles(events) == []
    row = await stored(app_engine, api.principal, conversation)
    # Kept as written, but settled: the titler is not asked again.
    assert (row.title, row.title_source, row.title_settled) == (
        "Cotação de rolamentos 6204",
        "fallback",
        True,
    )


@pytest.mark.db
async def test_a_failing_titler_keeps_the_first_message(
    api: Api, app_engine: AsyncEngine, copilot_rows: People, script: Script
) -> None:
    api.titler = FakeTitler([TitlerFailed("model_error")])
    conversation = str(uuid4())
    script(reply(text("Olá!")))

    events = await send(api, conversation, "oi tudo bem")

    assert titles(events) == []
    row = await stored(app_engine, api.principal, conversation)
    assert (row.title, row.title_source, row.title_settled) == ("oi tudo bem", "fallback", False)


@pytest.mark.db
async def test_an_rfq_names_the_conversation_once(
    api: Api, app_engine: AsyncEngine, copilot_rows: People, script: Script
) -> None:
    titler = FakeTitler([proposal("Nunca usado", settled=False)])
    api.titler = titler
    conversation = str(uuid4())
    script(
        reply(tool_use("fc_1", "create_rfq", text="MLCC and connectors", currency="EUR")),
        reply(text("Criei o RFQ.")),
        reply(tool_use("fc_2", "create_rfq", text="Another RFQ", currency="EUR")),
        reply(text("Criei outro RFQ.")),
    )

    first = await send(api, conversation, "cria um RFQ de MLCC e conectores")
    second = await send(api, conversation, "cria outro")

    [title] = titles(first)
    assert isinstance(title, str) and title.startswith("RFQ-")
    assert titles(second) == []
    # A milestone settles the title: the titler never ran.
    assert titler.seen == []
    row = await stored(app_engine, api.principal, conversation)
    assert (row.title, row.title_source, row.title_milestones) == (title, "milestone", ["rfq"])


@pytest.mark.db
async def test_a_rename_is_final(
    api: Api, app_engine: AsyncEngine, copilot_rows: People, script: Script
) -> None:
    titler = FakeTitler([proposal("Conversa vaga", settled=False)])
    api.titler = titler
    conversation = str(uuid4())
    script(
        reply(text("Olá!")),
        reply(tool_use("fc_1", "create_rfq", text="MLCC and connectors", currency="EUR")),
        reply(text("Criei o RFQ.")),
    )
    await send(api, conversation, "oi")

    renamed = await api.http.patch(
        f"/v1/copilot/conversations/{conversation}", json={"title": "  Minha   compra de MLCC "}
    )
    assert renamed.status_code == 200, renamed.text
    assert (renamed.json()["title"], renamed.json()["title_source"]) == (
        "Minha compra de MLCC",
        "user",
    )

    events = await send(api, conversation, "cria um RFQ de MLCC")

    assert titles(events) == []
    assert len(titler.seen) == 1
    row = await stored(app_engine, api.principal, conversation)
    assert (row.title, row.title_source, row.title_milestones) == (
        "Minha compra de MLCC",
        "user",
        [],
    )


@pytest.mark.db
async def test_only_the_owner_renames_and_titles_are_bounded(
    api: Api, copilot_rows: People, script: Script
) -> None:
    conversation = str(uuid4())
    script(reply(text("Olá!")))
    await send(api, conversation, "oi")
    url = f"/v1/copilot/conversations/{conversation}"

    for blank in ("", "   "):
        assert (await api.http.patch(url, json={"title": blank})).status_code == 422
    assert (await api.http.patch(url, json={"title": "x" * 121})).status_code == 422
    assert (await api.http.patch(url, json={"title": "x" * 120})).status_code == 200

    api.principal = Principal(
        tenant_id=copilot_rows.tenant, user_id=copilot_rows.member, role="member"
    )
    assert (await api.http.patch(url, json={"title": "meu"})).status_code == 404
    api.principal = other_buyer_of(copilot_rows)
    assert (await api.http.patch(url, json={"title": "meu"})).status_code == 404
    api.principal = buyer_of(copilot_rows)
    assert (
        await api.http.patch(f"/v1/copilot/conversations/{uuid4()}", json={"title": "x"})
    ).status_code == 404
