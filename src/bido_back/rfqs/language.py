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
