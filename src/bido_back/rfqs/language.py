"""Which language an email to a supplier is written in.

The language is decided once per invite, when its RFQ email is prepared
(`decide_email_language`), and every later email of that thread (chases,
clarifications, counter-offers, notices, the purchase order) reuses it
(`thread_language`: the language of the approved RFQ email's document). A
thread never switches language, even when the supplier's data changes.

The decision takes the strongest evidence first:

1. `supplier`: the supplier's own language, when it is anything but English.
   `suppliers.language` is NOT NULL DEFAULT 'en', so an `en` cannot be told
   apart from "never set": it only wins when nothing below says otherwise.
   An existing supplier's language is never changed.
2. `country`: the supplier's country, when we know it: its language, or
   English for a country Bido writes no other language for. A known foreign
   country never falls through to the buyer's language.
3. `company_suffix`: the legal form in the supplier's name ("Ltda", "GmbH",
   "S.r.l.", "SARL", "S.L.", "Ltd", "Inc"). A form used in two languages
   ("Ltda": Portuguese or Spanish) takes the one a weaker signal below agrees
   with, else the more common one.
4. `buyer_template`: the language of the buyer's own RFQ template, the one the
   email is rendered from.
5. `buyer`: the buyer's own language, from their latest copilot conversation.
6. `email_domain`: the country code of the supplier's email domain (.br, .de,
   .it, .fr, .es, .pt, ...).
7. `standard_template`: Bido's standard RFQ template is Portuguese, so a
   tenant without its own template works in Portuguese. It comes after the
   email domain because it is Bido's default, not the buyer's choice.
8. `default`: English.

RFQ-6 went out in English: a supplier added by the copilot (name and email,
no country) kept the column default. It now reads "Ltda", and the buyer's
Portuguese.

Pure functions, no I/O.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, cast

from bido_back.db.models import Supplier
from bido_back.templates.required import normalize

DEFAULT_LANGUAGE: Final = "en"

LanguageSource = Literal[
    "supplier",
    "country",
    "company_suffix",
    "buyer_template",
    "buyer",
    "email_domain",
    "standard_template",
    "default",
]

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
    """The language to write to a supplier in: its own, else its country's, else English.

    How threads whose RFQ email predates `decide_email_language` were decided.
    """
    own = (language or "").strip().lower()
    if own and own != DEFAULT_LANGUAGE:
        return own
    return country_language(country) or DEFAULT_LANGUAGE


def supplier_email_language(supplier: Supplier) -> str:
    return email_language(supplier.language, supplier.country)


# --- The decision at the first send -----------------------------------------------------

# Legal forms -> the languages they are used in, the most common first. Only languages
# Bido writes whole emails in; forms shared with others ("SA", "BV") say nothing.
_COMPANY_FORMS: Final[dict[str, tuple[str, ...]]] = {
    **dict.fromkeys(("ltda", "limitada"), ("pt", "es")),
    **dict.fromkeys(("lda", "eireli", "epp"), ("pt",)),
    **dict.fromkeys(("gmbh", "ag", "kg", "kgaa", "ug", "ohg", "mbh"), ("de",)),
    **dict.fromkeys(("spa", "srls"), ("it",)),
    "srl": ("it", "es"),
    **dict.fromkeys(("sarl", "sasu", "eurl"), ("fr",)),
    **dict.fromkeys(("sas", "snc"), ("fr", "it")),
    **dict.fromkeys(("sl", "slu", "sll"), ("es",)),
    **dict.fromkeys(
        ("ltd", "limited", "llc", "inc", "incorporated", "plc", "llp", "corp", "corporation"),
        ("en",),
    ),
}
# Two-word forms, after the dots are dropped: "S.A. de C.V.", "S. de R.L." (Mexico).
_COMPANY_FORM_PAIRS: Final[dict[tuple[str, str], tuple[str, ...]]] = {
    ("de", "cv"): ("es",),
    ("de", "rl"): ("es",),
}

# Email domain country codes -> ISO 639-1. Generic ones (.com, .co, .io) say nothing.
_DOMAIN_LANGUAGE: Final[dict[str, str]] = {
    **dict.fromkeys(("br", "pt", "ao", "mz"), "pt"),
    **dict.fromkeys(("de", "at"), "de"),
    **dict.fromkeys(("it", "sm"), "it"),
    **dict.fromkeys(("fr", "mc"), "fr"),
    **dict.fromkeys(("es", "mx", "ar", "cl", "pe", "uy", "py"), "es"),
    **dict.fromkeys(("uk", "ie", "us", "au", "nz"), "en"),
}

_LANGUAGE_CODE: Final = re.compile(r"^([a-z]{2})(?:[-_][a-z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class LanguageDecision:
    """The language of an invite's emails and the evidence that decided it."""

    language: str
    source: LanguageSource


