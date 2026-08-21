from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from cf_agent_gateway import wechat_poll_once
from cf_agent_gateway.adapters.wechat import (
    ChatPollResult,
    PollFailure,
    PollFailureStage,
    PollResult,
)
from cf_agent_gateway.config import (
    DatabaseSettings,
    HermesSettings,
    RuntimeSettings,
    Settings,
    WechatSettings,
)
from cf_agent_gateway.runtime import (
    WechatRuntimeDisabledError,
    WechatTokenEnvironmentError,
)
from cf_agent_gateway.runtime.status import WorkerLeaseHeldError


def invoke_cli(monkeypatch: pytest.MonkeyPatch, result: PollResult) -> int:
    monkeypatch.setattr(wechat_poll_once, "load_settings", lambda path: Settings())
    monkeypatch.setattr(wechat_poll_once, "run_exclusive_poll_once", lambda settings: result)
    return wechat_poll_once.main()


class _CodedPollError(RuntimeError):
    def __init__(self, code: object) -> None:
        self.code = code
        super().__init__("controlled coded poll failure")


def test_cli_prints_only_the_safe_poll_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sensitive_body = "complete-sensitive-message-body"
    sensitive_token = "secret-token-that-must-not-leak"
    result = PollResult(
        source_account_id="wxid_gateway",
        logged_in=True,
        chats_seen=1,
        chats_succeeded=1,
        messages_seen=3,
        messages_processed=1,
        messages_skipped_by_checkpoint=2,
        chat_results=[
            ChatPollResult(
                conversation_id="wxid_sensitive",
                conversation_name=sensitive_body,
                succeeded=True,
            )
        ],
    )

    exit_code = invoke_cli(monkeypatch, result)

    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert exit_code == 0
    assert captured.err == ""
    assert summary == {
        "bootstrapped_chats": 0,
        "chats_failed": 0,
        "chats_seen": 1,
        "chats_succeeded": 1,
        "failure_codes": [],
        "logged_in": True,
        "messages_processed": 1,
        "messages_seen": 3,
        "messages_skipped_by_checkpoint": 2,
        "source_account_id": "wxid_gateway",
    }
    serialized = captured.out + captured.err
    assert sensitive_body not in serialized
    assert sensitive_token not in serialized
    assert "wxid_sensitive" not in serialized


@pytest.mark.parametrize(
    ("result", "expected_exit_code"),
    [
        (PollResult(logged_in=True), 0),
        (
            PollResult(
                logged_in=True,
                chats_seen=1,
                chats_failed=1,
                failures=[PollFailure(stage=PollFailureStage.SINK, code="wechat_sink_error")],
            ),
            1,
        ),
        (PollResult(logged_in=False), 2),
    ],
)
def test_cli_exit_codes_are_stable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    result: PollResult,
    expected_exit_code: int,
) -> None:
    assert invoke_cli(monkeypatch, result) == expected_exit_code
    capsys.readouterr()


def test_cli_runtime_failure_does_not_print_exception_content(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sensitive_token = "runtime-secret-token"
    sensitive_body = "runtime-sensitive-body"
    monkeypatch.setattr(wechat_poll_once, "load_settings", lambda path: Settings())

    def fail(settings: Settings) -> PollResult:
        del settings
        raise RuntimeError(f"{sensitive_token}:{sensitive_body}")

    monkeypatch.setattr(wechat_poll_once, "run_exclusive_poll_once", fail)

    assert wechat_poll_once.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error_code": "wechat_poll_once_failed"}
    assert sensitive_token not in captured.err
    assert sensitive_body not in captured.err


@pytest.mark.parametrize(
    ("error", "expected_exit_code"),
    [
        (WechatRuntimeDisabledError(), 2),
        (WechatTokenEnvironmentError("TEST_WECHAT_TOKEN"), 1),
    ],
)
def test_cli_runtime_boundary_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    expected_exit_code: int,
) -> None:
    monkeypatch.setattr(wechat_poll_once, "load_settings", lambda path: Settings())

    def fail(settings: Settings) -> PollResult:
        del settings
        raise error

    monkeypatch.setattr(wechat_poll_once, "run_exclusive_poll_once", fail)

    assert wechat_poll_once.main() == expected_exit_code
    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err)
    assert payload["error_code"] == error.code  # type: ignore[attr-defined]


def test_python_module_entrypoint_exits_two_when_runtime_is_disabled(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'gateway.db').as_posix()}"
    config_path.write_text(
        f'database:\n  url: "{database_url}"\nwechat:\n  enabled: false\n',
        encoding="utf-8",
    )
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
        [sys.executable, "-m", "cf_agent_gateway.wechat_poll_once"],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert json.loads(completed.stderr) == {"error_code": "wechat_runtime_disabled"}


