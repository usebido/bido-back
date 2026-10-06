"""ECB daily rates: parsing (fixture), refusal of unsafe XML, fetch, and the upsert."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from bido_back.db.models import FxRate
from bido_back.fx import FxError, FxRefreshHandler, fetch_ecb_daily, parse_ecb_daily
from bido_back.fx.ecb import MAX_DOCUMENT_BYTES
from bido_back.worker.envelope import Envelope
from tests.db.conftest import app_engine, migrator_engine

__all__ = ["app_engine", "migrator_engine"]

FIXTURE = (Path(__file__).parent / "fixtures" / "eurofxref-daily.xml").read_bytes()


def test_parses_the_daily_document() -> None:
    daily = parse_ecb_daily(FIXTURE)

    assert daily.rate_date == date(2026, 10, 5)
    assert daily.rates["USD"] == Decimal("1.1710")
    assert daily.rates["GBP"] == Decimal("0.86955")
    assert set(daily.rates) == {"USD", "JPY", "CZK", "GBP", "CHF", "CNY"}


@pytest.mark.parametrize(
    ("document", "message"),
    [
        (
            b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>',
            "DTD",
        ),
        (
            b'<!DOCTYPE x SYSTEM "file:///etc/passwd"><x/>',
            "DTD",
        ),
        (b"<not-closed>", "well-formed"),
        (b"<x/>", "one dated Cube"),
        (FIXTURE.replace(b"rate='1.1710'", b"rate='-1'"), "not positive"),
        (FIXTURE.replace(b"rate='1.1710'", b"rate='abc'"), "not a number"),
        (FIXTURE.replace(b"currency='USD'", b"currency='usd'"), "valid currency"),
        (b" " * (MAX_DOCUMENT_BYTES + 1), "too large"),
    ],
)
def test_refuses_unsafe_or_unexpected_documents(document: bytes, message: str) -> None:
    with pytest.raises(FxError, match=message):
        parse_ecb_daily(document)


async def test_fetch_reads_the_document_with_a_size_cap() -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=FIXTURE)

    def huge(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (MAX_DOCUMENT_BYTES + 10))

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(ok)) as client:
        assert await fetch_ecb_daily(client) == FIXTURE
    async with httpx.AsyncClient(transport=httpx.MockTransport(huge)) as client:
        with pytest.raises(FxError, match="too large"):
            await fetch_ecb_daily(client)
    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as client:
        with pytest.raises(FxError, match="HTTPStatusError"):
            await fetch_ecb_daily(client)


@pytest.mark.db
async def test_refresh_job_upserts_rates(
    app_engine: AsyncEngine, migrator_engine: AsyncEngine
) -> None:
    # A date no real refresh will write, so the test cannot clash with real rows.
    document = FIXTURE.replace(b"2026-10-05", b"1999-01-04")
    revised = document.replace(b"rate='1.1710'", b"rate='1.1800'")
    documents = [document, revised]

    async def fetch() -> bytes:
        return documents.pop(0)

    handler = FxRefreshHandler(app_engine, fetch)
    envelope = Envelope(
        type="fx.refresh",
        tenant_id="00000000-0000-4000-8000-000000000000",  # pyright: ignore[reportArgumentType]
        idempotency_key="fx:1999-01-04",
    )
    try:
        await handler(envelope)
        await handler(envelope)  # same day again: updated, not duplicated
        async with app_engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(FxRate.currency, FxRate.per_eur).where(
                        FxRate.rate_date == date(1999, 1, 4)
                    )
                )
            ).all()
        assert len(rows) == 6
        assert {currency: rate for currency, rate in rows}["USD"] == Decimal("1.18")
    finally:
        async with migrator_engine.begin() as conn:
            await conn.execute(delete(FxRate).where(FxRate.rate_date == date(1999, 1, 4)))
