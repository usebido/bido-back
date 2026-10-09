"""Checking a logo the buyer uploaded: real image, allowed type, size, pixel size.

PNG, JPEG and WebP are opened with Pillow (which also refuses decompression
bombs) to read their pixel size; the bytes are kept as uploaded.

An SVG is kept as a vector, so the RFQ document stays sharp, but only after it
is rewritten through an allowlist: known drawing elements and presentation
attributes only. Scripts, `foreignObject`, event handlers (`on*`), `<style>`
blocks, and every reference that is not a local `#fragment` or an embedded
raster `data:` image are removed, and documents with a DOCTYPE or entities are
refused outright. What is stored and served is the rewritten SVG, so even a
direct visit to its link renders no active content. Converting SVG to PNG would
need a native renderer (cairo/resvg) in the image; sanitizing needs nothing new.
"""

import io
import re
import xml.etree.ElementTree as ET  # noqa: S405  # input is checked for DTD/entities first
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal, cast

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, UnidentifiedImageError

MAX_LOGO_BYTES: Final = 2 * 1024 * 1024
# Pillow refuses anything larger (decompression bombs); logos are far smaller.
MAX_LOGO_PIXELS: Final = 40_000_000

LogoType = Literal["image/png", "image/jpeg", "image/webp", "image/svg+xml"]

EXTENSIONS: Final[dict[str, LogoType]] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "svg": "image/svg+xml",
}
STORED_EXTENSION: Final[dict[LogoType, str]] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/svg+xml": "svg",
}


class InvalidLogo(ValueError):
    """Not an accepted image, too big, or an SVG that cannot be made safe."""


@dataclass(frozen=True, slots=True)
class CheckedLogo:
    content_type: LogoType
    data: bytes
    width: int | None
    height: int | None


def logo_type_for(filename: str) -> LogoType:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in EXTENSIONS:
        raise InvalidLogo("logos must be PNG, JPEG, WebP or SVG")
    return EXTENSIONS[ext]


def check_logo(data: bytes, declared: LogoType) -> CheckedLogo:
    """The logo as it will be stored. Raises `InvalidLogo`."""
    if not data:
        raise InvalidLogo("the file is empty")
    if len(data) > MAX_LOGO_BYTES:
        raise InvalidLogo("logos are limited to 2 MB")
    sniffed = _sniff(data)
    if sniffed is None:
        raise InvalidLogo("the file is not a PNG, JPEG, WebP or SVG image")
    if sniffed != declared:
        raise InvalidLogo("the file's content does not match its extension")
    if sniffed == "image/svg+xml":
        clean, width, height = sanitize_svg(data)
        return CheckedLogo("image/svg+xml", clean, width, height)
    width, height = _raster_size(data)
    return CheckedLogo(sniffed, data, width, height)


def _sniff(data: bytes) -> LogoType | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data[:2048].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith((b"<?xml", b"<svg", b"<!--")) and b"<svg" in data[:4096].lower():
        return "image/svg+xml"
    return None


def _raster_size(data: bytes) -> tuple[int, int]:
    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_LOGO_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
        raise InvalidLogo("the image cannot be read") from None
    finally:
        Image.MAX_IMAGE_PIXELS = previous
    return width, height


# --- SVG ------------------------------------------------------------------------------

SVG_NS: Final = "http://www.w3.org/2000/svg"
XLINK_NS: Final = "http://www.w3.org/1999/xlink"

