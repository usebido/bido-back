"""Logo checks: allowed types, size, pixel size, and the SVG allowlist."""

import io

import pytest
from PIL import Image, ImageDraw

from bido_back.branding.images import (
    MAX_LOGO_BYTES,
    InvalidLogo,
    check_logo,
    logo_for_document,
    logo_type_for,
    prepare_raster_logo,
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
    from bido_back.branding.images import MAX_DOCUMENT_LOGO_BYTES

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


# --- Preparation: transparency, trimming, background removal ---------------------------


def _save(image: Image.Image, fmt: str, **options: object) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **options)
    return buffer.getvalue()


def _ring_on(background: tuple[int, int, int], size: tuple[int, int] = (400, 200)) -> Image.Image:
    """A black ring with a white inside, centred on a plain background with wide margins."""
    image = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(image)
    draw.ellipse((150, 50, 250, 150), fill=(0, 0, 0))
    draw.ellipse((170, 70, 230, 130), fill=(255, 255, 255))  # enclosed white
    return image


def _alpha_at(data: bytes, point: tuple[int, int]) -> int:
    pixel = Image.open(io.BytesIO(data)).convert("RGBA").getpixel(point)
    assert isinstance(pixel, tuple)
    return int(pixel[3])


def test_a_transparent_png_keeps_its_alpha_and_loses_its_margins() -> None:
    image = Image.new("RGBA", (300, 200), (0, 0, 0, 0))
    ImageDraw.Draw(image).rectangle((100, 60, 200, 140), fill=(91, 63, 208, 255))
    prepared = prepare_raster_logo(_save(image, "PNG"), "image/png")
    assert prepared is not None and prepared.content_type == "image/png"
    out = Image.open(io.BytesIO(prepared.data))
    assert out.mode == "RGBA"
    assert out.size == (101 + 8, 81 + 8)  # trimmed to the mark plus 4px padding
    assert _alpha_at(prepared.data, (0, 0)) == 0  # still transparent around the mark
    assert not prepared.background_removable  # already transparent: nothing to remove


def test_a_palette_png_with_trns_stays_transparent() -> None:
    image = Image.new("P", (120, 80), 0)
    image.putpalette([255, 255, 255, 200, 30, 30] + [0, 0, 0] * 254)
    ImageDraw.Draw(image).rectangle((40, 20, 80, 60), fill=1)
    prepared = prepare_raster_logo(_save(image, "PNG", transparency=0), "image/png")
    assert prepared is not None
    assert _alpha_at(prepared.data, (0, 0)) == 0
    assert Image.open(io.BytesIO(prepared.data)).mode == "RGBA"


def test_a_white_background_is_detected_trimmed_and_removable() -> None:
    data = _save(_ring_on((255, 255, 255)), "JPEG", quality=92)
    kept = prepare_raster_logo(data, "image/jpeg")
    assert kept is not None and kept.background_removable and not kept.background_removed
    assert (kept.width, kept.height) == (101 + 8, 101 + 8)  # margins trimmed
    assert _alpha_at(kept.data, (0, 0)) == 255  # background kept until asked

    removed = prepare_raster_logo(data, "image/jpeg", remove_background=True)
    assert removed is not None and removed.background_removed
    assert removed.content_type == "image/png"  # never JPEG once it has transparency
    assert _alpha_at(removed.data, (0, 0)) == 0  # corners transparent
    centre = (removed.width // 2, removed.height // 2)
    assert _alpha_at(removed.data, centre) == 255  # the enclosed white stays


def test_a_baked_in_checkerboard_counts_as_background() -> None:
    image = Image.new("RGB", (320, 200), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for x in range(0, 320, 10):
        for y in range(0, 200, 10):
            if (x // 10 + y // 10) % 2:
                draw.rectangle((x, y, x + 9, y + 9), fill=(204, 204, 204))
    draw.rectangle((120, 70, 200, 130), fill=(220, 30, 30))
    removed = prepare_raster_logo(_save(image, "PNG"), "image/png", remove_background=True)
    assert removed is not None and removed.background_removable and removed.background_removed
    assert _alpha_at(removed.data, (0, 0)) == 0
    assert _alpha_at(removed.data, (removed.width // 2, removed.height // 2)) == 255


def test_a_busy_image_is_not_treated_as_background() -> None:
    image = Image.new("RGB", (64, 64))
    image.putdata([((x * 37) % 256, (x * 91) % 256, (x * 53) % 256) for x in range(64 * 64)])  # pyright: ignore[reportUnknownMemberType]
    prepared = prepare_raster_logo(_save(image, "PNG"), "image/png")
    assert prepared is not None and not prepared.background_removable
    assert (prepared.width, prepared.height) == (64, 64)


def test_modes_and_orientation_are_normalized() -> None:
    sixteen = Image.new("I;16", (60, 30), 40000)
    prepared = prepare_raster_logo(_save(sixteen, "PNG"), "image/png")
    assert prepared is not None and Image.open(io.BytesIO(prepared.data)).mode in ("RGB", "RGBA")

    grey_alpha = Image.new("LA", (60, 30), (0, 0))
    ImageDraw.Draw(grey_alpha).rectangle((20, 5, 40, 25), fill=(10, 255))
    prepared = prepare_raster_logo(_save(grey_alpha, "PNG"), "image/png")
    assert prepared is not None and _alpha_at(prepared.data, (0, 0)) == 0

    # EXIF orientation 6 (rotate 90° clockwise to display): a wide photo stands up.
    wide = Image.new("RGB", (120, 40), (10, 10, 10))
    exif = Image.Exif()
    exif[0x0112] = 6
    prepared = prepare_raster_logo(_save(wide, "JPEG", exif=exif.tobytes()), "image/jpeg")
    assert prepared is not None and (prepared.width, prepared.height) == (40, 120)


def test_svg_is_not_prepared() -> None:
    assert prepare_raster_logo(b"<svg/>", "image/svg+xml") is None


def test_document_logos_keep_transparency_when_re_encoded() -> None:
    big = Image.new("RGBA", (2000, 600), (0, 0, 0, 0))
    ImageDraw.Draw(big).rectangle((500, 100, 1500, 500), fill=(91, 63, 208, 255))
    fitted = logo_for_document(_save(big, "PNG"), "image/png")
    assert fitted is not None and fitted[1] == "image/png"
    assert _alpha_at(fitted[0], (0, 0)) == 0
