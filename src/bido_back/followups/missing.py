"""What a supplier's offer still lacks: the one list every "what is missing" view uses.

The clarification Bido asks, the buyer's view of a supplier and the copilot's
`what_is_missing` all come from `open_items`, computed in code from the stored
offer (never from the model's own `missing_fields`). The rules ask only what is
truly missing, so a supplier is never asked for what it already said:

- Currency: asked only when the offer states none (or an unclear one) and the
  RFQ named none to the supplier (`rfq_currency`, a currency the RFQ email told
  suppliers to quote in). Not asked when nothing is priced.
- Validity: a date, or a duration ("valid for 15 days": the provenance's
  `validity_days`, or a duration in the cited text), answers it.
- Price: a requested line with no price is asked once as "the price for this
  position, or tell us you do not quote it". Nothing else is asked about it.
- Quantity: a single per-unit price applies to the requested quantity. The
  quantity (tier) is asked only when several price breaks lack quantities.
- Lead time: a lead time given for the whole offer applies to every line. It
  is asked only when none is given at all. Where the line has a need-by date,
  the given lead time must be shown to meet it (`expected_delivery`). When it
  cannot be (no date can be computed, or an approximate one lands after the
  need-by date), Bido asks for a date: "can you deliver by <need-by>? If not,
  the earliest date". A definite lead time that misses the date is not a
  clarification: it is a commercial ask, the negotiation's delivery-date
  request. Without a delivery helper, no date is asked.
- Declined: positions the supplier declined or withdrew are never asked again.

Each `OpenItem` is typed for the email renderer: the position, the field, the
question kind and, for delivery questions, the need-by date. Whether an item
was already asked (and must not be asked again) is the ledger's business
(`bido_back.followups.ledger`), not this module's.

`expected_delivery(offer_line, reference_date) -> (date, exact) | None` is the
lead-time-to-date helper: when would `offer_line` arrive if ordered on
`reference_date`, and is that date exact (False for "approximately")? None
when it cannot tell. It reads the line's provenance (`OfferedLine.fields`:
the lead time, its unit and basis) and the offer's (`offer_fields`: an
offer-level lead time), with the supplier's country for holidays. Until the
extraction records a lead time's unit and basis, `DEFAULT_EXPECTED_DELIVERY`
is None (no helper): callers take one as an argument, and the extraction
workstream installs its helper there.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Final, Literal, cast
from uuid import UUID

from bido_back.agents.catalog.contracts import FollowupField, FollowupItem, FollowupQuestion

Problem = Literal["missing", "unclear"]
# One item of the ledger: (requested line, or None for the whole offer; field).
ItemKey = tuple[UUID | None, FollowupField]

# Where an offer-level lead time may sit in the proposal's provenance.
OFFER_LEAD_TIME_KEYS: Final = ("lead_time_days", "lead_time")
# A validity given as a duration, in the proposal's provenance.
VALIDITY_DAYS_KEY: Final = "validity_days"

# "valid for 15 days", "15 calendar days", "válida por 30 dias", "30 Tage gültig",
# "valable 30 jours", "valida 30 giorni", "válida por 30 días", "4 weeks", "1 mês".
_DURATION = re.compile(
    r"\b\d{1,3}\s*(?:calendar\s+|business\s+|working\s+|corridos\s+|úteis\s+|uteis\s+)?"
    r"(?:days?|dias?|d[ií]as|tagen?|jours?|giorni|weeks?|semanas?|wochen|semaines?"
    r"|settimane|months?|m[eê]s(?:es)?|monate?|mois|mesi)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RequestedLine:
    id: UUID
    position: int
    description: str
    mpn: str | None
    part_number: str | None = None
    need_by: date | None = None

    @property
    def label(self) -> str:
        code = self.mpn or self.part_number
        return f"{self.description} ({code})" if code else self.description


@dataclass(frozen=True, slots=True)
class OfferedLine:
    """One offered line (one price break) and the offer around it."""

    rfq_line_id: UUID | None
    values: Mapping[str, object]  # unit_price, min_quantity, lead_time_days
    fields: Mapping[str, object]  # the line's provenance
    # The proposal's provenance (an offer-level lead time) and the supplier's country.
    offer_fields: Mapping[str, object] = field(default_factory=dict[str, object])
    supplier_country: str | None = None


# (offer_line, reference_date) -> (expected delivery date, exact) | None
ExpectedDelivery = Callable[[OfferedLine, date], tuple[date, bool] | None]

# The lead-time-to-date helper the product uses (see the module docstring): none yet.
DEFAULT_EXPECTED_DELIVERY: ExpectedDelivery | None = None


@dataclass(frozen=True, slots=True)
class OpenItem:
    """One thing the offer still lacks, typed for the clarification that may ask it."""

    position: int | None
    rfq_line_id: UUID | None
    line: str | None
    field: FollowupField
    question: FollowupQuestion
    problem: Problem
    # Delivery questions: the date the line is needed by, and when the stated lead
    # time would deliver (None: it cannot be told).
    need_by: date | None = None
    expected: date | None = None

    @property
    def key(self) -> ItemKey:
        return (self.rfq_line_id, self.field)

    def followup_item(self) -> FollowupItem:
        return FollowupItem(
            position=self.position,
            line=self.line,
            field=self.field,
            problem=self.problem,
            question=self.question,
            need_by=self.need_by,
        )


def open_items(
    *,
    header_values: Mapping[str, object],
    header_fields: Mapping[str, object],
    requested: Sequence[RequestedLine],
    offered: Sequence[OfferedLine],
    declined_positions: frozenset[int] = frozenset(),
    rfq_currency: str | None = None,
    reference_date: date | None = None,
    expected_delivery: ExpectedDelivery | None = None,
) -> list[OpenItem]:
    """Everything the offer still lacks, by the rules in the module docstring."""
    items: list[OpenItem] = []
    priced = any(o.values.get("unit_price") is not None for o in offered)

    currency = _currency(header_values.get("currency"), header_fields, rfq_currency)
    if currency is not None and priced:
        items.append(_offer_item("currency", currency))
    validity = _validity(header_values.get("valid_until"), header_fields)
    if validity is not None:
        items.append(_offer_item("valid_until", validity))

    for line in sorted(requested, key=lambda r: r.position):
        if line.position in declined_positions:
            continue
        tiers = [o for o in offered if o.rfq_line_id == line.id]
        items.extend(_line_items(line, tiers, reference_date, expected_delivery))
    return items


def _offer_item(name: FollowupField, problem: Problem) -> OpenItem:
    return OpenItem(
        position=None,
        rfq_line_id=None,
        line=None,
        field=name,
        question="value" if problem == "missing" else "confirm",
        problem=problem,
    )


def _line_items(
    line: RequestedLine,
    tiers: Sequence[OfferedLine],
    reference_date: date | None,
    expected_delivery: ExpectedDelivery | None,
) -> list[OpenItem]:
    def item(
        name: FollowupField,
        question: FollowupQuestion,
        problem: Problem,
        *,
        need_by: date | None = None,
        expected: date | None = None,
    ) -> OpenItem:
        return OpenItem(
            position=line.position,
            rfq_line_id=line.id,
            line=line.label,
            field=name,
            question=question,
            problem=problem,
            need_by=need_by,
            expected=expected,
        )

    priced = [t for t in tiers if t.values.get("unit_price") is not None]
    if not priced:
        # Asked once, and nothing else about this position until it has a price.
        if any(_stated(t.fields, "unit_price") for t in tiers):
            return [item("unit_price", "confirm", "unclear")]
        return [item("unit_price", "price_or_decline", "missing")]

    items: list[OpenItem] = []
    if all(_flagged(t.fields, "unit_price") for t in priced):
        items.append(item("unit_price", "confirm", "unclear"))

    # A single price applies to the requested quantity; only several breaks need theirs.
    if len(priced) > 1:
        without = [t for t in priced if t.values.get("min_quantity") is None]
        if len(without) > 1:
            items.append(item("quantity", "value", "missing"))
        elif any(_flagged(t.fields, "min_quantity") for t in priced):
            items.append(item("quantity", "confirm", "unclear"))

    if not any(_lead_time_given(t) for t in tiers):
        items.append(item("lead_time", "value", "missing", need_by=line.need_by))
        return items
    delivery = _delivery(line, tiers, reference_date, expected_delivery)
    if delivery is not None and delivery != "late":
        # No date can be computed ("a few additional days"), or only an approximate one
        # after the need-by date: ask for a date. A definite late date is not asked here.
        expected = delivery if isinstance(delivery, date) else None
        items.append(
            item("delivery_date", "deliver_by", "unclear", need_by=line.need_by, expected=expected)
        )
    return items


def _delivery(
    line: RequestedLine,
    tiers: Sequence[OfferedLine],
    reference_date: date | None,
    expected_delivery: ExpectedDelivery | None,
) -> Literal["unknown", "late"] | date | None:
    """Whether the stated lead time is shown to meet the line's need-by date: None (it is,
    or nothing to check), "unknown" (no date can be computed), the approximate date
    after the need-by, or "late" (a definite date after it)."""
    if line.need_by is None or expected_delivery is None or reference_date is None:
        return None
    given = [t for t in tiers if _lead_time_given(t)]
    known = [e for e in (expected_delivery(t, reference_date) for t in given) if e is not None]
    if not known:
        return "unknown" if given else None
    if any(day <= line.need_by for day, _ in known):
        return None
    if any(exact for _, exact in known):
        return "late"
    return min(day for day, _ in known)


def late_deliveries(
    *,
    requested: Sequence[RequestedLine],
    offered: Sequence[OfferedLine],
    declined_positions: frozenset[int] = frozenset(),
    reference_date: date | None,
    expected_delivery: ExpectedDelivery | None,
) -> list[OpenItem]:
    """Lines whose stated lead time gives a definite delivery after the need-by date.

    Not a clarification (nothing is unclear): the negotiation's delivery-date request,
    "can you deliver by <need-by>? If not, the earliest date", typed for it.
    """
    late: list[OpenItem] = []
    for line in sorted(requested, key=lambda r: r.position):
        if line.position in declined_positions:
            continue
        tiers = [o for o in offered if o.rfq_line_id == line.id]
        if _delivery(line, tiers, reference_date, expected_delivery) != "late":
            continue
        assert expected_delivery is not None and reference_date is not None
        dates = [
            e[0]
            for e in (expected_delivery(t, reference_date) for t in tiers)
            if e is not None and e[1]
        ]
        late.append(
            OpenItem(
                position=line.position,
                rfq_line_id=line.id,
                line=line.label,
                field="delivery_date",
                question="deliver_by",
                problem="unclear",
                need_by=line.need_by,
                expected=min(dates) if dates else None,
            )
        )
    return late


# --- Reading the provenance ---------------------------------------------------------------


def _entry(provenance: Mapping[str, object], key: str) -> Mapping[str, object] | None:
    entry = provenance.get(key)
    return cast(dict[str, object], entry) if isinstance(entry, dict) else None


def _flagged(provenance: Mapping[str, object], key: str) -> bool:
    entry = _entry(provenance, key)
    return entry is not None and entry.get("needs_review") is True


def _stated(provenance: Mapping[str, object], key: str) -> bool:
    """The supplier wrote something for `key`, even if it could not be used."""
    entry = _entry(provenance, key)
    return entry is not None and entry.get("value") is not None


def _currency(
    value: object, provenance: Mapping[str, object], rfq_currency: str | None
) -> Problem | None:
    if value is None:
        return None if rfq_currency else "missing"
    if _flagged(provenance, "currency") and value != rfq_currency:
        return "unclear"
    return None


def validity_duration_given(provenance: Mapping[str, object]) -> bool:
    """The offer's validity was given as a duration ("valid for 15 days").

    Read from `validity_days` when the extraction records it, else from the text
    the extraction cited for the validity.
    """
    days = _entry(provenance, VALIDITY_DAYS_KEY)
    if days is not None and (days.get("parsed") is not None or days.get("value") is not None):
        return True
    entry = _entry(provenance, "valid_until")
    source = entry.get("source") if entry is not None else None
    if not isinstance(source, dict):
        return False
    quote = cast(dict[str, object], source).get("quote")
    return isinstance(quote, str) and _DURATION.search(quote) is not None


def _validity(value: object, provenance: Mapping[str, object]) -> Problem | None:
    if validity_duration_given(provenance):
        return None
    if value is None:
        return "missing"
    return "unclear" if _flagged(provenance, "valid_until") else None


def offer_lead_time_given(offer_fields: Mapping[str, object]) -> bool:
    """A lead time stated for the whole offer, which applies to every line."""
    return any(_stated(offer_fields, key) for key in OFFER_LEAD_TIME_KEYS)


def _lead_time_given(offer: OfferedLine) -> bool:
    return (
        offer.values.get("lead_time_days") is not None
        or _stated(offer.fields, "lead_time_days")
        or offer_lead_time_given(offer.offer_fields)
    )
