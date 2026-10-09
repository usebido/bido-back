"""Golden emails: the exact text and HTML, committed, so any change shows as a diff.

RFQ-6 (the deployed thread of 2026-10-08) as the standard renders it (§2.7): the
clarification after the first reply and the delivery-date request after the second, in
Portuguese and English, and one email per supplier answer (the open questions, then the
revised offer). Every other type is frozen per language in `all-types.<locale>.txt`;
the German, French, Italian and Spanish catalogs still need a native speaker's review, and
the review lands as a diff here.

Regenerate after a deliberate change: `UPDATE_GOLDEN=1 uv run pytest tests/supplier_email`.
"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest

from bido_back.supplier_email.catalog import phrases
from bido_back.supplier_email.checks import email_problems
from bido_back.supplier_email.model import SupplierEmail
from bido_back.supplier_email.render import render
from tests.supplier_email import rfq6

GOLDEN: Final = Path(__file__).parent / "golden"


def _matches(name: str, content: str) -> None:
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    assert path.is_file(), f"missing golden {name}: run with UPDATE_GOLDEN=1"
    assert content == path.read_text(encoding="utf-8"), f"{name} differs from its golden file"


CASES: Final[list[tuple[str, Callable[[str], SupplierEmail]]]] = [
    ("rfq6-clarification", rfq6.clarification_a),
    ("rfq6-delivery-date", rfq6.delivery_b),
    # One email per supplier answer: the clarification's questions, then the revised offer.
    ("rfq6-revised-with-questions", rfq6.revised_with_questions),
]


@pytest.mark.parametrize("language", ["pt", "en"])
@pytest.mark.parametrize(("name", "build"), CASES, ids=[n for n, _ in CASES])
def test_rfq6_golden_emails(
    name: str, build: Callable[[str], SupplierEmail], language: str
) -> None:
    email = build(language)
    rendered = render(email)
    assert email_problems(email, rendered, rfq6.gate(language)) == []
    _matches(f"{name}.{language}.txt", f"Subject: {rendered.subject}\n\n{rendered.text}")
    _matches(f"{name}.{language}.html", rendered.html + "\n")


def test_the_rfq6_clarification_reads_like_the_standard() -> None:
    """The spec's §2.7 A, line by line (the plain text is not wrapped)."""
    text = render(rfq6.clarification_a("pt")).text
    assert text.splitlines()[:12] == [
        "Prezados,",
        "",
        "Pedido de esclarecimento – RFQ-6",
        "Obrigado pela proposta de 08/10/2026. Antes de avaliá-la, precisamos confirmar a "
        "entrega de duas posições.",
        "",
        "Pos. 1 · Rolamento motor MTR-100 · cód. 000101 · 70 pç",
        "   Prazo informado: aprox. 10 dias úteis após o pedido",
        "   Entrega necessária: até 22/10/2026",
        "Pos. 2 · Eixo motor MTR-100 · cód. 000102 · 15 pç",
        "   Prazo informado: aprox. 10 dias úteis + alguns dias adicionais após o pedido",
        "   Entrega necessária: até 22/10/2026",
        "",
    ]
    assert (
        "1. Vocês conseguem entregar as posições 1 e 2 até 22/10/2026? Se não, informe a data "
        "de entrega que podem confirmar para cada uma."
    ) in text
    assert (
        "Por favor, responda a este e-mail até terça-feira, 13/10/2026, por exemplo:\n"
        "   Pos. 1 – entrega até: __/__/____\n"
        "   Pos. 2 – entrega até: __/__/____\n"
    ) in text
    assert text.endswith("Atenciosamente,\nBiduuuuu\n")


def test_the_rfq6_delivery_date_request_reads_like_the_standard() -> None:
    """The spec's §2.7 B, in English."""
    rendered = render(rfq6.delivery_b("en"))
    assert rendered.subject == "Re: [RFQ-6] Request for quotation: Components for motor MTR-100"
    assert rendered.text.splitlines()[:9] == [
        "Dear MetalNova Teste team,",
        "",
        "Delivery date request – RFQ-6",
        "Thank you for your reply of 8 Oct 2026. This request concerns position 2 only: the "
        "quoted lead time does not meet our required date.",
        "",
        "Pos. 2 · Eixo motor MTR-100 · part no. 000102 · 15 pcs",
        "   Your offer: BRL 89.00/pc · 12 business days after order",
        "   Required delivery: by 22 Oct 2026",
        "",
    ]
    assert (
        "1. Can you deliver all 15 pcs by 22 Oct 2026? If not, what is the earliest date you "
        "can confirm?"
    ) in rendered.text
    assert (
        "Please reply to this email by Wednesday, 14 Oct 2026. There is no need to resend the "
        "other positions."
    ) in rendered.text


@pytest.mark.parametrize("locale", ["pt", "en", "de", "fr", "it", "es", "es-MX", "de-CH"])
def test_every_type_in_every_language(locale: str) -> None:
    language, _, region = locale.partition("-")
    parts: list[str] = []
    for name, build in rfq6.SAMPLES.items():
        email = build(language, region or "BR")
        rendered = render(email)
        assert email_problems(email, rendered, rfq6.gate(language)) == [], (locale, name)
        parts.append(f"===== {name}\nSubject: {rendered.subject}\n\n{rendered.text}")
    _matches(f"all-types.{locale}.txt", "\n".join(parts))


@pytest.mark.parametrize("locale", ["pt", "en", "de", "fr", "it", "es", "es-MX", "de-CH"])
def test_one_email_per_answer_passes_the_gate_in_every_language(locale: str) -> None:
    """The questions and the revised offer in one email: three asks, each catalog phrase in
    the thread's language, the request's purpose line, one reply-by date."""
    language, _, region = locale.partition("-")
    email = rfq6.revised_with_questions(language, region or "BR")
    rendered = render(email)
    assert email_problems(email, rendered, rfq6.gate(language)) == []
    words = phrases(email.language)
    assert words["context.combined.questions"] in rendered.text
    assert rendered.text.count("\n1. ") == 1 and "\n3. " in rendered.text
    assert "\n4. " not in rendered.text
