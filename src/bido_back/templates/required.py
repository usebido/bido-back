"""The fields every EV-electronics RFQ must carry, and matching them to a template's labels.

A template's labels are the buyer's own words, in their language ("Prazo de
entrega", "Lieferzeit (Wochen)", "MOQ"). Each required field has synonyms in
the languages we see (pt, en, de, fr, es, it), and Bido's own words for it
(its labels and the render's headings, in every language) count as synonyms
too; a label covers a field when it contains one of them. Short synonyms
(seven letters or fewer, like "un", "menge" or "einheit") must be whole words
of the label, so "un" does not match "fundo" nor "menge" match
"Mindestbestellmenge".

Two kinds of required fields:

* `item`: data of the requested lines (MPN, manufacturer, quantity tiers,
  unit, need-by date, Incoterm and place, qualification). The render always
  puts them in the item table or under it, whatever the template says.
* `ask`: what the supplier must answer (lead time, validity, MOQ, packaging,
  RoHS/REACH). The render always asks for them, after the template's own
  footer fields.

`qualification` is conditional: required when a line asks for one (AEC-Q100
/ AEC-Q200 for automotive parts).

Pure functions, no I/O.
"""

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

RequiredKind = Literal["item", "ask"]


@dataclass(frozen=True, slots=True)
class RequiredField:
    key: str
    kind: RequiredKind
    synonyms: tuple[str, ...]
    # Label per language; "en" always present.
    labels: dict[str, str]
    # Required only when a line needs it (qualification).
    conditional: bool = False

    def label(self, language: str) -> str:
        return self.labels.get(language, self.labels["en"])


