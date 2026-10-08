"""Which currency an RFQ is in: resolved when it is created, changeable while a draft.

An RFQ's base currency is what the comparison puts every quote in and what its
negotiation limits are stored in. It is never a silent default: every creation
path (from text, from a purchase analysis, the API and the Copilot) resolves it
with `resolve_currency`, in this order:

1. `stated`: a currency the buyer named (a tool or API argument);
2. `analysis`: the RFQ comes from a purchase analysis whose lines to buy were all
   bought in one currency;
3. `purchase_history`: the currency of at least two thirds of the company's
   imported purchase orders. Counted by orders, not by spend: amounts in
   different currencies are not comparable without converting them first, and a
   single large import order would outweigh a year of local buying. A history
   without such a majority says nothing;
4. otherwise `CurrencyRequired`, and the caller asks the buyer.

Only currencies Bido can convert qualify (EUR and the ECB reference currencies,
`bido_back.fx.REFERENCE_CURRENCIES`).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bido_back.comparison.compute import Rate
from bido_back.comparison.loader import load_rates
from bido_back.db.models import PurchaseOrder, Rfq, RfqLine
from bido_back.fx import REFERENCE_CURRENCIES
from bido_back.purchasing.analysis import PRICE_PLACES
from bido_back.rfqs.errors import CurrencyRequired, InvalidState, UnknownCurrency
from bido_back.rfqs.schemas import UNKNOWN_CURRENCY, CurrencySource

HISTORY_MAJORITY: Final = Fraction(2, 3)
# Written for the Copilot's model, which relays it; the API returns it as is.
ASK_THE_BUYER: Final = (
    "the RFQ's currency is unknown: ask the buyer which currency this RFQ is in "
    "(e.g. BRL, EUR, USD) and create it again with that currency. Nothing was created."
)


@dataclass(frozen=True, slots=True)
class ResolvedCurrency:
    code: str
    source: CurrencySource


@dataclass(frozen=True, slots=True)
class CurrencyChange:
    """A draft's currency change: the lines whose limits were converted, and at which rate."""

    old: str
    new: str
    converted_lines: tuple[int, ...] = ()
    rate_date: date | None = None


def known_currency(code: str) -> str:
    """`code` as an upper-case ISO 4217 code Bido can convert; `UnknownCurrency` if not."""
    normalized = code.strip().upper()
    if normalized not in REFERENCE_CURRENCIES:
        raise UnknownCurrency(UNKNOWN_CURRENCY)
    return normalized


async def history_currency(session: AsyncSession) -> str | None:
    """The currency of at least two thirds of the tenant's imported purchase orders."""
    currency = func.upper(PurchaseOrder.currency)
    counts = (await session.execute(select(currency, func.count()).group_by(currency))).all()
    total = sum(count for _, count in counts)
    if not total:
        return None
    code, count = max(counts, key=lambda row: row[1])
    if Fraction(count, total) < HISTORY_MAJORITY or code not in REFERENCE_CURRENCIES:
        return None
    return code


async def resolve_currency(
    session: AsyncSession,
    *,
    stated: str | None = None,
    analysis: Iterable[str | None] = (),
) -> ResolvedCurrency:
    """The new RFQ's currency (see the module docstring), in the tenant's session.

    `analysis` is the currency of each line to buy (None: never bought).
    """
    if stated is not None and stated.strip():
        return ResolvedCurrency(known_currency(stated), "stated")
    bought_in = {code.strip().upper() for code in analysis if code}
    if len(bought_in) == 1:
        (only,) = bought_in
        if only in REFERENCE_CURRENCIES:
            return ResolvedCurrency(only, "analysis")
    dominant = await history_currency(session)
    if dominant is not None:
        return ResolvedCurrency(dominant, "purchase_history")
    raise CurrencyRequired(ASK_THE_BUYER)


def fx_factor(source: str | None, target: str, rates: Mapping[str, Rate]) -> Decimal | None:
    """Units of `target` per unit of `source` (ECB reference rates, per EUR)."""
    if source is None:
        return None
    if source == target:
        return Decimal(1)
    per_eur = {code: rate.per_eur for code, rate in rates.items()} | {"EUR": Decimal(1)}
    if source not in per_eur or target not in per_eur or per_eur[source] <= 0:
        return None
    return per_eur[target] / per_eur[source]


async def change_currency(
    session: AsyncSession, rfq: Rfq, code: str, today: date
) -> CurrencyChange | None:
    """Set a draft's currency, converting its negotiation limits along with it.

    The limits (`target_unit_price`, `max_unit_price`) are stored in the RFQ's
    currency, so they are converted at the latest ECB reference rate; without one
    the change is refused rather than leaving limits in the old currency. None when
    the currency is already `code`. The caller has checked the RFQ is editable and
    holds its lock.
    """
    target = known_currency(code)
    old = rfq.base_currency
    if target == old:
        return None
    lines = list(
        await session.scalars(
            select(RfqLine).where(RfqLine.rfq_id == rfq.id).order_by(RfqLine.position)
        )
    )
    limited = [
        line
        for line in lines
        if line.target_unit_price is not None or line.max_unit_price is not None
    ]
    rate_date: date | None = None
    if limited:
        rates = await load_rates(session, today)
        factor = fx_factor(rfq.base_currency, target, rates)
        if factor is None:
            positions = ", ".join(str(line.position) for line in limited)
            raise InvalidState(
                f"{rfq.ref} has negotiation limits in {rfq.base_currency} on line(s) "
                f"{positions} and there is no exchange rate from {rfq.base_currency} to "
                f"{target} to convert them; the currency can change only while no limits "
                "are set or a rate is available"
            )
        used = [rates[c].rate_date for c in (rfq.base_currency, target) if c in rates]
        rate_date = min(used) if used else None
        for line in limited:
            if line.target_unit_price is not None:
                line.target_unit_price = (line.target_unit_price * factor).quantize(PRICE_PLACES)
            if line.max_unit_price is not None:
                line.max_unit_price = (line.max_unit_price * factor).quantize(PRICE_PLACES)
    rfq.base_currency = target
    return CurrencyChange(
        old=old,
        new=target,
        converted_lines=tuple(line.position for line in limited),
        rate_date=rate_date,
    )
