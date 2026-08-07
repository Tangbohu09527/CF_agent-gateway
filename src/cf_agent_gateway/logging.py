from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

_RESERVED_FIELDS = frozenset(
    {
        "timestamp",
        "level",
        "logger",
        "message",
        "service",
        "process_id",
        "exception",
    }
)


class JsonFormatter(logging.Formatter):
    def __init__(self, *, service: str | None = None) -> None:
        super().__init__()
        self._service = service or os.getenv("CF_GATEWAY_SERVICE", "cf-agent-gateway")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self._service,
            "process_id": record.process,
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(
                (key, value)
                for key, value in fields.items()
                if isinstance(key, str) and key not in _RESERVED_FIELDS
            )
        if record.exc_info:
            exception_type = record.exc_info[0]
            payload["exception"] = {
                "type": exception_type.__name__ if exception_type is not None else "Exception",
                "stacktrace": self.formatException(record.exc_info),
            }
        return json.dumps(payload, default=_json_default, ensure_ascii=True)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(logger_name).handlers.clear()
        logging.getLogger(logger_name).propagate = True
