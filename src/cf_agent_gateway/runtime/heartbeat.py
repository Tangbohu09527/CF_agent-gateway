from __future__ import annotations

import argparse
import json
import math
import os
import socket
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from threading import TIMEOUT_MAX, Event, Lock, Thread
from typing import Any, Literal

HeartbeatState = Literal["starting", "running", "stopping", "stopped", "failed"]
Clock = Callable[[], datetime]

HEARTBEAT_PATH_ENV = "CF_GATEWAY_WORKER_HEARTBEAT_PATH"
HEARTBEAT_INTERVAL_ENV = "CF_GATEWAY_WORKER_HEARTBEAT_INTERVAL_SECONDS"
HEARTBEAT_MAX_AGE_ENV = "CF_GATEWAY_WORKER_HEARTBEAT_MAX_AGE_SECONDS"
HEARTBEAT_SCHEMA_VERSION = 1
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 10.0
DEFAULT_HEARTBEAT_MAX_AGE_SECONDS = 30.0

_HEALTHY_STATES = frozenset({"starting", "running"})
_ALL_STATES = _HEALTHY_STATES | {"stopping", "stopped", "failed"}
_MAX_FUTURE_SKEW_SECONDS = 5.0


class HeartbeatError(RuntimeError):
    """The worker heartbeat cannot be written or is not healthy."""


