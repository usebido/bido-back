"""ECB euro foreign exchange reference rates: fetch, parse, store.

The ECB publishes one XML document per working day with the rates as
"units of currency per 1 EUR", which is exactly what `fx_rates.per_eur`
holds. `fx_rates` is global reference data (no tenant, no RLS; on the
allowlist in scripts/check_rls.py) and the app role has DML on it, so this
module writes it with the app role, outside any tenant.

XML safety without defusedxml: the document is size-capped while it is read,
and any DTD (`<!DOCTYPE` / `<!ENTITY`) is refused before parsing. Without a
DTD there is nothing to expand or fetch (expat has no external entity loading
here and only the five predefined entities exist), so entity-expansion and
XXE attacks cannot happen by construction.
"""

import logging
import re
import xml.etree.ElementTree as ET  # noqa: S405 - only DTD-free input reaches it, see above
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

import httpx
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bido_back.db.models import FxRate
from bido_back.worker.envelope import Envelope

logger = logging.getLogger(__name__)

ECB_DAILY_URL: Final = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
MAX_DOCUMENT_BYTES: Final = 256 * 1024  # the real document is ~2 KiB
_TIMEOUT_S: Final = 10.0
_NS: Final = "{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}"
_CURRENCY: Final = re.compile(r"^[A-Z]{3}$")
_DTD: Final = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)

# The ISO 4217 codes Bido can convert: EUR and the currencies the ECB publishes a
# daily reference rate for. An RFQ's base currency must be one of them, or the
# comparison could not put quotes in other currencies next to each other.
REFERENCE_CURRENCIES: Final = frozenset(
    {
        "EUR", "USD", "JPY", "CZK", "DKK", "GBP", "HUF", "PLN", "RON", "SEK", "CHF", "ISK",
        "NOK", "TRY", "AUD", "BRL", "CAD", "CNY", "HKD", "IDR", "ILS", "INR", "KRW", "MXN",
        "MYR", "NZD", "PHP", "SGD", "THB", "ZAR",
    }
)  # fmt: skip


class FxError(Exception):
    """The ECB document could not be fetched or is not what we expect."""


@dataclass(frozen=True, slots=True)
class DailyRates:
    rate_date: date
    # Units of currency per 1 EUR. EUR itself is not listed (it is 1).
    rates: dict[str, Decimal]


def parse_ecb_daily(document: bytes) -> DailyRates:
    """Parse the ECB daily XML. Raises FxError on anything unexpected."""
    if len(document) > MAX_DOCUMENT_BYTES:
        raise FxError("ECB document is too large")
    if _DTD.search(document):
        raise FxError("ECB document contains a DTD; refused")
    try:
        root = ET.fromstring(document)  # noqa: S314 - size-capped, DTD refused above
    except ET.ParseError:
        raise FxError("ECB document is not well-formed XML") from None

    dated = [cube for cube in root.iter(f"{_NS}Cube") if cube.get("time") is not None]
    if len(dated) != 1:
        raise FxError(f"expected one dated Cube, found {len(dated)}")
    try:
        rate_date = date.fromisoformat(dated[0].get("time", ""))
    except ValueError:
        raise FxError("ECB rate date is not an ISO date") from None

    rates: dict[str, Decimal] = {}
    for cube in dated[0].findall(f"{_NS}Cube"):
        currency, raw = cube.get("currency"), cube.get("rate")
        if currency is None or raw is None or not _CURRENCY.match(currency):
            raise FxError("ECB rate entry without a valid currency or rate")
        try:
            rate = Decimal(raw)
        except InvalidOperation:
            raise FxError(f"ECB rate for {currency} is not a number") from None
        if not rate.is_finite() or rate <= 0:
            raise FxError(f"ECB rate for {currency} is not positive")
        rates[currency] = rate
    if not rates:
        raise FxError("ECB document has no rates")
    return DailyRates(rate_date=rate_date, rates=rates)


async def fetch_ecb_daily(
    client: httpx.AsyncClient | None = None, *, url: str = ECB_DAILY_URL
) -> bytes:
    """Download the daily document with a timeout and a size cap."""

    async def read(http: httpx.AsyncClient) -> bytes:
        async with http.stream("GET", url, timeout=_TIMEOUT_S) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_DOCUMENT_BYTES:
                    raise FxError("ECB document is too large")
                chunks.append(chunk)
            return b"".join(chunks)

    try:
        if client is not None:
            return await read(client)
        async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT_S) as http:
            return await read(http)
    except httpx.HTTPError as exc:
        raise FxError(f"could not fetch ECB rates: {type(exc).__name__}") from None


async def upsert_rates(
    db: AsyncEngine | async_sessionmaker[AsyncSession], daily: DailyRates
) -> int:
    """Insert or update one day's rates. Returns how many rows were written."""
    rows = [
        {"rate_date": daily.rate_date, "currency": currency, "per_eur": rate}
        for currency, rate in sorted(daily.rates.items())
    ]
    statement = insert(FxRate).values(rows)
    statement = statement.on_conflict_do_update(
        index_elements=[FxRate.rate_date, FxRate.currency],
        set_={"per_eur": statement.excluded.per_eur},
    )
    sessionmaker = db if isinstance(db, async_sessionmaker) else async_sessionmaker(db)
    # Global table: no tenant is set (and none is needed; fx_rates has no RLS).
    async with sessionmaker() as session, session.begin():
        await session.execute(statement)
    return len(rows)


Fetch = Callable[[], Awaitable[bytes]]


async def refresh_rates(
    db: AsyncEngine | async_sessionmaker[AsyncSession], fetch: Fetch | None = None
) -> DailyRates:
    daily = parse_ecb_daily(await (fetch or fetch_ecb_daily)())
    count = await upsert_rates(db, daily)
    logger.info("fx rates refreshed date=%s currencies=%d", daily.rate_date, count)
    return daily


async def refresh_if_stale(
    db: AsyncEngine | async_sessionmaker[AsyncSession],
    today: date,
    *,
    max_age_days: int = 3,
    fetch: Fetch | None = None,
) -> None:
    """Fetch today's rates when the newest stored ones are older than `max_age_days`.

    Run when the worker starts, so a fresh environment has rates before any
    scheduled `fx.refresh`. Fails open: without rates, conversions are skipped.
    """
    sessionmaker = db if isinstance(db, async_sessionmaker) else async_sessionmaker(db)
    async with sessionmaker() as session:
        latest = await session.scalar(select(func.max(FxRate.rate_date)))
    if latest is not None and (today - latest).days <= max_age_days:
        return
    try:
        await refresh_rates(db, fetch)
    except FxError as exc:
        logger.warning("fx rates not refreshed at start: %s", exc)


class FxRefreshHandler:
    """Worker handler for `fx.refresh` (job_types.FX_REFRESH). Idempotent (upsert).

    The envelope's tenant is required by the envelope format but irrelevant
    here: the rates are global.
    """

    def __init__(
        self, db: AsyncEngine | async_sessionmaker[AsyncSession], fetch: Fetch | None = None
    ) -> None:
        self._db = db
        self._fetch = fetch

    async def __call__(self, envelope: Envelope) -> None:
        await refresh_rates(self._db, self._fetch)