REQUIRED_FIELDS: Final[tuple[RequiredField, ...]] = (
    RequiredField(
        key="mpn",
        kind="item",
        synonyms=(
            "mpn",
            "p/n",
            "pn",
            "part number",
            "part no",
            "codigo",
            "cod",
            "referencia",
            "herstellernummer",
            "teilenummer",
            "artikelnummer",
            "reference fabricant",
            "reference du fabricant",
            "ref. fabricant",
            "ref fabricant",
            "code fabricant",
            "code du fabricant",
            "numero de piece",
            "numero de pieza",
            "numero de peca",
            "codice",
        ),
        labels={
            "en": "Manufacturer part number (MPN)",
            "pt": "Código do fabricante (MPN)",
            "de": "Herstellerteilenummer (MPN)",
            "fr": "Référence fabricant (MPN)",
            "es": "Número de pieza del fabricante (MPN)",
            "it": "Codice produttore (MPN)",
        },
    ),
    RequiredField(
        key="manufacturer",
        kind="item",
        synonyms=(
            "manufacturer",
            "fabricante",
            "hersteller",
            "fabricant",
            "marca",
            "brand",
            "produttore",
        ),
        labels={
            "en": "Manufacturer",
            "pt": "Fabricante",
            "de": "Hersteller",
            "fr": "Fabricant",
            "es": "Fabricante",
            "it": "Produttore",
        },
    ),
    RequiredField(
        key="quantity_tiers",
        kind="item",
        synonyms=(
            "qty",
            "qtd",
            "qtde",
            "quantity",
            "quantidade",
            "menge",
            "staffel",
            "staffeln",
            "mengenstaffel",
            "quantite",
            "cantidad",
            "quantita",
            "tiers",
        ),
        labels={
            "en": "Quantity tiers",
            "pt": "Quantidades",
            "de": "Mengenstaffeln",
            "fr": "Quantités",
            "es": "Cantidades",
            "it": "Quantità",
        },
    ),
    RequiredField(
        key="unit",
        kind="item",
        synonyms=(
            "un",
            "und",
            "unid",
            "unidade",
            "unit",
            "uom",
            "einheit",
            "unite",
            "unidad",
            "unita",
        ),
        labels={
            "en": "Unit",
            "pt": "Unidade",
            "de": "Einheit",
            "fr": "Unité",
            "es": "Unidad",
            "it": "Unità",
        },
    ),
    RequiredField(
        key="need_by",
        kind="item",
        synonyms=(
            "need by",
            "need-by",
            "required date",
            "delivery date",
            "data de entrega",
            "data necessaria",
            "data desejada",
            "liefertermin",
            "wunschtermin",
            "bedarfstermin",
            "date de livraison",
            "fecha de entrega",
            "data di consegna",
        ),
        labels={
            "en": "Required delivery date",
            "pt": "Data de entrega desejada",
            "de": "Wunschliefertermin",
            "fr": "Date de livraison souhaitée",
            "es": "Fecha de entrega requerida",
            "it": "Data di consegna richiesta",
        },
    ),
    RequiredField(
        key="incoterm_place",
        kind="item",
        synonyms=(
            "incoterm",
            "frete",
            "freight",
            "cif",
            "fob",
            "dap",
            "ddp",
            "fca",
            "exw",
            "local de entrega",
            "lieferbedingung",
            "lieferort",
            "delivery terms",
            "place of delivery",
            "conditions de livraison",
            "lieu de livraison",
            "lugar de entrega",
        ),
        labels={
            "en": "Incoterm and named place",
            "pt": "Incoterm e local de entrega",
            "de": "Incoterm und Lieferort",
            "fr": "Incoterm et lieu de livraison",
            "es": "Incoterm y lugar de entrega",
            "it": "Incoterm e luogo di consegna",
        },
    ),
    RequiredField(
        key="qualification",
        kind="item",
        synonyms=(
            "aec-q",
            "aec-q100",
            "aec-q101",
            "aec-q200",
            "aecq",
            "ppap",
            "qualificacao",
            "qualification",
            "qualifikation",
        ),
        labels={
            "en": "Qualification (e.g. AEC-Q200) and its certificates",
            "pt": "Qualificação (ex.: AEC-Q200) e certificados",
            "de": "Qualifikation (z. B. AEC-Q200) und Nachweise",
            "fr": "Qualification (ex. AEC-Q200) et certificats",
            "es": "Calificación (p. ej. AEC-Q200) y certificados",
            "it": "Qualifica (es. AEC-Q200) e certificati",
        },
        conditional=True,
    ),
    RequiredField(
        key="packaging",
        kind="ask",
        synonyms=(
            "packaging",
            "embalagem",
            "verpackung",
            "emballage",
            "embalaje",
            "imballaggio",
            "spq",
            "reel",
        ),
        labels={
            "en": "Packaging unit",
            "pt": "Embalagem",
            "de": "Verpackungseinheit",
            "fr": "Conditionnement",
            "es": "Embalaje",
            "it": "Imballaggio",
        },
    ),
    RequiredField(
        key="rohs_reach",
        kind="ask",
        synonyms=("rohs", "reach"),
        labels={
            "en": "RoHS and REACH declarations",
            "pt": "Declarações RoHS e REACH",
            "de": "RoHS- und REACH-Erklärungen",
            "fr": "Déclarations RoHS et REACH",
            "es": "Declaraciones RoHS y REACH",
            "it": "Dichiarazioni RoHS e REACH",
        },
    ),
    RequiredField(
        key="lead_time",
        kind="ask",
        synonyms=(
            "lead time",
            "delivery time",
            "delivery period",
            "prazo de entrega",
            "prazo",
            "lieferzeit",
            "lieferfrist",
            "delai",
            "plazo de entrega",
            "plazo",
            "tempi di consegna",
            "tempo di consegna",
            "termine di consegna",
        ),
        labels={
            "en": "Lead time",
            "pt": "Prazo de entrega",
            "de": "Lieferzeit",
            "fr": "Délai de livraison",
            "es": "Plazo de entrega",
            "it": "Tempi di consegna",
        },
    ),
    RequiredField(
        key="validity",
        kind="ask",
        synonyms=(
            "validity",
            "valid until",
            "validade",
            "gultigkeit",
            "gueltigkeit",
            "gultig",
            "bindefrist",
            "validite",
            "validez",
            "vigencia",
            "validita",
        ),
        labels={
            "en": "Offer validity",
            "pt": "Validade da proposta",
            "de": "Gültigkeit des Angebots",
            "fr": "Validité de l'offre",
            "es": "Validez de la oferta",
            "it": "Validità dell'offerta",
        },
    ),
    RequiredField(
        key="moq",
        kind="ask",
        synonyms=(
            "moq",
            "minimum order",
            "pedido minimo",
            "quantidade minima",
            "lote minimo",
            "mindestbestellmenge",
            "mindestmenge",
            "quantite minimum",
            "cantidad minima",
            "minimo d'ordine",
        ),
        labels={
            "en": "Minimum order quantity (MOQ)",
            "pt": "Quantidade mínima (MOQ)",
            "de": "Mindestbestellmenge (MOQ)",
            "fr": "Quantité minimale de commande (MOQ)",
            "es": "Cantidad mínima de pedido (MOQ)",
            "it": "Quantità minima d'ordine (MOQ)",
        },
    ),
)

