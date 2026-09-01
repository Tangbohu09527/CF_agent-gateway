from __future__ import annotations

import json
import logging
import os
import re
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
_REDACTED = "[REDACTED]"
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "database_url",
    "connection_string",
)
_RAW_IDENTIFIER_KEY_PARTS = (
    "account_id",
    "chat_id",
    "conversation_id",
)
_QUIET_THIRD_PARTY_LOGGERS = (
    "httpx",
    "httpcore",
    "alembic",
    "alembic.runtime.migration",
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_CREDENTIAL_URL_PATTERN = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@")
_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(authorization|(?:[a-z0-9_-]+[_-])?(?:body|content)|"
    r"cookie|password|secret|token|api[_-]?key|"
    r"account[_-]?id|chat[_-]?id|conversation[_-]?id)(\s*[:=]\s*)[^,;]+"
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
            "message": _redact_string(record.getMessage()),
            "service": self._service,
            "process_id": record.process,
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(
                (key, _redact_value(value, key=key))
                for key, value in fields.items()
                if isinstance(key, str) and key not in _RESERVED_FIELDS
            )
        if record.exc_info:
            exception_type = record.exc_info[0]
            payload["exception"] = {
                "type": exception_type.__name__ if exception_type is not None else "Exception",
                "stacktrace": _redact_string(self.formatException(record.exc_info)),
            }
        return json.dumps(payload, default=_json_default, ensure_ascii=True)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def _redact_value(value: object, *, key: str | None = None) -> object:
    if key is not None and _sensitive_key(key):
        return _REDACTED
    if isinstance(value, dict):
        return {
            str(item_key): _redact_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    if normalized in {"body", "content"} or normalized.endswith(("_body", "_content")):
        return True
    if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
        return True
    if normalized.endswith("_ref"):
        return False
    return any(part in normalized for part in _RAW_IDENTIFIER_KEY_PARTS)


def _redact_string(value: str) -> str:
    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    redacted = _CREDENTIAL_URL_PATTERN.sub(r"\1[REDACTED]@", redacted)
    return _ASSIGNMENT_PATTERN.sub(r"\1\2[REDACTED]", redacted)


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

    for logger_name in _QUIET_THIRD_PARTY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
