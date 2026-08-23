import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from cf_agent_gateway.config import (
    DatabaseSettings,
    HermesSettings,
    Settings,
    WechatSettings,
    WorkerSettings,
)
from cf_agent_gateway.gateway import app as gateway_app
from cf_agent_gateway.runtime.health import (
    DELIVERY_HEARTBEAT_PATH_ENV,
    DISPATCH_HEARTBEAT_PATH_ENV,
    WECHAT_HEARTBEAT_PATH_ENV,
    RuntimeHealthService,
)
from cf_agent_gateway.runtime.heartbeat import FileHeartbeat


def _serialized_sql_parameters(
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    return [
        {
            key: value.isoformat(sep=" ") if isinstance(value, datetime) else value
            for key, value in row.items()
        }
        for row in rows
    ]


def _seed_runtime_health_graph(client: TestClient, *, now: datetime) -> None:
    engine = client.app.state.database_engine
    threads = (
        "thread-blocked",
        "thread-uncertain",
        "thread-dead",
        "thread-stale",
        "thread-live",
        "thread-missing",
        "thread-delivery",
    )
    messages = [
        {
            "id": index,
            "event_id": f"health-event-{index}",
            "source_message_id": f"health-source-{index}",
            "timestamp": now - timedelta(seconds=600 - index),
        }
        for index in range(1, 12)
    ]
    dispatches = [
        {
            "id": 1,
            "message_id": 1,
            "thread_id": "thread-blocked",
            "status": "uncertain",
            "attempt_count": 1,
            "claim_token": None,
            "claimed_at": now - timedelta(seconds=190),
            "lease_expires_at": None,
            "completed_at": now - timedelta(seconds=180),
            "last_error_code": "hermes_timeout",
            "created_at": now - timedelta(seconds=300),
        },
        {
            "id": 2,
            "message_id": 2,
            "thread_id": "thread-blocked",
            "status": "queued",
            "attempt_count": 0,
            "claim_token": None,
            "claimed_at": None,
            "lease_expires_at": None,
            "completed_at": None,
            "last_error_code": None,
            "created_at": now - timedelta(seconds=120),
        },
        {
            "id": 3,
            "message_id": 3,
            "thread_id": "thread-uncertain",
            "status": "uncertain",
            "attempt_count": 1,
            "claim_token": None,
            "claimed_at": now - timedelta(seconds=100),
            "lease_expires_at": None,
            "completed_at": now - timedelta(seconds=90),
            "last_error_code": "hermes_timeout",
            "created_at": now - timedelta(seconds=200),
        },
        {
            "id": 4,
            "message_id": 4,
            "thread_id": "thread-dead",
            "status": "dead",
            "attempt_count": 3,
            "claim_token": None,
            "claimed_at": now - timedelta(seconds=70),
            "lease_expires_at": None,
            "completed_at": now - timedelta(seconds=60),
            "last_error_code": "retry_exhausted",
            "created_at": now - timedelta(seconds=400),
        },
        {
            "id": 5,
            "message_id": 5,
            "thread_id": "thread-stale",
            "status": "running",
            "attempt_count": 1,
            "claim_token": "stale-claim",
            "claimed_at": now - timedelta(seconds=200),
            "lease_expires_at": now - timedelta(seconds=1),
            "completed_at": None,
            "last_error_code": None,
            "created_at": now - timedelta(seconds=240),
        },
        {
            "id": 6,
            "message_id": 6,
            "thread_id": "thread-live",
            "status": "running",
            "attempt_count": 1,
            "claim_token": "live-claim",
            "claimed_at": now - timedelta(seconds=20),
            "lease_expires_at": now + timedelta(seconds=30),
            "completed_at": None,
            "last_error_code": None,
            "created_at": now - timedelta(seconds=210),
        },
    ]
    for index in range(7, 12):
        dispatches.append(
            {
                "id": index,
                "message_id": index,
                "thread_id": ("thread-missing" if index == 7 else "thread-delivery"),
                "status": "success",
                "attempt_count": 1,
                "claim_token": None,
                "claimed_at": now - timedelta(seconds=50 - index),
                "lease_expires_at": None,
                "completed_at": now - timedelta(seconds=40 - index),
                "last_error_code": None,
                "created_at": now - timedelta(seconds=100 + index),
            }
        )

    responses = [
        {
            "response_id": f"health-response-{index}",
            "message_id": index,
            "thread_id": "thread-delivery",
            "created_at": now - timedelta(seconds=350 - index),
        }
        for index in range(8, 12)
    ]
    deliveries = [
        {
            "id": 1,
            "response_id": "health-response-8",
            "status": "uncertain",
            "attempt_count": 1,
            "claim_token": None,
            "claimed_at": now - timedelta(seconds=160),
            "completed_at": now - timedelta(seconds=150),
            "last_error_code": "wechat_timeout",
            "created_at": now - timedelta(seconds=150),
        },
        {
            "id": 2,
            "response_id": "health-response-9",
            "status": "delivering",
            "attempt_count": 1,
            "claim_token": "stale-delivery-claim",
            "claimed_at": now - timedelta(seconds=301),
            "completed_at": None,
            "last_error_code": None,
            "created_at": now - timedelta(seconds=250),
        },
        {
            "id": 3,
            "response_id": "health-response-10",
            "status": "delivering",
            "attempt_count": 1,
            "claim_token": "live-delivery-claim",
            "claimed_at": now - timedelta(seconds=10),
            "completed_at": None,
            "last_error_code": None,
            "created_at": now - timedelta(seconds=320),
        },
        {
            "id": 4,
            "response_id": "health-response-11",
            "status": "delivered",
            "attempt_count": 1,
            "claim_token": None,
            "claimed_at": now - timedelta(seconds=510),
            "completed_at": now - timedelta(seconds=500),
            "last_error_code": None,
            "created_at": now - timedelta(seconds=500),
        },
    ]

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO conversations "
                "(id, source, source_account_id, conversation_id, conversation_type) "
                "VALUES (1, 'wechat', 'health-account', 'health-conversation', 'private')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO enterprise_identities (id, employee_id) "
                "VALUES ('health-identity', 'health-employee')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO employee_workspaces (id, enterprise_identity_id) "
                "VALUES ('health-workspace', 'health-identity')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO ai_threads "
                "(id, workspace_id, thread_type, thread_key) "
                "VALUES (:id, 'health-workspace', 'private', :thread_key)"
            ),
            [{"id": thread_id, "thread_key": f"health:{thread_id}"} for thread_id in threads],
        )
        connection.execute(
            text(
                "INSERT INTO messages "
                "(id, event_id, source, source_account_id, source_message_id, "
                "conversation_id, conversation_type, is_mentioned, is_self, "
                "sender_type, sender_id, message_type, content, timestamp, "
                "direction, occurred_at, received_at) "
                "VALUES (:id, :event_id, 'wechat', 'health-account', "
                ":source_message_id, 'health-conversation', 'private', NULL, false, "
                "'human', 'health-sender', 'text', 'health fixture', :timestamp, "
                "'inbound', :timestamp, :timestamp)"
            ),
            _serialized_sql_parameters(messages),
        )
        connection.execute(
            text(
                "INSERT INTO hermes_dispatch_records "
                "(id, idempotency_key, message_id, enterprise_identity_id, "
                "workspace_id, ai_thread_id, status, attempt_count, claim_token, "
                "claimed_at, lease_expires_at, completed_at, last_error_code, "
                "created_at, updated_at) "
                "VALUES (:id, 'health-dispatch-' || :id, :message_id, "
                "'health-identity', 'health-workspace', :thread_id, :status, "
                ":attempt_count, :claim_token, :claimed_at, :lease_expires_at, "
                ":completed_at, :last_error_code, :created_at, :created_at)"
            ),
            _serialized_sql_parameters(dispatches),
        )
        connection.execute(
            text(
                "INSERT INTO hermes_dispatch_responses "
                "(dispatch_record_id, hermes_response_id, assistant_content) "
                "VALUES (:dispatch_id, :hermes_response_id, 'health response')"
            ),
            [
                {
                    "dispatch_id": index,
                    "hermes_response_id": f"health-upstream-{index}",
                }
                for index in range(7, 12)
            ],
        )
        connection.execute(
            text(
                "INSERT INTO hermes_responses "
                "(response_id, idempotency_key, message_id, workspace_id, "
                "ai_thread_id, content_sha256, part_count, status, generated_at, "
                "created_at, updated_at) "
                "VALUES (:response_id, 'health-response-key-' || :message_id, "
                ":message_id, 'health-workspace', :thread_id, :content_sha256, "
                "1, 'generated', :created_at, :created_at, :created_at)"
            ),
            _serialized_sql_parameters(
                [
                    {
                        **response,
                        "content_sha256": f"{response['message_id']:064d}",
                    }
                    for response in responses
                ]
            ),
        )
        connection.execute(
            text(
                "INSERT INTO hermes_response_parts "
                "(response_id, ordinal, part_type, text, artifact_id) "
                "VALUES (:response_id, 0, 'text', 'health response', NULL)"
            ),
            responses,
        )
        connection.execute(
            text(
                "INSERT INTO delivery_outbox "
                "(id, idempotency_key, response_id, channel, account_id, "
                "conversation_id, target_key, status, next_part_ordinal, "
                "attempt_count, available_at, claim_token, claimed_at, completed_at, "
                "last_error_code, created_at, updated_at) "
                "VALUES (:id, 'health-delivery-' || :id, :response_id, 'wechat', "
                "'health-account', 'health-conversation', :target_key, :status, 0, "
                ":attempt_count, :created_at, :claim_token, :claimed_at, "
                ":completed_at, :last_error_code, :created_at, :created_at)"
            ),
            _serialized_sql_parameters(
                [
                    {
                        **delivery,
                        "target_key": f"{delivery['id']:064d}",
                    }
                    for delivery in deliveries
                ]
            ),
        )


