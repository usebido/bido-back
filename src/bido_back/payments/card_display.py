"""A card's branding (the issuer's art and colours), kept only when well formed.

Basis Theory returns `card.display` on a payment method: the issuer's card art,
colours, product description and name, every field nullable. The frontend draws
the buyer's card with it. It is public branding, not card data, but it is still
provider input that ends up in our database and in an `<img src>`: only an
https URL, `#rrggbb` colours and short printable text are kept; anything else is
dropped (never an error, a card is enrolled with or without its art).

The same cleaning runs on what Basis Theory sends and on what is read back from
`payment_methods.display`, so a bad stored value cannot break a view either.
"""

import re
from collections.abc import Mapping
from dataclasses import asdict
from typing import Final, cast
from urllib.parse import urlsplit

from bido_back.payments.ports import CardDisplayData

ART_URL_MAX: Final = 2048
TEXT_MAX: Final = 120
_COLOUR: Final = re.compile(r"#[0-9a-fA-F]{6}")


def _text(value: object) -> str | None:
    """Trimmed printable text, cut at `TEXT_MAX` characters; None otherwise."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or not text.isprintable():
        return None
    return text[:TEXT_MAX].rstrip()


def _colour(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    colour = value.strip()
    return colour if _COLOUR.fullmatch(colour) else None


def _art_url(value: object) -> str | None:
    """An absolute https URL with a host and no credentials; a cut URL is no URL, so a
    longer one is dropped."""
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url or len(url) > ART_URL_MAX or not url.isprintable() or " " in url:
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or not host or "@" in parts.netloc:
        return None
    return url


def card_display(
    *,
    art_url: object = None,
    background_color: object = None,
    foreground_color: object = None,
    description: object = None,
    issuer_name: object = None,
    issuer_fallback: object = None,
) -> CardDisplayData | None:
    """The well-formed part of a card's display; None when nothing is left.

    `issuer_fallback` (the card's `issuer.name`) names the issuer when the display
    does not.
    """
    display = CardDisplayData(
        art_url=_art_url(art_url),
        background_color=_colour(background_color),
        foreground_color=_colour(foreground_color),
        description=_text(description),
        issuer_name=_text(issuer_name) or _text(issuer_fallback),
    )
    return display if any(asdict(display).values()) else None


def display_json(display: CardDisplayData | None) -> dict[str, str | None] | None:
    """What `payment_methods.display` stores: the five keys, or NULL."""
    return asdict(display) if display is not None else None


def display_from_json(value: object) -> CardDisplayData | None:
    """A stored display, cleaned again: anything unexpected reads as no display."""
    if not isinstance(value, Mapping):
        return None
    fields = cast(Mapping[object, object], value)
    return card_display(
        art_url=fields.get("art_url"),
        background_color=fields.get("background_color"),
        foreground_color=fields.get("foreground_color"),
        description=fields.get("description"),
        issuer_name=fields.get("issuer_name"),
    )
