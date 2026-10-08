"""Application logs: one JSON object per line on stdout, with request context.

CloudWatch Logs Insights reads the JSON fields directly (README, "Debugging in
AWS"). Every line carries `ts`, `level`, `logger`, `msg`, `service`, the
release, the fields bound to the current request or job (`request_id`,
`tenant_id`, `user_id`, `conversation_id`, `job`...), the OpenTelemetry
`trace_id` / `span_id` when tracing is on, and whatever the call passed in
`extra=`.

What may be logged follows `observability.py`: identifiers, counts, durations,
statuses, error classes and code-written reasons. Never customer content:
messages, file contents, sheet or file names, emails, supplier names, prices.
Free text that might carry content (exception messages, tool errors) goes
through `safe_reason`, which masks quoted values and email addresses, and
tracebacks are formatted here without local variables.

Call `configure_logging` once per process, at startup (the api's `create_app`,
the worker's `main`). Repeated calls are ignored.
"""

import json
import logging
import re
import sys
import traceback
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Final, Literal, cast

from pydantic import ValidationError

from bido_back.settings import Settings

Service = Literal["api", "worker"]

# --- Context -----------------------------------------------------------------

_fields: ContextVar[dict[str, str] | None] = ContextVar("bido_log_fields", default=None)


@contextmanager
def bind(**fields: object) -> Generator[None]:
    """Add `fields` to every log line written inside the block (None values are skipped).

    The bound mapping is shared with tasks started inside the block, so
    `annotate` from anywhere in the same request or job reaches all of them.
    """
    parent = _fields.get() or {}
    token = _fields.set({**parent, **_strings(fields)})
    try:
        yield
    finally:
        _fields.reset(token)


def annotate(**fields: object) -> None:
    """Add `fields` to the innermost `bind` block, for the rest of it. No-op outside one.

    For values learned partway through, such as the tenant after authentication
    or the conversation once a Copilot turn starts.
    """
    current = _fields.get()
    if current is not None:
        current.update(_strings(fields))


def bound(name: str) -> str | None:
    """A field bound to the current context, e.g. `bound("request_id")`."""
    return (_fields.get() or {}).get(name)


def _strings(fields: dict[str, object]) -> dict[str, str]:
    return {key: str(value) for key, value in fields.items() if value is not None}


# --- Redaction -----------------------------------------------------------------

_QUOTED: Final = re.compile(r"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"")
_EMAIL: Final = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Postgres error details echo the conflicting values: "Key (email)=(a@b.c)".
_SQL_VALUES: Final = re.compile(r"\)=\([^)]*\)")
REASON_CHARS: Final = 300


def safe_reason(text: str, limit: int = REASON_CHARS) -> str:
    """A code-written message with anything that may be a value masked.

    Messages here are written by our code, but some interpolate a value with
    `!r` (a sheet name, an RFQ reference the model typed) or an address. Those
    become '…' / <email>; the shape of the message, which is what explains the
    error, stays.
    """
    masked = _QUOTED.sub("'…'", text)
    masked = _EMAIL.sub("<email>", masked)
    masked = _SQL_VALUES.sub(")=(…)", masked)
    masked = " ".join(masked.split())
    return masked if len(masked) <= limit else masked[: limit - 1] + "…"


def exception_message(exc: BaseException) -> str:
    """An exception's message, safe to log.

    Validation errors are reduced to field locations and messages (pydantic
    otherwise includes the input values); anything else goes through
    `safe_reason`.
    """
    if isinstance(exc, ValidationError):
        return safe_reason(
            "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
                for err in exc.errors(include_input=False, include_url=False)
            )
        )
    if type(exc).__name__ == "RequestValidationError":
        # FastAPI's message lists the input values; the field paths are logged
        # separately by the api's validation handler.
        return "request validation failed"
    return safe_reason(str(exc))


