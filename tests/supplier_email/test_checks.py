"""The send gate refuses what must never go out, with reason codes; the note has its own rules."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from bido_back.supplier_email.checks import GateContext, email_problems, note_problems
from bido_back.supplier_email.compose import Composed, Refused, compose
from bido_back.supplier_email.locale import Locale
from bido_back.supplier_email.model import Ask, Money, OfferedTerms, ReplyBy
from bido_back.supplier_email.render import RenderedEmail, render
from tests.supplier_email import rfq6


def _problems(email_text: str | None = None, html: str | None = None) -> list[str]:
    email = rfq6.revised_offer("pt")
    rendered = render(email)
    tampered = RenderedEmail(
        subject=rendered.subject,
        text=email_text if email_text is not None else rendered.text,
        html=html if html is not None else rendered.html,
        language=rendered.language,
    )
    return email_problems(email, tampered, rfq6.gate("pt"))


def test_a_clean_email_passes() -> None:
    assert _problems() == []


def test_the_language_must_be_the_threads() -> None:
    email = rfq6.revised_offer("en")
    gate = GateContext("pt", rfq6.FIRST_SUBJECTS["en"], rfq6.TITLE)
    assert "language_not_thread" in email_problems(email, render(email), gate)


def test_the_subject_is_the_first_one_with_re_once() -> None:
    email = rfq6.clarification_a("pt")
    changed = replace(email, subject="Re: [RFQ-6] Pedido de esclarecimento")
    assert "subject_not_frozen" in email_problems(changed, render(changed), rfq6.gate("pt"))
    twice = replace(email, subject=f"Re: Re: {rfq6.FIRST_SUBJECTS['pt']}")
    problems = email_problems(twice, render(twice), rfq6.gate("pt"))
    assert {"subject_not_frozen", "subject_reply_prefix_twice"} <= set(problems)


def test_every_number_must_be_the_decisions() -> None:
    text = render(rfq6.revised_offer("pt")).text
    invented = text.replace("Preço-alvo: R$ 17,90/pç", "Preço-alvo: R$ 16,90/pç")
    assert {"number_not_decided", "parity", "decided_value_missing"} <= set(_problems(invented))
    exponent = text.replace("R$ 90,00/kg", "R$ 9E+1/kg")
    assert "exponent" in _problems(exponent)
    assert "iso_date" in _problems(text.replace("14/10/2026", "2026-10-14"))
    assert "bare_dollar" in _problems(text.replace("R$ 17,90", "$ 17,90"))
    assert "brl_code" in _problems(text.replace("R$ 17,90", "BRL 17,90"))


def test_one_signature() -> None:
    text = render(rfq6.revised_offer("pt")).text
    twice = text + "Atenciosamente,\nBiduuuuu\n"
    assert {"signature_closing_count", "repeated_line"} & set(_problems(twice))
    doubled = text.replace("Atenciosamente,\nBiduuuuu\n", "Atenciosamente,\nBiduuuuu\nBiduuuuu\n")
    assert {"signature_not_last", "repeated_line"} <= set(_problems(doubled))


def test_asks_reply_by_and_the_legal_line() -> None:
    email = rfq6.revised_offer("pt")
    four = replace(
        email,
        asks=(
            *email.asks,
            Ask("revise_price", positions=(1,)),
            Ask("revise_price", positions=(3,)),
            Ask("revise_price", positions=(1, 3)),
        ),
    )
    assert "asks_too_many" in email_problems(four, render(four), rfq6.gate("pt"))
    undated = replace(email, reply_by=None)
    assert "reply_by_missing" in email_problems(undated, render(undated), rfq6.gate("pt"))
    notice = rfq6.not_selected("pt")
    dated = replace(notice, reply_by=ReplyBy(date(2026, 10, 20)))
    assert "reply_by_unexpected" in email_problems(dated, render(dated), rfq6.gate("pt"))
    text = render(email).text
    legal = text.split("\n\n")[-2]
    assert "legal_line_missing" in _problems(text.replace(legal + "\n\n", ""))


def test_the_html_has_no_links_images_or_foreign_tags() -> None:
    html = render(rfq6.revised_offer("pt")).html
    assert "html_link" in _problems(html=html.replace("<p>", '<p><a href="https://x.test">', 1))
    assert "html_tag" in _problems(html=html.replace("<p>", "<p><img alt=''>", 1))


@pytest.mark.parametrize(
    ("note", "code"),
    [
        ("Ein anderer Lieferant bietet 0,40 EUR an.", "note_digits"),
        ("Wir erwarten einen besseren Preis in Euro.", "note_currency"),
        ("Mehr unter www.example.test, bitte.", "note_link"),
        ("Sehr geehrte Damen und Herren, wir danken Ihnen.", "note_greeting"),
        ("Wir danken MetalNova für die Mitarbeit.", "note_names"),
        ("Wir danken Ihnen, auch Paulista hat geantwortet.", "note_names"),
        ("Wir nehmen das Angebot an und bestellen.", "note_commitment"),
        ("We accept your offer.", "note_commitment"),
        ("Sonst droht eine Klage.", "note_threat"),
        ("Thank you for the quick answer.", "note_language"),
        ("Danke. Danke. Danke.", "note_too_many_sentences"),
        (" ".join(["Wir danken Ihnen für die Mitarbeit"] * 8) + ".", "note_too_long"),
        ("- Wir danken Ihnen für die Mitarbeit.", "note_list"),
    ],
)
def test_note_rules(note: str, code: str) -> None:
    names = ["MetalNova Teste Ltda", "MetalNova Teste", "Biduuuuu", "Fundição Paulista Ltda"]
    names.append("Paulista")
    assert code in note_problems(note, language="de", names=names)


def test_a_good_note_passes_in_its_language() -> None:
    assert note_problems("Wir freuen uns auf Ihre Rückmeldung.", language="de", names=[]) == []
    assert note_problems("Agradecemos a atenção de vocês.", language="pt", names=[]) == []
    assert note_problems("Thank you for the quick answer.", language="en", names=[]) == []


async def test_compose_checks_the_email_before_asking_for_a_note() -> None:
    asked: list[str] = []

    async def note() -> str:
        asked.append("x")
        return "Thank you for the quick answer."

    email = rfq6.delivery_b("en")
    composed = await compose(email, rfq6.gate("en"), note)
    assert isinstance(composed, Composed) and asked == ["x"]
    assert composed.email.note == "Thank you for the quick answer."
    assert "Thank you for the quick answer." in composed.rendered.text

    # A code-level problem: refused, and the model is not called at all.
    broken = replace(email, locale=Locale.of("de"))
    refused = await compose(broken, rfq6.gate("en"), note)
    assert isinstance(refused, Refused) and "language_not_thread" in refused.problems
    assert asked == ["x"]


async def test_a_bad_note_is_left_out_and_the_email_still_goes() -> None:
    async def note() -> str:
        return "Another supplier offers BRL 80.00, so we accept your offer."

    composed = await compose(rfq6.delivery_b("en"), rfq6.gate("en"), note)
    assert isinstance(composed, Composed)
    assert composed.email.note == "" and "80.00" not in composed.rendered.text
    assert {"note_digits", "note_currency", "note_commitment"} <= set(composed.note_dropped)


async def test_a_note_over_the_word_budget_is_left_out() -> None:
    # Within the note's own limits (one sentence, under forty words) but a reminder in German
    # leaves about ten words for a note.
    long_note = (
        "Wir danken Ihnen sehr herzlich für Ihre bisherige Unterstützung und freuen uns auf "
        "Ihre baldige Rückmeldung."
    )
    assert note_problems(long_note, language="de", names=[]) == []

    async def note() -> str:
        return long_note

    composed = await compose(rfq6.reminder("de"), rfq6.gate("de"), note)
    assert isinstance(composed, Composed) and composed.email.note == ""
    assert composed.note_dropped == ("note_over_budget",)


def test_reminders_clarifications_and_notices_name_no_price() -> None:
    email = rfq6.clarification_missing("pt")
    priced = replace(
        email,
        positions=(
            replace(
                email.positions[0],
                facts=(*email.positions[0].facts, OfferedTerms(Money(Decimal("19.5"), "BRL"))),
            ),
            *email.positions[1:],
        ),
    )
    assert "price_mentioned" in email_problems(priced, render(priced), rfq6.gate("pt"))
    no_ref = replace(rfq6.reminder("pt"), rfq_ref="RFQ-60")
    problems = email_problems(no_ref, render(no_ref), rfq6.gate("pt"))
    assert "subject_not_frozen" in problems  # the subject names RFQ-6, not RFQ-60
