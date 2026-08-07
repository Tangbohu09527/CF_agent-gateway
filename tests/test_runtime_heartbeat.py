from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from cf_agent_gateway.runtime import heartbeat
from cf_agent_gateway.runtime.heartbeat import (
    FileHeartbeat,
    HeartbeatError,
    HeartbeatPublisher,
    check_heartbeat,
)

NOW = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)


def test_file_heartbeat_atomically_writes_json_with_a_utc_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heartbeat_path = tmp_path / "runtime" / "worker.json"
    local_time = NOW.astimezone(timezone(timedelta(hours=8)))
    replacements: list[tuple[Path, Path, dict[str, object]]] = []
    replace = os.replace

    def record_replace(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        replacements.append(
            (
                source_path,
                destination_path,
                json.loads(source_path.read_text(encoding="utf-8")),
            )
        )
        replace(source, destination)

    monkeypatch.setattr(heartbeat.os, "replace", record_replace)
    writer = FileHeartbeat(
        heartbeat_path,
        clock=lambda: local_time,
        process_id=4321,
        worker_id="worker-a",
    )

    payload = writer.write("running", details={"phase": "poll"})

    expected = {
        "schema_version": 1,
        "worker_id": "worker-a",
        "pid": 4321,
        "state": "running",
        "updated_at": "2026-08-07T09:30:00Z",
        "details": {"phase": "poll"},
    }
    assert payload == expected
    assert json.loads(heartbeat_path.read_text(encoding="utf-8")) == expected
    assert heartbeat_path.read_bytes().endswith(b"\n")
    assert len(replacements) == 1
    temporary_path, destination_path, temporary_payload = replacements[0]
    assert temporary_path.parent == heartbeat_path.parent
    assert temporary_path.name.startswith(f".{heartbeat_path.name}.")
    assert temporary_path.suffix == ".tmp"
    assert destination_path == heartbeat_path
    assert temporary_payload == expected
    assert not temporary_path.exists()


def test_file_heartbeat_removes_temporary_file_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    heartbeat_path = tmp_path / "worker.json"

    def fail_replace(source: str | Path, destination: str | Path) -> None:
        del source, destination
        raise OSError("controlled replace failure")

    monkeypatch.setattr(heartbeat.os, "replace", fail_replace)
    writer = FileHeartbeat(heartbeat_path, clock=lambda: NOW)

    with pytest.raises(OSError, match="controlled replace failure"):
        writer.write("running")

    assert not heartbeat_path.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("state", ["starting", "running"])
def test_check_heartbeat_accepts_fresh_healthy_states(tmp_path: Path, state: str) -> None:
    heartbeat_path = tmp_path / "worker.json"
    FileHeartbeat(
        heartbeat_path,
        clock=lambda: NOW,
        process_id=123,
        worker_id="worker-a",
    ).write(state)  # type: ignore[arg-type]

    payload = check_heartbeat(
        heartbeat_path,
        max_age_seconds=30,
        clock=lambda: NOW + timedelta(seconds=30),
    )

    assert payload["state"] == state
    assert payload["updated_at"] == "2026-08-07T09:30:00Z"


def test_check_heartbeat_rejects_a_stale_file(tmp_path: Path) -> None:
    heartbeat_path = tmp_path / "worker.json"
    FileHeartbeat(heartbeat_path, clock=lambda: NOW).write("running")

    with pytest.raises(HeartbeatError, match="stale"):
        check_heartbeat(
            heartbeat_path,
            max_age_seconds=30,
            clock=lambda: NOW + timedelta(seconds=30.001),
        )


def test_check_heartbeat_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(HeartbeatError, match="unavailable or invalid"):
        check_heartbeat(
            tmp_path / "missing.json",
            max_age_seconds=30,
            clock=lambda: NOW,
        )


def test_check_heartbeat_rejects_malformed_json(tmp_path: Path) -> None:
    heartbeat_path = tmp_path / "worker.json"
    heartbeat_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(HeartbeatError, match="unavailable or invalid"):
        check_heartbeat(heartbeat_path, max_age_seconds=30, clock=lambda: NOW)


def test_check_heartbeat_rejects_a_stopped_worker(tmp_path: Path) -> None:
    heartbeat_path = tmp_path / "worker.json"
    FileHeartbeat(heartbeat_path, clock=lambda: NOW).write("stopped")

    with pytest.raises(HeartbeatError, match="unavailable or invalid"):
        check_heartbeat(heartbeat_path, max_age_seconds=30, clock=lambda: NOW)


def test_heartbeat_main_reports_a_healthy_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    heartbeat_path = tmp_path / "worker.json"
    expected_payload = {"state": "running"}
    calls: list[tuple[str, float]] = []

    def healthy_check(path: str, *, max_age_seconds: float) -> dict[str, object]:
        calls.append((path, max_age_seconds))
        return expected_payload

    monkeypatch.setattr(heartbeat, "check_heartbeat", healthy_check)

    assert heartbeat.main(["--file", str(heartbeat_path), "--max-age-seconds", "12.5"]) == 0
    captured = capsys.readouterr()
    assert calls == [(str(heartbeat_path), 12.5)]
    assert json.loads(captured.out) == {"state": "running", "status": "ok"}
    assert captured.err == ""


def test_heartbeat_main_redacts_check_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "database-password-that-must-not-leak"
    heartbeat_path = tmp_path / "worker.json"

    def fail_check(path: str, *, max_age_seconds: float) -> dict[str, object]:
        del path, max_age_seconds
        raise HeartbeatError(f"heartbeat failure: {secret}")

    monkeypatch.setattr(heartbeat, "check_heartbeat", fail_check)

    assert heartbeat.main(["--file", str(heartbeat_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error_code": "worker_heartbeat_unhealthy"}
    assert secret not in captured.err


def test_publisher_renews_only_while_the_worker_main_loop_waits(tmp_path: Path) -> None:
    heartbeat_path = tmp_path / "worker.json"
    current_time = [NOW]
    wait_timeouts: list[float] = []

    class AdvancingEvent:
        def wait(self, timeout: float | None = None) -> bool:
            assert timeout is not None
            wait_timeouts.append(timeout)
            current_time[0] += timedelta(seconds=timeout)
            return False

    publisher = HeartbeatPublisher(
        FileHeartbeat(heartbeat_path, clock=lambda: current_time[0]),
        interval_seconds=10,
    )
    publisher.start()
    publisher.update("running", phase="waiting")

    assert publisher.wait(AdvancingEvent(), 25) is False  # type: ignore[arg-type]

    assert wait_timeouts == [10, 10, 5]
    payload = check_heartbeat(
        heartbeat_path,
        max_age_seconds=10,
        clock=lambda: current_time[0],
    )
    assert payload["updated_at"] == "2026-08-07T09:30:20Z"