def format_exception(exc: BaseException) -> str:
    """A traceback without local variables and with safe messages, causes included."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    parts: list[str] = []
    for item in reversed(chain):
        if parts:
            parts.append("\nThe above exception led to:\n")
        parts.append("Traceback (most recent call last):\n")
        parts.extend(traceback.format_tb(item.__traceback__))
        parts.append(f"{_qualified(item)}: {exception_message(item)}")
    return "".join(parts)


def _qualified(exc: BaseException) -> str:
    cls = type(exc)
    module = cls.__module__
    return cls.__qualname__ if module == "builtins" else f"{module}.{cls.__qualname__}"


# --- Formatting --------------------------------------------------------------------

# Attributes every LogRecord has; anything else on a record came from `extra=`.
_RECORD_ATTRS: Final = frozenset(
    {
        *vars(logging.LogRecord("", 0, "", 0, "", (), None)),
        "message",
        "asctime",
        "taskName",
        # uvicorn's ANSI-colored copy of the message.
        "color_message",
    }
)


class JsonFormatter(logging.Formatter):
    def __init__(self, *, service: Service, release: str | None) -> None:
        super().__init__()
        self._service = service
        self._release = release

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "service": self._service,
        }
        if self._release:
            payload["release"] = self._release
        payload.update(_fields.get() or {})
        payload.update(_trace_ids())
        for key, value in vars(record).items():
            if key not in _RECORD_ATTRS and not key.startswith("_"):
                payload[key] = _jsonable(value)
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            payload["exc_type"] = _qualified(exc)
            payload["exc"] = format_exception(exc)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """For a terminal: the usual line, then the bound and extra fields."""

    def format(self, record: logging.LogRecord) -> str:
        fields = {**(_fields.get() or {})}
        for key, value in vars(record).items():
            if key not in _RECORD_ATTRS and not key.startswith("_"):
                fields[key] = str(_jsonable(value))
        line = f"{self.formatTime(record)} {record.levelname} {record.name} {record.getMessage()}"
        if fields:
            line += " " + " ".join(f"{k}={v}" for k, v in fields.items())
        if record.exc_info and record.exc_info[1] is not None:
            line += "\n" + format_exception(record.exc_info[1])
        return line


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in cast(Sequence[object], value)]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in cast(Mapping[object, object], value).items()}
    return str(value)


def _trace_ids() -> dict[str, str]:
    from opentelemetry import trace

    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return {}
    return {"trace_id": f"{context.trace_id:032x}", "span_id": f"{context.span_id:016x}"}


# --- Setup ------------------------------------------------------------------------

# Third-party loggers that are noisy at INFO, or log request content at DEBUG.
# They are held at these levels whatever LOG_LEVEL says.
_QUIET: Final = {
    "botocore": logging.WARNING,
    "boto3": logging.WARNING,
    "s3transfer": logging.WARNING,
    "urllib3": logging.WARNING,
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "asyncio": logging.WARNING,
    "sqlalchemy.engine": logging.WARNING,
    "multipart": logging.WARNING,
    "python_multipart": logging.WARNING,
    # INFO keeps its "Retrying request to /responses in 0.4 seconds" lines;
    # its DEBUG lines include request bodies.
    "openai": logging.INFO,
    # Agno logs model output in some warnings; errors only (see observability).
    "agno": logging.ERROR,
    "agno-team": logging.ERROR,
    "agno-workflow": logging.ERROR,
}

_configured = False


def configure_logging(settings: Settings, *, service: Service) -> None:
    """Send every log record to stdout, formatted for CloudWatch. Idempotent."""
    global _configured
    if _configured:
        return
    _configured = True

    text = settings.log_format == "text" or (
        settings.log_format is None and settings.environment == "local"
    )
    release = settings.git_sha[:12] if settings.git_sha else None
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        TextFormatter() if text else JsonFormatter(service=service, release=release)
    )
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    for name, level in _QUIET.items():
        logging.getLogger(name).setLevel(
            max(level, logging.getLevelNamesMapping()[settings.log_level])
        )

    # uvicorn configured its own handlers before the app was created. Route its
    # loggers through ours; its access log is replaced by `api/request_context`,
    # which logs the route template instead of the raw path and query string.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
