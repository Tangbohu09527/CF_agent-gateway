from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Event

import pytest

from cf_agent_gateway.config import Settings
from cf_agent_gateway.delivery import DeliveryBatchResult
from cf_agent_gateway.runtime import delivery_worker


class RecordingHeartbeat:
    def __init__(self) -> None:
        self.events: list[object] = []
        self.wait_started = Event()

    def start(self) -> None:
        self.events.append("start")

    def update(self, state: str, **details: object) -> None:
        self.events.append(("update", state, details))

    def stop(self, state: str = "stopped") -> None:
        self.events.append(("stop", state))

    def wait(self, stop_event: Event, timeout_seconds: float) -> bool:
        self.events.append(("wait", timeout_seconds))
        self.wait_started.set()
        return stop_event.wait(timeout_seconds)


def test_delivery_worker_runs_existing_drain_and_publishes_heartbeat() -> None:
    settings = Settings()
    stop_event = Event()
    heartbeat = RecordingHeartbeat()
    calls: list[Settings] = []

    def deliver_once(candidate: Settings) -> DeliveryBatchResult:
        calls.append(candidate)
        assert heartbeat.wait_started.wait(timeout=1)
        stop_event.set()
        return DeliveryBatchResult(deliveries=())

    delivery_worker.run_delivery_worker(
        settings,
        stop_event=stop_event,
        deliver_once=deliver_once,
        heartbeat=heartbeat,  # type: ignore[arg-type]
        idle_poll_seconds=0.5,
    )

    assert calls == [settings]
    assert heartbeat.events[0:2] == [
        "start",
        ("update", "running", {"phase": "delivery"}),
    ]
    wait_event = heartbeat.events[2]
    assert isinstance(wait_event, tuple)
    assert wait_event[0] == "wait"
    assert isinstance(wait_event[1], float)
    assert wait_event[1] > 0
    assert heartbeat.events[-1] == ("stop", "stopped")


def test_delivery_worker_marks_heartbeat_failed_when_drain_raises() -> None:
    heartbeat = RecordingHeartbeat()

    def fail_delivery(settings: Settings) -> DeliveryBatchResult:
        del settings
        assert heartbeat.wait_started.wait(timeout=1)
        raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        delivery_worker.run_delivery_worker(
            Settings(),
            deliver_once=fail_delivery,
            heartbeat=heartbeat,  # type: ignore[arg-type]
        )

    assert heartbeat.events[0:2] == [
        "start",
        ("update", "running", {"phase": "delivery"}),
    ]
    assert isinstance(heartbeat.events[2], tuple)
    assert heartbeat.events[2][0] == "wait"
    assert heartbeat.events[-1] == ("stop", "failed")


def test_delivery_worker_does_not_drain_after_shutdown() -> None:
    stop_event = Event()
    stop_event.set()
    heartbeat = RecordingHeartbeat()

    def forbidden_delivery(settings: Settings) -> DeliveryBatchResult:
        del settings
        raise AssertionError("delivery must not run after shutdown")

    delivery_worker.run_delivery_worker(
        Settings(),
        stop_event=stop_event,
        deliver_once=forbidden_delivery,
        heartbeat=heartbeat,  # type: ignore[arg-type]
    )

    assert heartbeat.events[0:2] == [
        "start",
        ("update", "running", {"phase": "delivery"}),
    ]
    assert isinstance(heartbeat.events[2], tuple)
    assert heartbeat.events[2][0] == "wait"
    assert heartbeat.events[-1] == ("stop", "stopped")


@pytest.mark.parametrize("value", [True, 0, -1, float("nan"), float("inf")])
def test_delivery_worker_rejects_invalid_idle_interval(value: object) -> None:
    with pytest.raises(ValueError, match="idle_poll_seconds"):
        delivery_worker.run_delivery_worker(
            Settings(),
            stop_event=Event(),
            idle_poll_seconds=value,  # type: ignore[arg-type]
        )


def test_delivery_worker_module_exits_two_when_wechat_is_disabled(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("wechat:\n  enabled: false\n", encoding="utf-8")
    environment = os.environ.copy()
    environment["CF_GATEWAY_CONFIG"] = str(config_path)
    for name in (
        "CF_GATEWAY_WORKER_CONCURRENCY",
        "CF_GATEWAY_WORKER_LEASE_SECONDS",
        "CF_GATEWAY_WORKER_RETRY_LIMIT",
        "CF_GATEWAY_WORKER_HEARTBEAT_PATH",
    ):
        environment.pop(name, None)
    source_path = str(Path(__file__).resolve().parents[1] / "src")
    existing_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        os.pathsep.join((source_path, existing_python_path))
        if existing_python_path
        else source_path
    )

    completed = subprocess.run(
        [sys.executable, "-m", "cf_agent_gateway.runtime.delivery_worker"],
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
    assert payloads[-1]["message"] == "delivery worker failed"
    assert payloads[-1]["error_code"] == "wechat_runtime_disabled"
