import pytest
from sqlalchemy import inspect, text

from cf_agent_gateway import database, migration
from cf_agent_gateway.database import (
    DatabaseSchemaError,
    check_database_migrations,
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


def test_postgresql_engine_configuration() -> None:
    engine = create_database_engine("postgresql+psycopg://user:password@localhost/gateway")
    try:
        assert engine.dialect.name == "postgresql"
        assert engine.driver == "psycopg"
    finally:
        engine.dispose()


def test_check_database_migrations_rejects_an_outdated_packaged_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OutdatedMigrationTree:
        def get_heads(self) -> list[str]:
            return ["20260806_04"]

    monkeypatch.setattr(
        database.ScriptDirectory,
        "from_config",
        lambda config: OutdatedMigrationTree(),
    )
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        with pytest.raises(DatabaseSchemaError, match="unexpected Alembic migration tree"):
            check_database_migrations(engine)

        assert inspect(engine).get_table_names() == []
    finally:
        engine.dispose()


def test_check_database_migrations_rejects_a_database_before_head_without_upgrading() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        migration.upgrade_database(engine, "20260806_04")
        assert migration.get_schema_version(engine) == "20260806_04"

        with pytest.raises(DatabaseSchemaError, match="required head"):
            check_database_migrations(engine)

        assert migration.get_schema_version(engine) == "20260806_04"
    finally:
        engine.dispose()


def test_check_database_migrations_rejects_missing_thread_binding_constraints() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
            )
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES ('20260810_01')")
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE ai_threads (
                        id VARCHAR(36) PRIMARY KEY,
                        workspace_id VARCHAR(36) NOT NULL,
                        thread_key VARCHAR(96) NOT NULL,
                        UNIQUE (workspace_id, thread_key)
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE thread_source_bindings (
                        id VARCHAR(36) PRIMARY KEY,
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
            check_database_migrations(engine)
    finally:
        engine.dispose()


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