def test_health(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}


def test_health_rejects_post(client: TestClient) -> None:
    response = client.post("/health")

    assert response.status_code == 405


def test_runtime_health_separates_business_components(client: TestClient) -> None:
    response = client.get("/health/runtime")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["components"]["database"] == {"status": "ok"}
    assert body["components"]["migration_schema"] == {"status": "ok"}
    assert body["components"]["wechat_worker"] == {"status": "disabled"}
    assert body["components"]["dispatch_worker"] == {"status": "disabled"}
    assert body["components"]["delivery_worker"] == {"status": "disabled"}
    assert body["dispatch"]["uncertain"] == 0
    assert body["dispatch"]["dead"] == 0
    assert body["dispatch"]["blocked_threads"] == 0
    assert body["delivery"]["missing_delivery"] == 0


def test_runtime_health_reports_populated_queue_metrics(client: TestClient) -> None:
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    _seed_runtime_health_graph(client, now=now)
    client.app.state.runtime_health = RuntimeHealthService(
        client.app.state.database_engine,
        client.app.state.settings,
        clock=lambda: now,
    )

    response = client.get("/health/runtime")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["dispatch"] == {
        "queued": 1,
        "running": 2,
        "failed": 0,
        "uncertain": 2,
        "dead": 1,
        "stale_running": 1,
        "blocked_threads": 1,
        "missing_delivery": 1,
        "oldest_uncertain_age_seconds": 180.0,
        "oldest_backlog_age_seconds": 300.0,
    }
    assert body["delivery"] == {
        "queued": 0,
        "delivering": 2,
        "delivered": 1,
        "failed": 0,
        "uncertain": 1,
        "stale_delivering": 1,
        "missing_delivery": 1,
        "oldest_backlog_age_seconds": 320.0,
    }


