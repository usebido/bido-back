"""What an offer still lacks: the rules every "what is missing" list follows. No database."""

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from bido_back.followups.ledger import declined_positions
from bido_back.followups.missing import (
    DEFAULT_EXPECTED_DELIVERY,
    ExpectedDelivery,
    OfferedLine,
    OpenItem,
    RequestedLine,
    late_deliveries,
    open_items,
    stored_expected_delivery,
    validity_duration_given,
)
from bido_back.quotes.lead_time import LeadTime, encode_lead_time

TODAY = date(2026, 10, 9)
NEED_BY = date(2026, 10, 22)
BEARING = RequestedLine(
    id=uuid4(), position=1, description="Rolamento", mpn=None, part_number="000101", need_by=NEED_BY
)
SHAFT = RequestedLine(
    id=uuid4(), position=2, description="Eixo", mpn=None, part_number="000102", need_by=NEED_BY
)
WIRE = RequestedLine(id=uuid4(), position=3, description="Fio", mpn=None, need_by=None)


def stated(value: object, quote: str | None = None, *, review: bool = False) -> dict[str, object]:
    return {
        "value": None if value is None else str(value),
        "parsed": None if value is None else str(value),
        "source": {"kind": "email_body", "quote": quote} if quote is not None else None,
        "needs_review": review,
        "issues": ["working_days"] if review else [],
    }


def offered(
    line: RequestedLine,
    *,
    price: str | None = "19.50",
    min_quantity: str | None = None,
    lead: int | None = 10,
    fields: Mapping[str, object] | None = None,
    offer_fields: Mapping[str, object] | None = None,
) -> OfferedLine:
    return OfferedLine(
        rfq_line_id=line.id,
        values={
            "unit_price": Decimal(price) if price is not None else None,
            "min_quantity": Decimal(min_quantity) if min_quantity is not None else None,
            "lead_time_days": lead,
        },
        fields=fields or {},
        offer_fields=offer_fields or {},
        supplier_country="BR",
    )


def items(
    offers: Sequence[OfferedLine],
    *,
    requested: Sequence[RequestedLine] = (BEARING, SHAFT, WIRE),
    currency: str | None = "BRL",
    valid_until: date | None = date(2026, 10, 24),
    header_fields: Mapping[str, object] | None = None,
    declined: frozenset[int] = frozenset(),
    rfq_currency: str | None = None,
    helper: ExpectedDelivery | None = None,
) -> list[tuple[int | None, str, str]]:
    return [
        (i.position, i.field, i.question)
        for i in open_items(
            header_values={"currency": currency, "valid_until": valid_until},
            header_fields=header_fields or {},
            requested=requested,
            offered=offers,
            declined_positions=declined,
            rfq_currency=rfq_currency,
            reference_date=TODAY,
            expected_delivery=helper,
        )
    ]


def complete() -> list[OfferedLine]:
    return [offered(BEARING), offered(SHAFT), offered(WIRE)]


def test_a_complete_offer_lacks_nothing() -> None:
    assert items(complete()) == []


# --- Validity -----------------------------------------------------------------------------------


def test_a_validity_given_as_a_duration_is_answered() -> None:
    # "valid for 15 days": the extraction could not give a date (no message date).
    relative = {"valid_until": stated(None, "the quotation is valid for 15 days")}
    assert validity_duration_given(relative)
    assert items(complete(), valid_until=None, header_fields=relative) == []
    # A date the model computed from the duration, flagged for review: still answered.
    computed = {"valid_until": stated("2026-10-24", "valid for 15 calendar days", review=True)}
    assert items(complete(), header_fields=computed) == []
    # The extraction's own `validity_days`.
    assert items(complete(), valid_until=None, header_fields={"validity_days": stated(15)}) == []
    for text in ("válida por 30 dias", "Angebot 30 Tage gültig", "valable 30 jours"):
        assert validity_duration_given({"valid_until": stated(None, text)}), text


