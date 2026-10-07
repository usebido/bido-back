"""What the scan keeps, what the model sees, and the pt-BR display strings."""

from datetime import UTC, datetime

from bido_back.agents.catalog.contracts import RfqCandidateVerdict
from bido_back.onboarding.candidates import (
    StoredAttachment,
    classifier_input,
    display_attachments,
    display_date,
    keep,
)

from .conftest import summary

ITEM = summary(
    "m1",
    "Cotação – Válvulas esfera",
    recipients=("vendas@metalflex.example", "b@metalflex.example", "c@outra.example"),
)


def test_the_model_sees_domains_not_addresses_and_no_body() -> None:
    task = classifier_input(["c1"], [ITEM])
    [candidate] = task.candidates
    assert candidate.recipient_count == 3
    assert candidate.recipient_domains == ["metalflex.example", "outra.example"]
    assert "vendas@" not in task.model_dump_json()
    assert candidate.sent_on == datetime(2026, 9, 12, tzinfo=UTC).date()


def test_confident_rfqs_are_preselected_without_a_note() -> None:
    kept = keep("c1", ITEM, RfqCandidateVerdict(id="c1", is_rfq=True, confident=True))
    assert kept is not None
    assert kept["preselected"] is True and kept["note"] is None
    assert kept["provider_message_id"] == "m1"


def test_doubtful_candidates_are_kept_unticked_with_a_note() -> None:
    doubtful = RfqCandidateVerdict(
        id="c1", is_rfq=False, confident=False, note="Parece uma resposta"
    )
    kept = keep("c1", ITEM, doubtful)
    assert kept is not None and kept["preselected"] is False
    assert kept["note"] == "Parece uma resposta"

    unsure_rfq = keep("c1", ITEM, RfqCandidateVerdict(id="c1", is_rfq=True, confident=False))
    assert unsure_rfq is not None and unsure_rfq["preselected"] is False
    assert unsure_rfq["note"]


def test_sure_non_rfqs_are_dropped_and_missing_verdicts_kept_as_doubtful() -> None:
    assert keep("c1", ITEM, RfqCandidateVerdict(id="c1", is_rfq=False, confident=True)) is None
    missing = keep("c1", ITEM, None)
    assert missing is not None and missing["preselected"] is False and missing["note"]


def test_display_strings_match_the_onboarding_screens() -> None:
    assert display_date("2026-09-12T10:00:00+00:00") == "12 set"
    assert display_date(None) == ""
    assert display_attachments([]) == "sem anexo"
    one = [StoredAttachment(name="itens.XLSX", content_type="")]
    assert display_attachments(one) == "1 anexo · xlsx"
    two = [
        StoredAttachment(name="a.xlsx", content_type=""),
        StoredAttachment(name="b.pdf", content_type=""),
    ]
    assert display_attachments(two) == "2 anexos · xlsx, pdf"
