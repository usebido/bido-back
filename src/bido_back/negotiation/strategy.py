"""The negotiation strategy: per-line rules, pure code (no model, no database).

Every number and date Bido asks a supplier for comes from here; the model (or
the email renderer) only puts it into words. The supplier-level stages (what
to send next, when to stop) are in `bido_back.negotiation.planner`.

Price (`price_position`), against the buyer's target `T` and private maximum `M`:

- `in_target`: the offer is at or below `T` (without a target: at or below the
  internal price-history reference, which is never disclosed). Nothing to ask.
- `ask_met`: the offer meets the last price we asked (a concession the supplier
  accepted). Nothing more to ask on it.
- `in_range`: `T` < offer <= `M`. We ask.
- `above_max`: offer > `M`. We ask, never conceding on it.
- `no_target`: no target and no usable reference: we ask for a better price
  WITHOUT a number (never an arbitrary "5% off").

The first ask is `T`. A later ask concedes only on a line where the supplier
moved since our last ask, and only within `M`: a quarter of the gap between
our last ask and the supplier's current price in a revised-offer round, half
of it in the best-and-final round (concede late; a labeled step, never `M`
itself, never at or above the offer). A line where the supplier did not move,
or that is above `M`, keeps its ask. Asks are quantized DOWN when decided: to
the currency's minor unit, or to the supplier's own finer precision (at most 4
decimals). The email shows exactly the stored value.

Delivery (`delivery_position`) is a DATE question, recomputed at every
evaluation: the expected delivery (the supplier's lead time from the order
date, `bido_back.negotiation.signals.ExpectedDelivery`) against the date the
line is required by (the approved need-by date, or the buyer's own lead-time
limit counted from the order date, whichever is earlier).

- `on_time`, or `no_need_by` (known, nothing required);
- `unknown`: no usable lead time (a question for the clarification ledger);
- `late`: we ask once to deliver by the required date;
- `still_late`: it was asked already; the buyer decides (never asked twice);
- `need_by_unreachable`: the required date is not after the order date: no
  supplier can meet it; the buyer decides, shown the earliest feasible date.

What a supplier may be told: our target per line, the date we need, the
reply-by date and cut-off, and THAT a lower offer exists for a line. Never a
competitor's name, price or terms, and never the buyer's maximum. A request
never accepts or commits to buy.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Final, Literal

from bido_back.decimals import plain_decimal

# ISO 4217 minor units that differ from 2.
_MINOR_UNITS: Final[Mapping[str, int]] = {
    "BHD": 3,
    "BIF": 0,
    "CLP": 0,
    "DJF": 0,
    "GNF": 0,
    "IQD": 3,
    "ISK": 0,
    "JOD": 3,
    "JPY": 0,
    "KMF": 0,
    "KRW": 0,
    "KWD": 3,
    "LYD": 3,
    "OMR": 3,
    "PYG": 0,
    "RWF": 0,
    "TND": 3,
    "UGX": 0,
    "VND": 0,
    "VUV": 0,
    "XAF": 0,
    "XOF": 0,
    "XPF": 0,
}
DEFAULT_MINOR_UNIT: Final = 2
# Never ask with more decimals than this, even when the supplier quoted more.
MAX_PLACES: Final = 4
# The storage precision of prices (rfq_lines / proposal_lines are numeric(18, 6)).
STORED_PLACES: Final = 6

PriceStatus = Literal["in_target", "ask_met", "in_range", "above_max", "no_target"]
DeliveryStatus = Literal[
    "on_time", "no_need_by", "unknown", "late", "still_late", "need_by_unreachable"
]


# --- Numbers ------------------------------------------------------------------------------


def minor_unit(currency: str) -> int:
    return _MINOR_UNITS.get(currency.strip().upper(), DEFAULT_MINOR_UNIT)


def decimals(value: Decimal) -> int:
    """Significant decimals of a value: 46.90 -> 1, 0.055 -> 3, 90 -> 0."""
    exponent = value.normalize().as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def ask_places(currency: str, quoted: Decimal | None) -> int:
    """Decimals an ask may use: the currency's minor unit, or the supplier's finer precision."""
    places = minor_unit(currency)
    if quoted is not None:
        places = max(places, min(decimals(quoted), MAX_PLACES))
    return places


def _step(places: int) -> Decimal:
    return Decimal(1).scaleb(-places)


def floor_to(value: Decimal, places: int) -> Decimal:
    """Rounded down to `places` decimals, written with exactly that many (never `9E+1`)."""
    return value.quantize(_step(places), rounding=ROUND_FLOOR)


def round_to(value: Decimal, places: int) -> Decimal:
    return value.quantize(_step(places), rounding=ROUND_HALF_UP)


def quantize_ask(value: Decimal, places: int) -> Decimal:
    """An ask rounded down to `places`; a sub-unit price keeps its stored precision."""
    ask = floor_to(value, places)
    return ask if ask > 0 else floor_to(value, STORED_PLACES)


def plain(value: Decimal) -> Decimal:
    """The value without trailing zeros and never in exponent notation (90 -> 90, not 9E+1)."""
    return plain_decimal(value)


# --- Price --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PriceAsk:
    status: PriceStatus
    # The price we ask for (disclosed); None when nothing is asked or when we ask
    # for a better price without a number (`best_price`).
    ask: Decimal | None = None
    best_price: bool = False
    # The ask moved up from our previous one (the supplier moved first).
    conceded: bool = False

    @property
    def asks(self) -> bool:
        return self.ask is not None or self.best_price


# Share of the gap between our last ask and the supplier's price conceded when it moved.
COUNTER_CONCESSION: Final = Decimal("0.25")
FINAL_CONCESSION: Final = Decimal("0.5")


def price_position(
    *,
    offered: Decimal,
    target: Decimal | None,
    max_price: Decimal | None,
    places: int,
    history: Decimal | None = None,
    previous_ask: Decimal | None = None,
    previous_offer: Decimal | None = None,
    final_round: bool = False,
) -> PriceAsk:
    """Where one line's price stands and what we ask for it (see the module docstring).

    `previous_ask` / `previous_offer`: our last disclosed ask on this line and
    the supplier's price when we made it (from emails actually sent).
    `final_round`: the ask is for the best-and-final request.
    """
    if target is not None and max_price is not None and target > max_price:
        target = None  # never disclose more than the ceiling (the schema forbids it)
    if previous_ask is not None:
        # As stored by older code too ("5E+1"): the same value, written plainly.
        previous_ask = plain(previous_ask)
        if max_price is not None and previous_ask >= max_price:
            # The buyer lowered the maximum below our last ask: it is no anchor any more.
            previous_ask = previous_offer = None
    if target is not None and offered <= target:
        return PriceAsk("in_target")
    if previous_ask is not None and offered <= previous_ask:
        return PriceAsk("ask_met")
    above = max_price is not None and offered > max_price
    if target is None:
        usable = history is not None and (max_price is None or history <= max_price)
        if usable and history is not None and offered <= history:
            return PriceAsk("in_target")
        return PriceAsk("above_max" if above else "no_target", best_price=True)
    anchor = previous_ask if previous_ask is not None else quantize_ask(target, places)
    moved = previous_ask is not None and previous_offer is not None and offered < previous_offer
    if moved and not above:
        share = FINAL_CONCESSION if final_round else COUNTER_CONCESSION
        step = quantize_ask(anchor + (offered - anchor) * share, places)
        if anchor < step < offered and (max_price is None or step < max_price):
            return PriceAsk("in_range", ask=step, conceded=True)
    return PriceAsk("above_max" if above else "in_range", ask=anchor)


# --- Delivery -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeliveryAsk:
    status: DeliveryStatus
    # The date the line is required by (None: nothing required).
    required_by: date | None = None
    # Our ask: deliver by this date (late lines, once).
    deliver_by: date | None = None
    # The supplier's expected delivery when ordered on the order date: the earliest
    # feasible date on its own quote.
    expected: date | None = None

    @property
    def acceptable(self) -> bool:
        return self.status in ("on_time", "no_need_by")


def required_by(need_by: date | None, max_lead_days: int | None, order_date: date) -> date | None:
    """The date a line must arrive by: the need-by date, or the buyer's lead-time
    limit counted from the order date, whichever is earlier (moves with time)."""
    candidates = [d for d in (need_by,) if d is not None]
    if max_lead_days is not None:
        candidates.append(order_date + timedelta(days=max_lead_days))
    return min(candidates) if candidates else None


def delivery_position(
    *,
    required: date | None,
    expected: date | None,
    order_date: date,
    asked_before: bool,
) -> DeliveryAsk:
    """Where one line's delivery stands and what we ask (see the module docstring)."""
    if expected is None:
        return DeliveryAsk("unknown", required_by=required)
    if required is None:
        return DeliveryAsk("no_need_by", expected=expected)
    if expected <= required:
        return DeliveryAsk("on_time", required_by=required, expected=expected)
    if required <= order_date:
        return DeliveryAsk("need_by_unreachable", required_by=required, expected=expected)
    if asked_before:
        return DeliveryAsk("still_late", required_by=required, expected=expected)
    return DeliveryAsk("late", required_by=required, deliver_by=required, expected=expected)