def test_a_missing_or_unclear_validity_is_asked() -> None:
    assert items(complete(), valid_until=None) == [(None, "valid_until", "value")]
    unclear = {"valid_until": stated("2026-10-24", "valid until end of month", review=True)}
    assert items(complete(), header_fields=unclear) == [(None, "valid_until", "confirm")]


# --- Currency -----------------------------------------------------------------------------------


def test_currency_only_when_neither_the_offer_nor_the_rfq_states_it() -> None:
    assert items(complete(), currency=None) == [(None, "currency", "value")]
    assert items(complete(), currency=None, rfq_currency="BRL") == []
    # Unclear, but the RFQ's own currency: nothing to ask.
    flagged = {"currency": stated("BRL", "R$", review=True)}
    assert items(complete(), header_fields=flagged, rfq_currency="BRL") == []
    assert items(complete(), header_fields=flagged) == [(None, "currency", "confirm")]
    # Nothing priced: the currency of nothing is not asked.
    unpriced = [offered(line, price=None) for line in (BEARING, SHAFT, WIRE)]
    assert (None, "currency", "value") not in items(unpriced, currency=None)


# --- Price and quantity -------------------------------------------------------------------------


def test_a_missing_price_is_asked_once_as_price_or_decline_and_nothing_else() -> None:
    assert items([offered(BEARING), offered(WIRE)]) == [(2, "unit_price", "price_or_decline")]
    # Offered without a price: the same single question.
    assert items([offered(BEARING), offered(SHAFT, price=None, lead=None), offered(WIRE)]) == [
        (2, "unit_price", "price_or_decline")
    ]


def test_a_declined_line_is_never_asked() -> None:
    assert items([offered(BEARING), offered(WIRE)], declined=frozenset({2})) == []


def test_a_single_price_applies_to_the_requested_quantity() -> None:
    # One price without a tier quantity: nothing to ask (RFQ-6 asked "the quantity tier").
    assert items(complete()) == []
    # Several breaks without quantities: which one applies to what?
    two = [offered(BEARING, price="19.50"), offered(BEARING, price="18.00"), offered(SHAFT)]
    assert items(two, requested=(BEARING, SHAFT)) == [(1, "quantity", "value")]
    # Several breaks, only one without a quantity (the base price): nothing to ask.
    tiered = [offered(BEARING, min_quantity="100"), offered(BEARING), offered(SHAFT)]
    assert items(tiered, requested=(BEARING, SHAFT)) == []


# --- Lead time and delivery ---------------------------------------------------------------------


def test_a_lead_time_for_the_whole_offer_applies_to_every_line() -> None:
    offer_level = {"lead_time_days": stated(10, "approximately 10 business days")}
    lines = [offered(line, lead=None, offer_fields=offer_level) for line in (BEARING, SHAFT, WIRE)]
    assert items(lines) == []


def test_a_lead_time_is_asked_only_when_none_is_given() -> None:
    # In business days, flagged for review: given (the date check decides, not a re-ask).
    business = {"lead_time_days": stated(7, "7 business days", review=True)}
    assert items([offered(BEARING, fields=business), offered(SHAFT), offered(WIRE)]) == []
    asked = open_items(
        header_values={"currency": "BRL", "valid_until": date(2026, 10, 24)},
        header_fields={},
        requested=(BEARING,),
        offered=[offered(BEARING, lead=None)],
    )
    assert [(i.position, i.field, i.question, i.need_by) for i in asked] == [
        (1, "lead_time", "value", NEED_BY)
    ]


def _helper(days: Mapping[UUID, tuple[int, bool] | None]) -> ExpectedDelivery:
    def expected(line: OfferedLine, reference: date) -> tuple[date, bool] | None:
        found = days.get(line.rfq_line_id) if line.rfq_line_id is not None else None
        return None if found is None else (reference + timedelta(days=found[0]), found[1])

    return expected


def test_without_a_delivery_helper_no_date_is_asked() -> None:
    assert items(complete(), helper=None) == []


