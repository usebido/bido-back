"""A file attached in one message stays usable in the conversation's later messages.

The buyer attaches a workbook, the copilot asks before importing, and the
buyer's answer comes as text alone: the import must still find that file.
"""

import json
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.agents.config import RunnerConfig
from bido_back.auth import Principal
from bido_back.db import tenant_session
from bido_back.onboarding import service as onboarding
from tests.agents.test_runner import Script, reply, script, text, tool_use
from tests.approvals.conftest import People
from tests.db.conftest import agno_engine
from tests.purchasing.conftest import CLEANUP
from tests.purchasing.test_workbook import PURCHASES, XLSX, workbook

from .test_api import Api, api, parse_sse

__all__ = ["agno_engine", "api", "script"]

pytestmark = pytest.mark.db


@pytest_asyncio.fixture
async def imported(migrator_engine: AsyncEngine, copilot_rows: People) -> AsyncIterator[People]:
    """copilot_rows, plus removing what the imports wrote before the tenants go."""
    yield copilot_rows
    async with tenant_session(migrator_engine, copilot_rows.tenant) as session:
        for model in CLEANUP:
            await session.execute(delete(model).where(model.tenant_id == copilot_rows.tenant))


async def test_a_text_only_confirmation_imports_the_file_of_an_earlier_turn(
    api: Api,
    app_engine: AsyncEngine,
    agno_engine: AsyncEngine,
    imported: People,
    script: Script,
) -> None:
    api.runner_config = RunnerConfig(
        openai_api_key="test-not-a-real-key",
        agno_database_url=agno_engine.url.render_as_string(hide_password=False),
    )
    upload = await onboarding.store_upload(
        app_engine,
        api.storage,
        api.principal,
        filename="erp.xlsx",
        content_type=XLSX,
        data=workbook({"Compras": PURCHASES}),
    )
    conversation = str(uuid4())
    stub = script(
        reply(text("Antes de importar: posso seguir com as linhas válidas?")),
        reply(tool_use("fc_1", "import_spreadsheet", upload_id=str(upload.id), confirmed=True)),
        reply(text("Importado.")),
    )

    first = await api.http.post(
        "/v1/copilot/messages",
        json={
            "message": "importa",
            "attachment_ids": [str(upload.id)],
            "conversation_id": conversation,
        },
    )
    assert parse_sse(first.text)[-1]["ok"] is True
    second = await api.http.post(
        "/v1/copilot/messages",
        json={"message": "vai no que dá", "conversation_id": conversation},
    )

    events = parse_sse(second.text)
    [result] = [e for e in events if e["type"] == "tool_result"]
    assert result["ok"] is True, result
    assert result["output"]["status"] == "imported"
    # The model is told the earlier file is still there.
    assert f'\\"earlier_attachment_ids\\":[\\"{upload.id}\\"]' in json.dumps(stub.calls[1]["input"])


async def test_another_user_cannot_reach_the_files_of_a_conversation(
    api: Api,
    app_engine: AsyncEngine,
    agno_engine: AsyncEngine,
    imported: People,
    script: Script,
) -> None:
    copilot_rows = imported
    api.runner_config = RunnerConfig(
        openai_api_key="test-not-a-real-key",
        agno_database_url=agno_engine.url.render_as_string(hide_password=False),
    )
    upload = await onboarding.store_upload(
        app_engine,
        api.storage,
        api.principal,
        filename="erp.xlsx",
        content_type=XLSX,
        data=workbook({"Compras": PURCHASES}),
    )
    conversation = str(uuid4())
    script(
        reply(text("Recebi a planilha.")),
        reply(tool_use("fc_1", "import_spreadsheet", upload_id=str(upload.id), confirmed=True)),
        reply(text("Não consegui.")),
    )
    await api.http.post(
        "/v1/copilot/messages",
        json={"message": "oi", "attachment_ids": [str(upload.id)], "conversation_id": conversation},
    )

    # Same tenant, another user, the same conversation id: none of its files.
    api.principal = Principal(
        tenant_id=copilot_rows.tenant, user_id=copilot_rows.member, role="member"
    )
    events = parse_sse(
        (
            await api.http.post(
                "/v1/copilot/messages",
                json={"message": "importa", "conversation_id": conversation},
            )
        ).text
    )

    [result] = [e for e in events if e["type"] == "tool_result"]
    assert result["ok"] is False
    assert "attached in this conversation" in result["error"]
