"""Which language an email to a supplier is written in.

`suppliers.language` defaults to English, so a supplier imported from a buyer's
master with only a country ("BR", "Brasil") would get English emails. The
supplier's own language wins when it was set to something else; otherwise the
country decides when we know its language; otherwise English.

Pure functions, no I/O.
"""

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
