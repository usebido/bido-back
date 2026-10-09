"""The buyer's negotiation rules as properties of a run (step 5 of the buyer journey).

`evals.journey` grades what the negotiation sweeps decided, sent and showed against
the rules below, not against a golden sequence of asks: which sweep first sees a
supplier's usable offer varies between live runs (extraction), and the planner's
next step follows from it. The rules hold whatever the timing. The record
(`Negotiation`) is read from the run:

- every request the planner decided (`negotiation_messages`: the typed decision,
  the offer version it answers, the asks per line, the reply-by date, the BAFO
  cut-off), whether it was sent, and which sweep decided it;
- every stored offer version of each supplier (price and lead time per line), and
  the versions the supplier declared its best and final offer (the script's truth);
- the buyer-visible state (`get_negotiation`) right before and right after each
  sweep;
- the negotiation e-mails the outbox recorded.

A "request" is a revised-offer, delivery-date or best-and-final (BAFO) request; a
reminder repeats a request and is no round of its own. A supplier "moved" on a
request when a later offer has a lower price on a line the request asked a price
for, or a shorter lead time on a line it asked a date for.

1. `requests:<supplier>`: the requests sent are rounds 1, 2, ... (one request per
   round), never more than the mandate's rounds, each answering the newest offer
   at that moment with the prices stored for it; the last allowed round is the
   BAFO; at most one BAFO, and nothing after it; a revised-offer request after the
   first carries a new ask (an unchanged one is the BAFO instead); a reminder
   repeats a sent request, once.
2. `bafo_common_cutoff`: every BAFO of the RFQ carries the same cut-off, the one
   the RFQ shows, no later than the negotiation deadline, with the cut-off's date
   as its reply-by date.
3. `final_round_only_bafo`: once the first BAFO is decided, every later request to
   any supplier is a BAFO: the final round is common.
4. `no_bafo_after_final_offer`: once the supplier declared an offer its best and
   final (as the e-mail says, or as stored), nothing is asked in answer to it or to
   a later offer, unless that later offer improved on the final one (the supplier
   moved on its own).
5. `asks_within_limits`: every price asked is above zero, at or below the line's
   maximum and below the supplier's own price at that moment, in cents (BRL,
   quoted in cents: at most 2 decimals).
6. `no_ask_in_target`: no price is asked on a line already at or below its target,
   or at or below our last ask on it.
7. `no_concession_without_move`: the first ask on a line is its target rounded down
   to cents; an ask never goes down; it goes up only on a line whose price the
   supplier lowered since our previous ask, strictly between that ask and the new
   price and below the maximum, by at most a quarter of the gap in a revised-offer
   request and half in the BAFO (concede late, in labelled steps). A supplier that
   never moved gets no concession.
8. `late_line_date_ask`: a priced line whose delivery misses its need-by date
   (its lead time counted in calendar days from the request's order date: late
   under any reading), never asked for a date before, is asked "deliver by
   <need-by>" in that request, rather than left silent; every date asked is the
   line's need-by date, once per line.
9. `stops_without_progress`: a request after the first needs the supplier to have
   moved on what the previous one asked. A supplier that answered a request
   without moving gets nothing in that sweep, and the buyer sees "Needs your
   decision" (or "Needs your answer" to its question, a clarification, or
   "Closed" because it declined, withdrew or the negotiation closed).
10. `request_when_due`: an offer to negotiate (a supplier's first offer, or one
    that moved on our last request; not declared final, as written or as stored)
    with a price above target and above our last ask, rounds left and the final
    round still open, gets its request in the sweep that sees it, or the buyer sees
    why not (a question, a review, a clarification, an e-mail on its way).
11. `visible_state`: what the buyer sees matches what was sent. Before a sweep, the
    view's next action is what the sweep decides (the same kind and asks); after
    it, "Waiting for the supplier's ..." names the last request sent (until its
    reply-by date, or the cut-off) and is never shown once the supplier answered;
    "countered" / "bafo_requested" match the requests sent; "Accepted internally"
    only with every priced line in target and on time; nothing decided is left
    unsent; every label starts with its phase in words.
12. `email_matches_decision`: every negotiation e-mail has a sent request behind it
    (the same Message-ID) and shows exactly the decided asks (prices, need-by
    dates), its reply-by date or the BAFO's cut-off (date and time), and no amount
    other than the supplier's own prices and our asks.
"""

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Final

from bido_back.negotiation.planner import MIN_BAFO_NOTICE
from evals.negotiation_text import amounts, written_dates