def test_exclusive_poll_constructs_reporter_and_passes_callable_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        database=DatabaseSettings(url="sqlite+pysqlite:///exclusive.db"),
        wechat=WechatSettings(enabled=True),
        hermes=HermesSettings(enabled=True, base_url="https://hermes.example"),
        runtime=RuntimeSettings(
            heartbeat_interval_seconds=7,
            heartbeat_stale_after_seconds=23,
        ),
    )
    expected = PollResult(logged_in=True)
    events: list[str] = []
    constructor_arguments: dict[str, object] = {}

    class RecordingReporter:
        def start(self) -> None:
            events.append("start")

        def cycle_started(self) -> None:
            events.append("cycle_started")

        def ensure_active(self) -> None:
            events.append("guard")

        def cycle_succeeded(self, result: PollResult) -> None:
            assert result is expected
            events.append("cycle_succeeded")

        def stop(self) -> None:
            events.append("stop")

    reporter = RecordingReporter()

    def build_reporter(
        database_url: str,
        *,
        hermes_enabled: bool,
        heartbeat_interval_seconds: float,
        heartbeat_stale_after_seconds: float,
    ) -> RecordingReporter:
        constructor_arguments.update(
            {
                "database_url": database_url,
                "hermes_enabled": hermes_enabled,
                "heartbeat_interval_seconds": heartbeat_interval_seconds,
                "heartbeat_stale_after_seconds": heartbeat_stale_after_seconds,
            }
        )
        events.append("constructed")
        return reporter

    def poll_once(
        candidate: Settings,
        *,
        lease_guard: Callable[[], None],
    ) -> PollResult:
        assert candidate is settings
        assert callable(lease_guard)
        events.append("poll")
        lease_guard()
        return expected

    monkeypatch.setattr(wechat_poll_once, "DatabaseWorkerStatusReporter", build_reporter)
    monkeypatch.setattr(wechat_poll_once, "run_wechat_poll_once", poll_once)

    assert wechat_poll_once.run_exclusive_poll_once(settings) is expected
    assert constructor_arguments == {
        "database_url": settings.database.url,
        "hermes_enabled": True,
        "heartbeat_interval_seconds": 7.0,
        "heartbeat_stale_after_seconds": 23.0,
    }
    assert events == [
        "constructed",
        "start",
        "cycle_started",
        "poll",
        "guard",
        "cycle_succeeded",
        "stop",
    ]


@pytest.mark.parametrize(
    ("poll_error", "expected_error_code"),
    [
        (_CodedPollError("hermes.timeout-v1"), "hermes.timeout-v1"),
        (RuntimeError("controlled poll failure"), "wechat_poll_once_failed"),
        (_CodedPollError("invalid code\n"), "wechat_poll_once_failed"),
        (_CodedPollError("x" * 129), "wechat_poll_once_failed"),
    ],
    ids=["valid-code", "missing-code", "invalid-code", "overlong-code"],
)
def test_exclusive_poll_records_safe_error_code_and_preserves_poll_failure(
    monkeypatch: pytest.MonkeyPatch,
    poll_error: Exception,
    expected_error_code: str,
) -> None:
    events: list[str] = []
    recorded_error_codes: list[str] = []

    class RecordingReporter:
        def start(self) -> None:
            events.append("start")

        def cycle_started(self) -> None:
            events.append("cycle_started")

        def ensure_active(self) -> None:
            events.append("guard")

        def cycle_succeeded(self, result: PollResult) -> None:
            del result
            raise AssertionError("failed polls must not be recorded as succeeded")

        def cycle_failed(self, error_code: str) -> None:
            events.append("cycle_failed")
            recorded_error_codes.append(error_code)
            raise RuntimeError("controlled cycle failure recording error")

        def stop(self) -> None:
            events.append("stop")
            raise RuntimeError("controlled stop failure")

    reporter = RecordingReporter()

    def build_reporter(*args: object, **kwargs: object) -> RecordingReporter:
        del args, kwargs
        return reporter

    def fail(
        settings: Settings,
        *,
        lease_guard: Callable[[], None],
    ) -> PollResult:
        del settings, lease_guard
        events.append("poll")
        raise poll_error

    monkeypatch.setattr(wechat_poll_once, "DatabaseWorkerStatusReporter", build_reporter)
    monkeypatch.setattr(wechat_poll_once, "run_wechat_poll_once", fail)

    with pytest.raises(RuntimeError) as caught:
        wechat_poll_once.run_exclusive_poll_once(Settings(wechat=WechatSettings(enabled=True)))

    assert caught.value is poll_error
    assert recorded_error_codes == [expected_error_code]
    assert events == ["start", "cycle_started", "poll", "cycle_failed", "stop"]


