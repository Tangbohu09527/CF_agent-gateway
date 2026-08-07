from __future__ import annotations

import json
import logging
from collections.abc import Iterator

import pytest

from cf_agent_gateway import main as gateway_main


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


def test_gateway_configuration_failure_is_structured_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    preserve_logging_state: None,
) -> None:
    del preserve_logging_state
    secret = "postgresql://user:secret-password@database/gateway"

    def fail_config(path: str) -> None:
        del path
        raise RuntimeError(secret)

    monkeypatch.setattr(gateway_main, "load_settings", fail_config)

    assert gateway_main.main() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert secret not in captured.err
    payload = json.loads(captured.err)
    assert payload["level"] == "ERROR"
    assert payload["message"] == "gateway failed"
    assert payload["error_code"] == "runtime_configuration_invalid"