CENT: Final = Decimal("0.01")
# The most of the gap between our last ask and the supplier's new price an ask concedes.
COUNTER_SHARE: Final = Decimal("0.25")
BAFO_SHARE: Final = Decimal("0.5")
# The planner's next actions that write an e-mail.
EMAIL_ACTIONS: Final = frozenset({"revised_offer", "delivery_date", "bafo", "reminder"})
# What the buyer reads while Bido waits for the answer to a request.
WAITING_ON_REQUEST: Final = frozenset(
    {"revised_offer_sent", "delivery_date_sent", "bafo_sent", "reminder_sent", "reply_overdue"}
)
# After the reply-by date of an unanswered request: one reminder, then the offer stands.
AFTER_REPLY_BY: Final = frozenset(
    {"reminder_due", "reminder_sent", "reply_overdue", "no_reply_after_reminder"}
)
# Every label starts with its phase in words (`bido_back.negotiation.labels`).
PHASE_WORDS: Final[Mapping[str, str]] = {
    "not_started": "Not started",
    "waiting_for_supplier": "Waiting for the supplier",
    "waiting": "Waiting",
    "held_for_approval": "Held for your approval",
    "accepted_internally": "Accepted internally",
    "needs_decision": "Needs your decision",
    "needs_answer": "Needs your answer",
    "closed": "Closed",
}
# A request on its way or sent (`negotiation.service._live_cutoff`).
LIVE: Final = frozenset({"decided", "proposed", "sent"})
# After an answer that improved nothing we asked: the buyer decides, or answers the
# supplier's question; or the negotiation ended for another reason the buyer sees.
STOPPED: Final = frozenset({"needs_decision", "needs_answer"})
ENDED: Final = frozenset({"supplier_declined", "offer_withdrawn", "negotiation_closed"})
# A request already decided and on its way (being written, the daily cap, the mailbox).
EMAIL_WAIT: Final = frozenset({"preparing_email", "daily_limit", "mailbox_not_connected"})


# --- The record -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Quote:
    """A supplier's terms for one line in one stored offer version."""

    price: Decimal | None
    lead: int | None = None
    # The extraction left it for a person (not a usable value).
    review: bool = False


Offer = Mapping[str, Quote]  # part number -> the supplier's quote for it


@dataclass(frozen=True, slots=True)
class LineAsk:
    """What one request asked for one line, and the supplier's terms it answered."""

    part: str
    offered: Decimal | None = None
    # The lead time (days) the supplier offered then.
    offered_lead: int | None = None
    price: Decimal | None = None
    # A better price asked without a number (no target).
    best_price: bool = False
    deliver_by: date | None = None

    @property
    def asks_price(self) -> bool:
        return self.price is not None or self.best_price


@dataclass(frozen=True, slots=True)
class Request:
    """One negotiation e-mail the planner decided for a supplier, as stored."""

    supplier: str
    sequence: int
    round: int
    kind: str  # revised_offer, delivery_date, bafo, reminder
    state: str  # decided, proposed, sent, canceled, rejected, held
    offer_version: int
    order_date: date
    reply_by: date | None = None
    cutoff_at: datetime | None = None
    asks: tuple[LineAsk, ...] = ()
    reminder_of: int | None = None
    message_id: str | None = None
    # The sweep that decided it (its index in `Negotiation.sweeps`); None: not seen.
    sweep: int | None = None

    @property
    def sent(self) -> bool:
        return self.state == "sent"

    @property
    def reminder(self) -> bool:
        return self.kind == "reminder"


@dataclass(frozen=True, slots=True)
class LineView:
    """One line of a supplier's current offer as the buyer sees it (the planner's)."""

    part: str
    status: str  # priced, not_quoted, declined, blocked, question_open
    price: str | None = None  # in_target, ask_met, in_range, above_max, no_target
    delivery: str | None = None  # on_time, no_need_by, unknown, late, still_late, ...
    ask: Decimal | None = None
    best_price: bool = False
    deliver_by: date | None = None
    acceptable: bool = False


@dataclass(frozen=True, slots=True)
class SupplierView:
    """One supplier as the buyer sees it at one moment (`get_negotiation`)."""

    phase: str
    reason: str
    label: str
    next_action: str
    status: str
    until: datetime | None = None
    offer_version: int | None = None
    lines: tuple[LineView, ...] = ()
    # Its e-mails decided or awaiting approval: not sent yet.
    pending: int = 0


@dataclass(frozen=True, slots=True)
class Sweep:
    """One negotiation sweep and the buyer's view right before and right after it."""

    day: int
    at: datetime
    before: Mapping[str, SupplierView]
    after: Mapping[str, SupplierView]
    # The RFQ's common cut-off as the view shows it after the sweep.
    cutoff: datetime | None = None


@dataclass(frozen=True, slots=True)
class Email:
    """A negotiation e-mail the outbox recorded."""

    supplier: str
    message_id: str
    body: str
    at: datetime


