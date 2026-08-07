from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest

from cf_agent_gateway.adapters.wechat import PollResult
from cf_agent_gateway.config import LoggingSettings, Settings
from cf_agent_gateway.runtime import WechatRuntimeDisabledError, worker


class RecordingSignalRegistry:
    def __init__(self) -> None:
        self.original_handlers: dict[signal.Signals, object] = {
            signal.SIGINT: signal.SIG_DFL,
            signal.SIGTERM: signal.SIG_IGN,
        }
        self.current_handlers = self.original_handlers.copy()
        self.calls: list[tuple[signal.Signals, object]] = []

    def signal(self, shutdown_signal: signal.Signals, handler: object) -> object:
        previous_handler = self.current_handlers[shutdown_signal]
        self.current_handlers[shutdown_signal] = handler
        self.calls.append((shutdown_signal, handler))
        return previous_handler

    def invoke(self, shutdown_signal: signal.Signals) -> None:
        handler = self.current_handlers[shutdown_signal]
        assert callable(handler)
        handler(int(shutdown_signal), None)


def test_main_loads_config_configures_logging_and_starts_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = "custom/runtime.yaml"
    settings = Settings(logging=LoggingSettings(level="DEBUG"))
    loaded_paths: list[str] = []
    configured_levels: list[str] = []
    worker_calls: list[tuple[Settings, Event]] = []

    def load_settings(path: str) -> Settings:
        loaded_paths.append(path)
        return settings

    def run_worker(candidate: Settings, *, stop_event: Event) -> None:
        worker_calls.append((candidate, stop_event))

    monkeypatch.setenv("CF_GATEWAY_CONFIG", config_path)
    monkeypatch.setattr(worker, "load_settings", load_settings)
    monkeypatch.setattr(worker, "configure_logging", configured_levels.append)
    monkeypatch.setattr(worker, "run_worker", run_worker)

    assert worker.main() == 0
    assert loaded_paths == [config_path]
    assert configured_levels == ["DEBUG"]
    assert len(worker_calls) == 1
    assert worker_calls[0][0] is settings
    assert worker_calls[0][1].is_set() is False


def test_main_checks_database_before_starting_worker_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()
    events: list[str] = []

    def check_database(candidate: Settings) -> None:
        assert candidate is settings
        events.append("database")

    def run_worker(candidate: Settings, *, stop_event: Event) -> None:
        assert candidate is settings
        assert not stop_event.is_set()
        events.append("worker")

    monkeypatch.setenv("CF_GATEWAY_STARTUP_MIGRATION_MODE", "check")
    monkeypatch.delenv("CF_GATEWAY_WORKER_HEARTBEAT_PATH", raising=False)
    monkeypatch.setattr(worker, "load_settings", lambda path: settings)
    monkeypatch.setattr(worker, "configure_logging", lambda level: None)
    monkeypatch.setattr(worker, "run_database_startup", check_database)
    monkeypatch.setattr(worker, "run_worker", run_worker)

    assert worker.main() == 0
    assert events == ["database", "worker"]


def test_main_does_not_poll_when_database_startup_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "postgresql://user:secret-password@database/gateway"
    logged_errors: list[tuple[str, dict[str, Any]]] = []

    def fail_database_check(settings: Settings) -> None:
        del settings
        raise RuntimeError(secret)

    def forbidden_worker(settings: Settings, *, stop_event: Event) -> None:
        del settings, stop_event
        raise AssertionError("worker must not start after a database check failure")

    def log_error(message: str, *, extra: dict[str, Any]) -> None:
        logged_errors.append((message, extra))

    monkeypatch.setenv("CF_GATEWAY_STARTUP_MIGRATION_MODE", "check")
    monkeypatch.setattr(worker, "load_settings", lambda path: Settings())
    monkeypatch.setattr(worker, "configure_logging", lambda level: None)
    monkeypatch.setattr(worker, "run_database_startup", fail_database_check)
    monkeypatch.setattr(worker, "run_worker", forbidden_worker)
    monkeypatch.setattr(worker.logger, "error", log_error)

    assert worker.main() == 1
    assert logged_errors == [
        (
            "worker failed",
            {"fields": {"error_code": "database_migration_required"}},
        )
    ]
    assert secret not in str(logged_errors)


@pytest.mark.parametrize("shutdown_signal", [signal.SIGINT, signal.SIGTERM])
def test_main_signal_handler_sets_stop_event_and_restores_previous_handlers(
    monkeypatch: pytest.MonkeyPatch,
    shutdown_signal: signal.Signals,
) -> None:
    registry = RecordingSignalRegistry()
    observed_stop_events: list[Event] = []

    def run_worker(settings: Settings, *, stop_event: Event) -> None:
        del settings
        observed_stop_events.append(stop_event)
        registry.invoke(shutdown_signal)
        assert stop_event.is_set()

    monkeypatch.setattr(worker, "load_settings", lambda path: Settings())
    monkeypatch.setattr(worker, "configure_logging", lambda level: None)
    monkeypatch.setattr(worker.signal, "signal", registry.signal)
    monkeypatch.setattr(worker, "run_worker", run_worker)

    assert worker.main() == 0
    assert len(observed_stop_events) == 1
    assert registry.current_handlers == registry.original_handlers
    assert [registered_signal for registered_signal, _ in registry.calls] == [
        signal.SIGINT,
        signal.SIGTERM,
        signal.SIGINT,
        signal.SIGTERM,
    ]


