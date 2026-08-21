import sqlite3
from pathlib import Path
from time import monotonic

import pytest
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

import cf_agent_gateway.database as database_module
from cf_agent_gateway.database import (
    DATABASE_CONNECT_TIMEOUT_SECONDS,
    DatabaseSchemaError,
    create_database_engine,
    initialize_database,
)


def test_sqlite_engine() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT 1")) == 1
    finally:
        engine.dispose()


def test_sqlite_engine_enforces_foreign_keys() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
            assert connection.scalar(text("PRAGMA busy_timeout")) == 30_000
            assert connection.scalar(text("PRAGMA journal_mode")) == "memory"
    finally:
        engine.dispose()


def test_sqlite_file_engine_uses_wal_and_normal_sync(tmp_path: Path) -> None:
    database_path = tmp_path / "gateway.db"
    engine = create_database_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
            assert connection.scalar(text("PRAGMA busy_timeout")) == 30_000
            assert connection.scalar(text("PRAGMA journal_mode")) == "wal"
            assert connection.scalar(text("PRAGMA synchronous")) == 1
    finally:
        engine.dispose()


def test_sqlite_file_engine_bounds_pool_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(database_module, "DATABASE_CONNECT_TIMEOUT_SECONDS", 0.05)
    real_create_engine = database_module.create_engine

    def single_connection_engine(url: str, **options: object):
        return real_create_engine(url, pool_size=1, max_overflow=0, **options)

    monkeypatch.setattr(database_module, "create_engine", single_connection_engine)
    database_path = tmp_path / "pool-timeout.db"
    engine = create_database_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    connection = engine.connect()
    started_at = monotonic()
    try:
        with pytest.raises(SQLAlchemyTimeoutError):
            engine.connect()
    finally:
        elapsed = monotonic() - started_at
        connection.close()
        engine.dispose()

    assert elapsed < 0.5


def test_postgresql_engine_configuration() -> None:
    engine = create_database_engine("postgresql+psycopg://user:password@localhost/gateway")
    try:
        assert engine.dialect.name == "postgresql"
        assert engine.driver == "psycopg"
        assert engine.pool.timeout() == DATABASE_CONNECT_TIMEOUT_SECONDS
    finally:
        engine.dispose()


def test_postgresql_engine_bounds_connect_and_pool_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    sentinel = object()

    def capture_create_engine(url: str, **options: object) -> object:
        captured["url"] = url
        captured.update(options)
        return sentinel

    monkeypatch.setattr(database_module, "create_engine", capture_create_engine)

    assert (
        create_database_engine("postgresql+psycopg://user:password@localhost/gateway") is sentinel
    )
    assert captured["pool_timeout"] == DATABASE_CONNECT_TIMEOUT_SECONDS
    assert captured["connect_args"] == {"connect_timeout": int(DATABASE_CONNECT_TIMEOUT_SECONDS)}


def test_initialize_rejects_legacy_thread_binding_constraints() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE ai_threads (
                        id VARCHAR(36) PRIMARY KEY,
                        workspace_id VARCHAR(36) NOT NULL,
                        thread_key VARCHAR(96) NOT NULL,
                        hermes_thread_id VARCHAR(255),
                        UNIQUE (workspace_id, thread_key),
                        UNIQUE (hermes_thread_id)
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE thread_source_bindings (
                        id VARCHAR(36) PRIMARY KEY,
                        ai_thread_id VARCHAR(36) NOT NULL,
                        platform VARCHAR(64) NOT NULL,
                        account_id VARCHAR(255) NOT NULL,
                        physical_conversation_id VARCHAR(255) NOT NULL,
                        sender_id VARCHAR(255),
                        UNIQUE (
                            platform,
                            account_id,
                            physical_conversation_id,
                            sender_id
                        )
                    )
                    """
                )
            )

        with pytest.raises(DatabaseSchemaError, match="migrate or recreate"):
            initialize_database(engine)
    finally:
        engine.dispose()


def test_initialize_rejects_messages_without_idempotency_constraints() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE messages (
                        id INTEGER PRIMARY KEY,
                        event_id VARCHAR(255) NOT NULL,
                        source VARCHAR(64) NOT NULL,
                        source_account_id VARCHAR(255) NOT NULL,
                        conversation_id VARCHAR(255) NOT NULL,
                        source_message_id VARCHAR(255) NOT NULL
                    )
                    """
                )
            )

        with pytest.raises(DatabaseSchemaError, match="idempotency.*migrate or recreate"):
            initialize_database(engine)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("table_name", "definition"),
    [
        (
            "runtime_worker_status",
            "worker_name VARCHAR(64) PRIMARY KEY",
        ),
        (
            "hermes_dispatch_records",
            "message_id INTEGER UNIQUE",
        ),
    ],
)
def test_initialize_rejects_incomplete_runtime_tables(
    table_name: str,
    definition: str,
) -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(text(f"CREATE TABLE {table_name} ({definition})"))

        with pytest.raises(DatabaseSchemaError, match=table_name):
            initialize_database(engine)
    finally:
        engine.dispose()