@dataclass(frozen=True, slots=True)
class Limits:
    """The buyer's limits per part and the mandate's rounds and deadline."""

    targets: Mapping[str, Decimal]
    maxima: Mapping[str, Decimal]
    need_by: Mapping[str, date]
    max_rounds: int
    deadline: datetime | None = None


@dataclass(frozen=True, slots=True)
class Negotiation:
    limits: Limits
    suppliers: tuple[str, ...]
    requests: Mapping[str, tuple[Request, ...]]
    offers: Mapping[str, Mapping[int, Offer]]
    sweeps: tuple[Sweep, ...]
    emails: Mapping[str, tuple[Email, ...]] = field(default_factory=dict[str, tuple[Email, ...]])
    # Per supplier, the offer versions it declared its best and final offer (the e-mails).
    finals: Mapping[str, frozenset[int]] = field(default_factory=dict[str, frozenset[int]])
    # Per supplier, the offer versions stored as declared final (what the planner read).
    stored_finals: Mapping[str, frozenset[int]] = field(default_factory=dict[str, frozenset[int]])

    def final_versions(self, supplier: str) -> frozenset[int]:
        """The offer versions declared final, as written or as stored."""
        return self.finals.get(supplier, frozenset()) | self.stored_finals.get(
            supplier, frozenset()
        )

    def sent(self, supplier: str) -> list[Request]:
        """The requests sent to the supplier (reminders apart), in order."""
        return sorted(
            (r for r in self.requests.get(supplier, ()) if r.sent and not r.reminder),
            key=lambda r: r.sequence,
        )

    def sent_before(self, supplier: str, sweep: int) -> list[Request]:
        """The requests sent to the supplier by the sweeps before `sweep`."""
        return [r for r in self.sent(supplier) if r.sweep is not None and r.sweep < sweep]

    def decided_in(self, supplier: str, sweep: int) -> list[Request]:
        """The e-mails (reminders too) the sweep decided for the supplier."""
        return sorted(
            (r for r in self.requests.get(supplier, ()) if r.sweep == sweep),
            key=lambda r: r.sequence,
        )

    def quote(self, supplier: str, version: int, part: str) -> Quote | None:
        return self.offers.get(supplier, {}).get(version, {}).get(part)

    def price(self, supplier: str, request: Request, ask: LineAsk) -> Decimal | None:
        """The supplier's price for the asked line in the offer the request answers."""
        quote = self.quote(supplier, request.offer_version, ask.part)
        return quote.price if quote is not None and quote.price is not None else ask.offered


# --- Helpers ----------------------------------------------------------------------------------


def floor_cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_FLOOR)


def end_of_day(day: date) -> datetime:
    return datetime.combine(day, time(23, 59), tzinfo=UTC)


def moved(request: Request, offer: Offer) -> bool:
    """Whether `offer` improves on anything `request` asked: a lower price on a line asked
    for a price, a shorter lead time on a line asked for a date."""
    for ask in request.asks:
        quote = offer.get(ask.part)
        if quote is None or quote.price is None:
            continue
        if ask.asks_price and ask.offered is not None and quote.price < ask.offered:
            return True
        if (
            ask.deliver_by is not None
            and ask.offered_lead is not None
            and quote.lead is not None
            and quote.lead < ask.offered_lead
        ):
            return True
    return False


def last_asks(requests: Sequence[Request]) -> dict[str, Decimal]:
    """Our latest price asked per line, over these requests."""
    found: dict[str, Decimal] = {}
    for request in sorted(requests, key=lambda r: r.sequence):
        for ask in request.asks:
            if ask.price is not None:
                found[ask.part] = ask.price
    return found


def describe(request: Request) -> str:
    """One request for the report: round, kind, state, the offer it answers, its asks."""
    asks: list[str] = []
    for ask in request.asks:
        if ask.price is not None:
            asks.append(f"{ask.part} {ask.price}")
        elif ask.best_price:
            asks.append(f"{ask.part} better price")
        if ask.deliver_by is not None:
            asks.append(f"{ask.part} by {ask.deliver_by.isoformat()}")
    head = f"r{request.round} {request.kind} {request.state} v{request.offer_version}"
    if request.cutoff_at is not None:
        head += f" cut-off {request.cutoff_at:%Y-%m-%d %H:%M}"
    return f"{head}: {', '.join(asks) or '-'}"


def _new_asks(previous: Request, current: Request) -> bool:
    """Whether `current` asks something `previous` did not: another line or price, a date."""
    before = {(a.part, a.price, a.best_price) for a in previous.asks if a.asks_price}
    now = {(a.part, a.price, a.best_price) for a in current.asks if a.asks_price}
    dates_before = {(a.part, a.deliver_by) for a in previous.asks if a.deliver_by}
    dates_now = {(a.part, a.deliver_by) for a in current.asks if a.deliver_by}
    return bool(now - before) or bool(dates_now - dates_before)


