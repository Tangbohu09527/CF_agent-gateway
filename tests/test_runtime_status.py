from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest

from cf_agent_gateway.adapters.wechat import PollResult
from cf_agent_gateway.config import DatabaseSettings, RuntimeSettings, Settings
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    initialize_database,
)
from cf_agent_gateway.runtime import status as runtime_status
from cf_agent_gateway.runtime.errors import WechatRuntimeDisabledError
from cf_agent_gateway.runtime.models import RuntimeWorkerStatus
from cf_agent_gateway.runtime.status import (
    DatabaseWorkerStatusReporter,
    WorkerLeaseHeldError,
    WorkerLeaseLostError,
)
from cf_agent_gateway.runtime.worker import run_worker


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class RecordingEvent(Event):
    def __init__(self) -> None:
        super().__init__()
        self.wait_timeouts: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.wait_timeouts.append(timeout)
        return self.is_set()


class RecordingReporter:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")

    def ensure_active(self) -> None:
        self.calls.append("ensure_active")

    def cycle_started(self) -> None:
        self.calls.append("cycle_started")

    def cycle_succeeded(self, result: PollResult) -> None:
        self.calls.append(("cycle_succeeded", result))

    def cycle_failed(self, error_code: str) -> None:
        self.calls.append(("cycle_failed", error_code))


class CleanupFailingReporter(RecordingReporter):
    def stop(self) -> None:
        super().stop()
        raise RuntimeError("cleanup failed")


def database_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path / 'runtime-status.db'}"


def reporter(
    url: str,
    clock: MutableClock,
) -> DatabaseWorkerStatusReporter:
    return DatabaseWorkerStatusReporter(
        url,
        hermes_enabled=True,
        heartbeat_interval_seconds=3600,
        heartbeat_stale_after_seconds=30,
        clock=clock,
    )


def read_status(url: str) -> RuntimeWorkerStatus:
    engine = create_database_engine(url)
    try:
        initialize_database(engine)
        session_factory = create_database_session_factory(engine)
        with session_factory() as session:
            status = session.get(RuntimeWorkerStatus, "wechat")
            assert status is not None
            session.expunge(status)
            return status
    finally:
        engine.dispose()


def test_fresh_worker_lease_rejects_a_second_worker(tmp_path: Path) -> None:
    url = database_url(tmp_path)
    clock = MutableClock(datetime(2026, 8, 21, 9, 0, tzinfo=UTC))
    first = reporter(url, clock)
    second = reporter(url, clock)

    first.start()
    try:
        with pytest.raises(WorkerLeaseHeldError):
            second.start()
    finally:
        second.stop()
        first.stop()


def test_stale_worker_lease_is_recovered_and_old_owner_stops(tmp_path: Path) -> None:
    url = database_url(tmp_path)
    clock = MutableClock(datetime(2026, 8, 21, 9, 0, tzinfo=UTC))
    first = reporter(url, clock)
    second = reporter(url, clock)

    first.start()
    try:
        clock.value += timedelta(seconds=31)
        second.start()
        assert read_status(url).instance_id == second.instance_id
        with pytest.raises(WorkerLeaseLostError):
            first.cycle_started()
    finally:
        first.stop()
        second.stop()


def test_stopped_worker_can_restart_with_a_new_lease(tmp_path: Path) -> None:
    url = database_url(tmp_path)
    clock = MutableClock(datetime(2026, 8, 21, 9, 0, tzinfo=UTC))
    first = reporter(url, clock)
    first.start()
    first_instance_id = first.instance_id
    first.stop()

    clock.value += timedelta(seconds=1)
    second = reporter(url, clock)
    second.start()
    try:
        status = read_status(url)
        assert status.instance_id == second.instance_id
        assert status.instance_id != first_instance_id
        assert status.state == "starting"
    finally:
        second.stop()


def test_heartbeat_thread_start_failure_releases_lease_and_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = database_url(tmp_path)
    clock = MutableClock(datetime(2026, 8, 21, 9, 0, tzinfo=UTC))
    start_error = RuntimeError("controlled heartbeat thread start failure")

    class StartFailingThread:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def start(self) -> None:
            raise start_error

    failed_reporter = reporter(url, clock)
    with monkeypatch.context() as patch:
        patch.setattr(runtime_status, "Thread", StartFailingThread)
        with pytest.raises(RuntimeError) as caught:
            failed_reporter.start()

    assert caught.value is start_error
    assert read_status(url).state == "stopped"
    assert failed_reporter._heartbeat_thread is None
    assert failed_reporter._session_factory is None
    assert failed_reporter._engine is None

    replacement = reporter(url, clock)
    replacement.start()
    try:
        status = read_status(url)
        assert status.instance_id == replacement.instance_id
        assert status.state == "starting"
    finally:
        replacement.stop()