def test_readiness_checks_the_database(client: TestClient) -> None:
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ready"}


def test_readiness_rejects_post(client: TestClient) -> None:
    response = client.post("/ready")

    assert response.status_code == 405


def test_readiness_is_unavailable_before_startup_completes(client: TestClient) -> None:
    client.app.state.ready = False
    try:
        response = client.get("/ready")
    finally:
        client.app.state.ready = True

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}


def test_readiness_redacts_database_failures(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "postgresql://user:secret-password@database/gateway"

    class NotReadyMonitor:
        sensitive_context = secret

        def is_ready(self) -> bool:
            return False

    original_monitor = client.app.state.database_readiness
    client.app.state.database_readiness = NotReadyMonitor()
    try:
        with caplog.at_level(logging.WARNING):
            response = client.get("/ready")
    finally:
        client.app.state.database_readiness = original_monitor

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert secret not in caplog.text
    failure = next(
        record
        for record in caplog.records
        if record.name == "cf_agent_gateway.gateway.routes"
        and record.getMessage() == "readiness check failed"
    )
    assert failure.fields == {"error_code": "database_unavailable"}  # type: ignore[attr-defined]


def test_liveness_remains_available_when_readiness_fails(client: TestClient) -> None:
    client.app.state.ready = False
    try:
        response = client.get("/health")
    finally:
        client.app.state.ready = True

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_runtime_health_reads_three_worker_heartbeats_and_wechat_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    paths = {
        WECHAT_HEARTBEAT_PATH_ENV: tmp_path / "wechat.json",
        DISPATCH_HEARTBEAT_PATH_ENV: tmp_path / "dispatch.json",
        DELIVERY_HEARTBEAT_PATH_ENV: tmp_path / "delivery.json",
    }
    for name, path in paths.items():
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv("HERMES_API_KEY", "test-only-hermes-key")
    FileHeartbeat(paths[WECHAT_HEARTBEAT_PATH_ENV], clock=lambda: now).write(
        "running",
        details={"phase": "waiting", "wechat_auth": "logged_in"},
    )
    FileHeartbeat(paths[DISPATCH_HEARTBEAT_PATH_ENV], clock=lambda: now).write(
        "running",
        details={
            "phase": "dispatching",
            "last_operation_succeeded": True,
            "last_operation_at": now.isoformat().replace("+00:00", "Z"),
        },
    )
    FileHeartbeat(paths[DELIVERY_HEARTBEAT_PATH_ENV], clock=lambda: now).write(
        "running",
        details={"phase": "delivery"},
    )
    settings = Settings(
        database=DatabaseSettings(url="sqlite+pysqlite:///:memory:"),
        wechat=WechatSettings(enabled=True),
        hermes=HermesSettings(enabled=True, base_url="https://hermes.test"),
        worker=WorkerSettings(enabled=True),
    )
    with TestClient(gateway_app.create_app(settings)) as runtime_client:
        response = runtime_client.get("/health/runtime")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["components"]["wechat_auth"] == {"status": "logged_in"}
    assert body["components"]["hermes"] == {
        "status": "ok",
        "configuration": "configured",
        "connectivity": "last_operation_succeeded",
    }
    assert body["components"]["wechat_worker"]["status"] == "ok"
    assert body["components"]["dispatch_worker"]["status"] == "ok"
    assert body["components"]["delivery_worker"]["status"] == "ok"


def test_runtime_health_does_not_treat_dispatch_liveness_as_hermes_connectivity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    dispatch_path = tmp_path / "dispatch.json"
    monkeypatch.setenv(DISPATCH_HEARTBEAT_PATH_ENV, str(dispatch_path))
    monkeypatch.setenv("HERMES_API_KEY", "test-only-hermes-key")
    FileHeartbeat(dispatch_path, clock=lambda: now).write(
        "running",
        details={"phase": "dispatching"},
    )
    settings = Settings(
        database=DatabaseSettings(url="sqlite+pysqlite:///:memory:"),
        hermes=HermesSettings(enabled=True, base_url="https://hermes.test"),
        worker=WorkerSettings(enabled=True),
    )

    with TestClient(gateway_app.create_app(settings)) as runtime_client:
        response = runtime_client.get("/health/runtime")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["components"]["dispatch_worker"]["status"] == "ok"
    assert body["components"]["hermes"] == {
        "status": "degraded",
        "configuration": "configured",
        "connectivity": "unverified",
    }


def test_runtime_health_expires_stale_hermes_operation_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    dispatch_path = tmp_path / "dispatch.json"
    monkeypatch.setenv(DISPATCH_HEARTBEAT_PATH_ENV, str(dispatch_path))
    monkeypatch.setenv("HERMES_API_KEY", "test-only-hermes-key")
    FileHeartbeat(dispatch_path, clock=lambda: now).write(
        "running",
        details={
            "phase": "dispatching",
            "last_operation_succeeded": True,
            "last_operation_at": (now - timedelta(seconds=31)).isoformat().replace("+00:00", "Z"),
        },
    )
    settings = Settings(
        database=DatabaseSettings(url="sqlite+pysqlite:///:memory:"),
        hermes=HermesSettings(enabled=True, base_url="https://hermes.test"),
        worker=WorkerSettings(enabled=True),
    )

    with TestClient(gateway_app.create_app(settings)) as runtime_client:
        runtime_client.app.state.runtime_health = RuntimeHealthService(
            runtime_client.app.state.database_engine,
            settings,
            clock=lambda: now,
        )
        response = runtime_client.get("/health/runtime")

    assert response.status_code == 200
    assert response.json()["components"]["dispatch_worker"]["status"] == "ok"
    assert response.json()["components"]["hermes"] == {
        "status": "degraded",
        "configuration": "configured",
        "connectivity": "unverified",
    }


def test_runtime_health_reports_stale_worker_without_breaking_liveness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = datetime.now(UTC) - timedelta(minutes=5)
    heartbeat_path = tmp_path / "wechat.json"
    monkeypatch.setenv(WECHAT_HEARTBEAT_PATH_ENV, str(heartbeat_path))
    FileHeartbeat(heartbeat_path, clock=lambda: stale).write("running")
    settings = Settings(
        database=DatabaseSettings(url="sqlite+pysqlite:///:memory:"),
        wechat=WechatSettings(enabled=True),
    )
    with TestClient(gateway_app.create_app(settings)) as runtime_client:
        runtime_response = runtime_client.get("/health/runtime")
        liveness = runtime_client.get("/health")

    assert runtime_response.status_code == 200
    assert runtime_response.json()["status"] == "degraded"
    assert runtime_response.json()["components"]["wechat_worker"] == {"status": "stale_or_invalid"}
    assert liveness.status_code == 200


@pytest.mark.parametrize(
    ("wechat_auth", "last_cycle_succeeded"),
    [("logged_out", True), ("logged_in", False)],
)
def test_runtime_health_degrades_for_unavailable_wechat_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wechat_auth: str,
    last_cycle_succeeded: bool,
) -> None:
    now = datetime.now(UTC)
    wechat_path = tmp_path / "wechat.json"
    delivery_path = tmp_path / "delivery.json"
    monkeypatch.setenv(WECHAT_HEARTBEAT_PATH_ENV, str(wechat_path))
    monkeypatch.setenv(DELIVERY_HEARTBEAT_PATH_ENV, str(delivery_path))
    FileHeartbeat(wechat_path, clock=lambda: now).write(
        "running",
        details={
            "phase": "waiting",
            "wechat_auth": wechat_auth,
            "last_cycle_succeeded": last_cycle_succeeded,
        },
    )
    FileHeartbeat(delivery_path, clock=lambda: now).write(
        "running",
        details={"phase": "delivery"},
    )
    settings = Settings(
        database=DatabaseSettings(url="sqlite+pysqlite:///:memory:"),
        wechat=WechatSettings(enabled=True),
    )

    with TestClient(gateway_app.create_app(settings)) as runtime_client:
        response = runtime_client.get("/health/runtime")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["components"]["wechat_auth"] == {"status": wechat_auth}
    expected_worker_status = "ok" if last_cycle_succeeded else "degraded"
    assert response.json()["components"]["wechat_worker"]["status"] == (expected_worker_status)


