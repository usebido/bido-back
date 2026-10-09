"""The buyer's signature under every supplier email, written by code.

`signature()` is the one rule for every email type: the RFQ, chases and
clarifications, counter-offers and best-and-final requests, "not selected"
notices and purchase orders. It is the buyer's name, then the company when it
says something new. No code path creates buyer contacts, so in production the
buyer's name falls back to the company, and the company then signs once.

The writer models do not sign (`rfq_followup_writer.v2`,
`rfq_negotiation_writer.v2`, `award_notice_writer.v2`: their input carries no
buyer name or company). `signed()` puts the signature under what a model wrote,
after dropping the lines a model may still have signed with (the buyer's or the
company's name, a "[Your name]" placeholder, a line repeated at the end), so an
email never carries two signatures.
"""

import re
from collections.abc import Iterable
from typing import Final

# A writer's placeholder signature: "[Your name]", "<Name>", "{Company}".
_PLACEHOLDER: Final = re.compile(r"^\s*[\[<{].{0,60}[\]>}]\s*$")
# What a closing line ends with when the writer wrote no closing formula.
_SENTENCE_END: Final = (".", "!", "?", ":", ";")


def signature(buyer_name: str, buyer_company: str) -> list[str]:
    """The closing's name lines: the buyer, then the company when it says something new."""
    name, company = buyer_name.strip(), buyer_company.strip()
    if not name or name.casefold() == company.casefold():
        return [company] if company else []
    return [name, company] if company else [name]


def _key(line: str) -> str:
    return " ".join(line.strip().strip(",.;:-–—").casefold().split())


def strip_signature(body: str, names: Iterable[str]) -> str:
    """`body` without the signature lines a writer model added at its end.

    Drops, from the end: lines that are one of `names` (any case, trailing
    punctuation ignored), placeholders such as "[Your name]", and a line that
    repeats the line above it. Everything else is kept as written.
    """
    keys = {key for key in (_key(name) for name in names) if key}
    lines = body.rstrip().splitlines()
    while lines:
        last = lines[-1]
        if not last.strip() or _key(last) in keys or _PLACEHOLDER.match(last):
            lines.pop()
            continue
        if len(lines) > 1 and _key(last) and _key(last) == _key(lines[-2]):
            lines.pop()
            continue
        break
    return "\n".join(lines)


def signed(body: str, buyer_name: str, buyer_company: str) -> str:
    """A writer model's email closed with the buyer's signature, exactly once.

    The signature goes right under the closing formula ("Atenciosamente,",
    "Mit freundlichen Grüßen"); after a paragraph that ends a sentence (the
    writer left out the closing), it starts a block of its own.
    """
    text = strip_signature(body, (buyer_name, buyer_company))
    lines = signature(buyer_name, buyer_company)
    if not lines:
        return text
    if not text:
        return "\n".join(lines)
    last = text.splitlines()[-1].rstrip()
    gap = "\n\n" if last.endswith(_SENTENCE_END) else "\n"
    return text + gap + "\n".join(lines)