def test_reporter_can_restart_after_heartbeat_thread_start_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = database_url(tmp_path)
    clock = MutableClock(datetime(2026, 8, 21, 9, 0, tzinfo=UTC))
    status_reporter = reporter(url, clock)

    class StartFailingThread:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def start(self) -> None:
            raise RuntimeError("controlled heartbeat thread start failure")

    with monkeypatch.context() as patch:
        patch.setattr(runtime_status, "Thread", StartFailingThread)
        with pytest.raises(RuntimeError):
            status_reporter.start()

    status_reporter.start()
    try:
        assert read_status(url).instance_id == status_reporter.instance_id
    finally:
        status_reporter.stop()


def test_cycle_status_is_persisted_for_health_checks(tmp_path: Path) -> None:
    url = database_url(tmp_path)
    clock = MutableClock(datetime(2026, 8, 21, 9, 0, tzinfo=UTC))
    status_reporter = reporter(url, clock)
    status_reporter.start()
    try:
        status_reporter.cycle_started()
        assert read_status(url).state == "polling"

        clock.value += timedelta(seconds=1)
        status_reporter.cycle_succeeded(
            PollResult(
                logged_in=True,
                chats_seen=2,
                messages_seen=3,
                messages_processed=2,
            )
        )
        status = read_status(url)
        assert status.state == "idle"
        assert status.source_logged_in is True
        assert status.messages_seen == 3
        assert status.messages_processed == 2
        assert status.last_success_at is not None

        clock.value += timedelta(seconds=1)
        status_reporter.cycle_succeeded(PollResult(logged_in=False))
        status = read_status(url)
        assert status.state == "degraded"
        assert status.last_error_code == "wechat_not_logged_in"

        clock.value += timedelta(seconds=1)
        status_reporter.cycle_started()
        status = read_status(url)
        assert status.state == "polling"
        assert status.last_error_code == "wechat_not_logged_in"
    finally:
        status_reporter.stop()

    assert read_status(url).state == "stopped"


def test_status_storage_failure_fences_the_worker(tmp_path: Path) -> None:
    url = database_url(tmp_path)
    clock = MutableClock(datetime(2026, 8, 21, 9, 0, tzinfo=UTC))
    status_reporter = reporter(url, clock)
    status_reporter.start()
    engine = create_database_engine(url)
    try:
        RuntimeWorkerStatus.__table__.drop(engine)
        with pytest.raises(WorkerLeaseLostError):
            status_reporter.cycle_started()
    finally:
        engine.dispose()
        status_reporter.stop()


def test_worker_cleanup_failure_does_not_hide_poll_failure(tmp_path: Path) -> None:
    settings = Settings(database=DatabaseSettings(url=database_url(tmp_path)))
    status_reporter = CleanupFailingReporter()

    def poll_once(candidate: Settings) -> PollResult:
        assert candidate is settings
        raise WechatRuntimeDisabledError()

    with pytest.raises(WechatRuntimeDisabledError):
        run_worker(settings, poll_once=poll_once, status_reporter=status_reporter)  # type: ignore[arg-type]


def test_worker_uses_bounded_backoff_and_resets_after_success(tmp_path: Path) -> None:
    settings = Settings(
        database=DatabaseSettings(url=database_url(tmp_path)),
        runtime=RuntimeSettings(
            polling_interval_seconds=1,
            polling_retry_max_seconds=4,
        ),
    )
    stop_event = RecordingEvent()
    status_reporter = RecordingReporter()
    attempts = 0

    def poll_once(candidate: Settings) -> PollResult:
        nonlocal attempts
        assert candidate is settings
        attempts += 1
        if attempts <= 3:
            raise RuntimeError("temporary failure")
        stop_event.set()
        return PollResult(logged_in=True)

    run_worker(
        settings,
        stop_event=stop_event,
        poll_once=poll_once,
        status_reporter=status_reporter,  # type: ignore[arg-type]
    )

    assert stop_event.wait_timeouts == [1.0, 2.0, 4.0, 1.0]
    assert status_reporter.calls[0] == "start"
    assert status_reporter.calls[-1] == "stop"
    assert status_reporter.calls.count(("cycle_failed", "poll_cycle_failed")) == 3
    assert any(
        isinstance(call, tuple) and call[0] == "cycle_succeeded" for call in status_reporter.calls
    )


def test_worker_backs_off_for_failures_returned_in_poll_result(tmp_path: Path) -> None:
    settings = Settings(
        database=DatabaseSettings(url=database_url(tmp_path)),
        runtime=RuntimeSettings(
            polling_interval_seconds=1,
            polling_retry_max_seconds=4,
        ),
    )
    stop_event = RecordingEvent()
    status_reporter = RecordingReporter()
    attempts = 0

    def poll_once(candidate: Settings) -> PollResult:
        nonlocal attempts
        assert candidate is settings
        attempts += 1
        if attempts <= 3:
            return PollResult(logged_in=True, chats_failed=1)
        stop_event.set()
        return PollResult(logged_in=True)

    run_worker(
        settings,
        stop_event=stop_event,
        poll_once=poll_once,
        status_reporter=status_reporter,  # type: ignore[arg-type]
    )

    assert stop_event.wait_timeouts == [1.0, 2.0, 4.0, 1.0]
