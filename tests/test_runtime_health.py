from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from cf_agent_gateway.config import (
    DatabaseSettings,
    HermesSettings,
    RuntimeSettings,
    Settings,
    WechatSettings,
)
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
    get_database_session,
    initialize_database,
)
from cf_agent_gateway.gateway.app import create_app
from cf_agent_gateway.runtime import health as health_module
from cf_agent_gateway.runtime.health import check_runtime_health
from cf_agent_gateway.runtime.models import RuntimeWorkerStatus

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    initialize_database(engine)
    session_factory = create_database_session_factory(engine)
    try:
        with session_factory() as database_session:
            yield database_session
    finally:
        engine.dispose()


@pytest.fixture
def enabled_settings() -> Settings:
    return Settings(
        runtime=RuntimeSettings(heartbeat_stale_after_seconds=30),
        wechat=WechatSettings(enabled=True),
        hermes=HermesSettings(enabled=True, base_url="https://hermes.example"),
    )


def healthy_worker() -> RuntimeWorkerStatus:
    return RuntimeWorkerStatus(
        worker_name="wechat",
        instance_id="worker-instance",
        process_id=123,
        state="idle",
        hermes_enabled=True,
        delivery_enabled=True,
        started_at=NOW - timedelta(minutes=1),
        heartbeat_at=NOW - timedelta(seconds=1),
        last_cycle_started_at=NOW - timedelta(seconds=2),
        last_cycle_completed_at=NOW - timedelta(seconds=1),
        last_success_at=NOW - timedelta(seconds=1),
        last_error_code=None,
        source_logged_in=True,
        chats_failed=0,
        messages_seen=1,
        messages_processed=1,
    )


def reachable(settings: Settings) -> str:
    del settings
    return "reachable"


def seed_ledger_target(session: Session) -> None:
    session.execute(
        text("INSERT INTO enterprise_identities (id, status) VALUES ('identity', 'active')")
    )
    session.execute(
        text(
            "INSERT INTO employee_workspaces (id, enterprise_identity_id, status) "
            "VALUES ('workspace', 'identity', 'active')"
        )
    )
    session.execute(
        text(
            "INSERT INTO ai_threads (id, workspace_id, thread_type, thread_key, status) "
            "VALUES ('thread', 'workspace', 'private', 'thread-key', 'active')"
        )
    )
    session.execute(
        text(
            "INSERT INTO conversations "
            "(source, source_account_id, conversation_id, conversation_type) "
            "VALUES ('wechat', 'account', 'conversation', 'private')"
        )
    )
    session.execute(
        text(
            "INSERT INTO messages "
            "(id, event_id, source, source_account_id, source_message_id, conversation_id, "
            "conversation_type, is_mentioned, is_self, sender_type, sender_id, message_type, "
            "content, timestamp) VALUES "
            "(999, 'event', 'wechat', 'account', 'source-message', 'conversation', "
            "'private', NULL, 0, 'human', 'sender', 'text', 'content', :timestamp)"
        ),
        {"timestamp": NOW},
    )


def test_disabled_runtime_components_are_healthy(session: Session) -> None:
    result = check_runtime_health(session, Settings(), clock=lambda: NOW)

    assert result.status == "ok"
    assert result.components.database.status == "ok"
    assert result.components.worker.status == "disabled"
    assert result.components.queue.status == "disabled"
    assert result.components.hermes.status == "disabled"
    assert result.components.delivery.status == "disabled"


def test_enabled_runtime_requires_a_fresh_successful_worker_heartbeat(
    session: Session,
    enabled_settings: Settings,
) -> None:
    session.add(healthy_worker())
    session.commit()

    result = check_runtime_health(
        session,
        enabled_settings,
        clock=lambda: NOW,
        hermes_probe=reachable,  # type: ignore[arg-type]
        delivery_probe=reachable,  # type: ignore[arg-type]
    )

    assert result.status == "ok"
    assert result.components.worker.status == "ok"
    assert result.components.hermes.connection == "reachable"
    assert result.components.delivery.connection == "reachable"
    assert session.scalar(text("PRAGMA busy_timeout")) == 30_000


def test_worker_configuration_drift_degrades_health(
    session: Session,
    enabled_settings: Settings,
) -> None:
    worker = healthy_worker()
    worker.hermes_enabled = False
    worker.delivery_enabled = False
    session.add(worker)
    session.commit()

    result = check_runtime_health(
        session,
        enabled_settings,
        clock=lambda: NOW,
        hermes_probe=reachable,  # type: ignore[arg-type]
        delivery_probe=reachable,  # type: ignore[arg-type]
    )

    assert result.status == "degraded"
    assert result.components.worker.status == "degraded"
    assert result.components.worker.configuration_matches is False


