from __future__ import annotations

import logging
import math
from collections.abc import Callable
from threading import Event, Lock, Thread
from time import monotonic

from sqlalchemy import Engine, text

logger = logging.getLogger(__name__)

Clock = Callable[[], float]
Probe = Callable[[], bool]

DEFAULT_DATABASE_PROBE_INTERVAL_SECONDS = 5.0
DEFAULT_DATABASE_PROBE_MAX_AGE_SECONDS = 15.0
DEFAULT_DATABASE_PROBE_STOP_TIMEOUT_SECONDS = 1.0


class DatabaseReadinessMonitor:
    """Cache database health without blocking HTTP readiness requests."""

    def __init__(
        self,
        engine: Engine,
        *,
        interval_seconds: float = DEFAULT_DATABASE_PROBE_INTERVAL_SECONDS,
        max_age_seconds: float = DEFAULT_DATABASE_PROBE_MAX_AGE_SECONDS,
        stop_timeout_seconds: float = DEFAULT_DATABASE_PROBE_STOP_TIMEOUT_SECONDS,
        clock: Clock = monotonic,
        probe: Probe | None = None,
    ) -> None:
        self._interval_seconds = _positive_seconds(interval_seconds, "interval_seconds")
        self._max_age_seconds = _positive_seconds(max_age_seconds, "max_age_seconds")
        self._stop_timeout_seconds = _positive_seconds(
            stop_timeout_seconds,
            "stop_timeout_seconds",
        )
        if self._max_age_seconds <= self._interval_seconds:
            raise ValueError("max_age_seconds must be greater than interval_seconds")

        self._clock = clock
        self._probe = probe if probe is not None else lambda: _probe_database(engine)
        self._stop_event = Event()
        self._state_lock = Lock()
        self._last_success = clock()
        self._last_probe_succeeded = True
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("database readiness monitor is already started")
        self._thread = Thread(
            target=self._run,
            name="database-readiness",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=self._stop_timeout_seconds)
        if thread.is_alive():
            logger.warning(
                "database readiness monitor did not stop",
                extra={"fields": {"error_code": "database_probe_stuck"}},
            )

    def is_ready(self) -> bool:
        now = self._clock()
        with self._state_lock:
            last_success = self._last_success
            last_probe_succeeded = self._last_probe_succeeded
        return last_probe_succeeded and now - last_success <= self._max_age_seconds

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            try:
                succeeded = self._probe()
            except Exception:
                succeeded = False

            now = self._clock()
            with self._state_lock:
                previous_succeeded = self._last_probe_succeeded
                self._last_probe_succeeded = succeeded
                if succeeded:
                    self._last_success = now

            if not succeeded and previous_succeeded:
                logger.warning(
                    "database readiness probe failed",
                    extra={"fields": {"error_code": "database_unavailable"}},
                )
            elif succeeded and not previous_succeeded:
                logger.info("database readiness probe recovered")


def _probe_database(engine: Engine) -> bool:
    with engine.connect() as connection:
        return connection.scalar(text("SELECT 1")) == 1


def _positive_seconds(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive number")
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a positive number") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(f"{name} must be a positive number")
    return seconds
