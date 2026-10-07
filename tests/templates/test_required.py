"""Required sector fields: which labels cover which field, what is missing, what was removed."""

from bido_back.onboarding.standard import STANDARD_TEMPLATE
from bido_back.templates.required import (
    REQUIRED_FIELDS,
    covered_keys,
    label_matches,
    missing_required,
    removed_required,
    template_labels,
)


def test_labels_in_several_languages_cover_their_fields() -> None:
    assert covered_keys(["Código", "Fabricante", "Qtd.", "Un."]) == {
        "mpn",
        "manufacturer",
        "quantity_tiers",
        "unit",
    }
    assert covered_keys(["Lieferzeit (Wochen)", "Mindestbestellmenge", "Verpackung"]) == {
        "lead_time",
        "moq",
        "packaging",
    }
    assert covered_keys(["Incoterm / named place", "RoHS + REACH", "Offer validity"]) == {
        "incoterm_place",
        "rohs_reach",
        "validity",
    }
    assert covered_keys(["Frete (CIF/FOB)", "AEC-Q200 certificate"]) == {
        "incoterm_place",
        "qualification",
    }


def test_short_synonyms_match_whole_words_only() -> None:
    assert label_matches("Un.", "un")
    assert not label_matches("Fundo de reserva", "un")
    assert not label_matches("Empresa", "pn")
    assert label_matches("P/N", "p/n")


def test_standard_template_is_missing_the_sector_fields_it_does_not_name() -> None:
    labels = template_labels(
        STANDARD_TEMPLATE.columns, STANDARD_TEMPLATE.footer_fields, STANDARD_TEMPLATE.fields
    )

    missing = [f.key for f in missing_required(labels)]

    assert missing == [
        "mpn",
        "manufacturer",
        "need_by",
        "incoterm_place",
        "qualification",
        "packaging",
        "rohs_reach",
        "moq",
    ]


def test_removing_a_covered_field_is_reported_adding_or_renaming_is_not() -> None:
    before = ["Item", "Qtd.", "Prazo de entrega", "Validade da proposta"]

    assert [f.key for f in removed_required(before, ["Item", "Qtd."])] == [
        "lead_time",
        "validity",
    ]
    assert removed_required(before, ["Item", "Quantidade", "Lead time", "Validade", "MOQ"]) == []


def test_every_required_field_has_an_english_label_and_unique_key() -> None:
    keys = [f.key for f in REQUIRED_FIELDS]

    assert len(keys) == len(set(keys))
    assert all("en" in f.labels for f in REQUIRED_FIELDS)
    assert [f.key for f in REQUIRED_FIELDS if f.conditional] == ["qualification"]
