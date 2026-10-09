"""Which language an email to a supplier is written in.

`suppliers.language` defaults to English, so a supplier imported from a buyer's
master with only a country ("BR", "Brasil") would get English emails. The
supplier's own language wins when it was set to something else; otherwise the
country decides when we know its language; otherwise English.

A supplier created from just a name and an email (the Copilot's
`add_supplier_contact`, `POST /v1/suppliers` without a country) would also get
English. `infer_supplier_locale` reads the country off the email's country TLD
or an unambiguous legal suffix in the name, and nothing else: a `.com` address
and a name like "Cobre Sul S.A." infer nothing, since "S.A." is used across
Latin countries. The Copilot asks the buyer in those cases.

Pure functions, no I/O.
"""

import re
from typing import Final

from bido_back.db.models import Supplier
from bido_back.templates.required import normalize

DEFAULT_LANGUAGE: Final = "en"

# Country (ISO 3166-1 alpha-2, or a common name in the buyer's languages) -> ISO 639-1.
_COUNTRY_LANGUAGE: Final[dict[str, str]] = {
    **dict.fromkeys(("br", "brasil", "brazil", "pt", "portugal", "ao", "angola", "mz"), "pt"),
    **dict.fromkeys(("it", "italia", "italy", "italie", "italien", "sm"), "it"),
    **dict.fromkeys(
        (
            "de",
            "deutschland",
            "germany",
            "alemanha",
            "allemagne",
            "germania",
            "at",
            "austria",
            "osterreich",
            "li",
        ),
        "de",
    ),
    **dict.fromkeys(("fr", "france", "franca", "francia", "frankreich", "mc"), "fr"),
    **dict.fromkeys(
        (
            "es",
            "espana",
            "spain",
            "espanha",
            "spagna",
            "spanien",
            "mx",
            "mexico",
            "ar",
            "argentina",
            "cl",
            "chile",
            "co",
            "colombia",
            "pe",
            "peru",
            "uy",
            "uruguay",
            "py",
            "paraguay",
        ),
        "es",
    ),
}


# Country names and ISO alpha-3 codes a buyer's files write -> ISO 3166-1 alpha-2.
_COUNTRY_CODE: Final[dict[str, str]] = {
    **dict.fromkeys(("br", "bra", "brasil", "brazil", "brasile", "bresil", "brasilien"), "BR"),
    **dict.fromkeys(("pt", "prt", "portugal", "portogallo"), "PT"),
    **dict.fromkeys(("ao", "ago", "angola"), "AO"),
    **dict.fromkeys(("mz", "moz", "mocambique", "mozambique"), "MZ"),
    **dict.fromkeys(("it", "ita", "italia", "italy", "italie", "italien"), "IT"),
    **dict.fromkeys(
        ("de", "deu", "deutschland", "germany", "alemanha", "allemagne", "germania", "alemania"),
        "DE",
    ),
    **dict.fromkeys(("at", "aut", "austria", "osterreich", "autriche"), "AT"),
    **dict.fromkeys(("fr", "fra", "france", "franca", "francia", "frankreich"), "FR"),
    **dict.fromkeys(("es", "esp", "espana", "spain", "espanha", "spagna", "spanien"), "ES"),
    **dict.fromkeys(("mx", "mex", "mexico"), "MX"),
    **dict.fromkeys(("ar", "arg", "argentina"), "AR"),
    **dict.fromkeys(("cl", "chl", "chile"), "CL"),
    **dict.fromkeys(("co", "col", "colombia", "colombie", "kolumbien"), "CO"),
    **dict.fromkeys(("pe", "per", "peru"), "PE"),
    **dict.fromkeys(("uy", "ury", "uruguay", "uruguai"), "UY"),
    **dict.fromkeys(("py", "pry", "paraguay", "paraguai"), "PY"),
    **dict.fromkeys(
        ("us", "usa", "united states", "united states of america", "estados unidos", "eua"),
        "US",
    ),
    **dict.fromkeys(("gb", "gbr", "uk", "united kingdom", "reino unido"), "GB"),
    **dict.fromkeys(("cn", "chn", "china", "chine"), "CN"),
}


