"""The language the Copilot answers in is decided by code, from the buyer's own words.

Live, 2026-10-10: a buyer wrote every message in English and every reply came back
in Portuguese. The model now gets `reply_language` with each message.
"""

from datetime import date
from typing import Any, cast
from uuid import uuid4

import pytest

from bido_back.auth import Principal
from bido_back.copilot import service
from bido_back.copilot.contracts import (
    ATTACHMENTS_ONLY_MESSAGE,
    CopilotInput,
    CopilotMessageRequest,
)
from bido_back.copilot.language import MARKERS, language_of, reply_language

PRINCIPAL = Principal(tenant_id=uuid4(), user_id=uuid4())


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The conversation seen live: English from the first message.
        (
            "Add Elettronica Test Srl (mario.rossi@elettronica-test.it) as a supplier and "
            "create an RFQ for 200 STM32F407 MCUs needed by 7 November 2026",
            "en",
        ),
        ("Invite them to the RFQ", "en"),
        ("yes", "en"),
        ("Preciso comprar 50 motores MT-120-ASM até 20/10", "pt"),
        ("faz uma análise do MTR-100", "pt"),
        ("sim", "pt"),
        ("Ciao, vorrei un preventivo per 100 pezzi", "it"),
        ("Necesito una cotización para 100 piezas", "es"),
        ("Bitte senden Sie uns ein Angebot für 100 Stück", "de"),
        ("Pouvez-vous nous envoyer votre prix pour 100 pièces", "fr"),
        # No language to read: codes, references, a name and an address.
        ("RFQ-12?", None),
        ("STM32F407 x 200", None),
        ("Elettronica Test Srl, mario@elettronica.com", None),
        ("", None),
    ],
)
def test_language_of(text: str, expected: str | None) -> None:
    assert language_of(text) == expected


def test_markers_belong_to_one_language_only() -> None:
    codes = list(MARKERS)
    for i, code in enumerate(codes):
        for other in codes[i + 1 :]:
            assert not MARKERS[code] & MARKERS[other], (code, other)


def test_the_conversation_keeps_the_language_it_started_in() -> None:
    assert reply_language(["RFQ-12", "Create the RFQ for these parts", "sim"]) == "en"
    assert reply_language(["Cria a RFQ", "Ciao, vorrei un preventivo per 100 pezzi"]) == "pt"
    assert reply_language(["RFQ-12", "STM32F407 x 200"]) is None
    assert reply_language([]) is None


def _patch_earlier(monkeypatch: pytest.MonkeyPatch, messages: list[str]) -> None:
    async def earlier(*_: object) -> list[CopilotInput]:
        return [CopilotInput(message=m, today=date(2026, 10, 10)) for m in messages]

    monkeypatch.setattr(service, "_earlier_inputs", earlier)


async def _prepared(message: str, conversation: bool = True) -> CopilotInput:
    request = CopilotMessageRequest(
        message=message, conversation_id=uuid4() if conversation else None
    )
    # No attachments: neither the database nor the storage is read.
    prepared = await service.prepare_message(cast(Any, None), PRINCIPAL, request)
    return prepared.input


async def test_a_new_english_conversation_is_answered_in_english() -> None:
    prepared = await _prepared(
        "Add Elettronica Test Srl as a supplier and create an RFQ for 200 MCUs",
        conversation=False,
    )
    assert prepared.reply_language == "en"


async def test_files_alone_then_english_is_english(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_earlier(monkeypatch, [ATTACHMENTS_ONLY_MESSAGE])
    assert (await _prepared("What do I need to buy for the next build?")).reply_language == "en"


async def test_a_later_message_in_another_language_does_not_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_earlier(monkeypatch, [ATTACHMENTS_ONLY_MESSAGE, "Preciso comprar 50 motores até 20/10"])
    assert (await _prepared("Please register this quote from the supplier")).reply_language == "pt"


async def test_no_language_yet_is_null(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_earlier(monkeypatch, [ATTACHMENTS_ONLY_MESSAGE])
    assert (await _prepared("RFQ-12?")).reply_language is None
