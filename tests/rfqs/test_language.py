"""Which language Bido writes to a supplier in."""

from typing import Any

import pytest

from bido_back.db.models import Supplier
from bido_back.rfqs.language import (
    LanguageDecision,
    company_form_languages,
    country_language,
    decide_email_language,
    domain_language,
    email_language,
    infer_supplier_locale,
    sent_language,
    thread_language,
)


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


# --- The decision at the first send (email audit F5) -------------------------------------


def decide(**evidence: Any) -> LanguageDecision:
    fields: dict[str, Any] = {
        "supplier_language": "en",  # the column default: nothing was set
        "supplier_country": None,
        "supplier_name": "Fornecedor",
    }
    fields.update(evidence)
    return decide_email_language(**fields)


def test_rfq6_a_brazilian_supplier_added_by_the_copilot_gets_portuguese() -> None:
    # Name and email only; the buyer uses Bido's (Portuguese) standard template.
    rfq6 = decide(
        supplier_name="MetalNova Teste Ltda",
        email_addresses=("vendas@metalnova.example.test", None),
        standard_template_language="pt",
    )
    assert rfq6 == LanguageDecision("pt", "company_suffix")
    # Without the legal form, the buyer's own language decides.
    assert decide(supplier_name="MetalNova Teste", buyer_language="pt-BR") == LanguageDecision(
        "pt", "buyer"
    )
    # And without anything else, the tenant's standard template.
    assert decide(standard_template_language="pt") == LanguageDecision("pt", "standard_template")


def test_the_strongest_evidence_wins() -> None:
    assert decide(supplier_language="de", supplier_country="BR") == LanguageDecision(
        "de", "supplier"
    )
    assert decide(supplier_country="BR", buyer_language="en") == LanguageDecision("pt", "country")
    # A known foreign country never falls through to the buyer's language.
    assert decide(supplier_country="US", standard_template_language="pt") == LanguageDecision(
        "en", "country"
    )
    assert decide(supplier_country="CN", buyer_language="pt") == LanguageDecision("en", "country")
    assert decide(
        supplier_name="Pennine Components Ltd", standard_template_language="pt"
    ) == LanguageDecision("en", "company_suffix")
    assert decide(
        supplier_name="Würth Elektronik GmbH & Co. KG", buyer_language="pt"
    ) == LanguageDecision("de", "company_suffix")
    assert decide(
        buyer_template_language="it", buyer_language="pt", standard_template_language=None
    ) == LanguageDecision("it", "buyer_template")
    assert decide(
        email_addresses=("sales@firma.de", None), standard_template_language="pt"
    ) == LanguageDecision("de", "email_domain")
    assert decide() == LanguageDecision("en", "default")


def test_a_legal_form_used_in_two_languages_follows_a_signal_that_agrees() -> None:
    assert decide(supplier_name="Aceros Andinos Ltda", buyer_language="es") == LanguageDecision(
        "es", "company_suffix"
    )
    assert decide(
        supplier_name="Aceros Andinos Ltda", email_addresses=("ventas@aceros.cl",)
    ) == LanguageDecision("es", "company_suffix")
    assert decide(supplier_name="Aceros Andinos Ltda", buyer_language="en") == LanguageDecision(
        "pt", "company_suffix"
    )


@pytest.mark.parametrize(
    ("name", "languages"),
    [
        ("MetalNova Teste Ltda", ("pt", "es")),
        ("Fundição Paulista Ltda. - ME", ("pt", "es")),
        ("Bosch GmbH", ("de",)),
        ("Siemens AG", ("de",)),
        ("Ferrari S.p.A.", ("it",)),
        ("Componenti S.r.l.", ("it", "es")),
        ("Atelier SARL", ("fr",)),
        ("Aceros S.A. de C.V.", ("es",)),
        ("Acme Inc.", ("en",)),
        ("Ltda Comercial e Distribuidora", ()),
        ("Bagagem", ()),
        ("Fornecedor S.A.", ()),
    ],
)
def test_legal_forms(name: str, languages: tuple[str, ...]) -> None:
    assert company_form_languages(name) == languages


def test_email_domain_country_codes() -> None:
    assert domain_language("vendas@metalnova.com.br") == "pt"
    assert domain_language("metalnova.com.br") == "pt"
    assert domain_language("info@firma.at") == "de"
    assert domain_language("sales@acme.com") is None
    assert domain_language("sales@rhein.example.test") is None
    assert domain_language(None) is None


# --- Later emails keep the thread's language ---------------------------------------------


def test_later_emails_keep_the_language_the_rfq_email_went_out_in() -> None:
    supplier = Supplier(name="Rhein", language="de", country="DE")
    sent = {"invite_id": "x", "document": {"language": "pt", "rfq_ref": "RFQ-6"}}
    # The supplier's data changed after the RFQ email: the thread stays in Portuguese.
    assert sent_language(sent) == "pt"
    assert thread_language(sent, supplier) == "pt"
    # The full email without the PDF (it did not render): the arguments record it.
    assert sent_language({"invite_id": "x", "document": None, "language": "pt"}) == "pt"
    assert thread_language({"invite_id": "x", "language": "pt"}, supplier) == "pt"
    # An RFQ email approved before documents existed: the rule of that time.
    assert sent_language({"invite_id": "x", "document": None}) is None
    assert thread_language({"invite_id": "x"}, supplier) == "de"
    assert thread_language(None, Supplier(name="X", language="en", country="BR")) == "pt"


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
