"""Internal issue codes as plain words, for what the copilot's model reads.

Validation and comparison mark problems with codes (`lead_time_days:wrong_locator`,
`needs_review`, `above_buyer_lead_time_limit`) that code and the screen key on. The
model copied them into answers to the buyer (2026-10-10). The model view rewrites
them here, so the model reads words it can translate, not codes it can echo. The
tool's own output, the events and stored previews keep the codes.
"""

import re
from typing import Final

# The fields quote and comparison codes name, as a buyer says them.
FIELD_WORDS: Final = {
    "lead_time_days": "the lead time",
    "unit_price": "the unit price",
    "min_quantity": "the quantity",
    "quantity": "the quantity",
    "unit": "the unit",
    "currency": "the currency",
    "offered_mpn": "the part number",
    "valid_until": "the validity",
    "validity": "the validity",
    "incoterm": "the Incoterm",
    "delivery_location": "the delivery place",
    "payment_terms": "the payment terms",
    "tax_rate": "the tax rate",
    "moq": "the minimum order",
}

# `<field>:<code>` and `<code>:<field>`: a field's problem.
FIELD_PROBLEMS: Final = {
    "wrong_locator": "{field} is not where the file points, so it is not confirmed",
    "not_grounded": "{field} could not be confirmed in the file",
    "quote_not_found": "{field} could not be confirmed in the file",
    "source_not_read": "{field} comes from a file Bido could not read",
    "unknown_attachment": "{field} points to a file that is not attached",
    "no_source": "{field} has no source in the file",
    "out_of_range": "{field} looks out of range",
    "low_confidence": "{field} was read with low confidence",
    "missing": "the file does not give {field}",
    "unverified": "{field} is not confirmed by the file",
    "conflicting_sources": "the files disagree on {field}",
}

# Codes on their own: a quote's, a line's or an offer's problem.
PROBLEMS: Final = {
    "needs_review": "needs a person to check it",
    "low_confidence": "was read with low confidence",
    "empty_quote": "the quote is empty",
    "no_lines": "the quote has no lines",
    "validity_expired": "the quote's validity has expired",
    "unmatched_rfq_line": "a quoted line matches no line of the RFQ",
    "unknown_rfq_line": "a quoted line names a line the RFQ does not have",
    "currency_mismatch": "the currency differs from the RFQ's",
    "currency_unverified": "the currency is not confirmed by the file",
    "quantity_mismatch": "the quantity differs from the RFQ's",
    "incoterm_mismatch": "the Incoterm differs from the RFQ's",
    "delivery_location_mismatch": "the delivery place differs from the RFQ's",
    "invalid_tax_rate": "the tax rate is not valid",
    "conditional_terms_require_review": "the terms have conditions a person must check",
    "uncertain_message_chronology": "the order of the supplier's messages is unclear",
    "above_buyer_lead_time_limit": "delivers later than the lead time you set",
    "above_buyer_maximum": "is above the maximum price you set",
    "alternative_needs_approval": "offers an alternative part that needs your approval",
    "no_quote_for_line": "did not quote this line",
    "missing_price": "has no price for this line",
    "late_for_need_by": "arrives after the date needed",
}

_CODE: Final = re.compile(r"^[a-z][a-z0-9_]*(?::[a-z0-9_]+)?$")
_LINE: Final = re.compile(r"^(line \S+: )(.+)$")


def _field(name: str) -> str:
    return FIELD_WORDS.get(name, "the " + name.replace("_", " "))


def plain(text: str) -> str:
    """`text` in plain words when it is an issue code (optionally `line N: <code>`);
    anything else unchanged."""
    if (line := _LINE.match(text)) is not None:
        return line.group(1) + plain(line.group(2))
    if _CODE.match(text) is None:
        return text
    if ":" in text:
        left, right = text.split(":", 1)
        if right in FIELD_PROBLEMS:
            return FIELD_PROBLEMS[right].format(field=_field(left))
        if left in FIELD_PROBLEMS:
            return FIELD_PROBLEMS[left].format(field=_field(right))
        return f"{_field(left)}: {right.replace('_', ' ')}"
    if text in PROBLEMS:
        return PROBLEMS[text]
    return text.replace("_", " ")
