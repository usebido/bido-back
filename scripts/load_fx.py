"""Load today's ECB reference rates into `fx_rates`.

    uv run python scripts/load_fx.py

Uses DATABASE_URL (the app role; `fx_rates` is global reference data with no
RLS). Fetches https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml.
Safe to re-run: rows are upserted by (rate_date, currency).
"""

import asyncio
import sys

from bido_back.db import create_engine
from bido_back.fx import FxError, refresh_rates
from bido_back.settings import get_settings


async def main() -> int:
    engine = create_engine(get_settings())
    try:
        daily = await refresh_rates(engine)
    except FxError as exc:
        print(f"FX refresh failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(f"loaded {len(daily.rates)} ECB rates for {daily.rate_date.isoformat()}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