# Email country-code TLDs that name one country whose language is not in doubt (or, for
# US and GB, whose RFQs are in English either way). Never generic-looking ones: `.co` is
# sold as "company", `.io`, `.me`, `.tv` likewise; `.ch`, `.be` and `.ca` are multilingual.
_TLD_COUNTRY: Final[dict[str, str]] = {
    "br": "BR",
    "pt": "PT",
    "ao": "AO",
    "mz": "MZ",
    "de": "DE",
    "at": "AT",
    "it": "IT",
    "fr": "FR",
    "es": "ES",
    "mx": "MX",
    "ar": "AR",
    "cl": "CL",
    "pe": "PE",
    "uy": "UY",
    "py": "PY",
    "us": "US",
    "uk": "GB",
}

# Legal-form suffixes that belong to one country, matched as whole tokens of the name.
# Left out on purpose: "S.A." (Brazil, Portugal, Spain, France, Argentina...), "AG"
# (Germany and Switzerland), "SAS" (France, but also a plain acronym), bare "SPA" (a spa),
# "Ltd"/"Inc" (anywhere English is written). Case matters where a spelling is also a word.
_SUFFIX_COUNTRY: Final[tuple[tuple[str, int, str], ...]] = (
    (r"ltda\.?", re.IGNORECASE, "BR"),
    (r"eireli", re.IGNORECASE, "BR"),
    (r"EPP", 0, "BR"),
    (r"ME", 0, "BR"),  # "Padaria Silva ME"
    (r"lda\.?", re.IGNORECASE, "PT"),
    (r"g?mbh", re.IGNORECASE, "DE"),  # "GmbH", "GMBH", "Gesellschaft mbH", "GmbH & Co. KG"
    (r"s\.?r\.?l\.?", re.IGNORECASE, "IT"),
    (r"S\.p\.A\.?|S\.P\.A\.?|SpA", 0, "IT"),
    (r"S\.L\.(?:U\.?)?|SLU?", 0, "ES"),
    (r"s\.?a\.?r\.?l\.?|sasu|eurl", re.IGNORECASE, "FR"),
    (r"s\.?a\.? de c\.?v\.?|s\.? de r\.?l\.?(?: de c\.?v\.?)?", re.IGNORECASE, "MX"),
)
_SUFFIX_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = tuple(
    (re.compile(rf"(?:^|(?<=\s))(?:{suffix})(?=$|[\s,.;)])", flags), country)
    for suffix, flags, country in _SUFFIX_COUNTRY
)


def _tld_country(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().lower().rstrip(".")
    if "." not in domain:
        return None
    return _TLD_COUNTRY.get(domain.rsplit(".", 1)[1])


def _suffix_country(name: str | None) -> str | None:
    if not name:
        return None
    collapsed = " ".join(name.split())
    for pattern, country in _SUFFIX_PATTERNS:
        if pattern.search(collapsed):
            return country
    return None


def infer_supplier_locale(name: str | None, email: str | None) -> tuple[str | None, str | None]:
    """A supplier's (country, language) when its email's country TLD or an unambiguous
    legal suffix in its name says so; (None, None) otherwise. The TLD wins over the name:
    it is where the mail goes. Conservative by design: `.com` alone infers nothing, and
    the language is only the country's known one (None for US or GB: English by default).
    """
    country = _tld_country(email) or _suffix_country(name)
    if country is None:
        return None, None
    return country, country_language(country)


def country_code(country: str | None) -> str | None:
    """The ISO 3166-1 alpha-2 code of a country written as a code or a common name.

    None for anything not in the table (a state, a city, a misspelling): never guessed.
    """
    if not country:
        return None
    return _COUNTRY_CODE.get(normalize(country))


def country_language(country: str | None) -> str | None:
    """The language spoken in a country, when we know it; None otherwise."""
    if not country:
        return None
    return _COUNTRY_LANGUAGE.get(normalize(country))


def email_language(language: str | None, country: str | None) -> str:
    """The language to write to a supplier in: its own, else its country's, else English."""
    own = (language or "").strip().lower()
    if own and own != DEFAULT_LANGUAGE:
        return own
    return country_language(country) or DEFAULT_LANGUAGE


def supplier_email_language(supplier: Supplier) -> str:
    return email_language(supplier.language, supplier.country)