# --- The rules --------------------------------------------------------------------------------


def check_requests(n: Negotiation, supplier: str) -> list[str]:
    """Rule 1: one request per round, the last allowed round is the BAFO, one BAFO."""
    problems: list[str] = []
    sent = n.sent(supplier)
    rounds = [r.round for r in sent]
    if rounds != list(range(1, len(sent) + 1)):
        problems.append(f"rounds {rounds}, want 1..{len(sent)} once each")
    if len(sent) > n.limits.max_rounds:
        problems.append(f"{len(sent)} requests for {n.limits.max_rounds} rounds")
    versions = [r.offer_version for r in sent]
    if any(later <= earlier for earlier, later in zip(versions, versions[1:], strict=False)):
        problems.append(f"requests answer offer versions {versions}: two for one offer")
    bafos = [r for r in sent if r.kind == "bafo"]
    if len(bafos) > 1:
        problems.append(f"{len(bafos)} best-and-final requests")
    if bafos and sent[-1].kind != "bafo":
        problems.append(f"a {sent[-1].kind} after the best-and-final request")
    last_round = n.limits.max_rounds
    if len(sent) >= last_round and sent[last_round - 1].kind != "bafo":
        problems.append(f"round {last_round} (the last allowed) is a {sent[last_round - 1].kind}")
    for previous, current in zip(sent, sent[1:], strict=False):
        if current.kind != "bafo" and not _new_asks(previous, current):
            problems.append(
                f"round {current.round} repeats round {previous.round}'s asks unchanged "
                "(an unchanged request is the BAFO)"
            )
    reminded = Counter(r.reminder_of for r in n.requests.get(supplier, ()) if r.reminder and r.sent)
    for of, count in reminded.items():
        if of not in {r.sequence for r in sent}:
            problems.append(f"a reminder of #{of}, a request never sent")
        if count > 1:
            problems.append(f"request #{of} reminded {count} times")
    for request in sent:
        seen = (
            n.sweeps[request.sweep].before.get(supplier)
            if request.sweep is not None and request.sweep < len(n.sweeps)
            else None
        )
        if seen is not None and seen.offer_version not in (None, request.offer_version):
            problems.append(
                f"round {request.round} answers offer v{request.offer_version}; the newest "
                f"was v{seen.offer_version}"
            )
        for ask in request.asks:
            quote = n.quote(supplier, request.offer_version, ask.part)
            if (
                ask.offered is not None
                and quote is not None
                and quote.price is not None
                and ask.offered.normalize() != quote.price.normalize()
            ):
                problems.append(
                    f"round {request.round} {ask.part}: decided on {ask.offered}, the offer "
                    f"v{request.offer_version} says {quote.price}"
                )
    return problems


def check_cutoff(n: Negotiation) -> list[str]:
    """Rule 2: every BAFO carries the RFQ's one cut-off, no later than the deadline."""
    problems: list[str] = []
    bafos = [r for s in n.suppliers for r in n.sent(s) if r.kind == "bafo"]
    cutoffs = {r.cutoff_at for r in bafos if r.cutoff_at is not None}
    if any(r.cutoff_at is None for r in bafos):
        problems.append("a best-and-final request without a cut-off")
    if len(cutoffs) > 1:
        problems.append(
            "cut-offs differ: "
            + "; ".join(f"{r.supplier} {r.cutoff_at}" for r in bafos if r.cutoff_at)
        )
    deadline = n.limits.deadline
    problems += [
        f"cut-off {c} after the deadline {deadline}"
        for c in cutoffs
        if deadline is not None and c > deadline
    ]
    problems += [
        f"{r.supplier}: reply-by {r.reply_by} is not the cut-off's date"
        for r in bafos
        if r.cutoff_at is not None and r.reply_by != r.cutoff_at.date()
    ]
    shown = {s.cutoff for s in n.sweeps if s.cutoff is not None}
    if cutoffs and shown - cutoffs:
        problems.append(f"the RFQ shows the cut-off {sorted(shown - cutoffs)}, not the BAFOs'")
    return problems


def check_final_round(n: Negotiation) -> list[str]:
    """Rule 3: once the first BAFO is decided, every later request is a BAFO."""
    # A BAFO that never reached a supplier (canceled, rejected, held) binds nobody.
    started = [
        r.sweep
        for s in n.suppliers
        for r in n.requests.get(s, ())
        if r.kind == "bafo" and r.state in LIVE and r.sweep is not None
    ]
    if not started:
        return []
    first = min(started)
    return [
        f"{r.supplier} day {n.sweeps[r.sweep].day}: a {r.kind} in the final round"
        for s in n.suppliers
        for r in n.requests.get(s, ())
        if r.sweep is not None
        and r.sweep >= first
        and r.kind not in ("bafo", "reminder")
        and r.state != "canceled"
    ]