@pytest.mark.parametrize("stale_fact", ["polling_cycle", "last_success"])
def test_fresh_heartbeat_does_not_hide_stalled_business_loop(
    session: Session,
    enabled_settings: Settings,
    stale_fact: str,
) -> None:
    worker = healthy_worker()
    if stale_fact == "polling_cycle":
        worker.state = "polling"
        worker.last_cycle_started_at = NOW - timedelta(seconds=301)
    else:
        worker.last_success_at = NOW - timedelta(seconds=301)
    session.add(worker)
    session.commit()

    result = check_runtime_health(
        session,
        enabled_settings,
        clock=lambda: NOW,
        hermes_probe=reachable,  # type: ignore[arg-type]
        delivery_probe=reachable,  # type: ignore[arg-type]
    )

    assert result.status == "degraded"
    assert result.components.worker.status == "degraded"
    assert result.components.worker.heartbeat_at == NOW - timedelta(seconds=1)


def test_stale_worker_and_expired_dispatch_lease_degrade_health(
    session: Session,
    enabled_settings: Settings,
) -> None:
    worker = healthy_worker()
    worker.heartbeat_at = NOW - timedelta(seconds=31)
    session.add(worker)
    seed_ledger_target(session)
    session.execute(
        text(
            "INSERT INTO hermes_dispatch_records "
            "(message_id, workspace_id, ai_thread_id, status, attempt_count, lease_token, "
            "lease_expires_at, requested_hermes_thread_id) "
            "VALUES (999, 'workspace', 'thread', 'in_progress', 1, 'lease', :expires, 'h')"
        ),
        {"expires": NOW - timedelta(seconds=1)},
    )
    session.commit()

    result = check_runtime_health(
        session,
        enabled_settings,
        clock=lambda: NOW,
        hermes_probe=reachable,  # type: ignore[arg-type]
        delivery_probe=reachable,  # type: ignore[arg-type]
    )

    assert result.status == "degraded"
    assert result.components.worker.status == "degraded"
    assert result.components.queue.stale == 1
    assert result.components.queue.status == "degraded"
    assert result.components.hermes.operations.stale == 1
    assert result.components.hermes.status == "degraded"


def test_failed_dispatch_and_unreachable_dependencies_degrade_health(
    session: Session,
    enabled_settings: Settings,
) -> None:
    session.add(healthy_worker())
    seed_ledger_target(session)
    session.execute(
        text(
            "INSERT INTO hermes_dispatch_records "
            "(message_id, workspace_id, ai_thread_id, status, attempt_count, "
            "requested_hermes_thread_id, last_error_code) "
            "VALUES (999, 'workspace', 'thread', 'failed', 1, 'h', 'hermes_api_error')"
        )
    )
    session.commit()

    def unreachable(settings: Settings) -> str:
        del settings
        return "unreachable"

    result = check_runtime_health(
        session,
        enabled_settings,
        clock=lambda: NOW,
        hermes_probe=unreachable,  # type: ignore[arg-type]
        delivery_probe=unreachable,  # type: ignore[arg-type]
    )

    assert result.status == "degraded"
    assert result.components.queue.failed == 1
    assert result.components.hermes.operations.failed == 1
    assert result.components.hermes.connection == "unreachable"
    assert result.components.delivery.connection == "unreachable"


def test_missing_delivery_degrades_queue_and_delivery_health(
    session: Session,
    enabled_settings: Settings,
) -> None:
    session.add(healthy_worker())
    seed_ledger_target(session)
    session.execute(
        text(
            "INSERT INTO hermes_dispatch_records "
            "(message_id, workspace_id, ai_thread_id, status, attempt_count, "
            "requested_hermes_thread_id, result_hermes_thread_id, assistant_content) "
            "VALUES (999, 'workspace', 'thread', 'succeeded', 1, 'h', 'h-next', 'reply')"
        )
    )
    session.commit()

    result = check_runtime_health(
        session,
        enabled_settings,
        clock=lambda: NOW,
        hermes_probe=reachable,  # type: ignore[arg-type]
        delivery_probe=reachable,  # type: ignore[arg-type]
    )

    assert result.status == "degraded"
    assert result.components.queue.status == "degraded"
    assert result.components.queue.missing == 1
    assert result.components.delivery.status == "degraded"
    assert result.components.delivery.operations.missing == 1
    assert result.components.hermes.status == "ok"