_ALLOWED_ELEMENTS: Final = frozenset(
    {
        "svg", "g", "defs", "symbol", "use", "title", "desc", "metadata",
        "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
        "text", "tspan", "textPath",
        "linearGradient", "radialGradient", "stop", "pattern", "clipPath", "mask",
        "image", "filter", "feGaussianBlur", "feOffset", "feBlend", "feColorMatrix",
        "feFlood", "feComposite", "feMerge", "feMergeNode",
    }
)  # fmt: skip
_ALLOWED_ATTRIBUTES: Final = frozenset(
    {
        "id", "class", "viewBox", "width", "height", "x", "y", "x1", "y1", "x2", "y2",
        "cx", "cy", "r", "rx", "ry", "d", "points", "transform", "fill", "fill-opacity",
        "fill-rule", "clip-rule", "stroke", "stroke-width", "stroke-opacity",
        "stroke-linecap", "stroke-linejoin", "stroke-miterlimit", "stroke-dasharray",
        "stroke-dashoffset", "opacity", "style", "font-family", "font-size", "font-weight",
        "font-style", "text-anchor", "dominant-baseline", "letter-spacing", "offset",
        "stop-color", "stop-opacity", "gradientUnits", "gradientTransform", "spreadMethod",
        "fx", "fy", "fr", "patternUnits", "patternContentUnits", "patternTransform",
        "clipPathUnits", "maskUnits", "maskContentUnits", "clip-path", "mask", "filter",
        "preserveAspectRatio", "version", "visibility", "display", "href",
        "stdDeviation", "dx", "dy", "in", "in2", "mode", "result", "values", "type",
        "operator", "flood-color", "flood-opacity", "filterUnits",
    }
)  # fmt: skip
_SAFE_DATA_IMAGE: Final = re.compile(r"^data:image/(png|jpeg|webp|gif);base64,[A-Za-z0-9+/=\s]+$")
_URL_REF: Final = re.compile(r"url\(\s*['\"]?\s*([^)'\"\s]*)", re.IGNORECASE)
_LENGTH: Final = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*(px)?\s*$")


def sanitize_svg(data: bytes) -> tuple[bytes, int | None, int | None]:
    """The SVG rewritten through the allowlist, and its size in pixels when known."""
    text = data.decode("utf-8", errors="strict") if _is_utf8(data) else None
    if text is None:
        raise InvalidLogo("SVG logos must be UTF-8 text")
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise InvalidLogo("SVG logos cannot declare a DOCTYPE or entities")
    try:
        root = ET.fromstring(text)  # noqa: S314  # DTD and entities refused above
    except ET.ParseError:
        raise InvalidLogo("the SVG cannot be read") from None
    if _local(root.tag) != "svg" or _namespace(root.tag) not in ("", SVG_NS):
        raise InvalidLogo("the file is not an SVG image")
    _clean(root)
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK_NS)
    clean = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return clean, _svg_length(root, "width", 2), _svg_length(root, "height", 3)


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _clean(element: ET.Element) -> None:
    for child in list(element):
        if _local(child.tag) not in _ALLOWED_ELEMENTS:
            element.remove(child)
            continue
        if _namespace(child.tag) not in ("", SVG_NS):
            element.remove(child)
            continue
        _clean(child)
    for name, value in list(element.attrib.items()):
        if not _attribute_allowed(name, value):
            del element.attrib[name]


def _attribute_allowed(name: str, value: str) -> bool:
    local = _local(name)
    namespace = _namespace(name)
    if namespace not in ("", XLINK_NS) or local.lower().startswith("on"):
        return False
    if namespace == XLINK_NS and local != "href":
        return False
    if local not in _ALLOWED_ATTRIBUTES:
        return False
    lowered = value.strip().lower()
    if "javascript:" in lowered or "expression(" in lowered or "@import" in lowered:
        return False
    if local == "href":
        return value.startswith("#") or bool(_SAFE_DATA_IMAGE.match(value.strip()))
    # url(...) anywhere (fill, style, clip-path...) may only point inside the document.
    return all(ref.startswith("#") for ref in _URL_REF.findall(value))


