from __future__ import annotations

import logging
from collections.abc import Callable
from threading import Event, Thread

import pytest

from cf_agent_gateway.adapters.wechat import PollResult
from cf_agent_gateway.config import RuntimeSettings, Settings, WechatSettings
from cf_agent_gateway.database import DatabaseSchemaError
from cf_agent_gateway.runtime import worker
from cf_agent_gateway.runtime.errors import (
    HermesAPIKeyEnvironmentError,
    HermesClientInitializationError,
    WechatClientInitializationError,
    WechatRuntimeDisabledError,
    WechatTokenEnvironmentError,
)


class RecordingEvent(Event):
    def __init__(self) -> None:
        super().__init__()
        self.wait_timeouts: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.wait_timeouts.append(timeout)
        return self.is_set()


class LeaseRecordingReporter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")

    def ensure_active(self) -> None:
        self.calls.append("ensure_active")

    def cycle_started(self) -> None:
        self.calls.append("cycle_started")

    def cycle_succeeded(self, result: PollResult) -> None:
        del result
        self.calls.append("cycle_succeeded")

    def cycle_failed(self, error_code: str) -> None:
        del error_code
        self.calls.append("cycle_failed")


@pytest.fixture
def settings() -> Settings:
    return Settings(runtime=RuntimeSettings(polling_interval_seconds=1.25))


def worker_log_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == worker.logger.name]