def test_gateway_production_startup_uses_read_only_migration_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    checked_engines: list[object] = []
    monitored_engines: list[object] = []

    class RecordingMonitor:
        def __init__(self, engine: object) -> None:
            monitored_engines.append(engine)
            events.append("monitor")

        def start(self) -> None:
            events.append("monitor.start")

        def stop(self) -> None:
            events.append("monitor.stop")

        def is_ready(self) -> bool:
            return True

    def check_database(engine: object) -> None:
        checked_engines.append(engine)
        events.append("check")

    def forbidden_migration(engine: object) -> None:
        del engine
        raise AssertionError("production gateway startup must not run migrations")

    monkeypatch.setattr(gateway_app, "database_startup_check_enabled", lambda: True)
    monkeypatch.setattr(gateway_app, "check_database_migrations", check_database)
    monkeypatch.setattr(gateway_app, "initialize_database", forbidden_migration)
    monkeypatch.setattr(gateway_app, "DatabaseReadinessMonitor", RecordingMonitor)

    settings = Settings(database=DatabaseSettings(url="sqlite+pysqlite:///:memory:"))
    with TestClient(gateway_app.create_app(settings)) as production_client:
        assert production_client.get("/ready").json() == {"status": "ready"}

    assert checked_engines == monitored_engines
    assert events == ["check", "monitor", "monitor.start", "monitor.stop"]
