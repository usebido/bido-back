"""Structured logs: context fields, safe messages, and no customer content."""

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from bido_back.logs import (
    JsonFormatter,
    annotate,
    bind,
    bound,
    exception_message,
    format_exception,
    safe_reason,
)

# Stands in for anything a customer sent: a message, a sheet name, an email.
CONTENT = "customer-content-7f3a"


class JsonLines(logging.Handler):
    """Keeps every record as the JSON object CloudWatch would get."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(JsonFormatter(service="api", release="abc123"))
        self.lines: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(self.format(record)))


@pytest.fixture
def lines() -> Iterator[list[dict[str, Any]]]:
    handler = JsonLines()
    logger = logging.getLogger("tests.logs")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    yield handler.lines
    logger.removeHandler(handler)


def test_safe_reason_masks_quoted_values_addresses_and_sql_details() -> None:
    assert safe_reason(f"no RFQ {CONTENT!r}; use list_rfqs") == "no RFQ '…'; use list_rfqs"
    assert safe_reason(f'sheet "{CONTENT}" has no header') == "sheet '…' has no header"
    assert safe_reason(f"bounced: {CONTENT}@acme.example") == "bounced: <email>"
    assert safe_reason(f"Key (email)=({CONTENT}) already exists") == (
        "Key (email)=(…) already exists"
    )
    assert safe_reason("that id is not one of this message's attachments") == (
        "that id is not one of this message's attachments"
    )
    assert len(safe_reason("x" * 1000)) == 300


def test_validation_errors_keep_locations_not_inputs() -> None:
    class Line(BaseModel):
        quantity: int

    with pytest.raises(ValidationError) as caught:
        Line.model_validate({"quantity": CONTENT})
    message = exception_message(caught.value)
    assert CONTENT not in message
    assert message.startswith("quantity: ")


def test_tracebacks_have_frames_and_safe_messages_but_no_locals() -> None:
    def fail(secret: str) -> None:
        raise KeyError(secret)

    with pytest.raises(RuntimeError) as caught:
        try:
            fail(CONTENT)
        except KeyError as exc:
            raise RuntimeError("lookup failed") from exc
    text = format_exception(caught.value)
    assert CONTENT not in text
    assert "in fail" in text
    assert "KeyError: '…'" in text
    assert "RuntimeError: lookup failed" in text


def test_lines_carry_bound_fields_extras_and_release(lines: list[dict[str, Any]]) -> None:
    logger = logging.getLogger("tests.logs")
    with bind(request_id="req-1"):
        annotate(tenant_id="t-1")
        with bind(job="mailbox.sync"):
            assert bound("tenant_id") == "t-1"
            logger.info("tool call", extra={"tool": "list_rfqs", "duration_ms": 12.5})
        logger.warning("after the job")
    logger.info("outside")

    first, second, third = lines
    assert first["msg"] == "tool call"
    assert first["level"] == "INFO"
    assert first["logger"] == "tests.logs"
    assert first["service"] == "api"
    assert first["release"] == "abc123"
    assert (first["request_id"], first["tenant_id"], first["job"]) == (
        "req-1",
        "t-1",
        "mailbox.sync",
    )
    assert (first["tool"], first["duration_ms"]) == ("list_rfqs", 12.5)
    assert "job" not in second and second["tenant_id"] == "t-1"
    assert "request_id" not in third


def test_logged_exceptions_are_safe(lines: list[dict[str, Any]]) -> None:
    logger = logging.getLogger("tests.logs")
    try:
        raise ValueError(f"bad sheet {CONTENT!r}")
    except ValueError:
        logger.exception("import failed")
    (line,) = lines
    assert line["exc_type"] == "ValueError"
    assert "ValueError: bad sheet '…'" in line["exc"]
    assert CONTENT not in json.dumps(line)
