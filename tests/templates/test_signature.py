"""The buyer's signature, written by code under every supplier email (email audit F4)."""

from bido_back.templates.signature import signature, signed, strip_signature


def test_the_company_signs_once_when_the_buyer_has_no_name_of_their_own() -> None:
    # Production: no buyer contact exists, so the name falls back to the company.
    assert signature("Biduuuuu", "Biduuuuu") == ["Biduuuuu"]
    assert signature(" biduuuuu ", "Biduuuuu") == ["Biduuuuu"]
    assert signature("", "Biduuuuu") == ["Biduuuuu"]
    assert signature("Ana Lima", "Biduuuuu") == ["Ana Lima", "Biduuuuu"]
    assert signature("Ana Lima", "") == ["Ana Lima"]


def test_a_writer_email_gets_the_signature_once_under_its_closing() -> None:
    body = "Prezados MetalNova Teste Ltda,\n\nSobre a RFQ-6, ...\n\nAtenciosamente,"
    assert signed(body, "Biduuuuu", "Biduuuuu") == body + "\nBiduuuuu"
    assert signed(body, "Ana Lima", "Biduuuuu") == body + "\nAna Lima\nBiduuuuu"
    german = "Sehr geehrte Damen und Herren,\n\nzu RFQ-6 ...\n\nMit freundlichen Grüßen"
    assert signed(german, "Biduuuuu", "Biduuuuu") == german + "\nBiduuuuu"


def test_a_signature_the_writer_added_anyway_is_replaced_not_repeated() -> None:
    expected = "Olá,\n\nRFQ-6 ...\n\nAtenciosamente,\nBiduuuuu"
    # What the v1 prompts produced in production ("Biduuuuu" twice).
    assert (
        signed("Olá,\n\nRFQ-6 ...\n\nAtenciosamente,\nBiduuuuu\nBiduuuuu", "Biduuuuu", "Biduuuuu")
        == expected
    )
    assert (
        signed("Olá,\n\nRFQ-6 ...\n\nAtenciosamente,\nbiduuuuu.\n\n", "Biduuuuu", "Biduuuuu")
        == expected
    )
    assert (
        signed("Olá,\n\nRFQ-6 ...\n\nAtenciosamente,\n[Seu nome]", "Biduuuuu", "Biduuuuu")
        == expected
    )
    assert (
        signed("Olá,\n\nRFQ-6 ...\n\nAtenciosamente,\nAna Lima\nBiduuuuu", "Ana Lima", "Biduuuuu")
        == "Olá,\n\nRFQ-6 ...\n\nAtenciosamente,\nAna Lima\nBiduuuuu"
    )


def test_without_a_closing_the_signature_starts_its_own_block() -> None:
    assert signed("Sobre a RFQ-6: aguardamos sua resposta.", "Biduuuuu", "Biduuuuu") == (
        "Sobre a RFQ-6: aguardamos sua resposta.\n\nBiduuuuu"
    )


def test_only_trailing_signature_lines_are_dropped() -> None:
    body = "A Biduuuuu agradece.\nBiduuuuu\nAtenciosamente,"
    assert strip_signature(body, ["Biduuuuu"]) == body
    assert strip_signature("Obrigado.\nEquipe\nEquipe", ["Biduuuuu"]) == "Obrigado.\nEquipe"
