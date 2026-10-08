"""The reply-by rule for preparing RFQ emails, without a database."""

from datetime import date

import pytest

from bido_back.copilot.journey_tools import reply_by_explained
from bido_back.rfqs.errors import ReplyByRequired
from bido_back.rfqs.readiness import reply_by_refusal, send_blockers

TODAY = date(2026, 10, 8)


@pytest.mark.parametrize(
    ("status", "due", "expected"),
    [
        ("draft", None, ["reply_by_missing"]),
        ("draft", date(2026, 10, 7), ["reply_by_past"]),
        ("draft", TODAY, []),
        ("draft", date(2026, 10, 22), []),
        ("open", None, ["reply_by_missing"]),
        ("open", date(2026, 10, 1), ["reply_by_past"]),
        ("closed", None, []),
        ("cancelled", date(2026, 10, 1), []),
    ],
)
def test_send_blockers(status: str, due: date | None, expected: list[str]) -> None:
    assert send_blockers(status, due, TODAY) == expected


def test_refusals_name_the_rfq_and_the_dates_only() -> None:
    assert reply_by_refusal("RFQ-2", "draft", TODAY, TODAY) is None
    assert reply_by_refusal("RFQ-2", "draft", None, TODAY) == (
        "RFQ-2 has no reply-by date: set the date suppliers must answer by "
        "(2026-10-08 or later) before sending"
    )
    past = reply_by_refusal("RFQ-2", "open", date(2026, 10, 1), TODAY)
    assert past is not None and past.startswith("RFQ-2's reply-by date 2026-10-01 is in the past")


def test_the_copilot_is_told_where_the_date_is_set() -> None:
    draft = reply_by_explained(ReplyByRequired("RFQ-2 has no reply-by date", draft=True))
    assert "set_rfq_terms(response_due=" in draft
    sent = reply_by_explained(ReplyByRequired("RFQ-2 has no reply-by date", draft=False))
    assert "set_rfq_terms" not in sent and "follow-up settings" in sent
