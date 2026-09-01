from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from cf_agent_gateway.logging import JsonFormatter, configure_logging


def make_record(
    message: str = "gateway started",
    *,
    level: int = logging.INFO,
    exc_info: tuple[type[BaseException], BaseException, Any] | None = None,
) -> logging.LogRecord:
    return logging.LogRecord(
        name="cf_agent_gateway.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=exc_info,
    )


@pytest.fixture
def preserve_logging_state() -> Iterator[None]:
    root_logger = logging.getLogger()
    root_handlers = root_logger.handlers.copy()
    root_level = root_logger.level
    named_loggers = [
        logging.getLogger(name) for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
    ]
    named_state = [
        (logger, logger.handlers.copy(), logger.propagate, logger.level) for logger in named_loggers
    ]
    try:
        yield
    finally:
        root_logger.handlers[:] = root_handlers
        root_logger.setLevel(root_level)
        for logger, handlers, propagate, level in named_state:
            logger.handlers[:] = handlers
            logger.propagate = propagate
            logger.setLevel(level)


def test_json_formatter_emits_core_fields_and_structured_fields() -> None:
    record = make_record()
    record.fields = {"event": "startup", "port": 8080}  # type: ignore[attr-defined]

    payload = json.loads(JsonFormatter(service="gateway-api").format(record))

    timestamp = datetime.fromisoformat(payload.pop("timestamp"))
    assert timestamp.utcoffset() == timedelta(0)
    assert payload == {
        "level": "INFO",
        "logger": "cf_agent_gateway.test",
        "message": "gateway started",
        "service": "gateway-api",
        "process_id": record.process,
        "event": "startup",
        "port": 8080,
    }


def test_json_formatter_does_not_allow_fields_to_override_reserved_values() -> None:
    record = make_record()
    record.fields = {  # type: ignore[attr-defined]
        "timestamp": "forged timestamp",
        "level": "CRITICAL",
        "logger": "forged.logger",
        "message": "forged message",
        "service": "forged-service",
        "process_id": -1,
        "exception": "forged exception",
        "safe_field": "preserved",
    }

    payload = json.loads(JsonFormatter(service="gateway-api").format(record))

    assert payload["timestamp"] != "forged timestamp"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "cf_agent_gateway.test"
    assert payload["message"] == "gateway started"
    assert payload["service"] == "gateway-api"
    assert payload["process_id"] == record.process
    assert "exception" not in payload
    assert payload["safe_field"] == "preserved"


def test_json_formatter_serializes_non_json_field_values() -> None:
    class Marker:
        def __str__(self) -> str:
            return "marker-value"

    local_time = datetime(2026, 8, 7, 17, 30, tzinfo=timezone(timedelta(hours=8)))
    record = make_record()
    record.fields = {"observed_at": local_time, "marker": Marker()}  # type: ignore[attr-defined]

    payload = json.loads(JsonFormatter().format(record))

    assert payload["observed_at"] == "2026-08-07T09:30:00+00:00"
    assert payload["marker"] == "marker-value"


def test_json_formatter_emits_a_structured_exception() -> None:
    try:
        raise ValueError("controlled logging failure")
    except ValueError:
        record = make_record("request failed", level=logging.ERROR, exc_info=sys.exc_info())

    payload = json.loads(JsonFormatter().format(record))

    assert payload["exception"]["type"] == "ValueError"
    assert "ValueError: controlled logging failure" in payload["exception"]["stacktrace"]


def test_configure_logging_is_idempotent(
    preserve_logging_state: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del preserve_logging_state
    root_logger = logging.getLogger()

    configure_logging("DEBUG")
    first_handler = root_logger.handlers[0]
    configure_logging("INFO")

    assert len(root_logger.handlers) == 1
    assert root_logger.handlers[0] is not first_handler
    assert isinstance(root_logger.handlers[0].formatter, JsonFormatter)
    assert root_logger.level == logging.INFO
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        configured_logger = logging.getLogger(logger_name)
        assert configured_logger.handlers == []
        assert configured_logger.propagate is True

    logging.getLogger("cf_agent_gateway.idempotency_test").info(
        "configured once",
        extra={"fields": {"attempt": 2}},
    )
    captured = capsys.readouterr()
    lines = captured.err.splitlines()
    assert captured.out == ""
    assert len(lines) == 1
    assert json.loads(lines[0])["attempt"] == 2
