"""The fields every EV-electronics RFQ must carry, and matching them to a template's labels.

A template's labels are the buyer's own words, in their language ("Prazo de
entrega", "Lieferzeit (Wochen)", "MOQ"). Each required field has synonyms in
the languages we see (pt, en, de, fr, es, it); a label covers a field when it
contains one of them. Short synonyms (seven letters or fewer, like "un",
"menge" or "einheit") must be whole words of the label, so "un" does not
match "fundo" nor "menge" match "Mindestbestellmenge".

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
            "prazo de entrega",
            "prazo",
            "lieferzeit",
            "delai",
            "plazo de entrega",
            "plazo",
            "tempi di consegna",
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


def covered_keys(labels: Iterable[str]) -> set[str]:
    """Which required fields the labels cover."""
    found: set[str] = set()
    for label in labels:
        for field in REQUIRED_FIELDS:
            if field.key not in found and any(label_matches(label, s) for s in field.synonyms):
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
