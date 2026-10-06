"""Attachments (PDF, Excel, CSV, text) -> model-ready text with locator markers."""

from bido_back.documents.extract import (
    DEFAULT_LIMITS,
    DocumentKind,
    DocumentText,
    Limits,
    extract_text,
    marker_name,
    render_documents,
)

__all__ = [
    "DEFAULT_LIMITS",
    "DocumentKind",
    "DocumentText",
    "Limits",
    "extract_text",
    "marker_name",
    "render_documents",
]
