"""The thread: subjects, threading headers, the reply-by rules, and the thread's language."""

from datetime import UTC, date, datetime, timedelta

from bido_back.supplier_email.deadlines import reply_by_for
from bido_back.supplier_email.language import rfq_email_language, thread_language, thread_locale
from bido_back.supplier_email.locale import Locale
from bido_back.supplier_email.subject import (
    first_subject,
    reply_subject,
    shorten_title,
    subject_problems,
)
from bido_back.supplier_email.threading import MAX_REFERENCES, ThreadMessage, thread_headers

# --- Subjects ---------------------------------------------------------------------------------


def test_the_first_subject_names_the_type_once() -> None:
    assert shorten_title("Cotação de componentes para motor MTR-100") == (
        "Componentes para motor MTR-100"
    )
    assert shorten_title("RFQ for 3 bearings") == "3 bearings"
    assert shorten_title("Angebotsanfrage: Lager") == "Lager"
    assert shorten_title("Solicitação de cotação - parafusos") == "Parafusos"
    # Nothing to shorten, or nothing left: the title as written.
    assert shorten_title("MLCC and connectors") == "MLCC and connectors"
    assert shorten_title("Cotação") == "Cotação"
    assert shorten_title("Cotação MTR-100") == "Cotação MTR-100"
    assert first_subject("RFQ-6", "Solicitação de cotação", "Cotação de componentes") == (
        "[RFQ-6] Solicitação de cotação: Componentes"
    )


def test_later_subjects_are_re_plus_the_first_byte_for_byte() -> None:
    first = "[RFQ-6] Solicitação de cotação:  Componentes  (rev. B)"
    assert reply_subject(first, "RFQ-6", "x") == f"Re: {first}"
    assert reply_subject("AW: [RFQ-6] Anfrage", "RFQ-6", "x") == "AW: [RFQ-6] Anfrage"
    assert reply_subject(None, "RFQ-6", "Peças") == "Re: [RFQ-6] Peças"
    assert len(reply_subject("[RFQ-6] " + "x" * 400, "RFQ-6", "x")) == 300
    assert subject_problems(f"Re: {first}", first, "RFQ-6", "x") == []
    assert subject_problems(f"Re: Re: {first}", first, "RFQ-6", "x") == [
        "subject_not_frozen",
        "subject_reply_prefix_twice",
    ]
    assert "subject_marker_missing" in subject_problems("Re: Peças", None, "RFQ-6", "Peças")


# --- Threading headers --------------------------------------------------------------------------

ROOT = "<rfq@quotes.example.test>"
T0 = datetime(2026, 10, 8, 12, tzinfo=UTC)


def _message(name: str, minutes: int) -> ThreadMessage:
    return ThreadMessage(name, T0 + timedelta(minutes=minutes))


def test_in_reply_to_is_the_latest_message_and_references_start_at_the_rfq_email() -> None:
    messages = [
        _message(ROOT, 0),
        _message("<offer@supplier.example.test>", 60),
        _message("<chase@quotes.example.test>", 30),
    ]
    in_reply_to, references = thread_headers(ROOT, messages)
    assert in_reply_to == "<offer@supplier.example.test>"
    assert references == (
        ROOT,
        "<chase@quotes.example.test>",
        "<offer@supplier.example.test>",
    )


def test_synthetic_or_malformed_ids_are_never_a_parent() -> None:
    synthetic = _message("<sha256.abc@inbound.invalid>", 90)
    assert thread_headers(ROOT, [synthetic]) == (ROOT, (ROOT,))
    assert thread_headers(ROOT, []) == (ROOT, (ROOT,))
    assert thread_headers(ROOT, [_message("not-an-id", 5)]) == (ROOT, (ROOT,))


def test_long_threads_keep_the_root_and_the_latest_references() -> None:
    messages = [_message(f"<m{i}@supplier.example.test>", i) for i in range(1, 30)]
    in_reply_to, references = thread_headers(ROOT, messages)
    assert in_reply_to == "<m29@supplier.example.test>"
    assert len(references) == MAX_REFERENCES
    assert references[0] == ROOT and references[-1] == in_reply_to


# --- Reply-by ----------------------------------------------------------------------------------


def test_reply_by_per_type() -> None:
    # Thursday 08/10/2026 in Brazil: Monday 12/10 is a holiday.
    now = datetime(2026, 10, 8, 14, tzinfo=UTC)
    clarification = reply_by_for("clarification", now=now, country="BR")
    assert clarification is not None and clarification.day == date(2026, 10, 13)
    revised = reply_by_for("revised_offer", now=now, country="BR")
    assert revised is not None and revised.day == date(2026, 10, 14)
    reminder = reply_by_for("reminder", now=now, country="BR", response_due=date(2026, 10, 20))
    assert reminder is not None and reminder.day == date(2026, 10, 20)
    # The due date has passed: two business days from now.
    late = reply_by_for("reminder", now=now, country="BR", response_due=date(2026, 10, 1))
    assert late is not None and late.day == date(2026, 10, 13)
    # A clarification never asks past the RFQ's due date when that is still ahead.
    soon = reply_by_for("clarification", now=now, country="BR", response_due=date(2026, 10, 9))
    assert soon is not None and soon.day == date(2026, 10, 9)
    assert reply_by_for("not_selected", now=now, country="BR") is None


def test_nothing_after_the_negotiation_deadline_and_one_cutoff_for_a_bafo() -> None:
    now = datetime(2026, 10, 8, 14, tzinfo=UTC)
    deadline = datetime(2026, 10, 9, 18, tzinfo=UTC)
    capped = reply_by_for("revised_offer", now=now, country="BR", deadline=deadline)
    assert capped is not None and capped.day == date(2026, 10, 9) and capped.cutoff is None
    bafo = reply_by_for("bafo", now=now, country="DE", deadline=deadline)
    assert bafo is not None and bafo.cutoff == deadline and bafo.day == date(2026, 10, 9)
    # Every supplier gets the same cut-off, whatever its country or the evaluation time.
    other = reply_by_for("bafo", now=now + timedelta(hours=3), country="BR", deadline=deadline)
    assert other == bafo


# --- The thread's language (stand-in for N1's per-invite language) ----------------------------


def test_the_thread_keeps_the_language_its_rfq_email_went_out_in() -> None:
    sent = {"html_body": '<!doctype html><html lang="pt"><body>…'}
    assert rfq_email_language(sent, None) == "pt"
    assert rfq_email_language(None, "[RFQ-6] Angebotsanfrage: Lager") == "de"
    assert rfq_email_language(None, "[RFQ-6] Lager") is None
    # The supplier was edited to English after its RFQ email went out in Portuguese.
    assert (
        thread_language(
            rfq_email_args=sent,
            first_subject=None,
            supplier_language="en",
            supplier_country="GB",
        )
        == "pt"
    )
    # Nothing was sent through Bido: the supplier's language as resolved today.
    assert (
        thread_language(
            rfq_email_args=None,
            first_subject=None,
            supplier_language="en",
            supplier_country="Brasil",
        )
        == "pt"
    )
    # A language without a catalog: English, never half and half.
    assert (
        thread_language(
            rfq_email_args={"html_body": '<html lang="zh">'},
            first_subject=None,
            supplier_language="zh",
            supplier_country="CN",
        )
        == "en"
    )
    assert thread_locale("es", "Mexico") == Locale("es", "MX")
    assert thread_locale("pt", None) == Locale("pt")
