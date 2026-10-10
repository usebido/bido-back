"""Follow-ups about an analysis are answered from the analysis the conversation ran.

Measured 2026-10-09 on the SAP review fixture (`evals.sap_review_case`): after a correct
first answer, "E o item 100000152, por quê?" re-ran the analysis in 3 of 4 runs, once
with the material code as `product_code`; that narrower run found no demand and the
reply contradicted the first one. Here: the read tool answers from the stored result,
the same inputs are never recomputed, and a narrower run never replaces the analysis.
"""

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.config import RunnerConfig
from bido_back.agents.tools import AnyTool, ToolError
from bido_back.auth import Principal
from bido_back.copilot import analyses, journey_tools
from bido_back.copilot.analyses import AnalysisItemOut, StoredAnalysis
from bido_back.copilot.model_views import analysis_item_view
from bido_back.copilot.tools import CopilotToolDeps, copilot_tools
from bido_back.db import tenant_session
from bido_back.db.models import CopilotAnalysis
from bido_back.onboarding import service as onboarding
from bido_back.purchasing.sources.contracts import (
    AssessmentLine,
    PlanningScope,
    ScopeRequest,
    SourceAssessment,
)
from bido_back.purchasing.sources.service import analyze_uploads
from evals.sap_review_case import reports
from evals.source_fixtures import EXPECTED
from tests.agents.test_runner import Script, reply, script, text, tool_use
from tests.approvals.conftest import People
from tests.db.conftest import agno_engine
from tests.purchasing.conftest import FakeStorage
from tests.purchasing.motor_sources import MotorMapper, motor_workbook
from tests.purchasing.test_workbook import XLSX
from tests.rfqs.conftest import FakeMailboxes, fake_agents

from .conftest import buyer_of, context_of
from .test_api import Api, api, parse_sse

__all__ = ["agno_engine", "api", "script"]

MOTOR_SCOPE: dict[str, object] = {
    "product": "Motor MTR-100",
    "location": "PLANT A",
    "horizon_days": 14,
    "safety_stock": 0,
}


