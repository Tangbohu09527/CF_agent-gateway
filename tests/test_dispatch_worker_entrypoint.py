from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from cf_agent_gateway.config import (
    DatabaseSettings,
    HermesSettings,
    Settings,
    WorkerSettings,
)
from cf_agent_gateway.runtime import dispatch_worker
from cf_agent_gateway.runtime.errors import (
    DispatchWorkerDisabledError,
    HermesAPIKeyEnvironmentError,
    HermesClientInitializationError,
    HermesRuntimeDisabledError,
)


def enabled_settings() -> Settings:
    return Settings(
        database=DatabaseSettings(url="sqlite+pysqlite:///:memory:"),
        hermes=HermesSettings(
            enabled=True,
            base_url="https://hermes.test",
            model="hermes-worker-test",
        ),
        worker=WorkerSettings(
            enabled=True,
            concurrency=3,
            lease_seconds=12,
            retry_limit=2,
        ),
    )


@pytest.mark.parametrize(
    ("settings", "error_type"),
    [
        (Settings(), DispatchWorkerDisabledError),
        (Settings(worker=WorkerSettings(enabled=True)), HermesRuntimeDisabledError),
    ],
)
def test_runtime_enablement_fails_before_reading_credentials(
    settings: Settings,
    error_type: type[Exception],
) -> None:
    def forbidden_environment_read(name: str) -> str | None:
        raise AssertionError(f"environment must not be read: {name}")

    with pytest.raises(error_type):
        dispatch_worker.run_dispatch_worker(
            settings,
            stop_event=Event(),
            environment_reader=forbidden_environment_read,
        )


def test_runtime_requires_hermes_api_key_before_creating_resources() -> None:
    engine_calls = 0

    def forbidden_engine(url: str) -> Any:
        nonlocal engine_calls
        engine_calls += 1
        raise AssertionError(f"engine must not be created: {url}")

    with pytest.raises(HermesAPIKeyEnvironmentError) as caught:
        dispatch_worker.run_dispatch_worker(
            enabled_settings(),
            stop_event=Event(),
            engine_factory=forbidden_engine,  # type: ignore[arg-type]
            environment_reader=lambda name: None,
        )

    assert caught.value.environment_variable == "HERMES_API_KEY"
    assert engine_calls == 0


def test_runtime_sanitizes_hermes_client_initialization_failure() -> None:
    secret = "secret-hermes-api-key"

    def failing_client_factory(*, base_url: str, api_key: str, model: str) -> Any:
        del base_url, model
        raise RuntimeError(f"client rejected {api_key}")

    with pytest.raises(HermesClientInitializationError) as caught:
        dispatch_worker.run_dispatch_worker(
            enabled_settings(),
            stop_event=Event(),
            hermes_client_factory=failing_client_factory,
            environment_reader=lambda name: secret,
        )

    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_runtime_builds_and_runs_worker_with_configured_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = enabled_settings()
    stop_event = Event()
    events: list[object] = []
    session_factory_marker = object()
    sender_factory_marker = object()

    class TrackingClient:
        def close(self) -> None:
            events.append("client.close")

    class TrackingEngine:
        def dispose(self) -> None:
            events.append("engine.dispose")

    class TrackingWorker:
        def run(
            self,
            *,
            stop_event: Event,
            concurrency: int,
        ) -> None:
            events.append(("run", stop_event, concurrency))

    client = TrackingClient()
    engine = TrackingEngine()

    def client_factory(*, base_url: str, api_key: str, model: str) -> TrackingClient:
        events.append(("client", base_url, api_key, model))
        return client

    def engine_factory(url: str) -> TrackingEngine:
        events.append(("engine", url))
        return engine

    def initialize_database(candidate: object) -> None:
        assert candidate is engine
        events.append("initialize_database")

    def create_session_factory(candidate: object) -> object:
        assert candidate is engine
        events.append("session_factory")
        return session_factory_marker

    def build_worker(
        candidate: Settings,
        *,
        session_factory: object,
        hermes_client: object,
        sender_factory: object,
    ) -> TrackingWorker:
        assert candidate is settings
        assert session_factory is session_factory_marker
        assert hermes_client is client
        assert sender_factory is sender_factory_marker
        events.append(
            (
                "build",
                candidate.worker.lease_seconds,
                candidate.worker.retry_limit,
            )
        )
        return TrackingWorker()

    monkeypatch.setattr(dispatch_worker, "initialize_database", initialize_database)
    monkeypatch.setattr(
        dispatch_worker,
        "create_database_session_factory",
        create_session_factory,
    )
    monkeypatch.setattr(dispatch_worker, "build_dispatch_worker", build_worker)

    dispatch_worker.run_dispatch_worker(
        settings,
        stop_event=stop_event,
        hermes_client_factory=client_factory,
        sender_factory=sender_factory_marker,  # type: ignore[arg-type]
        engine_factory=engine_factory,  # type: ignore[arg-type]
        environment_reader=lambda name: "worker-api-key",
    )

    assert events == [
        ("client", "https://hermes.test", "worker-api-key", "hermes-worker-test"),
        ("engine", "sqlite+pysqlite:///:memory:"),
        "initialize_database",
        "session_factory",
        ("build", 12.0, 2),
        ("run", stop_event, 3),
        "client.close",
        "engine.dispose",
    ]


def test_python_module_entrypoint_exits_two_when_worker_is_disabled(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("worker:\n  enabled: false\n", encoding="utf-8")
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
        [sys.executable, "-m", "cf_agent_gateway.runtime.dispatch_worker"],
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
    assert payloads[-1]["message"] == "dispatch worker failed"
    assert payloads[-1]["error_code"] == "dispatch_worker_disabled"
