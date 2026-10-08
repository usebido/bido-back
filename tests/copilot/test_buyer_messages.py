"""`buyer_messages`: what create_rfq hands the intake agent as the buyer's own words."""

from datetime import date
from typing import Any, cast
from uuid import uuid4

import pytest

from bido_back.auth import Principal
from bido_back.copilot import service
from bido_back.copilot.contracts import ATTACHMENTS_ONLY_MESSAGE, CopilotInput

PRINCIPAL = Principal(tenant_id=uuid4(), user_id=uuid4())


def _patch_earlier(monkeypatch: pytest.MonkeyPatch, messages: list[str]) -> None:
    async def earlier(*_: object) -> list[CopilotInput]:
        return [CopilotInput(message=m, today=date(2026, 10, 8)) for m in messages]

    monkeypatch.setattr(service, "_earlier_inputs", earlier)


async def test_oldest_first_ending_with_this_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_earlier(monkeypatch, ["RL-6206-2RS x 200, DAP Curitiba", ATTACHMENTS_ONLY_MESSAGE, " "])

    said = await service.buyer_messages(cast(Any, None), PRINCIPAL, uuid4(), "cria a RFQ")

    # The standing instruction for files alone is not the buyer's words.
    assert said == ["RL-6206-2RS x 200, DAP Curitiba", "cria a RFQ"]


async def test_keeps_the_latest_messages_within_the_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    many = [f"m{i}" for i in range(30)]
    _patch_earlier(monkeypatch, many)
    said = await service.buyer_messages(cast(Any, None), PRINCIPAL, uuid4(), "last")
    assert len(said) == service.MAX_BUYER_MESSAGES and said[-1] == "last"
    assert said[0] == many[30 - service.MAX_BUYER_MESSAGES + 1]

    big = "x" * (service.MAX_BUYER_MESSAGES_CHARS - 10)
    _patch_earlier(monkeypatch, ["first, dropped", big])
    said = await service.buyer_messages(cast(Any, None), PRINCIPAL, uuid4(), "and this")
    # The oldest message no longer fits; the latest ones do.
    assert said == [big, "and this"]