def check_final_offers(n: Negotiation) -> list[str]:
    """Rule 4: after a final offer (as written or as stored) nothing is asked, unless a later
    offer of the supplier's own improved on it."""
    problems: list[str] = []
    for s in n.suppliers:
        offers = n.offers.get(s, {})
        for final in sorted(n.final_versions(s)):
            for r in n.requests.get(s, ()):
                if r.reminder or r.offer_version < final:
                    continue
                later = offers.get(r.offer_version)
                if r.offer_version == final:
                    why = "its declared final offer"
                elif later is None or not improves(later, offers.get(final, {})):
                    why = f"no better than its declared final offer v{final}"
                else:
                    continue
                problems.append(f"{s}: a {r.kind} ({r.state}) answers v{r.offer_version}, {why}")
    return problems


def improves(later: Offer, earlier: Offer) -> bool:
    """Whether `later` beats `earlier` on some line: a lower price or a shorter lead time."""
    for part, quote in later.items():
        before = earlier.get(part)
        if before is None or quote.price is None or before.price is None:
            continue
        if quote.price < before.price:
            return True
        if quote.lead is not None and before.lead is not None and quote.lead < before.lead:
            return True
    return False


def check_limits(n: Negotiation) -> list[str]:
    """Rule 5: every price asked is positive, in cents, below the maximum and the offer."""
    problems: list[str] = []
    for s in n.suppliers:
        for request in n.sent(s):
            for ask in request.asks:
                if ask.price is None:
                    continue
                where = f"{s} r{request.round} {ask.part} ask {ask.price}"
                maximum = n.limits.maxima.get(ask.part)
                offered = n.price(s, request, ask)
                if ask.price <= 0:
                    problems.append(f"{where}: not positive")
                if ask.price != ask.price.quantize(CENT):
                    problems.append(f"{where}: more than 2 decimals")
                if maximum is not None and ask.price > maximum:
                    problems.append(f"{where}: above the maximum {maximum}")
                if offered is not None and ask.price >= offered:
                    problems.append(f"{where}: not below its own price {offered}")
    return problems


def check_in_target(n: Negotiation) -> list[str]:
    """Rule 6: no price asked on a line at or below its target or our last ask."""
    problems: list[str] = []
    for s in n.suppliers:
        sent = n.sent(s)
        for index, request in enumerate(sent):
            before = last_asks(sent[:index])
            for ask in request.asks:
                price = n.price(s, request, ask)
                if not ask.asks_price or price is None:
                    continue
                target = n.limits.targets.get(ask.part)
                where = f"{s} r{request.round} {ask.part} at {price}"
                if target is not None and price <= target:
                    problems.append(f"{where}: asked for less although within target {target}")
                last = before.get(ask.part)
                if last is not None and price <= last:
                    problems.append(f"{where}: asked again although it meets our ask {last}")
    return problems


def check_concessions(n: Negotiation) -> list[str]:
    """Rule 7: anchor at the target; concede only on a line the supplier moved, late."""
    problems: list[str] = []
    for s in n.suppliers:
        previous: dict[str, tuple[Decimal, Decimal | None]] = {}  # part -> (ask, price then)
        for request in n.sent(s):
            share = BAFO_SHARE if request.kind == "bafo" else COUNTER_SHARE
            for ask in request.asks:
                if ask.price is None:
                    continue
                now = n.price(s, request, ask)
                where = f"{s} r{request.round} {ask.part} ask {ask.price}"
                target = n.limits.targets.get(ask.part)
                maximum = n.limits.maxima.get(ask.part)
                if ask.part not in previous:
                    if target is not None and ask.price != floor_cents(target):
                        problems.append(f"{where}: the first ask is not the target {target}")
                else:
                    last, then = previous[ask.part]
                    if ask.price < last:
                        problems.append(f"{where}: below our previous ask {last}")
                    elif ask.price > last:
                        if then is None or now is None or now >= then:
                            problems.append(
                                f"{where}: conceded from {last} although the price did not "
                                f"move ({then} -> {now})"
                            )
                        elif not ask.price < now:
                            problems.append(f"{where}: conceded up to its price {now}")
                        elif maximum is not None and not ask.price < maximum:
                            problems.append(f"{where}: conceded up to the maximum {maximum}")
                        elif ask.price > last + (now - last) * share:
                            problems.append(
                                f"{where}: conceded more than {share} of the gap {last}..{now}"
                            )
                previous[ask.part] = (ask.price, now)
    return problems