def test_a_date_is_asked_only_where_the_lead_time_cannot_be_shown_to_meet_need_by() -> None:
    on_time = {BEARING.id: (10, True), SHAFT.id: (13, True), WIRE.id: (40, True)}
    # WIRE has no need-by date: any lead time is fine.
    assert items(complete(), helper=_helper(on_time)) == []
    # Approximately after the date ("approx. 10 business days"): ask for a date.
    approximate = {**on_time, SHAFT.id: (16, False)}
    found = open_items(
        header_values={"currency": "BRL", "valid_until": date(2026, 10, 24)},
        header_fields={},
        requested=(BEARING, SHAFT, WIRE),
        offered=complete(),
        reference_date=TODAY,
        expected_delivery=_helper(approximate),
    )
    assert [(i.position, i.field, i.question) for i in found] == [
        (2, "delivery_date", "deliver_by")
    ]
    assert (found[0].need_by, found[0].expected) == (NEED_BY, TODAY + timedelta(days=16))
    # No date can be computed ("a few additional days"): ask for one.
    unknown: dict[UUID, tuple[int, bool] | None] = {**on_time, SHAFT.id: None}
    assert items(complete(), helper=_helper(unknown)) == [(2, "delivery_date", "deliver_by")]
    # A definite date after the need-by is not a clarification: the negotiation asks it.
    late = {**on_time, SHAFT.id: (19, True)}
    assert items(complete(), helper=_helper(late)) == []


def test_a_definite_late_delivery_is_typed_for_the_negotiation() -> None:
    late = {BEARING.id: (10, True), SHAFT.id: (19, True), WIRE.id: (40, True)}
    found = late_deliveries(
        requested=(BEARING, SHAFT, WIRE),
        offered=complete(),
        reference_date=TODAY,
        expected_delivery=_helper(late),
    )
    assert [(i.position, i.field, i.question, i.need_by, i.expected) for i in found] == [
        (2, "delivery_date", "deliver_by", NEED_BY, TODAY + timedelta(days=19))
    ]
    # Approximate, unknown, on time or declined: not late (the first two are clarifications).
    approximate = {**late, SHAFT.id: (19, False)}
    assert not late_deliveries(
        requested=(BEARING, SHAFT, WIRE),
        offered=complete(),
        reference_date=TODAY,
        expected_delivery=_helper(approximate),
    )
    assert not late_deliveries(
        requested=(BEARING, SHAFT, WIRE),
        offered=complete(),
        declined_positions=frozenset({2}),
        reference_date=TODAY,
        expected_delivery=_helper(late),
    )
    assert not late_deliveries(
        requested=(BEARING, SHAFT, WIRE),
        offered=complete(),
        reference_date=TODAY,
        expected_delivery=None,
    )


def test_open_items_are_typed_for_the_email() -> None:
    [item] = open_items(
        header_values={"currency": "BRL", "valid_until": date(2026, 10, 24)},
        header_fields={},
        requested=(SHAFT,),
        offered=[],
    )
    assert item == OpenItem(
        position=2,
        rfq_line_id=SHAFT.id,
        line="Eixo (000102)",
        field="unit_price",
        question="price_or_decline",
        problem="missing",
    )
    followup = item.followup_item()
    assert (followup.position, followup.field, followup.question) == (
        2,
        "unit_price",
        "price_or_decline",
    )
    assert item.key == (SHAFT.id, "unit_price")


# --- What the extraction stores (prompt v5) -----------------------------------------------------


def quantity(basis: str) -> dict[str, object]:
    """A line's `_terms`: which quantity its price applies to (`quotes.terms.line_terms`)."""
    return {
        "_terms": {
            "quantity_basis": basis,
            "price_applies_to_requested_quantity": basis != "ambiguous",
        }
    }


