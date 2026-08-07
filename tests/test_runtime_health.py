from __future__ import annotations

import logging
import time
from collections.abc import Callable
from threading import Event, Lock

import pytest

from cf_agent_gateway.runtime.health import DatabaseReadinessMonitor


class MutableClock:
    def __init__(self) -> None:
        self._value = 0.0
        self._lock = Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._value

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._value += seconds


def _wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_database_readiness_monitor_redacts_probe_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "postgresql://user:secret-password@database/gateway"
    probe_called = Event()

    def fail_probe() -> bool:
        probe_called.set()
        raise RuntimeError(secret)

    monitor = DatabaseReadinessMonitor(
        object(),  # type: ignore[arg-type]
        interval_seconds=0.01,
        max_age_seconds=0.2,
        stop_timeout_seconds=0.1,
        probe=fail_probe,
    )
    try:
        with caplog.at_level(logging.WARNING):
            monitor.start()
            assert probe_called.wait(timeout=1)
            assert _wait_until(lambda: not monitor.is_ready())
    finally:
        monitor.stop()

    assert secret not in caplog.text
    failure = next(
        record
        for record in caplog.records
        if record.name == "cf_agent_gateway.runtime.health"
        and record.getMessage() == "database readiness probe failed"
    )
    assert failure.fields == {"error_code": "database_unavailable"}  # type: ignore[attr-defined]


def test_hung_database_probe_becomes_stale_without_blocking_shutdown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = MutableClock()
    probe_started = Event()
    release_probe = Event()

    def hang_probe() -> bool:
        probe_started.set()
        release_probe.wait(timeout=2)
        return True

    monitor = DatabaseReadinessMonitor(
        object(),  # type: ignore[arg-type]
        interval_seconds=0.01,
        max_age_seconds=0.05,
        stop_timeout_seconds=0.01,
        clock=clock,
        probe=hang_probe,
    )
    try:
        monitor.start()
        assert probe_started.wait(timeout=1)

        clock.advance(0.06)
        check_started = time.monotonic()
        assert not monitor.is_ready()
        assert time.monotonic() - check_started < 0.05

        with caplog.at_level(logging.WARNING):
            stop_started = time.monotonic()
            monitor.stop()
            assert time.monotonic() - stop_started < 0.2
    finally:
        release_probe.set()
        monitor.stop()

    assert any(
        record.getMessage() == "database readiness monitor did not stop"
        and record.fields == {"error_code": "database_probe_stuck"}  # type: ignore[attr-defined]
        for record in caplog.records
    )