def language_code(value: str | None) -> str | None:
    """An ISO 639-1 code from "pt", "PT", "pt-BR"; None for anything else."""
    match = _LANGUAGE_CODE.match((value or "").strip().lower())
    return match.group(1) if match else None


def company_form_languages(name: str) -> tuple[str, ...]:
    """The languages the legal form at the end of a company name points to, most common first.

    "MetalNova Teste Ltda" -> ("pt", "es"); "Würth Elektronik GmbH & Co. KG" -> ("de",);
    "Pennine Components Ltd" -> ("en",); no known form -> ().
    """
    words = [w for w in re.split(r"[^a-z0-9]+", re.sub(r"[./]", "", normalize(name))) if w]
    tail = words[-3:]
    for i in range(len(tail) - 1, -1, -1):
        if i > 0 and (tail[i - 1], tail[i]) in _COMPANY_FORM_PAIRS:
            return _COMPANY_FORM_PAIRS[(tail[i - 1], tail[i])]
        if tail[i] in _COMPANY_FORMS:
            return _COMPANY_FORMS[tail[i]]
    return ()


def domain_language(address: str | None) -> str | None:
    """The language of an email address's (or domain's) country code: .br -> pt; else None."""
    domain = (address or "").rpartition("@")[2].strip().rstrip(".").lower()
    if "." not in domain:
        return None
    return _DOMAIN_LANGUAGE.get(domain.rpartition(".")[2])


# English-speaking countries the country tables above leave out.
_ENGLISH_COUNTRIES: Final = frozenset(
    {
        *("ie", "ireland", "au", "australia", "nz", "new zealand", "ca", "canada"),
        *("in", "india", "sg", "singapore", "za", "south africa"),
    }
)


def _country(country: str | None) -> str | None:
    spoken = country_language(country)
    if spoken is not None:
        return spoken
    # A country we know but write no other language for (US, GB, CN, IE): English.
    known = country_code(country) is not None or normalize(country or "") in _ENGLISH_COUNTRIES
    return DEFAULT_LANGUAGE if known else None


def decide_email_language(
    *,
    supplier_language: str | None,
    supplier_country: str | None,
    supplier_name: str,
    email_addresses: Sequence[str | None] = (),
    buyer_template_language: str | None = None,
    buyer_language: str | None = None,
    standard_template_language: str | None = None,
) -> LanguageDecision:
    """The language to write an invite's emails in, decided once at its first send.

    See the module docstring for the order. `email_addresses` are the
    supplier contact's address and the supplier's email domain;
    `buyer_template_language` is the language of the buyer's own template the
    RFQ uses, `standard_template_language` the language of Bido's standard
    template when the RFQ uses that one instead; `buyer_language` is the
    buyer's own (their latest conversation's).
    """
    own = language_code(supplier_language)
    if own is not None and own != DEFAULT_LANGUAGE:
        return LanguageDecision(own, "supplier")
    country = _country(supplier_country)
    if country is not None:
        return LanguageDecision(country, "country")
    domain = next((d for d in map(domain_language, email_addresses) if d is not None), None)
    later: list[tuple[str | None, LanguageSource]] = [
        (language_code(buyer_template_language), "buyer_template"),
        (language_code(buyer_language), "buyer"),
        (domain, "email_domain"),
        (language_code(standard_template_language), "standard_template"),
    ]
    forms = company_form_languages(supplier_name)
    if forms:
        agreed = next((value for value, _ in later if value in forms), None)
        return LanguageDecision(agreed or forms[0], "company_suffix")
    for value, source in later:
        if value is not None:
            return LanguageDecision(value, source)
    return LanguageDecision(DEFAULT_LANGUAGE, "default")


# --- Later emails of the thread -----------------------------------------------------------


def sent_language(rfq_send_args: Mapping[str, object] | None) -> str | None:
    """The language an invite's RFQ email went out in, from its approved send arguments.

    The approved document (`SendRfqEmailArgs.document`) carries it; None for an
    email approved before documents existed.
    """
    document = (rfq_send_args or {}).get("document")
    if not isinstance(document, Mapping):
        return None
    language = cast(Mapping[str, object], document).get("language")
    return language_code(language) if isinstance(language, str) else None


def thread_language(rfq_send_args: Mapping[str, object] | None, supplier: Supplier) -> str:
    """The language of every later email in an invite's thread: its RFQ email's.

    `rfq_send_args` are the approved arguments of the invite's executed RFQ
    email. A thread whose RFQ email predates the document falls back to the
    rule it was decided by then (`supplier_email_language`).
    """
    return sent_language(rfq_send_args) or supplier_email_language(supplier)
