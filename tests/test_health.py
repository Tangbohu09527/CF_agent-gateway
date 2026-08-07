import logging

import pytest
from fastapi.testclient import TestClient

from cf_agent_gateway.config import DatabaseSettings, Settings
from cf_agent_gateway.gateway import app as gateway_app


def test_health(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}


def test_health_rejects_post(client: TestClient) -> None:
    response = client.post("/health")

    assert response.status_code == 405


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
