"""Logo checks: allowed types, size, pixel size, and the SVG allowlist."""

import io

import pytest
from PIL import Image

from bido_back.branding.images import (
    MAX_LOGO_BYTES,
    InvalidLogo,
    check_logo,
    logo_type_for,
    sanitize_svg,
)


def _png(width: int = 120, height: int = 40) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (width, height), (90, 60, 200, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 32), (10, 20, 30)).save(buffer, format="JPEG")
    return buffer.getvalue()


def test_raster_logos_keep_their_bytes_and_report_pixels() -> None:
    png = _png()
    checked = check_logo(png, "image/png")
    assert (checked.content_type, checked.width, checked.height) == ("image/png", 120, 40)
    assert checked.data == png
    assert check_logo(_jpeg(), "image/jpeg").width == 64


def test_type_comes_from_the_extension_and_must_match_the_content() -> None:
    assert logo_type_for("Logo.PNG") == "image/png"
    assert logo_type_for("marca.jpeg") == "image/jpeg"
    for name in ("logo.gif", "logo.html", "logo"):
        with pytest.raises(InvalidLogo):
            logo_type_for(name)
    with pytest.raises(InvalidLogo, match="does not match"):
        check_logo(_png(), "image/jpeg")
    with pytest.raises(InvalidLogo, match="not a PNG"):
        check_logo(b"<html><script>alert(1)</script></html>", "image/png")


def test_size_limits() -> None:
    with pytest.raises(InvalidLogo, match="empty"):
        check_logo(b"", "image/png")
    with pytest.raises(InvalidLogo, match="2 MB"):
        check_logo(b"\x89PNG\r\n\x1a\n" + b"0" * MAX_LOGO_BYTES, "image/png")
    with pytest.raises(InvalidLogo, match="cannot be read"):
        check_logo(b"\x89PNG\r\n\x1a\n" + b"garbage", "image/png")


SVG_ATTACK = b"""<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
     width="200" height="60" viewBox="0 0 200 60" onload="alert(1)">
  <script>alert('x')</script>
  <style>@import url(https://evil.example/x.css);</style>
  <foreignObject><div xmlns="http://www.w3.org/1999/xhtml">hi</div></foreignObject>
  <a href="https://evil.example"><rect width="10" height="10"/></a>
  <rect width="200" height="60" fill="url(#grad)" onclick="steal()"/>
  <rect width="5" height="5" style="fill: url(https://evil.example/p.svg#a)"/>
  <use xlink:href="https://evil.example/sprite.svg#icon"/>
  <use href="#mark"/>
  <image href="javascript:alert(1)"/>
  <image href="data:image/png;base64,iVBORw0KGgo="/>
  <linearGradient id="grad"><stop offset="0" stop-color="#5B3FD0"/></linearGradient>
  <path id="mark" d="M0 0 L10 10" stroke="#000"/>
</svg>"""


def test_svg_is_rewritten_through_the_allowlist() -> None:
    clean, width, height = sanitize_svg(SVG_ATTACK)
    text = clean.decode()
    assert (width, height) == (200, 60)
    for banned in (
        "script",
        "onload",
        "onclick",
        "@import",
        "foreignObject",
        "evil.example",
        "javascript:",
        "<a ",
        "<style",
    ):
        assert banned not in text, banned
    assert 'fill="url(#grad)"' in text
    assert 'href="#mark"' in text
    assert "data:image/png;base64" in text
    assert "<path" in text and "linearGradient" in text


def test_svg_with_dtd_or_entities_is_refused() -> None:
    bomb = b"""<?xml version="1.0"?>
<!DOCTYPE svg [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>
<svg xmlns="http://www.w3.org/2000/svg">&b;</svg>"""
    with pytest.raises(InvalidLogo, match="DOCTYPE"):
        check_logo(bomb, "image/svg+xml")


def test_svg_size_falls_back_to_the_viewbox() -> None:
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 90"><rect/></svg>'
    checked = check_logo(svg, "image/svg+xml")
    assert (checked.width, checked.height) == (320, 90)
    with pytest.raises(InvalidLogo):
        check_logo(b"<html xmlns='http://www.w3.org/1999/xhtml'><svg/></html>", "image/svg+xml")


def test_document_logos_are_small_png_jpeg_or_svg() -> None:
    from bido_back.branding.images import MAX_DOCUMENT_LOGO_BYTES, logo_for_document

    small = _png()
    assert logo_for_document(small, "image/png") == (small, "image/png")

    buffer = io.BytesIO()
    Image.new("RGB", (3000, 1000), (1, 2, 3)).save(buffer, format="WEBP")
    converted = logo_for_document(buffer.getvalue(), "image/webp")
    assert converted is not None and converted[1] == "image/png"
    with Image.open(io.BytesIO(converted[0])) as image:
        assert image.width <= 1200 and image.height <= 400

    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>'
    assert logo_for_document(svg, "image/svg+xml") == (svg, "image/svg+xml")
    assert logo_for_document(b"<svg>" + b" " * MAX_DOCUMENT_LOGO_BYTES, "image/svg+xml") is None
    assert logo_for_document(b"not an image", "image/png") is None