class FileHeartbeat:
    def __init__(
        self,
        path: str | Path,
        *,
        clock: Clock | None = None,
        process_id: int | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.path = Path(path)
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._process_id = os.getpid() if process_id is None else process_id
        self._worker_id = worker_id or os.getenv("CF_GATEWAY_WORKER_ID") or socket.gethostname()
        self._write_lock = Lock()

    def write(
        self,
        state: HeartbeatState,
        *,
        details: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if state not in _ALL_STATES:
            raise ValueError(f"unsupported heartbeat state: {state}")

        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("heartbeat clock must return a timezone-aware datetime")

        payload: dict[str, object] = {
            "schema_version": HEARTBEAT_SCHEMA_VERSION,
            "worker_id": self._worker_id,
            "pid": self._process_id,
            "state": state,
            "updated_at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
        if details:
            payload["details"] = dict(details)

        with self._write_lock:
            self._atomic_write(payload)
        return payload

    def _atomic_write(self, payload: Mapping[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            text=True,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as temporary_file:
                json.dump(payload, temporary_file, ensure_ascii=True, sort_keys=True)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, self.path)
        except BaseException:
            with suppress(OSError):
                os.unlink(temporary_name)
            raise


class HeartbeatPublisher:
    def __init__(
        self,
        heartbeat: FileHeartbeat,
        *,
        interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        error_handler: Callable[[], None] | None = None,
    ) -> None:
        self._heartbeat = heartbeat
        self._interval_seconds = _positive_seconds(interval_seconds, HEARTBEAT_INTERVAL_ENV)
        self._error_handler = error_handler
        self._state_lock = Lock()
        self._state: HeartbeatState = "starting"
        self._details: dict[str, object] = {"phase": "startup"}
        self._started = False
        self._write_failure_reported = False

    def start(self) -> None:
        if self._started:
            raise RuntimeError("heartbeat publisher is already started")
        self._started = True
        self._publish()

    def update(self, state: HeartbeatState, **details: object) -> None:
        with self._state_lock:
            self._state = state
            self._details = dict(details)
        self._publish()

    def stop(self, state: Literal["stopped", "failed"] = "stopped") -> None:
        self.update(state, phase="shutdown")

    def wait(self, stop_event: Event, timeout_seconds: float) -> bool:
        """Wait in bounded slices and publish at each configured interval."""

        remaining = _positive_seconds(timeout_seconds, "timeout_seconds")
        while remaining > 0:
            wait_seconds = min(self._interval_seconds, remaining)
            if stop_event.wait(wait_seconds):
                return True
            remaining = max(0.0, remaining - wait_seconds)
            if remaining > 0:
                self._publish()
        return False

    def _publish(self) -> None:
        with self._state_lock:
            state = self._state
            details = self._details.copy()
        try:
            self._heartbeat.write(state, details=details)
            self._write_failure_reported = False
        except Exception:
            if not self._write_failure_reported and self._error_handler is not None:
                self._error_handler()
            self._write_failure_reported = True


def create_worker_heartbeat_from_environment(
    *,
    error_handler: Callable[[], None] | None = None,
) -> HeartbeatPublisher | None:
    path = os.getenv(HEARTBEAT_PATH_ENV)
    if path is None or not path.strip():
        return None
    interval = _positive_seconds(
        os.getenv(HEARTBEAT_INTERVAL_ENV, str(DEFAULT_HEARTBEAT_INTERVAL_SECONDS)),
        HEARTBEAT_INTERVAL_ENV,
    )
    return HeartbeatPublisher(
        FileHeartbeat(path.strip()),
        interval_seconds=interval,
        error_handler=error_handler,
    )


@contextmanager
def resident_heartbeat(
    heartbeat: HeartbeatPublisher | None,
    **details: object,
) -> Iterator[None]:
    """Publish process liveness while a resident worker owns its blocking loop."""

    monitor_stop = Event()
    monitor_thread: Thread | None = None
    final_state: Literal["stopped", "failed"] = "stopped"
    try:
        if heartbeat is not None:
            heartbeat.start()
            heartbeat.update("running", **details)
            monitor_thread = Thread(
                target=heartbeat.wait,
                args=(monitor_stop, TIMEOUT_MAX),
                name="resident-worker-heartbeat",
                daemon=True,
            )
            monitor_thread.start()
        yield
    except BaseException:
        final_state = "failed"
        raise
    finally:
        monitor_stop.set()
        if monitor_thread is not None:
            monitor_thread.join(timeout=1.0)
        if heartbeat is not None:
            heartbeat.stop(final_state)


def check_heartbeat(
    path: str | Path,
    *,
    max_age_seconds: float,
    clock: Clock | None = None,
) -> dict[str, Any]:
    max_age = _positive_seconds(max_age_seconds, "max_age_seconds")
    now = (clock if clock is not None else lambda: datetime.now(UTC))()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("heartbeat clock must return a timezone-aware datetime")

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
        if payload.get("schema_version") != HEARTBEAT_SCHEMA_VERSION:
            raise ValueError
        if payload.get("state") not in _HEALTHY_STATES:
            raise ValueError
        if not isinstance(payload.get("pid"), int) or payload["pid"] <= 0:
            raise ValueError
        updated_at_value = payload.get("updated_at")
        if not isinstance(updated_at_value, str):
            raise ValueError
        updated_at = datetime.fromisoformat(updated_at_value.replace("Z", "+00:00"))
        if updated_at.tzinfo is None or updated_at.utcoffset() is None:
            raise ValueError
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        raise HeartbeatError("worker heartbeat is unavailable or invalid") from None

    age_seconds = (now.astimezone(UTC) - updated_at.astimezone(UTC)).total_seconds()
    if age_seconds < -_MAX_FUTURE_SKEW_SECONDS or age_seconds > max_age:
        raise HeartbeatError("worker heartbeat is stale")
    return payload


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the resident worker heartbeat")
    parser.add_argument("--file", default=os.getenv(HEARTBEAT_PATH_ENV))
    parser.add_argument(
        "--max-age-seconds",
        default=os.getenv(HEARTBEAT_MAX_AGE_ENV, str(DEFAULT_HEARTBEAT_MAX_AGE_SECONDS)),
    )
    arguments = parser.parse_args(argv)
    if arguments.file is None or not arguments.file.strip():
        parser.error(f"--file or {HEARTBEAT_PATH_ENV} is required")

    try:
        max_age = _positive_seconds(arguments.max_age_seconds, "--max-age-seconds")
        payload = check_heartbeat(arguments.file, max_age_seconds=max_age)
    except (HeartbeatError, ValueError):
        print(
            json.dumps({"error_code": "worker_heartbeat_unhealthy"}, sort_keys=True),
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            {"state": payload["state"], "status": "ok"},
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
