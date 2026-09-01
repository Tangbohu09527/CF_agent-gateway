from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread

import pytest

from cf_agent_gateway.adapters.wechat import (
    ChatPollResult,
    PollFailure,
    PollFailureStage,
    PollResult,
)
from cf_agent_gateway.config import RuntimeSettings, Settings
from cf_agent_gateway.runtime import worker
from cf_agent_gateway.runtime.errors import (
    HermesAPIKeyEnvironmentError,
    WechatRuntimeDisabledError,
    WechatTokenEnvironmentError,
)
from cf_agent_gateway.runtime.heartbeat import (
    FileHeartbeat,
    HeartbeatError,
    HeartbeatPublisher,
    check_heartbeat,
)


class RecordingEvent(Event):
    def __init__(self) -> None:
        super().__init__()
        self.wait_timeouts: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.wait_timeouts.append(timeout)
        return self.is_set()


class RecordingHeartbeat:
    def __init__(self) -> None:
        self.events: list[object] = []

    def start(self) -> None:
        self.events.append("start")

    def update(self, state: str, **details: object) -> None:
        self.events.append(("update", state, details))

    def stop(self, state: str = "stopped") -> None:
        self.events.append(("stop", state))

    def wait(self, stop_event: Event, timeout_seconds: float) -> bool:
        return stop_event.wait(timeout_seconds)


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
            chats_succeeded=2,
            chats_failed=1,
            messages_seen=5,
            messages_processed=2,
            messages_new=1,
            messages_duplicate=1,
            messages_failed=1,
            messages_skipped_by_checkpoint=1,
            messages_skipped_as_self=1,
            messages_without_server_id=2,
            bootstrapped_chats=1,
            failures=[
                PollFailure(
                    stage=PollFailureStage.POLL_CHAT,
                    code="wechat_poll_chat_error",
                )
            ],
        )

    with caplog.at_level(logging.INFO, logger=worker.logger.name):
        worker.run_worker(settings, stop_event=stop_event, poll_once=poll_once)

    records = worker_log_records(caplog)
    assert calls == [settings]
    assert [record.getMessage() for record in records] == [
        "worker started",
        "poll cycle completed",
        "worker stopped",
    ]
    assert records[0].fields == {"polling_interval_seconds": 1.25}  # type: ignore[attr-defined]
    assert records[1].fields == {  # type: ignore[attr-defined]
        "logged_in": True,
        "chats_seen": 3,
        "chats_succeeded": 2,
        "chats_failed": 1,
        "messages_seen": 5,
        "messages_processed": 2,
        "messages_new": 1,
        "messages_duplicate": 1,
        "messages_skipped_checkpoint": 1,
        "messages_skipped_self": 1,
        "messages_failed": 1,
        "messages_without_server_id": 2,
        "bootstrapped_chats": 1,
        "failure_count": 1,
    }
    assert [record.levelno for record in records] == [logging.INFO, logging.INFO, logging.INFO]


