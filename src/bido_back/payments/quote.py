"""What a purchase order costs on the buyer's card: its total in US dollars.

Bido charges cards in USD, on its own Stripe account. A PO in another currency is
converted at the ECB reference rates in effect on the day (`fx_rates`, the rates the
comparison uses; `rfqs.currency.fx_factor`): the rate is rounded to the 8 decimals it
is stored and shown with, and the charge is the PO total times that rate, rounded half
up to the cent, so a person can redo the multiplication from what the approval shows.

The quote is frozen in the approval's arguments with an expiry
(`PAYMENTS_QUOTE_TTL_MINUTES`); it is a reference rate, not a market quote or a lock.
The stablecoin minted for the payment is the charge itself, in the mint's base units.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.comparison.compute import Rate
from bido_back.comparison.loader import load_rates
from bido_back.payments.errors import PaymentRefused
from bido_back.rfqs.currency import fx_factor

CHARGE_CURRENCY: Final = "USD"
# Stripe refuses smaller USD charges.
MIN_CHARGE: Final = Decimal("0.50")
CENT: Final = Decimal("0.01")
# `po_payments.fx_rate` is NUMERIC(18, 8).
RATE_PLACES: Final = Decimal("0.00000001")
# The escrow's `total` and every token amount on chain are u64.
U64_MAX: Final = 2**64 - 1


@dataclass(frozen=True, slots=True)
class ChargeQuote:
    """The card amount for one PO, and the rate behind it."""

    amount: Decimal  # USD, two decimals
    # USD per unit of the PO's currency; None when the PO is in USD.
    fx_rate: Decimal | None
    # The ECB publication the rate comes from (the older one of a cross rate).
    fx_rate_date: date | None
    currency: Literal["USD"] = CHARGE_CURRENCY


def charge_quote(*, po_currency: str, po_total: Decimal, rates: Mapping[str, Rate]) -> ChargeQuote:
    """The charge for `po_total` in `po_currency`, from the given ECB rates.

    Raises `PaymentRefused` when there is no rate to USD, or the charge is below the
    card minimum (USD 0.50).
    """
    rate: Decimal | None = None
    rate_date: date | None = None
    if po_currency == CHARGE_CURRENCY:
        amount = po_total.quantize(CENT, rounding=ROUND_HALF_UP)
    else:
        factor = fx_factor(po_currency, CHARGE_CURRENCY, rates)
        if factor is not None:
            rate = factor.quantize(RATE_PLACES, rounding=ROUND_HALF_UP)
        if rate is None or rate <= 0:
            raise PaymentRefused(f"no exchange rate from {po_currency} to {CHARGE_CURRENCY} yet")
        amount = (po_total * rate).quantize(CENT, rounding=ROUND_HALF_UP)
        used = [rates[code].rate_date for code in (po_currency, CHARGE_CURRENCY) if code in rates]
        rate_date = min(used) if used else None
    if amount < MIN_CHARGE:
        raise PaymentRefused(
            f"a card payment is at least {CHARGE_CURRENCY} {MIN_CHARGE}; this order is below it"
        )
    return ChargeQuote(amount=amount, fx_rate=rate, fx_rate_date=rate_date)


async def quote_charge(
    session: AsyncSession, *, po_currency: str, po_total: Decimal, today: date
) -> ChargeQuote:
    """`charge_quote` at the latest ECB rates published on or before `today`."""
    rates = {} if po_currency == CHARGE_CURRENCY else await load_rates(session, today)
    return charge_quote(po_currency=po_currency, po_total=po_total, rates=rates)


def token_base_units(amount: Decimal, decimals: int) -> int:
    """`amount` (major units) in the stablecoin's base units, exactly.

    Raises `ValueError` when the amount is not positive, has more decimals than the
    token, or does not fit a u64 (the program's amounts).
    """
    scaled = amount.scaleb(decimals)
    if scaled != scaled.to_integral_value():
        raise ValueError("the amount has more decimals than the token")
    units = int(scaled)
    if not 0 < units <= U64_MAX:
        raise ValueError("the token amount must be positive and fit a u64")
    return units