def test_worker_starts_polls_logs_result_and_stops(
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop_event = Event()
    calls: list[Settings] = []

    def poll_once(candidate: Settings) -> PollResult:
        calls.append(candidate)
        stop_event.set()
        return PollResult(
            logged_in=True,
            chats_seen=3,
            chats_failed=1,
            messages_seen=5,
            messages_processed=2,
        )

    with caplog.at_level(logging.INFO, logger=worker.logger.name):
        worker.run_worker(settings, stop_event=stop_event, poll_once=poll_once)

    records = worker_log_records(caplog)
    assert calls == [settings]
    assert [record.getMessage() for record in records] == [
        "worker started",
        "poll cycle started",
        "messages processed",
        "worker stopped",
    ]
    assert records[0].fields == {"polling_interval_seconds": 1.25}  # type: ignore[attr-defined]
    assert records[2].fields == {  # type: ignore[attr-defined]
        "logged_in": True,
        "chats_seen": 3,
        "chats_failed": 1,
        "messages_seen": 5,
        "messages_processed": 2,
        "cycle_degraded": True,
        "consecutive_failures": 1,
        "retry_delay_seconds": 1.25,
        "failure_codes": ["poll_chats_failed"],
    }


def test_worker_does_not_poll_when_stop_is_already_set(
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop_event = Event()
    stop_event.set()

    def forbidden_poll(settings: Settings) -> PollResult:
        del settings
        raise AssertionError("poll must not run after shutdown was requested")

    with caplog.at_level(logging.INFO, logger=worker.logger.name):
        worker.run_worker(settings, stop_event=stop_event, poll_once=forbidden_poll)

    assert [record.getMessage() for record in worker_log_records(caplog)] == [
        "worker started",
        "worker stopped",
    ]


def test_worker_waits_for_the_configured_interval(settings: Settings) -> None:
    stop_event = RecordingEvent()

    def poll_once(candidate: Settings) -> PollResult:
        assert candidate is settings
        stop_event.set()
        return PollResult(logged_in=True)

    worker.run_worker(settings, stop_event=stop_event, poll_once=poll_once)

    assert stop_event.wait_timeouts == [1.25]


def test_default_worker_injects_lease_guard_into_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        runtime=RuntimeSettings(polling_interval_seconds=1.25),
        wechat=WechatSettings(enabled=True),
    )
    stop_event = Event()
    reporter = LeaseRecordingReporter()
    observed_guards: list[Callable[[], None] | None] = []

    def poll_once(
        candidate: Settings,
        *,
        lease_guard: Callable[[], None] | None,
    ) -> PollResult:
        assert candidate is settings
        observed_guards.append(lease_guard)
        assert lease_guard is not None
        lease_guard()
        stop_event.set()
        return PollResult(logged_in=True)

    monkeypatch.setattr(worker, "run_wechat_poll_once", poll_once)

    worker.run_worker(
        settings,
        stop_event=stop_event,
        status_reporter=reporter,  # type: ignore[arg-type]
    )

    assert observed_guards == [reporter.ensure_active]
    assert reporter.calls.count("ensure_active") >= 2


def test_stop_interrupts_interval_wait() -> None:
    settings = Settings(runtime=RuntimeSettings(polling_interval_seconds=60))
    stop_event = Event()
    poll_finished = Event()
    poll_calls = 0

    def poll_once(candidate: Settings) -> PollResult:
        nonlocal poll_calls
        assert candidate is settings
        poll_calls += 1
        poll_finished.set()
        return PollResult(logged_in=True)

    thread = Thread(
        target=worker.run_worker,
        kwargs={
            "settings": settings,
            "stop_event": stop_event,
            "poll_once": poll_once,
        },
        daemon=True,
    )
    thread.start()
    try:
        assert poll_finished.wait(timeout=2)
        stop_event.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert poll_calls == 1
    finally:
        stop_event.set()
        thread.join(timeout=2)


def test_worker_retries_an_ordinary_poll_error_without_leaking_it(
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop_event = RecordingEvent()
    sensitive_detail = "message-content-that-must-not-be-logged"
    poll_calls = 0

    def poll_once(candidate: Settings) -> PollResult:
        nonlocal poll_calls
        assert candidate is settings
        poll_calls += 1
        if poll_calls == 1:
            raise RuntimeError(sensitive_detail)
        stop_event.set()
        return PollResult(logged_in=True, messages_processed=1)

    with caplog.at_level(logging.INFO, logger=worker.logger.name):
        worker.run_worker(settings, stop_event=stop_event, poll_once=poll_once)

    records = worker_log_records(caplog)
    failure_record = next(
        record for record in records if record.getMessage() == "poll cycle failed"
    )
    assert poll_calls == 2
    assert stop_event.wait_timeouts == [1.25, 1.25]
    assert failure_record.fields == {  # type: ignore[attr-defined]
        "error_code": "poll_cycle_failed",
        "consecutive_failures": 1,
        "retry_delay_seconds": 1.25,
    }
    assert sensitive_detail not in caplog.text
    assert [record.getMessage() for record in records].count("poll cycle started") == 2
    assert records[-1].getMessage() == "worker stopped"


@pytest.mark.parametrize(
    "error_factory",
    [
        pytest.param(WechatRuntimeDisabledError, id="wechat-disabled"),
        pytest.param(
            lambda: WechatTokenEnvironmentError("TEST_WECHAT_TOKEN"),
            id="wechat-token-missing",
        ),
        pytest.param(
            lambda: HermesAPIKeyEnvironmentError("TEST_HERMES_API_KEY"),
            id="hermes-key-missing",
        ),
        pytest.param(HermesClientInitializationError, id="hermes-client-invalid"),
        pytest.param(WechatClientInitializationError, id="wechat-client-invalid"),
        pytest.param(DatabaseSchemaError, id="database-schema-invalid"),
    ],
)
def test_worker_propagates_permanent_poll_errors(
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
    error_factory: Callable[[], Exception],
) -> None:
    stop_event = RecordingEvent()
    error = error_factory()

    def poll_once(candidate: Settings) -> PollResult:
        assert candidate is settings
        raise error

    with (
        caplog.at_level(logging.INFO, logger=worker.logger.name),
        pytest.raises(type(error)) as raised,
    ):
        worker.run_worker(settings, stop_event=stop_event, poll_once=poll_once)

    assert raised.value is error
    assert stop_event.wait_timeouts == []
    assert [record.getMessage() for record in worker_log_records(caplog)] == [
        "worker started",
        "poll cycle started",
        "worker stopped",
    ]
