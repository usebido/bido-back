"""Fill in the issuer's branding of cards saved before Bido kept it.

    uv run python scripts/backfill_payment_method_display.py --tenant <uuid> [--tenant <uuid>...]
    uv run python scripts/backfill_payment_method_display.py --tenant <uuid> --apply

Cards saved before migration 0041 have `payment_methods.display` NULL, so the
frontend draws them with the brand's colours instead of the issuer's card art.
For every saved (not removed) card of the given tenants without a display, this
reads the payment method at Basis Theory (`GET /agentic/payment-methods/{id}`)
and stores its display, cleaned exactly like at enrollment
(`bido_back.payments.card_display`). It writes `display` only, and only where it
is still NULL. Nothing is written without `--apply` (Basis Theory is still read).
Best effort: a card Basis Theory fails to answer for is left as is and logged by
id; re-run later.

Uses DATABASE_URL (the app role) through tenant-scoped transactions: no role sees
across tenants, so the tenants are given explicitly. Needs the payments settings
of the environment (BASIS_THEORY_API_KEY, ...). Safe to re-run.
"""

import argparse
import asyncio
import logging
import sys
from uuid import UUID

from bido_back.db import create_engine, create_sessionmaker
from bido_back.payments.card import build_card_gateway
from bido_back.payments.service import backfill_card_display
from bido_back.settings import get_settings


async def run(tenants: list[UUID], apply: bool) -> int:
    settings = get_settings()
    card = build_card_gateway(settings)
    engine = create_engine(settings)
    db = create_sessionmaker(engine)
    try:
        for tenant_id in tenants:
            result = await backfill_card_display(db, tenant_id, card, apply=apply)
            print(
                f"tenant {tenant_id}: {result.missing} without display, {result.filled} "
                f"{'filled' if apply else 'to fill'}, {result.empty} with none at the provider, "
                f"{result.failed} failed"
            )
    finally:
        await engine.dispose()
    if not apply:
        print("dry run: nothing written (pass --apply to write)")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0])
    parser.add_argument(
        "--tenant", action="append", type=UUID, required=True, help="a tenant id; repeatable"
    )
    parser.add_argument("--apply", action="store_true", help="write the displays")
    args = parser.parse_args()
    return asyncio.run(run(args.tenant, args.apply))


if __name__ == "__main__":
    sys.exit(main())
