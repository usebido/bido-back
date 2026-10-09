"""Which language and locale a thread's emails are written in.

The language is fixed when the RFQ email goes out; every later email of the invite
reuses it, so a supplier edited mid-thread does not get a different language under the
same subject.

STAND-IN (N1): workstream N1 persists the resolved language on the invite. At merge,
`thread_language` returns that stored language (and `thread_locale` its locale) and the
fallbacks below go. Until then the language is read back from the RFQ email that was
actually sent: the `lang` of its HTML part, else the type label in its subject, else the
supplier's language as `rfqs.language` resolves it today.
"""

import re
from collections.abc import Mapping
from typing import Final

from bido_back.rfqs.language import country_code, email_language
from bido_back.supplier_email.locale import Locale, catalog_language
from bido_back.templates.render import subject_language

_HTML_LANG: Final = re.compile(r'<html lang="([A-Za-z]{2,3})(?:-[A-Za-z]{2})?"')


def rfq_email_language(
    rfq_email_args: Mapping[str, object] | None, first_subject: str | None
) -> str | None:
    """The language the thread's RFQ email went out in, read from the email itself."""
    html = rfq_email_args.get("html_body") if rfq_email_args else None
    if isinstance(html, str):
        found = _HTML_LANG.search(html[:300])
        if found:
            return found.group(1).lower()
    if first_subject:
        return subject_language(first_subject)
    return None


def thread_language(
    *,
    rfq_email_args: Mapping[str, object] | None,
    first_subject: str | None,
    supplier_language: str | None,
    supplier_country: str | None,
) -> str:
    """The catalog language of every later email in the thread."""
    sent = rfq_email_language(rfq_email_args, first_subject)
    return catalog_language(sent or email_language(supplier_language, supplier_country))


def thread_locale(language: str, supplier_country: str | None) -> Locale:
    """The thread's language with the supplier's region (es-MX writes 19.50, de-CH 1’250)."""
    return Locale.of(language, country_code(supplier_country))