def test_initialize_rejects_checkpoint_without_regression_generation() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE wechat_sync_checkpoints (
                        id INTEGER PRIMARY KEY,
                        source_account_id VARCHAR(255) NOT NULL,
                        conversation_id VARCHAR(255) NOT NULL,
                        last_local_id BIGINT NOT NULL,
                        UNIQUE (source_account_id, conversation_id)
                    )
                    """
                )
            )

        with pytest.raises(DatabaseSchemaError, match="regression generation.*migrate"):
            initialize_database(engine)
    finally:
        engine.dispose()


def test_initialize_rejects_checkpoint_without_message_anchor() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE wechat_sync_checkpoints (
                        id INTEGER PRIMARY KEY,
                        source_account_id VARCHAR(255) NOT NULL,
                        conversation_id VARCHAR(255) NOT NULL,
                        last_local_id BIGINT NOT NULL,
                        regression_generation BIGINT NOT NULL DEFAULT 0,
                        UNIQUE (source_account_id, conversation_id)
                    )
                    """
                )
            )

        with pytest.raises(DatabaseSchemaError, match="checkpoint message anchors.*migrate"):
            initialize_database(engine)
    finally:
        engine.dispose()


def test_initialize_rejects_nullable_checkpoint_generation() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE wechat_sync_checkpoints (
                        id INTEGER PRIMARY KEY,
                        source_account_id VARCHAR(255) NOT NULL,
                        conversation_id VARCHAR(255) NOT NULL,
                        last_local_id BIGINT NOT NULL,
                        regression_generation BIGINT,
                        last_message_fingerprint VARCHAR(64),
                        UNIQUE (source_account_id, conversation_id)
                    )
                    """
                )
            )

        with pytest.raises(DatabaseSchemaError, match="generation is nullable"):
            initialize_database(engine)
    finally:
        engine.dispose()


def test_initialize_rejects_unanchored_legacy_checkpoint_rows() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        initialize_database(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO wechat_sync_checkpoints "
                    "(source_account_id, conversation_id, last_local_id, "
                    "regression_generation, last_message_fingerprint) "
                    "VALUES ('account', 'conversation', 15, 0, NULL)"
                )
            )

        with pytest.raises(DatabaseSchemaError, match="unanchored legacy checkpoints"):
            initialize_database(engine)
    finally:
        engine.dispose()


def test_initialize_allows_pending_recovery_checkpoint_without_anchor() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        initialize_database(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO wechat_sync_checkpoints "
                    "(source_account_id, conversation_id, last_local_id, "
                    "regression_generation, last_message_fingerprint) "
                    "VALUES ('account', 'conversation', 9, 1, NULL)"
                )
            )

        initialize_database(engine)
    finally:
        engine.dispose()


def test_sqlite_hardening_migration_upgrades_zero_baseline_checkpoint(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "baseline.db"
    engine = create_database_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    migration_path = (
        Path(__file__).resolve().parents[1] / "migrations" / "20260821_v1_beta_hardening_sqlite.sql"
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE wechat_sync_checkpoints (
                        id INTEGER NOT NULL PRIMARY KEY,
                        source_account_id VARCHAR(255) NOT NULL,
                        conversation_id VARCHAR(255) NOT NULL,
                        last_local_id BIGINT NOT NULL,
                        initialized_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                        CONSTRAINT uq_wechat_sync_checkpoint_account_conversation
                            UNIQUE (source_account_id, conversation_id),
                        CONSTRAINT ck_wechat_sync_checkpoint_nonnegative_local_id
                            CHECK (last_local_id >= 0)
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO wechat_sync_checkpoints (
                        id,
                        source_account_id,
                        conversation_id,
                        last_local_id
                    ) VALUES (1, 'account', 'conversation', 0)
                    """
                )
            )

        raw_connection = engine.raw_connection()
        try:
            raw_connection.executescript(migration_path.read_text(encoding="utf-8"))
        finally:
            raw_connection.close()

        initialize_database(engine)
        with engine.connect() as connection:
            migrated = connection.execute(
                text(
                    """
                    SELECT last_local_id, regression_generation, last_message_fingerprint
                    FROM wechat_sync_checkpoints
                    WHERE id = 1
                    """
                )
            ).one()
            assert tuple(migrated) == (0, 0, None)
    finally:
        engine.dispose()


def test_sqlite_hardening_migration_rejects_nonzero_checkpoint_without_changes(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "nonzero-baseline.db"
    engine = create_database_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    migration_path = (
        Path(__file__).resolve().parents[1] / "migrations" / "20260821_v1_beta_hardening_sqlite.sql"
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE wechat_sync_checkpoints (
                        id INTEGER NOT NULL PRIMARY KEY,
                        source_account_id VARCHAR(255) NOT NULL,
                        conversation_id VARCHAR(255) NOT NULL,
                        last_local_id BIGINT NOT NULL,
                        initialized_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                        CONSTRAINT uq_wechat_sync_checkpoint_account_conversation
                            UNIQUE (source_account_id, conversation_id),
                        CONSTRAINT ck_wechat_sync_checkpoint_nonnegative_local_id
                            CHECK (last_local_id >= 0)
                    )
                    """
                )
            )
            connection.execute(
                text(
                    "INSERT INTO wechat_sync_checkpoints "
                    "(id, source_account_id, conversation_id, last_local_id) "
                    "VALUES (1, 'account', 'conversation', 15)"
                )
            )

        raw_connection = engine.raw_connection()
        try:
            with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
                raw_connection.executescript(migration_path.read_text(encoding="utf-8"))
        finally:
            raw_connection.close()

        with engine.connect() as connection:
            columns = {
                row[1]
                for row in connection.execute(text("PRAGMA table_info(wechat_sync_checkpoints)"))
            }
            checkpoint = connection.scalar(
                text("SELECT last_local_id FROM wechat_sync_checkpoints WHERE id = 1")
            )
            dispatch_table = connection.scalar(
                text(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'hermes_dispatch_records'"
                )
            )
        assert checkpoint == 15
        assert "regression_generation" not in columns
        assert dispatch_table == 0
    finally:
        engine.dispose()
