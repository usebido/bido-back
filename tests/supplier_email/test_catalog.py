"""The phrase catalog: complete in every language, same placeholders, nothing that commits."""

import re
import string
from typing import get_args

import pytest

from bido_back.mandates.policy import commitment_language
from bido_back.supplier_email.catalog import CATALOG, Key, count_word, phrases, type_label
from bido_back.supplier_email.locale import CATALOG_LANGUAGES, NBSP
from bido_back.supplier_email.model import EMAIL_TYPES

KEYS = set(get_args(Key))


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_every_key_exists_in_every_language_with_the_same_placeholders() -> None:
    assert set(CATALOG) == set(CATALOG_LANGUAGES)
    for language, words in CATALOG.items():
        assert set(words) == KEYS, (language, KEYS ^ set(words))
    for key in KEYS:
        english = _placeholders(CATALOG["en"][key])
        for language in CATALOG_LANGUAGES:
            found = _placeholders(CATALOG[language][key])
            if key == "salutation":
                # Only English names the supplier ("Dear MetalNova team,").
                assert found <= english, (language, key)
            else:
                assert found == english, (language, key, found, english)


@pytest.mark.parametrize("language", CATALOG_LANGUAGES)
def test_no_digits_in_the_catalog(language: str) -> None:
    """Every number comes from the data, through the formatters."""
    for key, text in CATALOG[language].items():
        assert not re.search(r"\d", text), (language, key)


@pytest.mark.parametrize("language", [lang for lang in CATALOG_LANGUAGES if lang != "en"])
def test_no_english_left_in_another_language(language: str) -> None:
    english = re.compile(r"\b(?:the|please|your|our|thank|offer|request|reply|delivery|price)\b")
    for key, text in CATALOG[language].items():
        if key in ("table.position", "identity.position", "fill.delivery", "fill.lead"):
            continue  # "Pos." is the same word everywhere
        words = re.sub(r"\{[a-z_]+\}", "", text.replace("Pos.", ""))
        assert not english.search(words), (language, key, text)


def test_language_conventions() -> None:
    # Spanish ends the salutation with a colon; German closes without a comma.
    assert CATALOG["es"]["salutation"].endswith(":")
    assert CATALOG["de"]["closing"] == "Mit freundlichen Grüßen"
    for language in ("pt", "en", "fr", "it", "es"):
        assert CATALOG[language]["closing"].endswith(",")
    assert CATALOG["pt"]["salutation"] == "Prezados,"
    # French: a no-break space before : ; ? !
    for key, text in CATALOG["fr"].items():
        for mark in (":", ";", "?", "!"):
            for at in [m.start() for m in re.finditer(re.escape(mark), text)]:
                assert at > 0 and text[at - 1] == NBSP, (key, text)


@pytest.mark.parametrize("language", CATALOG_LANGUAGES)
def test_nothing_in_the_catalog_commits(language: str) -> None:
    """The legal line says the email is not an order without tripping the mandate's own
    commitment check, which reads the whole email before an automatic approval."""
    for key, text in CATALOG[language].items():
        filled = text.replace("{company}", "Acme").replace("{label}", "x")
        assert not commitment_language(filled), (language, key, text)


def test_type_labels_and_counts() -> None:
    assert type_label("clarification", "pt") == "Pedido de esclarecimento"
    assert type_label("revised_offer", "en") == "Request for a revised offer"
    assert type_label("delivery_date", "pt") == "Consulta de prazo de entrega"
    assert type_label("bafo", "en") == "Request for best and final offer"
    for kind in EMAIL_TYPES:
        for language in CATALOG_LANGUAGES:
            assert type_label(kind, language)
    assert type_label("bafo", "zh") == type_label("bafo", "en")  # never half and half
    assert count_word(2, "pt") == "duas" and count_word(2, "en") == "two"
    assert count_word(11, "de") == "11"
    assert phrases("xx") is CATALOG["en"]