@pytest.mark.parametrize(
    ("failure_stage", "expected_events"),
    [
        ("cycle_started", ["start", "cycle_started", "cycle_failed", "stop"]),
        (
            "cycle_succeeded",
            [
                "start",
                "cycle_started",
                "poll",
                "cycle_succeeded",
                "cycle_failed",
                "stop",
            ],
        ),
    ],
)
def test_exclusive_poll_stops_reporter_when_lifecycle_hook_raises(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    expected_events: list[str],
) -> None:
    events: list[str] = []
    lifecycle_error = RuntimeError(f"controlled {failure_stage} failure")
    expected = PollResult(logged_in=True)

    class LifecycleFailingReporter:
        def start(self) -> None:
            events.append("start")

        def cycle_started(self) -> None:
            events.append("cycle_started")
            if failure_stage == "cycle_started":
                raise lifecycle_error

        def ensure_active(self) -> None:
            events.append("guard")

        def cycle_succeeded(self, result: PollResult) -> None:
            assert result is expected
            events.append("cycle_succeeded")
            if failure_stage == "cycle_succeeded":
                raise lifecycle_error

        def cycle_failed(self, error_code: str) -> None:
            assert error_code == "wechat_poll_once_failed"
            events.append("cycle_failed")
            raise RuntimeError("controlled cycle failure recording error")

        def stop(self) -> None:
            events.append("stop")
            raise RuntimeError("controlled stop failure")

    reporter = LifecycleFailingReporter()

    def build_reporter(*args: object, **kwargs: object) -> LifecycleFailingReporter:
        del args, kwargs
        return reporter

    def poll_once(
        settings: Settings,
        *,
        lease_guard: Callable[[], None],
    ) -> PollResult:
        del settings, lease_guard
        events.append("poll")
        return expected

    monkeypatch.setattr(wechat_poll_once, "DatabaseWorkerStatusReporter", build_reporter)
    monkeypatch.setattr(wechat_poll_once, "run_wechat_poll_once", poll_once)

    with pytest.raises(RuntimeError) as caught:
        wechat_poll_once.run_exclusive_poll_once(Settings(wechat=WechatSettings(enabled=True)))

    assert caught.value is lifecycle_error
    assert events == expected_events


def test_cli_reports_lease_held_without_polling(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    poll_calls = 0

    class LeaseHeldReporter:
        def start(self) -> None:
            events.append("start")
            raise WorkerLeaseHeldError("wechat")

        def ensure_active(self) -> None:
            raise AssertionError("guard must not run when reporter start fails")

        def stop(self) -> None:
            events.append("stop")

    reporter = LeaseHeldReporter()

    def build_reporter(*args: object, **kwargs: object) -> LeaseHeldReporter:
        del args, kwargs
        return reporter

    def poll_once(
        settings: Settings,
        *,
        lease_guard: Callable[[], None],
    ) -> PollResult:
        nonlocal poll_calls
        del settings, lease_guard
        poll_calls += 1
        return PollResult(logged_in=True)

    monkeypatch.setattr(wechat_poll_once, "DatabaseWorkerStatusReporter", build_reporter)
    monkeypatch.setattr(wechat_poll_once, "run_wechat_poll_once", poll_once)
    monkeypatch.setattr(
        wechat_poll_once,
        "load_settings",
        lambda path: Settings(wechat=WechatSettings(enabled=True)),
    )

    assert wechat_poll_once.main() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error_code": "worker_lease_held"}
    assert poll_calls == 0
    assert events == ["start"]


def test_exclusive_poll_fails_fast_before_reporter_construction_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor_calls = 0
    poll_calls = 0

    def build_reporter(*args: object, **kwargs: object) -> object:
        nonlocal constructor_calls
        del args, kwargs
        constructor_calls += 1
        raise AssertionError("reporter must not be constructed for a disabled runtime")

    def poll_once(*args: object, **kwargs: object) -> PollResult:
        nonlocal poll_calls
        del args, kwargs
        poll_calls += 1
        raise AssertionError("poll must not run for a disabled runtime")

    monkeypatch.setattr(wechat_poll_once, "DatabaseWorkerStatusReporter", build_reporter)
    monkeypatch.setattr(wechat_poll_once, "run_wechat_poll_once", poll_once)

    with pytest.raises(WechatRuntimeDisabledError):
        wechat_poll_once.run_exclusive_poll_once(Settings())

    assert constructor_calls == 0
    assert poll_calls == 0


def test_exclusive_poll_propagates_stop_failure_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    stop_error = RuntimeError("controlled stop failure")
    expected = PollResult(logged_in=True)

    class StopFailingReporter:
        def start(self) -> None:
            events.append("start")

        def cycle_started(self) -> None:
            events.append("cycle_started")

        def ensure_active(self) -> None:
            events.append("guard")

        def cycle_succeeded(self, result: PollResult) -> None:
            assert result is expected
            events.append("cycle_succeeded")

        def stop(self) -> None:
            events.append("stop")
            raise stop_error

    reporter = StopFailingReporter()

    def build_reporter(*args: object, **kwargs: object) -> StopFailingReporter:
        del args, kwargs
        return reporter

    def poll_once(
        settings: Settings,
        *,
        lease_guard: Callable[[], None],
    ) -> PollResult:
        del settings, lease_guard
        events.append("poll")
        return expected

    monkeypatch.setattr(wechat_poll_once, "DatabaseWorkerStatusReporter", build_reporter)
    monkeypatch.setattr(wechat_poll_once, "run_wechat_poll_once", poll_once)

    with pytest.raises(RuntimeError) as caught:
        wechat_poll_once.run_exclusive_poll_once(Settings(wechat=WechatSettings(enabled=True)))

    assert caught.value is stop_error
    assert events == ["start", "cycle_started", "poll", "cycle_succeeded", "stop"]
