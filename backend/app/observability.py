"""Structured logging and request correlation.

Every log line is a single JSON object so that model, retrieval, database and
artifact failures can be filtered and correlated without a log platform
(assignment section 5, "Observability"). A request_id is generated per HTTP
request and attached to every line emitted while handling it.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
session_id_var: ContextVar[str | None] = ContextVar("session_id", default=None)

# Keys that must never reach the logs, whatever a caller passes in.
_REDACT_KEYS = {
    "api_key",
    "anthropic_api_key",
    "openai_api_key",
    "authorization",
    "password",
    "database_url",
    "token",
    "secret",
}
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "asctime",
    "message",
    "taskName",
}


def _redact(key: str, value: Any) -> Any:
    if any(marker in key.lower() for marker in _REDACT_KEYS):
        return "***redacted***"
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        if rid := request_id_var.get():
            payload["request_id"] = rid
        if sid := session_id_var.get():
            payload["session_id"] = sid
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = _redact(key, value)
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-readable variant for local debugging (LOG_FORMAT=console)."""

    def format(self, record: logging.LogRecord) -> str:
        extras = {
            k: _redact(k, v)
            for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        tail = " ".join(f"{k}={v}" for k, v in extras.items())
        base = f"{record.levelname:<5} {record.name:<28} {record.getMessage()}"
        return f"{base}  {tail}".rstrip()


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # uvicorn duplicates access logs in its own format; route them through ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    logging.getLogger("httpx").setLevel("WARNING")


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


@contextmanager
def timed(logger: logging.Logger, event: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Log `event` with a latency_ms field, and `<event>_failed` on error.

    The yielded dict can be mutated to add fields discovered during the block.
    """
    started = time.perf_counter()
    extra: dict[str, Any] = dict(fields)
    try:
        yield extra
    except Exception as exc:
        extra["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        extra["error_type"] = type(exc).__name__
        logger.warning(f"{event}_failed", extra=extra)
        raise
    else:
        extra["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        logger.info(event, extra=extra)