def test_main_keyboard_interrupt_returns_zero_and_restores_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = RecordingSignalRegistry()
    observed_stop_events: list[Event] = []

    def interrupt_worker(settings: Settings, *, stop_event: Event) -> None:
        del settings
        observed_stop_events.append(stop_event)
        raise KeyboardInterrupt

    monkeypatch.setattr(worker, "load_settings", lambda path: Settings())
    monkeypatch.setattr(worker, "configure_logging", lambda level: None)
    monkeypatch.setattr(worker.signal, "signal", registry.signal)
    monkeypatch.setattr(worker, "run_worker", interrupt_worker)

    assert worker.main() == 0
    assert len(observed_stop_events) == 1
    assert observed_stop_events[0].is_set()
    assert registry.current_handlers == registry.original_handlers


def test_main_keyboard_interrupt_during_configuration_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt(path: str) -> Settings:
        del path
        raise KeyboardInterrupt

    monkeypatch.setattr(worker, "load_settings", interrupt)

    assert worker.main() == 0


@pytest.mark.parametrize(
    ("error", "expected_exit_code"),
    [
        (WechatRuntimeDisabledError(), 2),
        (RuntimeError("sensitive worker failure"), 1),
    ],
)
def test_main_maps_worker_failures_to_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_exit_code: int,
) -> None:
    logged_errors: list[Exception] = []

    def fail(settings: Settings, *, stop_event: Event) -> None:
        del settings, stop_event
        raise error

    monkeypatch.setattr(worker, "load_settings", lambda path: Settings())
    monkeypatch.setattr(worker, "configure_logging", lambda level: None)
    monkeypatch.setattr(worker, "run_worker", fail)
    monkeypatch.setattr(worker, "_log_worker_failure", logged_errors.append)

    assert worker.main() == expected_exit_code
    assert logged_errors == [error]


def test_main_invalid_configuration_uses_fallback_logging_and_returns_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_levels: list[str] = []
    logged_errors: list[tuple[str, dict[str, Any]]] = []

    def fail_to_load(path: str) -> Settings:
        del path
        raise ValueError("sensitive configuration content")

    def log_error(message: str, *, extra: dict[str, Any]) -> None:
        logged_errors.append((message, extra))

    monkeypatch.setattr(worker, "load_settings", fail_to_load)
    monkeypatch.setattr(worker, "configure_logging", configured_levels.append)
    monkeypatch.setattr(worker.logger, "error", log_error)

    assert worker.main() == 1
    assert configured_levels == ["INFO"]
    assert logged_errors == [
        (
            "worker failed",
            {"fields": {"error_code": "runtime_configuration_invalid"}},
        )
    ]


def test_python_module_entrypoint_exits_two_when_runtime_is_disabled(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("wechat:\n  enabled: false\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["CF_GATEWAY_CONFIG"] = str(config_path)
    source_path = str(Path(__file__).resolve().parents[1] / "src")
    existing_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        os.pathsep.join((source_path, existing_python_path))
        if existing_python_path
        else source_path
    )

    completed = subprocess.run(
        [sys.executable, "-m", "cf_agent_gateway.runtime.worker"],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "RuntimeWarning" not in completed.stderr
    payloads = [json.loads(line) for line in completed.stderr.splitlines()]
    assert [payload["message"] for payload in payloads] == [
        "worker started",
        "poll cycle started",
        "worker stopped",
        "worker failed",
    ]
    assert payloads[-1]["error_code"] == "wechat_runtime_disabled"


def test_main_sigterm_waits_for_the_in_flight_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = RecordingSignalRegistry()
    settings = Settings()
    poll_started = Event()
    release_poll = Event()
    poll_finished = Event()
    observations: list[tuple[str, bool]] = []

    def blocked_poll(candidate: Settings) -> PollResult:
        assert candidate is settings
        poll_started.set()
        release_poll.wait(timeout=2)
        poll_finished.set()
        return PollResult(logged_in=True)

    def send_sigterm() -> None:
        started = poll_started.wait(timeout=2)
        observations.append(("poll_started", started))
        if started:
            registry.invoke(signal.SIGTERM)
            observations.append(("finished_after_signal", poll_finished.is_set()))
        release_poll.set()

    monkeypatch.setattr(worker, "load_settings", lambda path: settings)
    monkeypatch.setattr(worker, "configure_logging", lambda level: None)
    monkeypatch.setattr(worker.signal, "signal", registry.signal)
    monkeypatch.setattr(worker, "run_wechat_poll_once", blocked_poll)
    monkeypatch.setattr(
        worker,
        "create_worker_heartbeat_from_environment",
        lambda **kwargs: None,
    )

    signal_thread = Thread(target=send_sigterm, daemon=True)
    signal_thread.start()
    try:
        assert worker.main() == 0
    finally:
        release_poll.set()
        signal_thread.join(timeout=2)

    assert observations == [
        ("poll_started", True),
        ("finished_after_signal", False),
    ]
    assert poll_finished.is_set()
    assert registry.current_handlers == registry.original_handlers