def test_the_tier_is_asked_only_when_the_extraction_finds_it_ambiguous() -> None:
    # Two prices for the bearings without tier quantities, each read with its quantity.
    read = [
        offered(BEARING, fields=quantity("stated_tier")),
        offered(BEARING, price="18.00", fields=quantity("stated_tier")),
        offered(SHAFT, fields=quantity("requested_quantity")),
    ]
    assert items(read, requested=(BEARING, SHAFT)) == []
    ambiguous = [
        offered(BEARING, fields=quantity("ambiguous")),
        offered(BEARING, price="18.00", fields=quantity("ambiguous")),
        offered(SHAFT, fields=quantity("requested_quantity")),
    ]
    assert items(ambiguous, requested=(BEARING, SHAFT)) == [(1, "quantity", "value")]


def lead_terms(
    unit: str,
    amount: str,
    days: int,
    *,
    approximate: bool = False,
    review: bool = False,
) -> dict[str, object]:
    """A line's lead time as the extraction stores it: the statement and its calendar days."""
    lead = LeadTime.model_validate(
        {
            "unit": unit,
            "amount": Decimal(amount),
            "approximate": approximate,
            "basis": "order",
            "holiday_calendar": "BR",
        }
    )
    return {
        "lead_time_terms": {"value": f"{amount} {unit}", "parsed": encode_lead_time(lead, TODAY)},
        "lead_time_days": {"value": str(days), "parsed": days, "needs_review": review},
    }


def test_the_expected_delivery_comes_from_the_stored_lead_time() -> None:
    # "approximately 10 business days" from Friday 9 Oct (12 Oct a holiday in Brazil).
    approximate = offered(
        BEARING, lead=17, fields=lead_terms("business_days", "10", 17, approximate=True)
    )
    assert stored_expected_delivery(approximate, TODAY) == (date(2026, 10, 26), False)
    exact = offered(SHAFT, lead=19, fields=lead_terms("business_days", "12", 19))
    assert stored_expected_delivery(exact, TODAY) == (date(2026, 10, 28), True)
    assert stored_expected_delivery(exact, date(2026, 10, 10)) == (date(2026, 10, 28), True)
    # A person's correction: its calendar days, exact.
    fixed = {"lead_time_days": {"value": "30", "parsed": 30, "corrected": True}}
    assert stored_expected_delivery(offered(SHAFT, lead=30, fields=fixed), TODAY) == (
        TODAY + timedelta(days=30),
        True,
    )
    # Stated, but it needs review: no date can be told.
    review = offered(SHAFT, lead=19, fields=lead_terms("business_days", "12", 19, review=True))
    assert stored_expected_delivery(review, TODAY) is None
    # Read before prompt v5: calendar days, never exact (the unit written is unknown).
    legacy = offered(SHAFT, lead=20, fields={"lead_time_days": stated(20, "Lead time 20 days")})
    assert stored_expected_delivery(legacy, TODAY) == (TODAY + timedelta(days=20), False)
    assert stored_expected_delivery(offered(SHAFT, lead=None), TODAY) is None
    # It is the product's helper.
    assert DEFAULT_EXPECTED_DELIVERY is stored_expected_delivery


def test_the_extractions_validity_and_offer_lead_time_answer_their_questions() -> None:
    validity = {"validity": {"value": "15 calendar_days", "parsed": None}}
    assert validity_duration_given(validity)
    assert items(complete(), valid_until=None, header_fields=validity) == []
    lead = LeadTime.model_validate(
        {"unit": "business_days", "amount": Decimal(10), "approximate": True, "scope": "offer"}
    )
    offer_level = {
        "lead_time": {"value": "10 business_days", "parsed": encode_lead_time(lead, TODAY)}
    }
    lines = [offered(line, lead=None, offer_fields=offer_level) for line in (BEARING, SHAFT, WIRE)]
    assert items(lines) == []


def test_declined_lines_come_from_the_replies_and_the_offer_state() -> None:
    fields = {
        "_signals": {
            "declined_lines": [{"rfq_line_position": 2, "quote": "We cannot supply the shafts."}]
        },
        "_offer_state": {"declined_line_positions": [3], "withdrawn_line_positions": []},
    }
    assert declined_positions(fields) == frozenset({2, 3})
