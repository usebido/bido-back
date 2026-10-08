"""The copilot's SSE stream stays alive through long tools and ends like the turn ends."""

import asyncio
from collections.abc import AsyncIterator

import pytest

from bido_back.api.copilot import KEEP_ALIVE, _stream  # pyright: ignore[reportPrivateUsage]
from bido_back.copilot.contracts import StreamItem


async def _collect(stream: AsyncIterator[str]) -> list[str]:
    return [chunk async for chunk in stream]


async def test_a_quiet_stretch_sends_keep_alives_between_events() -> None:
    async def turn() -> AsyncIterator[StreamItem]:
        yield {"type": "text_delta", "text": "a"}
        await asyncio.sleep(0.12)  # a long tool, scaled down
        yield {"type": "answer", "text": "a"}

    chunks = await _collect(_stream(turn(), keep_alive=0.03))

    assert chunks[0].startswith("event: text_delta")
    assert chunks[-1].startswith("event: answer")
    assert chunks[1:-1] and set(chunks[1:-1]) == {KEEP_ALIVE}


async def test_a_fast_turn_sends_no_keep_alive() -> None:
    async def turn() -> AsyncIterator[StreamItem]:
        yield {"type": "answer", "text": "ok"}

    assert await _collect(_stream(turn(), keep_alive=5)) == [
        'event: answer\ndata: {"type":"answer","text":"ok"}\n\n'
    ]


async def test_an_error_in_the_turn_still_raises() -> None:
    async def turn() -> AsyncIterator[StreamItem]:
        yield {"type": "text_delta", "text": "a"}
        raise RuntimeError("provider down")

    with pytest.raises(RuntimeError, match="provider down"):
        await _collect(_stream(turn(), keep_alive=5))


async def test_the_client_leaving_cancels_the_turn() -> None:
    cancelled = asyncio.Event()

    async def turn() -> AsyncIterator[StreamItem]:
        yield {"type": "text_delta", "text": "a"}
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        yield {"type": "answer", "text": "never"}

    stream = _stream(turn(), keep_alive=5)
    first = await anext(stream)
    await stream.aclose()  # what Starlette does when the client disconnects

    assert first.startswith("event: text_delta") and cancelled.is_set()