def test_health_route_returns_503_for_enabled_runtime_without_worker() -> None:
    settings = Settings(
        database=DatabaseSettings(url="sqlite+pysqlite:///:memory:"),
        wechat=WechatSettings(enabled=True),
        hermes=HermesSettings(enabled=False),
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["components"]["worker"]["status"] == "degraded"


class FailingDatabaseSession:
    def __init__(self) -> None:
        self.rollback_calls = 0

    def get_bind(self) -> SimpleNamespace:
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def execute(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OperationalError(
            "SELECT set_config('statement_timeout', :timeout, true)",
            {},
            RuntimeError("controlled database connection failure"),
        )

    def rollback(self) -> None:
        self.rollback_calls += 1


def test_database_sql_failure_degrades_health_and_returns_503() -> None:
    settings = Settings(database=DatabaseSettings(url="sqlite+pysqlite:///:memory:"))
    failing_session = FailingDatabaseSession()

    result = check_runtime_health(
        failing_session,  # type: ignore[arg-type]
        settings,
        clock=lambda: NOW,
    )

    assert result.status == "degraded"
    assert result.components.database.status == "degraded"
    assert failing_session.rollback_calls == 1

    app = create_app(settings)

    def failed_database_dependency() -> Iterator[FailingDatabaseSession]:
        yield failing_session

    app.dependency_overrides[get_database_session] = failed_database_dependency
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["components"]["database"]["status"] == "degraded"
    assert failing_session.rollback_calls == 2


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (200, "reachable"),
        (204, "reachable"),
        (405, "reachable"),
        (400, "unreachable"),
        (401, "unreachable"),
        (404, "unreachable"),
        (408, "unreachable"),
        (429, "unreachable"),
        (503, "unreachable"),
    ],
)
def test_hermes_probe_checks_chat_endpoint_without_dispatching(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected: str,
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def head(self, endpoint: str, *, headers: dict[str, str]) -> SimpleNamespace:
            calls.append((endpoint, headers))
            return SimpleNamespace(status_code=status_code)

    monkeypatch.setenv("TEST_HERMES_KEY", "secret")
    monkeypatch.setattr(health_module.httpx, "Client", FakeClient)
    settings = Settings(
        hermes=HermesSettings(
            enabled=True,
            base_url="https://hermes.example/base/",
            api_key_env="TEST_HERMES_KEY",
        )
    )

    assert health_module._probe_hermes(settings) == expected
    assert calls == [
        (
            "https://hermes.example/base/v1/chat/completions",
            {
                "Accept": "application/json",
                "Authorization": "Bearer secret",
            },
        )
    ]


def test_health_probe_capacity_is_bounded_without_queuing_more_work(
    enabled_settings: Settings,
) -> None:
    release = Event()
    both_started = Event()
    lock = Lock()
    started = 0
    first_result: dict[str, str] = {}

    def blocking_probe(settings: Settings) -> str:
        nonlocal started
        del settings
        with lock:
            started += 1
            if started == 2:
                both_started.set()
        assert release.wait(timeout=5)
        return "reachable"

    def run_first_check() -> None:
        first_result.update(
            health_module._probe_dependencies(
                enabled_settings,
                hermes_probe=blocking_probe,  # type: ignore[arg-type]
                delivery_probe=blocking_probe,  # type: ignore[arg-type]
            )
        )

    first_check = Thread(target=run_first_check, daemon=True)
    first_check.start()
    try:
        assert both_started.wait(timeout=2)
        unexpected_calls = 0

        def must_not_run(settings: Settings) -> str:
            nonlocal unexpected_calls
            del settings
            unexpected_calls += 1
            return "reachable"

        saturated_result = health_module._probe_dependencies(
            enabled_settings,
            hermes_probe=must_not_run,  # type: ignore[arg-type]
            delivery_probe=must_not_run,  # type: ignore[arg-type]
        )

        assert saturated_result == {
            "hermes": "unreachable",
            "delivery": "unreachable",
        }
        assert unexpected_calls == 0
    finally:
        release.set()
        first_check.join(timeout=2)

    assert first_check.is_alive() is False
    assert first_result == {
        "hermes": "reachable",
        "delivery": "reachable",
    }
