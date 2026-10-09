"""Which language Bido writes to a supplier in."""

import pytest

from bido_back.rfqs.language import country_language, email_language, infer_supplier_locale


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


@pytest.mark.parametrize(
    ("name", "email", "expected"),
    [
        # The prod case (2026-10-09): saved from the chat with no country, written to in English.
        ("Cobre Sul Fios Esmaltados S.A.", "vendas@cobresul.com.br", ("BR", "pt")),
        ("Magnetica Europe GmbH", "sales@magnetica.com", ("DE", "de")),
        ("MAGNETICA EUROPE GMBH", None, ("DE", "de")),
        ("Weber GmbH & Co. KG", None, ("DE", "de")),
        ("Metalúrgica Horizonte Ltda.", "x@gmail.com", ("BR", "pt")),
        ("Oficina Silva EIRELI", None, ("BR", "pt")),
        ("Padaria Silva ME", None, ("BR", "pt")),
        ("Lisboa Metais Lda", None, ("PT", "pt")),
        ("Rame Italia S.r.l.", None, ("IT", "it")),
        ("Cuscinetti SpA", None, ("IT", "it")),
        ("Tornillos del Norte S.L.", None, ("ES", "es")),
        ("Visserie SARL", None, ("FR", "fr")),
        ("Aceros S.A. de C.V.", None, ("MX", "es")),
        ("Acme", "a@acme.at", ("AT", "de")),
        # The TLD is where the mail goes: it wins over the name.
        ("Rame Italia S.r.l.", "sales@rame.com.br", ("BR", "pt")),
        # A country whose language is not in the table: the country only (English by default).
        ("Acme Corp", "sales@acme.co.uk", ("GB", None)),
        # Nothing unambiguous: never guessed.
        ("Cobre Sul Fios Esmaltados S.A.", "vendas@cobresul.com", (None, None)),  # S.A.: many
        ("Acme Corp", "sales@acme.com", (None, None)),
        ("Follow me Inc", None, (None, None)),  # "me" is a word; only "ME" is the suffix
        ("Termas Spa", None, (None, None)),  # a spa, not an S.p.A.
        ("SAS Institute", None, (None, None)),  # an acronym as often as a French SAS
        ("Schweizer AG", None, (None, None)),  # AG: Germany or Switzerland
        ("Acme Consulting", "x@y.co", (None, None)),  # .co is sold as "company"
        ("", "", (None, None)),
        (None, None, (None, None)),
    ],
)
def test_locale_inferred_only_from_unambiguous_tlds_and_suffixes(
    name: str | None, email: str | None, expected: tuple[str | None, str | None]
) -> None:
    assert infer_supplier_locale(name, email) == expected
