"""Which language Bido writes to a supplier in."""

import pytest

from bido_back.rfqs.language import country_language, email_language


@pytest.mark.parametrize(
    ("language", "country", "expected"),
    [
        ("en", "BR", "pt"),  # the column default with a Brazilian supplier (RFQ-3)
        ("en", "Brasil", "pt"),
        (None, "PT", "pt"),
        ("en", "IT", "it"),
        ("en", "Deutschland", "de"),
        ("en", "FR", "fr"),
        ("en", "MX", "es"),
        ("de", "BR", "de"),  # the supplier's own language wins
        ("en", "US", "en"),
        ("en", None, "en"),
        (None, None, "en"),
    ],
)
def test_own_language_else_the_countrys_else_english(
    language: str | None, country: str | None, expected: str
) -> None:
    assert email_language(language, country) == expected


def test_unknown_countries_have_no_language() -> None:
    assert country_language("Atlantis") is None
    assert country_language("") is None
