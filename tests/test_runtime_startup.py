from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import inspect

from cf_agent_gateway.config import DatabaseSettings, Settings
from cf_agent_gateway.database import DatabaseSchemaError, create_database_engine
from cf_agent_gateway.runtime import startup
from cf_agent_gateway.runtime.startup import (
    STARTUP_MODE_ENV,
    StartupMigrationError,
    check_database_migrations,
    database_startup_check_enabled,
    prepare_database,
    run_database_startup,
)


def test_migrated_current_schema_passes_startup_check() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        prepare_database(engine, migrate=True)

        check_database_migrations(engine)
        assert inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_prepare_database_uses_the_selected_lifecycle_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    calls: list[tuple[str, object]] = []

    def migrate(candidate: object) -> None:
        calls.append(("migrate", candidate))

    def check(candidate: object) -> None:
        calls.append(("check", candidate))

    monkeypatch.setattr(startup, "initialize_database", migrate)
    monkeypatch.setattr(startup, "check_database_migrations", check)
    try:
        prepare_database(engine, migrate=True)
        assert calls == [("migrate", engine)]

        calls.clear()
        prepare_database(engine)
        assert calls == [("check", engine)]
    finally:
        engine.dispose()


def test_empty_database_fails_read_only_check_without_creating_tables() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        assert inspect(engine).get_table_names() == []

        with pytest.raises(DatabaseSchemaError, match="required head"):
            check_database_migrations(engine)

        assert inspect(engine).get_table_names() == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("fails", [False, True], ids=("success", "failure"))
def test_run_database_startup_always_disposes_engine(
    monkeypatch: pytest.MonkeyPatch,
    fails: bool,
) -> None:
    events: list[object] = []

    class RecordingEngine:
        def dispose(self) -> None:
            events.append("dispose")

    engine = RecordingEngine()

    def prepare(candidate: Any, *, migrate: bool) -> None:
        events.append(("prepare", candidate, migrate))
        if fails:
            raise StartupMigrationError("controlled startup failure")

    monkeypatch.setattr(startup, "create_database_engine", lambda url: engine)
    monkeypatch.setattr(startup, "prepare_database", prepare)
    settings = Settings(database=DatabaseSettings(url="sqlite:///ignored.db"))

    if fails:
        with pytest.raises(StartupMigrationError, match="controlled startup failure"):
            run_database_startup(settings, migrate=True)
    else:
        run_database_startup(settings, migrate=True)

    assert events == [("prepare", engine, True), "dispose"]


@pytest.mark.parametrize("value", [None, "", "   "])
def test_database_startup_check_is_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
) -> None:
    if value is None:
        monkeypatch.delenv(STARTUP_MODE_ENV, raising=False)
    else:
        monkeypatch.setenv(STARTUP_MODE_ENV, value)

    assert database_startup_check_enabled() is False


@pytest.mark.parametrize("value", ["check", " CHECK ", "Check"])
def test_database_startup_check_is_enabled_explicitly(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv(STARTUP_MODE_ENV, value)

    assert database_startup_check_enabled() is True


@pytest.mark.parametrize("value", ["migrate", "true", "1"])
def test_database_startup_check_rejects_unsupported_modes(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv(STARTUP_MODE_ENV, value)

    with pytest.raises(StartupMigrationError, match=f"{STARTUP_MODE_ENV} must be check"):
        database_startup_check_enabled()


def test_startup_cli_migrates_then_defaults_to_a_read_only_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "gateway.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    config_path = tmp_path / "production.yaml"
    config_path.write_text(
        f"""
database:
  url: {database_url}
logging:
  level: INFO
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("CF_GATEWAY_CONFIG", str(config_path))
    monkeypatch.delenv("CF_AGENT_GATEWAY_DATABASE_URL", raising=False)
    monkeypatch.setattr(startup, "configure_logging", lambda level: None)

    assert startup.main(["migrate"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "action": "migrate",
        "status": "ok",
    }

    assert startup.main([]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "action": "check",
        "status": "ok",
    }


def test_startup_cli_redacts_database_check_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "postgresql://user:secret-password@database/gateway"
    errors: list[tuple[str, dict[str, Any]]] = []

    def fail_startup(settings: Settings, *, migrate: bool) -> None:
        del settings, migrate
        raise RuntimeError(secret)

    def record_error(message: str, *, extra: dict[str, Any]) -> None:
        errors.append((message, extra))

    monkeypatch.setattr(startup, "load_settings", lambda path: Settings())
    monkeypatch.setattr(startup, "configure_logging", lambda level: None)
    monkeypatch.setattr(startup, "run_database_startup", fail_startup)
    monkeypatch.setattr(startup.logger, "error", record_error)

    assert startup.main([]) == 1
    assert errors == [
        (
            "database startup failed",
            {"fields": {"error_code": "database_migration_required"}},
        )
    ]
    assert secret not in str(errors)