def _dated_lines(n: Negotiation, supplier: str, request: Request) -> set[str]:
    """The lines whose delivery date the planner could tell right before the request's
    sweep: priced (negotiable), with a known delivery that the need-by can still meet."""
    if request.sweep is None or request.sweep >= len(n.sweeps):
        return set()
    view = n.sweeps[request.sweep].before.get(supplier)
    if view is None:
        return set()
    return {
        line.part
        for line in view.lines
        if line.status == "priced" and line.delivery not in ("unknown", "need_by_unreachable")
    }


def check_late_lines(n: Negotiation) -> list[str]:
    """Rule 8: a late line is asked "deliver by <need-by>", once."""
    problems: list[str] = []
    for s in n.suppliers:
        asked: Counter[str] = Counter()
        for request in n.sent(s):
            known = _dated_lines(n, s, request)
            dated = {a.part: a.deliver_by for a in request.asks if a.deliver_by is not None}
            for part, by in dated.items():
                need = n.limits.need_by.get(part)
                if need is not None and by != need:
                    problems.append(
                        f"{s} r{request.round} {part}: delivery asked by {by}, the need-by is "
                        f"{need}"
                    )
            for part, quote in n.offers.get(s, {}).get(request.offer_version, {}).items():
                need = n.limits.need_by.get(part)
                if (
                    need is None
                    or part not in known
                    or part in dated
                    or asked[part]
                    or quote.price is None
                    or quote.lead is None
                    or quote.review
                    or need <= request.order_date
                ):
                    continue
                if request.order_date + timedelta(days=quote.lead) > need:
                    problems.append(
                        f"{s} r{request.round} {part}: {quote.lead} days from "
                        f"{request.order_date} miss the need-by {need}; no date asked"
                    )
            asked.update(dated.keys())
        problems += [
            f"{s} {part}: a date asked {count} times" for part, count in asked.items() if count > 1
        ]
    return problems


def check_progress(n: Negotiation) -> list[str]:
    """Rule 9: no further request without a move; the buyer sees the stop."""
    problems: list[str] = []
    for s in n.suppliers:
        sent = n.sent(s)
        for previous, current in zip(sent, sent[1:], strict=False):
            offer = n.offers.get(s, {}).get(current.offer_version)
            if offer is not None and not moved(previous, offer):
                problems.append(
                    f"{s}: round {current.round} ({current.kind}) although offer "
                    f"v{current.offer_version} improved nothing round {previous.round} asked"
                )
        for index, sweep in enumerate(n.sweeps):
            answer = _answer(n, s, index)
            if answer is None or answer.moved:
                continue
            decided = [r for r in n.decided_in(s, index) if not r.reminder]
            if decided:
                problems.append(
                    f"{s} day {sweep.day}: a {decided[0].kind} although its answer to round "
                    f"{answer.request.round} improved nothing"
                )
            after = sweep.after.get(s)
            if after is not None and not (
                after.phase in STOPPED
                or after.reason in ENDED
                or after.reason.startswith("clarification")
            ):
                problems.append(
                    f"{s} day {sweep.day}: answered round {answer.request.round} without "
                    f"improving; the buyer sees {after.label!r}"
                )
    return problems


@dataclass(frozen=True, slots=True)
class Answer:
    """A supplier's answer to our last request, as a sweep sees it."""

    request: Request
    offer: Offer
    moved: bool


def _answer(n: Negotiation, supplier: str, sweep: int) -> Answer | None:
    """The supplier's answer to its last request sent before the sweep: the newer offer the
    sweep sees (None when there is none, after a BAFO, or for a declared final offer)."""
    sent = n.sent_before(supplier, sweep)
    view = n.sweeps[sweep].before.get(supplier)
    if not sent or view is None or view.offer_version is None:
        return None
    last = sent[-1]
    version = view.offer_version
    if last.kind == "bafo" or version <= last.offer_version:
        return None
    offer = n.offers.get(supplier, {}).get(version)
    if offer is None or version in n.final_versions(supplier):
        return None
    return Answer(last, offer, moved(last, offer))


def _to_negotiate(n: Negotiation, supplier: str, sweep: int) -> tuple[Offer, Request | None] | None:
    """The supplier's newest offer the sweep sees, when it is one to negotiate: its first
    offer, or a newer one that moved on our last request (not after a BAFO, not final).
    With the last request sent before the sweep (None: the first offer)."""
    view = n.sweeps[sweep].before.get(supplier)
    if view is None or view.offer_version is None:
        return None
    version = view.offer_version
    offer = n.offers.get(supplier, {}).get(version)
    if offer is None or version in n.final_versions(supplier):
        return None
    sent = n.sent_before(supplier, sweep)
    if not sent:
        return offer, None
    last = sent[-1]
    if last.kind == "bafo" or version <= last.offer_version or not moved(last, offer):
        return None
    return offer, last