# Short headings for the line-data columns the render adds.
LINE_HEADINGS: Final[dict[str, dict[str, str]]] = {
    "en": {
        "position": "Pos.",
        "description": "Description",
        "part_number": "Part no.",
        "mpn": "MPN",
        "manufacturer": "Manufacturer",
        "quantity": "Quantity",
        "unit": "Unit",
        "need_by": "Need-by date",
        "qualification": "Qualification",
        "incoterm": "Incoterm",
    },
    "pt": {
        "position": "Item",
        "description": "Descrição",
        "part_number": "Código",
        "mpn": "Código do fabricante (MPN)",
        "manufacturer": "Fabricante",
        "quantity": "Quantidade",
        "unit": "Un.",
        "need_by": "Data de entrega",
        "qualification": "Qualificação",
        "incoterm": "Incoterm",
    },
    "de": {
        "position": "Pos.",
        "description": "Bezeichnung",
        "part_number": "Artikelnr.",
        "mpn": "MPN",
        "manufacturer": "Hersteller",
        "quantity": "Menge",
        "unit": "Einheit",
        "need_by": "Liefertermin",
        "qualification": "Qualifikation",
        "incoterm": "Incoterm",
    },
    "fr": {
        "position": "Pos.",
        "description": "Désignation",
        "part_number": "Référence",
        "mpn": "Réf. fabricant",
        "manufacturer": "Fabricant",
        "quantity": "Quantité",
        "unit": "Unité",
        "need_by": "Date souhaitée",
        "qualification": "Qualification",
        "incoterm": "Incoterm",
    },
    "es": {
        "position": "Pos.",
        "description": "Descripción",
        "part_number": "Código",
        "mpn": "MPN",
        "manufacturer": "Fabricante",
        "quantity": "Cantidad",
        "unit": "Unidad",
        "need_by": "Fecha requerida",
        "qualification": "Calificación",
        "incoterm": "Incoterm",
    },
    "it": {
        "position": "Pos.",
        "description": "Descrizione",
        "part_number": "Codice",
        "mpn": "Codice produttore",
        "manufacturer": "Produttore",
        "quantity": "Quantità",
        "unit": "Unità",
        "need_by": "Data richiesta",
        "qualification": "Qualifica",
        "incoterm": "Incoterm",
    },
}

# The render's heading key -> the required field it carries.
_HEADING_FIELD: Final[dict[str, str]] = {
    "part_number": "mpn",
    "mpn": "mpn",
    "manufacturer": "manufacturer",
    "quantity": "quantity_tiers",
    "unit": "unit",
    "need_by": "need_by",
    "qualification": "qualification",
    "incoterm": "incoterm_place",
}

_BY_KEY: Final = {f.key: f for f in REQUIRED_FIELDS}
_WHOLE_WORD_MAX: Final = 7


def normalize(text: str) -> str:
    """Lower case, no accents, single spaces."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(plain.lower().split())


def label_matches(label: str, synonym: str) -> bool:
    """Whole words for short synonyms ("un", "menge", "einheit"), substrings for long ones.

    Long synonyms may sit inside a compound ("Mengenstaffeln"); short ones would
    match too much ("unita" in "unitário", "menge" in "Mindestbestellmenge").
    """
    text = normalize(label)
    if len(synonym) <= _WHOLE_WORD_MAX:
        return re.search(rf"(?<![a-z0-9]){re.escape(synonym)}(?![a-z0-9])", text) is not None
    return synonym in text


def _own_words(text: str) -> str:
    """A label or heading of ours as a synonym: normalized, without "(MPN)"-style asides."""
    return normalize(re.sub(r"\([^)]*\)", " ", text))


def _accepted(field: RequiredField) -> tuple[str, ...]:
    own = [
        *field.labels.values(),
        *(
            heading
            for headings in LINE_HEADINGS.values()
            for key, heading in headings.items()
            if _HEADING_FIELD.get(key) == field.key
        ),
    ]
    return tuple(dict.fromkeys([*field.synonyms, *(w for w in map(_own_words, own) if w)]))


# What covers each field: its synonyms plus Bido's own words for it in every
# language (its labels and the render's headings), so what Bido writes is
# always recognised and the two lists cannot drift apart.
_ACCEPTED: Final[dict[str, tuple[str, ...]]] = {f.key: _accepted(f) for f in REQUIRED_FIELDS}


def accepted_terms(key: str) -> tuple[str, ...]:
    """The normalized terms that cover a required field."""
    return _ACCEPTED[key]


def covered_keys(labels: Iterable[str]) -> set[str]:
    """Which required fields the labels cover."""
    found: set[str] = set()
    for label in labels:
        for field in REQUIRED_FIELDS:
            if field.key not in found and any(
                label_matches(label, s) for s in _ACCEPTED[field.key]
            ):
                found.add(field.key)
    return found


def missing_required(labels: Iterable[str]) -> list[RequiredField]:
    """Required fields none of the labels cover, in the canonical order."""
    covered = covered_keys(labels)
    return [f for f in REQUIRED_FIELDS if f.key not in covered]


def removed_required(before: Iterable[str], after: Iterable[str]) -> list[RequiredField]:
    """Required fields the old labels covered and the new ones no longer do."""
    lost = covered_keys(before) - covered_keys(after)
    return [f for f in REQUIRED_FIELDS if f.key in lost]


def required_field(key: str) -> RequiredField:
    return _BY_KEY[key]


def template_labels(
    columns: Sequence[str], footer_fields: Sequence[str], fields: Sequence[str]
) -> list[str]:
    """Every label of a template, for coverage checks."""
    return [*columns, *footer_fields, *fields]
