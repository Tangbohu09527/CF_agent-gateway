from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

from cf_agent_gateway import migration
from cf_agent_gateway.database import create_database_engine, initialize_database

ALEMBIC_CONFIG_PATH = Path(__file__).resolve().parents[1] / "alembic.ini"
BUSINESS_TABLES = frozenset(
    {
        "ai_threads",
        "attachments",
        "conversations",
        "employee_workspaces",
        "enterprise_identities",
        "gateway_access_policies",
        "messages",
        "source_identity_mappings",
        "thread_source_bindings",
        "user_access_policies",
        "wechat_sync_checkpoints",
    }
)
ARCHIVE_TABLES = frozenset({"message_delivery_attempts", "message_raw_payloads"})
SCHEMA_TABLES = BUSINESS_TABLES | ARCHIVE_TABLES
FOUNDATION_REVISION = "20260806_01"


def _head_revision() -> str:
    migration_config = migration.create_migration_config()
    head_revision = ScriptDirectory.from_config(migration_config).get_current_head()
    assert head_revision is not None
    return head_revision


def test_upgrade_preserves_foundation_marker_then_applies_schema_chain() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        assert migration.get_schema_version(engine) is None

        migration.upgrade_database(engine, FOUNDATION_REVISION)

        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
        assert migration.get_schema_version(engine) == FOUNDATION_REVISION

        migration.upgrade_database(engine)

        assert set(inspect(engine).get_table_names()) == SCHEMA_TABLES | {"alembic_version"}
        assert migration.get_schema_version(engine) == _head_revision()

        migration.upgrade_database(engine)

        assert set(inspect(engine).get_table_names()) == SCHEMA_TABLES | {"alembic_version"}
        assert migration.get_schema_version(engine) == _head_revision()
    finally:
        engine.dispose()


def test_upgrade_preserves_existing_tables_and_data() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE existing_data (id INTEGER PRIMARY KEY)"))
            connection.execute(text("INSERT INTO existing_data (id) VALUES (7)"))

        migration.upgrade_database(engine)

        assert set(inspect(engine).get_table_names()) == SCHEMA_TABLES | {
            "alembic_version",
            "existing_data",
        }
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT id FROM existing_data")) == 7
    finally:
        engine.dispose()


def test_upgrade_is_idempotent_after_database_initialization() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        initialize_database(engine)
        assert set(inspect(engine).get_table_names()) == SCHEMA_TABLES | {"alembic_version"}

        migration.upgrade_database(engine)

        assert set(inspect(engine).get_table_names()) == SCHEMA_TABLES | {"alembic_version"}
        assert migration.get_schema_version(engine) == _head_revision()
    finally:
        engine.dispose()


def test_migrate_database_creates_file_sqlite_parent_and_persists_version(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "nested" / "gateway.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"

    schema_version = migration.migrate_database(database_url)

    assert database_path.is_file()
    assert schema_version == _head_revision()
    engine = create_database_engine(database_url)
    try:
        assert migration.get_schema_version(engine) == _head_revision()
        assert set(inspect(engine).get_table_names()) == SCHEMA_TABLES | {"alembic_version"}
    finally:
        engine.dispose()


def test_postgresql_offline_upgrade_emits_complete_schema_ddl() -> None:
    output = StringIO()
    migration_config = Config(str(ALEMBIC_CONFIG_PATH), output_buffer=output)
    migration_config.attributes["database_url"] = (
        "postgresql+psycopg://user:password@localhost/gateway"
    )

    command.upgrade(migration_config, "head", sql=True)

    rendered_sql = output.getvalue().lower()
    assert "create table alembic_version" in rendered_sql
    assert _head_revision().lower() in rendered_sql
    assert all(table_name in rendered_sql for table_name in SCHEMA_TABLES)


def test_sqlite_online_upgrade_applies_batch_migration(tmp_path: Path) -> None:
    database_path = tmp_path / "batch.db"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    migration_config = Config(str(ALEMBIC_CONFIG_PATH))
    migration_config.attributes["configure_logger"] = False
    migration_config.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(migration_config, "20260806_0001")

    engine = create_database_engine(database_url)
    try:
        baseline_columns = {column["name"] for column in inspect(engine).get_columns("messages")}
        assert {"direction", "occurred_at", "received_at"}.isdisjoint(baseline_columns)
    finally:
        engine.dispose()

    command.upgrade(migration_config, "head")

    engine = create_database_engine(database_url)
    try:
        inspector = inspect(engine)
        archive_columns = {column["name"] for column in inspector.get_columns("messages")}
        assert {"direction", "occurred_at", "received_at"}.issubset(archive_columns)
        assert {"message_delivery_attempts", "message_raw_payloads"}.issubset(
            inspector.get_table_names()
        )
        assert "ck_message_direction" in {
            constraint["name"] for constraint in inspector.get_check_constraints("messages")
        }
        assert migration.get_schema_version(engine) == _head_revision()
    finally:
        engine.dispose()


def test_migration_cli_reads_gateway_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_path = tmp_path / "cli" / "gateway.db"
    application_config_path = tmp_path / "config.yaml"
    application_config_path.write_text(
        f'database:\n  url: "sqlite+pysqlite:///{database_path.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CF_GATEWAY_CONFIG", str(application_config_path))
    monkeypatch.delenv("CF_GATEWAY_ALEMBIC_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)

    assert migration.main() == 0

    output = json.loads(capsys.readouterr().out)
    assert output == {"schema_version": _head_revision()}
    assert database_path.is_file()


def test_migration_cli_returns_sanitized_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CF_GATEWAY_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.delenv("CF_GATEWAY_ALEMBIC_CONFIG", raising=False)

    assert migration.main() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"error_code": "database_migration_failed"}