def _why_not(view: SupplierView | None) -> bool:
    """The buyer sees why no request goes out: a question, a review, a clarification, an
    e-mail already on its way."""
    return view is not None and (
        view.phase in ("needs_answer", "held_for_approval")
        or (view.phase == "needs_decision" and view.reason != "no_progress")
        or view.reason.startswith("clarification")
        or view.reason in EMAIL_WAIT
    )


def check_request_due(n: Negotiation) -> list[str]:
    """Rule 10: an offer to negotiate gets its request (or the buyer sees why not)."""
    problems: list[str] = []
    deadline = n.limits.deadline
    targets = n.limits.targets
    for s in n.suppliers:
        for index, sweep in enumerate(n.sweeps):
            found = _to_negotiate(n, s, index)
            if found is None:
                continue
            offer, last = found
            before = n.sent_before(s, index)
            asked = last_asks(before)
            due = sorted(
                part
                for part, quote in offer.items()
                if quote.price is not None
                and not quote.review
                and part in targets
                and quote.price > targets[part]
                and (part not in asked or quote.price > asked[part])
            )
            cutoff = n.sweeps[index - 1].cutoff if index > 0 else None
            open_round = (cutoff is None or cutoff - sweep.at >= MIN_BAFO_NOTICE) and (
                deadline is None or deadline - sweep.at >= MIN_BAFO_NOTICE
            )
            if not due or len(before) >= n.limits.max_rounds or not open_round:
                continue
            if any(not r.reminder for r in n.decided_in(s, index)):
                continue
            after = sweep.after.get(s)
            if _why_not(after):
                continue
            what = f"moved on round {last.round}" if last is not None else "quoted"
            shown = after.label if after is not None else None
            problems.append(
                f"{s} day {sweep.day}: {what} with {due} above target, but got no request; "
                f"the buyer sees {shown!r}"
            )
    return problems


def _expected_status(sent: Sequence[Request]) -> str:
    if any(r.kind == "bafo" for r in sent):
        return "bafo_requested"
    return "countered" if sent else "open"


def _asks_shown(view: SupplierView) -> set[tuple[str, Decimal | None, bool, date | None]]:
    return {
        (line.part, line.ask, line.best_price, line.deliver_by)
        for line in view.lines
        if line.ask is not None or line.best_price or line.deliver_by is not None
    }


def _asks_decided(request: Request) -> set[tuple[str, Decimal | None, bool, date | None]]:
    return {(a.part, a.price, a.best_price, a.deliver_by) for a in request.asks}


def check_visible(n: Negotiation) -> list[str]:
    """Rule 11: the buyer-visible state matches what the sweep sends and what was sent."""
    problems: list[str] = []
    for index, sweep in enumerate(n.sweeps):
        for s in n.suppliers:
            day = f"{s} day {sweep.day}"
            decided = n.decided_in(s, index)
            before = sweep.before.get(s)
            if before is not None:
                problems += _predicts(day, before, decided)
            after = sweep.after.get(s)
            if after is not None:
                sent = [r for r in n.sent(s) if r.sweep is not None and r.sweep <= index]
                problems += _shows(day, after, sent, sweep.at)
    return problems


def _predicts(day: str, view: SupplierView, decided: Sequence[Request]) -> list[str]:
    """What the buyer saw before the sweep is what the sweep decided."""
    kinds = [r.kind for r in decided]
    if view.next_action in EMAIL_ACTIONS:
        if kinds != [view.next_action]:
            return [f"{day}: the buyer saw {view.label!r}; the sweep decided {kinds or 'nothing'}"]
        if view.next_action != "reminder" and _asks_shown(view) != _asks_decided(decided[0]):
            return [
                f"{day}: the buyer saw asks {sorted(map(str, _asks_shown(view)))}; the "
                f"{decided[0].kind} asks {sorted(map(str, _asks_decided(decided[0])))}"
            ]
        return []
    if decided and view.phase not in ("held_for_approval", "waiting"):
        return [f"{day}: the buyer saw {view.label!r}; the sweep decided {kinds}"]
    return []