class CountingAnalysis:
    """Counts the analyses actually computed (the sources read and planned)."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *args: Any, **kwargs: Any) -> SourceAssessment:
        self.calls += 1
        return await analyze_uploads(*args, **kwargs)


@pytest.fixture
def computed(monkeypatch: pytest.MonkeyPatch) -> CountingAnalysis:
    counter = CountingAnalysis()
    monkeypatch.setattr(journey_tools, "analyze_uploads", counter)
    return counter


class Conversation:
    def __init__(
        self,
        engine: AsyncEngine,
        principal: Principal,
        storage: FakeStorage,
        upload: UUID,
        mapper: MotorMapper,
    ) -> None:
        self.engine = engine
        self.principal = principal
        self.storage = storage
        self.upload = upload
        self.mapper = mapper
        self.conversation_id = uuid4()

    def tools(self, conversation_id: UUID | None = None) -> dict[str, AnyTool]:
        return {
            tool.name: tool
            for tool in copilot_tools(
                CopilotToolDeps(
                    db=self.engine,
                    agents=fake_agents(),
                    mailboxes=FakeMailboxes(),
                    storage=self.storage,
                    source_mapper=self.mapper,
                    earlier_attachment_ids=(self.upload,),
                    conversation_id=conversation_id or self.conversation_id,
                )
            )
        }

    async def analyze(
        self,
        scope: dict[str, object] | None,
        conversation_id: UUID | None = None,
        *,
        recompute: bool = False,
    ) -> SourceAssessment:
        tool = self.tools(conversation_id)["analyze_procurement_sources"]
        args: dict[str, object] = {
            "upload_ids": [str(self.upload)],
            "objective": "O que preciso comprar para o motor?",
            "recompute": recompute,
        }
        if scope is not None:
            args["scope"] = scope
        return cast(
            SourceAssessment, await tool.invoke(context_of(self.principal), tool.parse_input(args))
        )

    async def explain(self, **args: object) -> AnalysisItemOut:
        tool = self.tools()["explain_analysis_item"]
        return cast(
            AnalysisItemOut, await tool.invoke(context_of(self.principal), tool.parse_input(args))
        )

    async def stored_rows(self) -> int:
        async with tenant_session(self.engine, self.principal.tenant_id) as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(CopilotAnalysis)
                    .where(CopilotAnalysis.conversation_id == self.conversation_id)
                )
                or 0
            )


async def motor_conversation(engine: AsyncEngine, people: People) -> Conversation:
    principal = buyer_of(people)
    data, mapper = motor_workbook()
    storage = FakeStorage()
    upload = await onboarding.store_upload(
        engine, storage, principal, filename="erp.xlsx", content_type=XLSX, data=data
    )
    return Conversation(engine, principal, storage, upload.id, mapper)


def _line(assessment: SourceAssessment, part: str) -> AssessmentLine:
    return next(line for line in assessment.lines if line.part_number == part)


@pytest.mark.db
async def test_the_same_analysis_again_is_the_stored_result_not_a_new_computation(
    app_engine: AsyncEngine, copilot_rows: People, computed: CountingAnalysis
) -> None:
    chat = await motor_conversation(app_engine, copilot_rows)

    first = await chat.analyze(MOTOR_SCOPE)
    mapped = chat.mapper.calls
    again = await chat.analyze(MOTOR_SCOPE)
    # The values the first analysis resolved to, spelled out, are the same request.
    spelled = await chat.analyze(
        {
            "product_code": "MTR-100",
            "location": "Plant A",
            "as_of": "2026-10-08",
            "horizon_end": "2026-10-22",
            "safety_stock": 0,
        }
    )

    assert computed.calls == 1 and chat.mapper.calls == mapped
    assert again == first and spelled == first
    assert {line.part_number: line.calculated_to_buy for line in first.lines} == EXPECTED
    assert await chat.stored_rows() == 1
    # Only the buyer's explicit request recomputes it, replacing the stored one.
    assert await chat.analyze(MOTOR_SCOPE, recompute=True) == first
    assert computed.calls == 2 and await chat.stored_rows() == 1
    # Another conversation computes its own.
    await chat.analyze(MOTOR_SCOPE, conversation_id=uuid4())
    assert computed.calls == 3


@pytest.mark.db
async def test_a_material_code_as_product_code_points_to_the_stored_item_and_replaces_nothing(
    app_engine: AsyncEngine, copilot_rows: People, computed: CountingAnalysis
) -> None:
    chat = await motor_conversation(app_engine, copilot_rows)
    first = await chat.analyze(MOTOR_SCOPE)
    position = next(i for i, line in enumerate(first.lines, 1) if line.part_number == "000104")

    # The follow-up's narrower call, as gpt-6-luna made it: the material code as product.
    with pytest.raises(ToolError) as caught:
        await chat.analyze({"product_code": "000104", "horizon_days": 14})

    message = str(caught.value)
    assert message.startswith("not recomputed: '000104'")
    assert f"line {position} of the analysis" in message
    assert "explain_analysis_item" in message
    assert computed.calls == 1 and await chat.stored_rows() == 1
    explained = await chat.explain(material="104")
    assert explained.found and explained.analysis is not None
    [item] = explained.items
    assert item.line == _line(first, "000104")
    assert item.line.calculated_to_buy == Decimal(22)
    assert item.position == position
    assert explained.analysis.scope == first.scope


@pytest.mark.db
async def test_a_new_horizon_runs_again_and_becomes_the_analysis_explained(
    app_engine: AsyncEngine, copilot_rows: People, computed: CountingAnalysis
) -> None:
    chat = await motor_conversation(app_engine, copilot_rows)
    first = await chat.analyze(MOTOR_SCOPE)
    before = await chat.explain(material="000105")

    longer = await chat.analyze({**MOTOR_SCOPE, "horizon_days": 24})
    after = await chat.explain(material="000105")

    assert computed.calls == 2 and await chat.stored_rows() == 2
    assert first.scope is not None and first.scope.horizon_end == date(2026, 10, 22)
    assert longer.scope is not None and longer.scope.horizon_end == date(2026, 11, 1)
    assert before.analysis is not None and after.analysis is not None
    assert after.analysis.analysis_id != before.analysis.analysis_id
    assert after.analysis.scope == longer.scope
    assert after.items[0].line == _line(longer, "000105")
    # Asking for the first scope again reuses it, and it is the latest again.
    assert await chat.analyze(MOTOR_SCOPE) == first
    assert computed.calls == 2
    latest = await chat.explain(material="000105")
    assert latest.analysis is not None
    assert latest.analysis.analysis_id == before.analysis.analysis_id


@pytest.mark.db
async def test_explaining_without_an_analysis_or_an_unknown_item_says_so(
    app_engine: AsyncEngine, copilot_rows: People, computed: CountingAnalysis
) -> None:
    chat = await motor_conversation(app_engine, copilot_rows)

    none = await chat.explain(material="000101")
    assert not none.found and none.analysis is None and "no source analysis yet" in none.note

    await chat.analyze(MOTOR_SCOPE)
    unknown = await chat.explain(material="999999")
    by_line = await chat.explain(line=1)
    past_the_end = await chat.explain(line=99)

    assert not unknown.found and unknown.analysis is not None
    assert set(unknown.part_numbers) == set(EXPECTED)
    assert by_line.found and by_line.items[0].position == 1
    assert not past_the_end.found
    assert computed.calls == 1


# --- Pure comparisons ------------------------------------------------------------------


def _stored(request: ScopeRequest | None, scope: PlanningScope | None) -> StoredAnalysis:
    return StoredAnalysis(
        id=uuid4(),
        created_at=datetime.now(UTC),
        upload_ids=(UUID(int=1), UUID(int=2)),
        request=request,
        assessment=SourceAssessment(
            scope=scope,
            lines=[AssessmentLine(part_number="100000152", description="MOTOR ELETRICO 15CV")],
        ),
    )


SAP_SCOPE = PlanningScope(
    location="1000", as_of=date(2026, 10, 8), horizon_end=date(2026, 11, 5), all_materials=True
)
FILES: Sequence[UUID] = (UUID(int=2), UUID(int=1))


@pytest.mark.parametrize(
    ("request_", "same", "narrows"),
    [
        (ScopeRequest(horizon_days=28), True, False),
        (ScopeRequest(horizon_end=date(2026, 11, 5), location="1000"), True, False),
        (ScopeRequest(horizon_days=14), False, False),
        (ScopeRequest(), False, False),
        (ScopeRequest(product_code="100000152", horizon_days=28), False, True),
        (ScopeRequest(product="motor eletrico"), False, True),
        (ScopeRequest(horizon_days=28, location="2000"), False, True),
        (ScopeRequest(horizon_days=28, mrp_element="NecDep"), False, True),
    ],
)
def test_same_inputs_and_narrower_requests(
    request_: ScopeRequest, same: bool, narrows: bool
) -> None:
    stored = _stored(ScopeRequest(horizon_days=28), SAP_SCOPE)

    assert analyses.same_inputs(FILES, request_, stored) is same
    assert analyses.narrower(FILES, request_, stored) is narrows
    # Other files are never the same analysis.
    assert not analyses.same_inputs((UUID(int=3),), request_, stored)


def test_the_key_ignores_spelling_and_order() -> None:
    one = analyses.input_key(FILES, ScopeRequest(location=" Plant  A ", plants=["2000", "1000"]))
    two = analyses.input_key(
        list(reversed(FILES)), ScopeRequest(location="plant a", plants=["1000", "2000"])
    )

    assert one == two
    assert one != analyses.input_key(FILES, ScopeRequest(location="plant a"))
    assert analyses.input_key(FILES, None) == analyses.input_key(FILES, ScopeRequest())


def test_the_model_reads_an_item_without_cell_references() -> None:
    stored = _stored(ScopeRequest(horizon_days=28), SAP_SCOPE)
    view = analysis_item_view(analyses.explain_item(stored, material="100000152", position=None))

    assert isinstance(view, dict)
    assert view["found"] is True
    assert "source_id" not in json.dumps(view)


# --- The follow-up turn, end to end with a scripted model --------------------------------


async def sap_uploads(api: Api, engine: AsyncEngine) -> list[str]:
    ids: list[str] = []
    for name, data in reports().items():
        view = await onboarding.store_upload(
            engine, api.storage, api.principal, filename=name, content_type=XLSX, data=data
        )
        ids.append(str(view.id))
    return ids


async def turn(api: Api, message: str, conversation: str, files: list[str]) -> list[Any]:
    response = await api.http.post(
        "/v1/copilot/messages",
        json={"message": message, "conversation_id": conversation, "attachment_ids": files},
    )
    assert response.status_code == 200, response.text
    events = parse_sse(response.text)
    assert events[-1]["type"] == "done" and events[-1]["ok"] is True, events[-1]
    return events


def results(events: list[Any], tool: str) -> list[dict[str, Any]]:
    return [e for e in events if e["type"] == "tool_result" and e["tool"] == tool]


@pytest.mark.db
async def test_a_follow_up_about_an_item_is_answered_from_the_analysis_already_run(
    api: Api,
    app_engine: AsyncEngine,
    agno_engine: AsyncEngine,
    copilot_rows: People,
    script: Script,
    computed: CountingAnalysis,
) -> None:
    api.runner_config = RunnerConfig(
        openai_api_key="test-not-a-real-key",
        agno_database_url=agno_engine.url.render_as_string(hide_password=False),
    )
    files = await sap_uploads(api, app_engine)
    conversation = str(uuid4())
    question = "O que eu preciso comprar para as próximas 4 semanas?"
    analysis = {"upload_ids": files, "objective": question, "scope": {"horizon_days": 28}}
    stub = script(
        # Turn 1: the Copilot analyzes; the writer answers.
        reply(tool_use("fc_1", "analyze_procurement_sources", **analysis)),
        reply(text("O 100000152 acaba em 12/10: antecipe o pedido 4500019736/10.")),
        # Turn 2: the follow-up reads the item from the stored analysis.
        reply(tool_use("fc_2", "explain_analysis_item", material="100000152")),
        reply(text("Porque o estoque não cobre a necessidade de 12/10.")),
        # Turn 3: the same analysis again is the stored one (the writer answers again).
        reply(tool_use("fc_3", "analyze_procurement_sources", **analysis)),
        reply(text("A mesma análise.")),
    )

    first = await turn(api, question, conversation, files)
    second = await turn(api, "E o item 100000152, por quê?", conversation, [])
    third = await turn(api, question, conversation, [])

    [analyzed] = results(first, "analyze_procurement_sources")
    line = next(line for line in analyzed["output"]["lines"] if line["part_number"] == "100000152")
    assert results(second, "analyze_procurement_sources") == []
    [explained] = results(second, "explain_analysis_item")
    assert explained["ok"] is True
    [item] = explained["output"]["items"]
    # The same dates and quantities as the first answer, as code computed them.
    for field in (
        "calculated_to_buy",
        "available",
        "need_schedule",
        "demand_schedule",
        "inbound",
        "netting",
        "timing",
        "expedite",
    ):
        assert item["line"][field] == line[field], field
    assert item["alert_status"] == "alert"
    assert item["timing_alert"]["reason"]
    assert item["line"]["timing"]["outcome"] == "unavoidable_stockout"
    # The model read the record (its model view), not a re-run.
    sent = json.dumps(stub.calls[3]["input"], ensure_ascii=False)
    assert "unavoidable_stockout" in sent and "100000152" in sent
    # Turn 3 recomputed nothing: one analysis for the whole conversation.
    [again] = results(third, "analyze_procurement_sources")
    assert again["output"] == analyzed["output"]
    assert computed.calls == 1
    assert len(stub.calls) == 6
