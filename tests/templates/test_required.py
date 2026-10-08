"""Required sector fields: which labels cover which field, what is missing, what was removed."""

from bido_back.onboarding.standard import STANDARD_TEMPLATE
from bido_back.templates.required import (
    LINE_HEADINGS,
    REQUIRED_FIELDS,
    accepted_terms,
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


def test_bido_own_labels_and_headings_cover_their_field_in_every_language() -> None:
    """No drift: whatever Bido writes for a field is recognised as that field."""
    for field in REQUIRED_FIELDS:
        for language, label in field.labels.items():
            assert field.key in covered_keys([label]), (field.key, language, label)
    heading_field = {
        "mpn": "mpn",
        "manufacturer": "manufacturer",
        "quantity": "quantity_tiers",
        "unit": "unit",
        "need_by": "need_by",
        "qualification": "qualification",
        "incoterm": "incoterm_place",
    }
    for language, headings in LINE_HEADINGS.items():
        for key, heading in headings.items():
            if key in heading_field:
                assert heading_field[key] in covered_keys([heading]), (language, heading)


def test_translated_labels_cover_their_field() -> None:
    assert covered_keys(["Code fabricant"]) >= {"mpn"}
    assert covered_keys(["Réf. fabricant"]) >= {"mpn"}
    assert covered_keys(["Référence du fabricant"]) >= {"mpn"}
    assert covered_keys(["Delivery time"]) == {"lead_time"}
    assert covered_keys(["Tempo di consegna"]) == {"lead_time"}
    assert covered_keys(["Data richiesta"]) == {"need_by"}
    assert covered_keys(["Date souhaitée"]) == {"need_by"}
    assert covered_keys(["Conditionnement"]) == {"packaging"}
    assert "mpn" in accepted_terms("mpn")
    assert "reference fabricant" in accepted_terms("mpn")  # the fr label, without "(MPN)"


def test_neighbouring_words_do_not_cover_a_missing_field() -> None:
    assert "lead_time" not in covered_keys(["Delivery date", "Unit price", "Data richiesta"])
    assert "need_by" not in covered_keys(["Delivery time", "Tempi di consegna", "Lead time"])
    assert "mpn" not in covered_keys(["Fabricant", "Désignation", "Quantité"])
    assert "packaging" not in covered_keys(["Conditions de paiement", "Quantité"])
    body = "Pos. | Désignation | Quantité | Unité\nDélai de livraison\nValidité de l'offre"
    missing = [f.key for f in missing_required(body.replace("|", "\n").splitlines())]
    assert "mpn" in missing
    assert "packaging" in missing
    assert "lead_time" not in missing
