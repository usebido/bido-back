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
from dataclasses import dataclass
from typing import Final, Literal

from PIL import Image, UnidentifiedImageError

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