def _svg_length(root: ET.Element, attribute: str, view_box_index: int) -> int | None:
    value = root.attrib.get(attribute)
    if value is not None:
        match = _LENGTH.match(value)
        if match:
            return round(float(match.group(1)))
        if not value.strip().endswith("%"):
            return None
    view_box = root.attrib.get("viewBox", "").replace(",", " ").split()
    if len(view_box) == 4:
        try:
            return round(float(view_box[view_box_index]))
        except ValueError:
            return None
    return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _namespace(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


# --- Preparing a raster logo: orientation, transparency, margins, background -------------
#
# Logos found online are often PNGs with no transparency at all: a white or a baked-in
# grey-and-white checkerboard "transparent" background, and wide blank margins that make
# the mark tiny in the RFQ header. Preparing a logo:
#
# * applies the EXIF orientation and turns every mode into RGB or RGBA (palette PNGs
#   with a tRNS chunk, LA, 16-bit and CMYK images keep their transparency or colours);
# * detects an opaque, near-uniform background from the border pixels: at most three
#   colours (a plain fill, or a checkerboard) covering almost the whole border;
# * trims the blank margins around the mark (always, a few pixels of padding kept);
# * on request, removes that background: a flood fill from the edges over pixels close
#   to the background colours, so enclosed areas of the same colour (the white inside a
#   letter) stay; the edge is feathered by one pixel.
#
# Everything is deterministic Pillow code, no model. The result is a PNG (RGBA when it
# has transparency), never a JPEG, so transparency is never lost.

_PREPARE_MAX_SIDE: Final = 1600
_BG_TOLERANCE: Final = 30
_BG_COVERAGE: Final = 0.92
_BG_MAX_COLOURS: Final = 3
_TRIM_PADDING: Final = 4


@dataclass(frozen=True, slots=True)
class PreparedLogo:
    data: bytes
    content_type: LogoType
    width: int
    height: int
    background_removable: bool
    background_removed: bool


def prepare_raster_logo(
    data: bytes, content_type: LogoType, *, remove_background: bool = False
) -> PreparedLogo | None:
    """The logo as stored for display and documents; None for an SVG (kept as a vector).

    Raises `InvalidLogo` when the image cannot be read.
    """
    if content_type == "image/svg+xml":
        return None
    image = _open_normalized(data)
    background = _background_colours(image)
    removed = False
    flooded = _flood_background(image, background) if background is not None else None
    if flooded is not None and _content_box(flooded) is None:
        background = flooded = None  # all background, no mark: nothing to separate
    if background is not None and flooded is not None:
        if remove_background:
            image = _without(image, flooded)
            removed = True
        image = _trim(image, content_box=_content_box(flooded))
    elif _has_alpha(image):
        image = _trim(image, content_box=_apply(image.getchannel("A"), _over(8)).getbbox())
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return PreparedLogo(
        data=buffer.getvalue(),
        content_type="image/png",
        width=image.width,
        height=image.height,
        background_removable=background is not None,
        background_removed=removed,
    )


def _open_normalized(data: bytes) -> Image.Image:
    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_LOGO_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as opened:
            opened.load()
            image = ImageOps.exif_transpose(opened) or opened.copy()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
        raise InvalidLogo("the image cannot be read") from None
    finally:
        Image.MAX_IMAGE_PIXELS = previous
    image = _to_rgb_or_rgba(image)
    if max(image.size) > _PREPARE_MAX_SIDE:
        image.thumbnail((_PREPARE_MAX_SIDE, _PREPARE_MAX_SIDE), Image.Resampling.LANCZOS)
    return image


def _to_rgb_or_rgba(image: Image.Image) -> Image.Image:
    mode = image.mode
    if mode in ("RGB", "RGBA"):
        return image
    if mode in ("P", "PA", "LA", "La", "RGBa"):
        converted = image.convert("RGBA")
    elif mode in ("I;16", "I;16B", "I;16L", "I;16N", "I"):
        # 16/32-bit greyscale: scale to 8 bits before converting.
        extrema = image.getextrema()
        top = extrema[1] if isinstance(extrema[1], (int, float)) else 255
        scale = 255 / top if top > 255 else 1.0
        converted = image.point(lambda value: value * scale).convert("L").convert("RGB")  # pyright: ignore[reportUnknownLambdaType, reportOperatorIssue, reportUnknownMemberType]
    elif mode == "F":
        converted = image.convert("L").convert("RGB")
    else:  # "1", "L", "CMYK", "YCbCr", "LAB", "HSV"
        converted = image.convert("RGB")
    if converted.mode == "RGBA" and _min_alpha(converted) == 255:
        return converted.convert("RGB")
    return converted


def _has_alpha(image: Image.Image) -> bool:
    return image.mode == "RGBA" and _min_alpha(image) < 250


def _lut(test: Callable[[int], bool]) -> list[int]:
    """A Pillow `point()` table for an L image: 255 where `test` holds, else 0."""
    return [255 if test(value) else 0 for value in range(256)]


def _over(threshold: int) -> list[int]:
    return _lut(lambda value: value > threshold)


_NOT_FLOODED: Final = _lut(lambda value: value != 128)


def _apply(image: Image.Image, table: list[int]) -> Image.Image:
    """`image.point(table)`, typed (Pillow's stub leaves the argument partly unknown)."""
    return image.point(table)  # pyright: ignore[reportUnknownMemberType]


def _min_alpha(image: Image.Image) -> int:
    low = image.getchannel("A").getextrema()[0]
    return int(low) if isinstance(low, (int, float)) else 255


def _border_pixels(image: Image.Image) -> list[tuple[int, ...]]:
    width, height = image.size
    rgba = image.convert("RGBA")
    points = [(x, 0) for x in range(width)] + [(x, height - 1) for x in range(width)]
    inner = range(1, height - 1)
    points += [(0, y) for y in inner] + [(width - 1, y) for y in inner]
    return [cast(tuple[int, ...], rgba.getpixel(point)) for point in points]


def _background_colours(image: Image.Image) -> list[tuple[int, int, int]] | None:
    """The opaque border's few colours (a plain fill or a checkerboard), or None."""
    if image.width < 8 or image.height < 8:
        return None
    border = _border_pixels(image)
    if sum(1 for pixel in border if pixel[3] < 250) > len(border) * 0.05:
        return None  # already transparent at the edges
    buckets: dict[tuple[int, int, int], list[tuple[int, int, int]]] = {}
    for red, green, blue, _alpha in border:
        buckets.setdefault((red // 24, green // 24, blue // 24), []).append((red, green, blue))
    ranked = sorted(buckets.values(), key=len, reverse=True)
    covered = 0
    colours: list[tuple[int, int, int]] = []
    for group in ranked[:_BG_MAX_COLOURS]:
        covered += len(group)
        count = len(group)
        colours.append(
            (
                sum(pixel[0] for pixel in group) // count,
                sum(pixel[1] for pixel in group) // count,
                sum(pixel[2] for pixel in group) // count,
            )
        )
        if covered >= len(border) * _BG_COVERAGE:
            return colours
    return None


def _near_background(image: Image.Image, colours: list[tuple[int, int, int]]) -> Image.Image:
    """An L mask: 255 where the pixel is within the tolerance of a background colour."""
    rgb = image.convert("RGB")
    mask: Image.Image | None = None
    for colour in colours:
        diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, colour))
        red, green, blue = diff.split()
        distance = ImageChops.lighter(ImageChops.lighter(red, green), blue)
        near = _apply(distance, _lut(lambda value: value <= _BG_TOLERANCE))
        mask = near if mask is None else ImageChops.lighter(mask, near)
    assert mask is not None
    return mask


def _flood_background(image: Image.Image, colours: list[tuple[int, int, int]]) -> Image.Image:
    """An L mask: 128 on background pixels connected to the image's edge, else 0 or 255."""
    mask = _near_background(image, colours)
    width, height = mask.size
    edge = [(x, 0) for x in range(width)] + [(x, height - 1) for x in range(width)]
    edge += [(0, y) for y in range(height)] + [(width - 1, y) for y in range(height)]
    for point in edge:
        if mask.getpixel(point) == 255:
            ImageDraw.floodfill(mask, point, 128, thresh=0)
    return mask


def _content_box(flooded: Image.Image) -> tuple[int, int, int, int] | None:
    """Where the mark is: everything but the edge-connected background, ignoring specks
    of a few pixels (JPEG noise, faint watermarks) that would keep the margins wide."""
    content = _apply(flooded, _NOT_FLOODED)
    core = content.filter(ImageFilter.MinFilter(5)).getbbox()  # erodes 2px on every side
    if core is None:
        return content.getbbox()
    left, top, right, bottom = core
    return (
        max(0, left - 2),
        max(0, top - 2),
        min(content.width, right + 2),
        min(content.height, bottom + 2),
    )


def _without(image: Image.Image, flooded: Image.Image) -> Image.Image:
    """The image with the flooded background made transparent, the edge feathered 1px."""
    keep = _apply(flooded, _NOT_FLOODED)
    soft = ImageChops.darker(keep, keep.filter(ImageFilter.GaussianBlur(0.75)))
    rgba = image.convert("RGBA")
    rgba.putalpha(ImageChops.darker(rgba.getchannel("A"), soft))
    return rgba


def _trim(image: Image.Image, *, content_box: tuple[int, int, int, int] | None) -> Image.Image:
    if content_box is None:
        return image
    left, top, right, bottom = content_box
    box = (
        max(0, left - _TRIM_PADDING),
        max(0, top - _TRIM_PADDING),
        min(image.width, right + _TRIM_PADDING),
        min(image.height, bottom + _TRIM_PADDING),
    )
    return image if box == (0, 0, image.width, image.height) else image.crop(box)


# --- For the RFQ document -----------------------------------------------------------------

# The RFQ document carries the logo inside the approved arguments (base64), so it is kept
# small: wide enough to print sharp, never the multi-megabyte original.
MAX_DOCUMENT_LOGO_BYTES: Final = 450_000
_DOCUMENT_LOGO_BOX: Final = (1200, 400)

DocumentLogoType = Literal["image/png", "image/jpeg", "image/svg+xml"]


def logo_for_document(data: bytes, content_type: str) -> tuple[bytes, DocumentLogoType] | None:
    """The logo as the RFQ document embeds it: SVG and small PNG/JPEG unchanged, WebP and
    large images re-encoded (PNG, or JPEG for a photo-like JPEG) to fit the document; None
    when it cannot be made to fit."""
    if content_type == "image/svg+xml":
        return (data, "image/svg+xml") if len(data) <= MAX_DOCUMENT_LOGO_BYTES else None
    try:
        with Image.open(io.BytesIO(data)) as image:
            fits = (
                content_type in ("image/png", "image/jpeg")
                and len(data) <= MAX_DOCUMENT_LOGO_BYTES
                and image.width <= _DOCUMENT_LOGO_BOX[0]
                and image.height <= _DOCUMENT_LOGO_BOX[1]
            )
            if fits:
                return data, "image/png" if content_type == "image/png" else "image/jpeg"
            converted = image.copy()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
        return None
    converted.thumbnail(_DOCUMENT_LOGO_BOX)
    buffer = io.BytesIO()
    if content_type == "image/jpeg":
        converted.convert("RGB").save(buffer, format="JPEG", quality=85, optimize=True)
        media: DocumentLogoType = "image/jpeg"
    else:
        converted = _to_rgb_or_rgba(converted)
        converted.save(buffer, format="PNG", optimize=True)
        media = "image/png"
        if len(buffer.getvalue()) > MAX_DOCUMENT_LOGO_BYTES and converted.mode == "RGB":
            # A photo-like opaque image: JPEG fits where PNG does not, and loses nothing
            # of a transparency it does not have.
            buffer = io.BytesIO()
            converted.save(buffer, format="JPEG", quality=85, optimize=True)
            media = "image/jpeg"
    out = buffer.getvalue()
    return (out, media) if len(out) <= MAX_DOCUMENT_LOGO_BYTES else None