def _shows(day: str, view: SupplierView, sent: Sequence[Request], at: datetime) -> list[str]:
    """What the buyer sees after the sweep follows from the requests actually sent."""
    problems: list[str] = []
    words = PHASE_WORDS.get(view.phase)
    if words is None or not view.label.startswith(words):
        problems.append(f"{day}: phase {view.phase} reads {view.label!r}")
    if view.pending:
        problems.append(f"{day}: {view.pending} e-mail(s) decided but not sent ({view.label!r})")
    expected = _expected_status(sent)
    if view.status in ("open", "countered", "bafo_requested") and view.status != expected:
        problems.append(f"{day}: shown as {view.status}, the requests sent make it {expected}")
    if view.phase == "accepted_internally":
        off = [line.part for line in view.lines if line.status == "priced" and not line.acceptable]
        if off:
            problems.append(f"{day}: accepted internally with {off} off target or late")
    last = sent[-1] if sent else None
    if last is None or view.offer_version is None:
        if view.reason in WAITING_ON_REQUEST:
            problems.append(f"{day}: {view.label!r}, but no request was sent")
        return problems
    if view.offer_version > last.offer_version:
        if view.reason in WAITING_ON_REQUEST:
            problems.append(f"{day}: {view.label!r}, but the supplier answered round {last.round}")
        return problems
    if last.kind == "bafo" and last.cutoff_at is not None and at < last.cutoff_at:
        if (view.reason, view.until) != ("bafo_sent", last.cutoff_at):
            problems.append(f"{day}: the BAFO waits until {last.cutoff_at}; shown {view.label!r}")
    elif last.kind == "bafo":
        if view.phase != "closed":
            problems.append(f"{day}: the BAFO's cut-off passed; shown {view.label!r}")
    elif last.reply_by is not None and at <= end_of_day(last.reply_by):
        want = (f"{last.kind}_sent", end_of_day(last.reply_by))
        if (view.reason, view.until) != want:
            problems.append(
                f"{day}: round {last.round} ({last.kind}) waits until {last.reply_by}; shown "
                f"{view.label!r}"
            )
    elif view.reason not in AFTER_REPLY_BY:
        problems.append(
            f"{day}: round {last.round} unanswered after its reply-by; shown {view.label!r}"
        )
    return problems


def check_emails(n: Negotiation) -> list[str]:
    """Rule 12: every negotiation e-mail is a sent decision, showing exactly its values."""
    problems: list[str] = []
    for s in n.suppliers:
        emails = {e.message_id: e for e in n.emails.get(s, ())}
        sent = [r for r in n.requests.get(s, ()) if r.sent]
        ids = {r.message_id for r in sent}
        for key, email in emails.items():
            if key not in ids:
                problems.append(f"{s}: an e-mail with no sent request behind it ({email.at})")
        for request in sent:
            email = emails.get(request.message_id or "")
            where = f"{s} r{request.round} {request.kind}"
            if email is None:
                problems.append(f"{where}: sent, no e-mail recorded")
                continue
            problems += [f"{where}: {p}" for p in _email_problems(n, s, request, email)]
    return problems


def _email_problems(n: Negotiation, supplier: str, request: Request, email: Email) -> list[str]:
    problems: list[str] = []
    body = email.body
    written = amounts(body)
    values: set[Decimal] = set()
    for readings in written:
        values |= readings
    dates = written_dates(body, email.at.date() - timedelta(days=1))
    allowed: set[Decimal] = set()
    for ask in request.asks:
        if ask.price is not None:
            allowed.add(ask.price.normalize())
            if ask.price.normalize() not in values:
                problems.append(f"ask {ask.price} for {ask.part} not written")
        if ask.deliver_by is not None and ask.deliver_by not in dates:
            problems.append(f"delivery by {ask.deliver_by} for {ask.part} not written")
        for price in (ask.offered, n.price(supplier, request, ask)):
            if price is not None:
                allowed.add(price.normalize())
    cutoff = request.cutoff_at
    if cutoff is not None:
        if cutoff.date() not in dates or f"{cutoff:%H:%M}" not in body:
            problems.append(f"cut-off {cutoff:%Y-%m-%d %H:%M} not written")
    elif request.reply_by is not None and request.reply_by not in dates:
        problems.append(f"reply-by {request.reply_by} not written")
    unknown = sorted(str(min(r)) for r in written if r and not r & allowed)
    if unknown:
        problems.append(f"amounts not decided: {unknown}")
    return problems


Rule = Callable[[Negotiation], list[str]]
RULES: Final[tuple[tuple[str, Rule], ...]] = (
    ("bafo_common_cutoff", check_cutoff),
    ("final_round_only_bafo", check_final_round),
    ("no_bafo_after_final_offer", check_final_offers),
    ("asks_within_limits", check_limits),
    ("no_ask_in_target", check_in_target),
    ("no_concession_without_move", check_concessions),
    ("late_line_date_ask", check_late_lines),
    ("stops_without_progress", check_progress),
    ("request_when_due", check_request_due),
    ("visible_state", check_visible),
    ("email_matches_decision", check_emails),
)


def grade(n: Negotiation) -> list[tuple[str, list[str]]]:
    """Every rule's problems (empty: the rule holds), per supplier first."""
    results = [(f"requests:{s}", check_requests(n, s)) for s in n.suppliers]
    return results + [(name, rule(n)) for name, rule in RULES]
