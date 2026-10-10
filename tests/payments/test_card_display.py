"""A card's branding: only well-formed values are kept, on the way in and on the way out."""

import pytest
from pydantic import JsonValue

from bido_back.payments.card_display import (
    ART_URL_MAX,
    TEXT_MAX,
    card_display,
    display_from_json,
    display_json,
)
from bido_back.payments.ports import CardDisplayData

ART = "https://cdn.example.test/art/visa-signature.png"


@pytest.mark.parametrize(
    ("url", "kept"),
    [
        (ART, ART),
        (f"  {ART}\n", ART),
        ("HTTPS://cdn.example.test/a.png", "HTTPS://cdn.example.test/a.png"),
        ("http://cdn.example.test/a.png", None),
        ("javascript:alert(1)", None),
        ("data:image/png;base64,AAAA", None),
        ("//cdn.example.test/a.png", None),
        ("https:///a.png", None),
        ("https://user:secret@cdn.example.test/a.png", None),
        ("https://cdn.example.test/a b.png", None),
        ("https://[::1/a.png", None),
        pytest.param("https://cdn.example.test/" + "a" * ART_URL_MAX, None, id="too-long"),
        (42, None),
    ],
)
def test_the_art_is_an_https_url(url: object, kept: str | None) -> None:
    display = card_display(art_url=url, issuer_name="Example Bank")

    assert display is not None and display.art_url == kept


@pytest.mark.parametrize(
    ("colour", "kept"),
    [
        ("#1A1F71", "#1A1F71"),
        (" #ffffff ", "#ffffff"),
        ("#FFF", None),
        ("1A1F71", None),
        ("#1A1F7G", None),
        ("rgb(0, 0, 0)", None),
        ("#1A1F71; background: url(x)", None),
        (None, None),
    ],
)
def test_colours_are_six_digit_hex(colour: object, kept: str | None) -> None:
    display = card_display(background_color=colour, foreground_color=colour, issuer_name="X")

    assert display is not None
    assert (display.background_color, display.foreground_color) == (kept, kept)


def test_text_is_trimmed_printable_and_capped() -> None:
    display = card_display(description="  Visa Signature  ", issuer_name="x" * (TEXT_MAX + 50))

    assert display is not None
    assert display.description == "Visa Signature"
    assert display.issuer_name == "x" * TEXT_MAX
    assert card_display(description="Line\nbreak") is None


def test_the_issuer_fallback_names_the_card_only_when_the_display_does_not() -> None:
    assert card_display(issuer_fallback=" Example Bank ") == CardDisplayData(
        None, None, None, None, "Example Bank"
    )
    named = card_display(issuer_name="Chase", issuer_fallback="JPMorgan Chase Bank")
    assert named is not None and named.issuer_name == "Chase"


def test_nothing_usable_is_no_display() -> None:
    assert card_display() is None
    assert card_display(art_url="http://x.test/a.png", background_color="red") is None


def test_a_stored_display_round_trips_and_junk_reads_as_none() -> None:
    display = CardDisplayData(ART, "#1A1F71", "#FFFFFF", "Visa Signature", "Example Bank")

    stored = display_json(display)

    assert stored == {
        "art_url": ART,
        "background_color": "#1A1F71",
        "foreground_color": "#FFFFFF",
        "description": "Visa Signature",
        "issuer_name": "Example Bank",
    }
    assert display_from_json(stored) == display
    assert display_json(None) is None
    junk: list[JsonValue] = [None, "x", [ART], {}, {"art_url": "http://x.test/a.png"}]
    assert [display_from_json(value) for value in junk] == [None] * len(junk)