def test_idle_poll_cycle_is_debug_while_worker_lifecycle_remains_info(
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop_event = Event()

    def poll_once(candidate: Settings) -> PollResult:
        assert candidate is settings
        stop_event.set()
        return PollResult(logged_in=True, chats_seen=3, chats_succeeded=3)

    with caplog.at_level(logging.DEBUG, logger=worker.logger.name):
        worker.run_worker(settings, stop_event=stop_event, poll_once=poll_once)

    records = worker_log_records(caplog)
    assert [(record.getMessage(), record.levelno) for record in records] == [
        ("worker started", logging.INFO),
        ("poll cycle started", logging.DEBUG),
        ("poll cycle completed", logging.DEBUG),
        ("worker stopped", logging.INFO),
    ]


def test_checkpoint_history_cycle_count_change_reenables_info(
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop_event = Event()
    stable_chat = ChatPollResult(
        conversation_id="wxid-history",
        succeeded=True,
        messages_seen=9,
        messages_skipped_by_checkpoint=9,
    )
    stable = PollResult(
        logged_in=True,
        chats_seen=21,
        chats_succeeded=21,
        messages_seen=95,
        messages_skipped_by_checkpoint=95,
        chat_results=[stable_chat],
    )
    changed = stable.model_copy(
        update={
            "messages_seen": 96,
            "messages_skipped_by_checkpoint": 96,
            "chat_results": [
                stable_chat.model_copy(
                    update={
                        "messages_seen": 10,
                        "messages_skipped_by_checkpoint": 10,
                    }
                )
            ],
        }
    )
    results = iter((stable, stable, changed))
    calls = 0

    def poll_once(candidate: Settings) -> PollResult:
        nonlocal calls
        assert candidate is settings
        calls += 1
        if calls == 3:
            stop_event.set()
        return next(results)

    with caplog.at_level(logging.DEBUG, logger=worker.logger.name):
        worker.run_worker(settings, stop_event=stop_event, poll_once=poll_once)

    cycle_records = [
        record
        for record in worker_log_records(caplog)
        if record.getMessage() == "poll cycle completed"
    ]
    assert [record.levelno for record in cycle_records] == [
        logging.INFO,
        logging.DEBUG,
        logging.INFO,
    ]


def test_default_worker_reuses_one_polling_lifecycle_state_across_cycles(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop_event = RecordingEvent()
    lifecycle_states: list[object] = []

    def poll_once(
        candidate: Settings,
        *,
        lifecycle_state: object,
    ) -> PollResult:
        assert candidate is settings
        lifecycle_states.append(lifecycle_state)
        if len(lifecycle_states) == 2:
            stop_event.set()
        return PollResult(logged_in=True)

    monkeypatch.setattr(worker, "run_wechat_poll_once", poll_once)

    worker.run_worker(settings, stop_event=stop_event)

    assert len(lifecycle_states) == 2
    assert isinstance(lifecycle_states[0], worker.WechatPollingLifecycleState)
    assert lifecycle_states[0] is lifecycle_states[1]


def test_worker_publishes_heartbeat_for_a_successful_cycle(settings: Settings) -> None:
    stop_event = Event()
    heartbeat = RecordingHeartbeat()

    def poll_once(candidate: Settings) -> PollResult:
        assert candidate is settings
        stop_event.set()
        return PollResult(logged_in=True, messages_processed=1)

    worker.run_worker(
        settings,
        stop_event=stop_event,
        poll_once=poll_once,
        heartbeat=heartbeat,  # type: ignore[arg-type]
    )

    assert heartbeat.events == [
        "start",
        ("update", "running", {"phase": "idle", "cycle_sequence": 0}),
        ("update", "running", {"phase": "polling", "cycle_sequence": 1}),
        (
            "update",
            "running",
            {
                "phase": "waiting",
                "cycle_sequence": 1,
                "last_cycle_succeeded": True,
                "wechat_auth": "logged_in",
            },
        ),
        ("stop", "stopped"),
    ]


def test_worker_does_not_poll_when_initial_heartbeat_publish_fails(
    settings: Settings,
    tmp_path: Path,
) -> None:
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("blocked", encoding="utf-8")
    heartbeat_publisher = HeartbeatPublisher(
        FileHeartbeat(blocked_parent / "worker.json"),
        interval_seconds=0.01,
    )
    poll_called = False

    def forbidden_poll(candidate: Settings) -> PollResult:
        nonlocal poll_called
        del candidate
        poll_called = True
        raise AssertionError("poll must not run without a durable heartbeat")

    with pytest.raises(HeartbeatError, match="initial worker heartbeat publish failed"):
        worker.run_worker(
            settings,
            stop_event=Event(),
            poll_once=forbidden_poll,
            heartbeat=heartbeat_publisher,
        )

    assert poll_called is False


def test_worker_marks_returned_poll_failures_unhealthy(settings: Settings) -> None:
    stop_event = Event()
    heartbeat = RecordingHeartbeat()

    def poll_once(candidate: Settings) -> PollResult:
        assert candidate is settings
        stop_event.set()
        return PollResult(
            logged_in=True,
            chats_seen=1,
            chats_failed=1,
            failures=[
                PollFailure(
                    stage=PollFailureStage.LIST_MESSAGES,
                    code="wechat_timeout",
                    conversation_id="conversation-1",
                )
            ],
        )

    worker.run_worker(
        settings,
        stop_event=stop_event,
        poll_once=poll_once,
        heartbeat=heartbeat,  # type: ignore[arg-type]
    )

    waiting = heartbeat.events[-2]
    assert isinstance(waiting, tuple)
    assert waiting[2]["last_cycle_succeeded"] is False
    assert waiting[2]["wechat_auth"] == "logged_in"


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


def test_stop_during_poll_waits_for_the_in_flight_cycle_to_finish() -> None:
    settings = Settings(runtime=RuntimeSettings(polling_interval_seconds=60))
    stop_event = Event()
    poll_started = Event()
    release_poll = Event()
    poll_calls = 0

    def poll_once(candidate: Settings) -> PollResult:
        nonlocal poll_calls
        assert candidate is settings
        poll_calls += 1
        poll_started.set()
        assert release_poll.wait(timeout=2)
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
        assert poll_started.wait(timeout=2)
        stop_event.set()
        thread.join(timeout=0.05)
        assert thread.is_alive()

        release_poll.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert poll_calls == 1
    finally:
        stop_event.set()
        release_poll.set()
        thread.join(timeout=2)


def test_fatal_poll_failure_marks_heartbeat_failed(settings: Settings) -> None:
    heartbeat = RecordingHeartbeat()

    def poll_once(candidate: Settings) -> PollResult:
        assert candidate is settings
        raise WechatRuntimeDisabledError()

    with pytest.raises(WechatRuntimeDisabledError):
        worker.run_worker(
            settings,
            poll_once=poll_once,
            heartbeat=heartbeat,  # type: ignore[arg-type]
        )

    assert heartbeat.events[-1] == ("stop", "failed")


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
    assert failure_record.fields == {"error_code": "poll_cycle_failed"}  # type: ignore[attr-defined]
    assert sensitive_detail not in caplog.text
    assert [record.getMessage() for record in records].count("poll cycle started") == 0
    assert [record.getMessage() for record in records].count("poll cycle completed") == 1
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
        "worker stopped",
    ]


def test_stuck_poll_does_not_renew_the_worker_heartbeat(tmp_path: Path) -> None:
    settings = Settings(runtime=RuntimeSettings(polling_interval_seconds=60))
    heartbeat_path = tmp_path / "worker.json"
    current_time = [datetime(2026, 8, 7, 9, 30, tzinfo=UTC)]
    heartbeat = HeartbeatPublisher(
        FileHeartbeat(
            heartbeat_path,
            clock=lambda: current_time[0],
            process_id=123,
            worker_id="worker-a",
        ),
        interval_seconds=0.01,
    )
    stop_event = Event()
    poll_started = Event()
    release_poll = Event()

    def stuck_poll(candidate: Settings) -> PollResult:
        assert candidate is settings
        poll_started.set()
        release_poll.wait(timeout=2)
        return PollResult(logged_in=True)

    thread = Thread(
        target=worker.run_worker,
        kwargs={
            "settings": settings,
            "stop_event": stop_event,
            "poll_once": stuck_poll,
            "heartbeat": heartbeat,
        },
        daemon=True,
    )
    thread.start()
    try:
        assert poll_started.wait(timeout=2)
        polling_payload = check_heartbeat(
            heartbeat_path,
            max_age_seconds=30,
            clock=lambda: current_time[0],
        )
        assert polling_payload["details"] == {"phase": "polling", "cycle_sequence": 1}

        current_time[0] += timedelta(seconds=31)

        with pytest.raises(HeartbeatError, match="stale"):
            check_heartbeat(
                heartbeat_path,
                max_age_seconds=30,
                clock=lambda: current_time[0],
            )
    finally:
        stop_event.set()
        release_poll.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
