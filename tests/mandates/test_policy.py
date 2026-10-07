"""The pure checks every automatic send passes."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from pydantic import JsonValue

from bido_back.mandates.policy import (
    commitment_language,
    negotiation_terms_problem,
    recipients_problem,
)
from bido_back.mandates.schemas import ApproveMandateArgs, MandateLine, MandateSupplier

LINE = uuid4()


def mandate() -> ApproveMandateArgs:
    return ApproveMandateArgs(
        rfq_id=uuid4(),
        rfq_ref="RFQ-1",
        sender_user_id=uuid4(),
        currency="EUR",
        deadline=datetime(2026, 11, 1, tzinfo=UTC),
        max_rounds=3,
        max_messages_per_supplier_per_day=2,
        disclose_target_price=True,
        disclose_competing_offers=True,
        suppliers=[MandateSupplier(invite_id=uuid4(), supplier_name="Rhein", to=["a@x.test"])],
        lines=[
            MandateLine(
                rfq_line_id=LINE,
                position=1,
                description="MLCC",
                target_unit_price=Decimal("0.040"),
                max_unit_price=Decimal("0.060"),
            )
        ],
    )


def test_commitment_language_in_several_languages() -> None:
    for text in (
        "We accept your offer of 0.04 EUR.",
        "Please find our purchase order attached.",
        "Accettiamo la vostra offerta.",
        "Wir senden Ihnen die Bestellung.",
        "Segue a ordem de compra.",
        "Nous acceptons votre proposition.",
    ):
        assert commitment_language(text), text
    for text in (
        "Could you reach 0.04 EUR per piece?",
        "An acceptable lead time would be 42 days.",
        "Potreste fare 0,04 EUR al pezzo?",
    ):
        assert not commitment_language(text), text


def test_only_mandated_addresses_and_no_copies() -> None:
    assert recipients_problem(["A@X.test"], [], ["a@x.test"]) is None
    assert recipients_problem(["b@x.test"], [], ["a@x.test"]) == "recipient_not_in_mandate"
    assert recipients_problem(["a@x.test"], ["boss@x.test"], ["a@x.test"]) == "cc_not_in_mandate"


def terms(currency: str, line: object = None, ask: str | None = None) -> dict[str, JsonValue]:
    lines: list[JsonValue] = [] if line is None else [{"rfq_line_id": str(line), "ask": ask}]
    return {"currency": currency, "lines": lines}


def test_asked_prices_stay_within_the_maximum() -> None:
    assert negotiation_terms_problem(terms("EUR", LINE, "0.040000"), mandate()) is None
    assert negotiation_terms_problem(terms("EUR", LINE, None), mandate()) is None
    above = terms("EUR", LINE, "0.07")
    assert negotiation_terms_problem(above, mandate()) == "price_outside_mandate"
    other = terms("EUR", uuid4(), "0.04")
    assert negotiation_terms_problem(other, mandate()) == "line_not_in_mandate"
    assert negotiation_terms_problem(terms("USD"), mandate()) == "currency_not_in_mandate"
