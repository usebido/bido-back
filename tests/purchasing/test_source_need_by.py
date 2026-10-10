"""An RFQ line's need-by is the line's first dated purchase, not the first need of all.

2026-10-10: every line of an RFQ drafted from Pedro's SAP exports asked delivery by
12/10, the first shortage of the whole plan, including lines first short in December.
"""

from datetime import date
from decimal import Decimal

from bido_back.purchasing.sources.contracts import AssessmentLine, DatedQuantity, TimingCheck
from bido_back.purchasing.sources.snapshots import late_arrival, rfq_need_by


def _line(
    need_by: date | None,
    schedule: list[tuple[date, str]],
    arrival: date | None = None,
) -> AssessmentLine:
    return AssessmentLine(
        part_number="100000102",
        need_by=need_by,
        need_schedule=[DatedQuantity(need_by=d, quantity=Decimal(q)) for d, q in schedule],
        timing=(
            TimingCheck(order_date=date(2026, 10, 8), outcome="meets", recommended_arrival=arrival)
            if arrival is not None
            else None
        ),
    )


def test_the_first_dated_purchase_sets_the_need_by() -> None:
    line = _line(date(2026, 10, 12), [(date(2026, 12, 7), "55"), (date(2026, 12, 21), "30")])
    assert rfq_need_by(line) == date(2026, 12, 7)


def test_dates_that_buy_nothing_are_skipped() -> None:
    line = _line(date(2026, 10, 12), [(date(2026, 10, 12), "0"), (date(2026, 10, 26), "45")])
    assert rfq_need_by(line) == date(2026, 10, 26)


def test_without_a_schedule_the_first_need_stays() -> None:
    assert rfq_need_by(_line(date(2026, 10, 12), [])) == date(2026, 10, 12)
    assert rfq_need_by(_line(None, [])) is None


def test_an_arrival_after_the_need_by_is_flagged_for_the_buyer() -> None:
    motor = _line(date(2026, 10, 12), [(date(2026, 10, 12), "3")], arrival=date(2026, 11, 22))
    assert late_arrival(motor) == date(2026, 11, 22)
    in_time = _line(date(2026, 10, 12), [(date(2026, 11, 23), "210")], arrival=date(2026, 10, 18))
    assert late_arrival(in_time) is None
    assert late_arrival(_line(date(2026, 10, 12), [(date(2026, 10, 12), "3")])) is None
