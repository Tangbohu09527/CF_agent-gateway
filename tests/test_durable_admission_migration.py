from __future__ import annotations

from io import StringIO

import pytest
from alembic import command
from sqlalchemy import inspect, text

from cf_agent_gateway import migration
from cf_agent_gateway.database import create_database_engine

PRE_ADMISSION_REVISION = "20260823_02"
DURABLE_ADMISSION_REVISION = "20260823_03"


def _install_legacy_fixture(engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO conversations "
                "(id, source, source_account_id, conversation_id, conversation_type) "
                "VALUES (1, 'wechat', 'bot-1', 'conversation-1', 'private')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO enterprise_identities (id, employee_id) "
                "VALUES ('identity-1', 'employee-1')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO employee_workspaces (id, enterprise_identity_id) "
                "VALUES ('workspace-1', 'identity-1')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO ai_threads "
                "(id, workspace_id, thread_type, thread_key) "
                "VALUES ('thread-1', 'workspace-1', 'private', 'thread-key-1')"
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO messages (
                    id, event_id, source, source_account_id, source_message_id,
                    conversation_id, conversation_type, is_mentioned, is_self,
                    sender_type, sender_id, message_type, content, timestamp,
                    source_message_id_is_fallback, direction, occurred_at, received_at
                ) VALUES (
                    :id, :event_id, 'wechat', 'bot-1', :source_message_id,
                    'conversation-1', 'private', NULL, false,
                    'human', 'sender-1', 'text', :content, :occurred_at,
                    false, 'inbound', :occurred_at, :received_at
                )
                """
            ),
            [
                {
                    "id": 1,
                    "event_id": "event-allowed",
                    "source_message_id": "source-allowed",
                    "content": "legacy allowed",
                    "occurred_at": "2026-08-23 01:00:00",
                    "received_at": "2026-08-23 01:00:01",
                },
                {
                    "id": 2,
                    "event_id": "event-unresolved",
                    "source_message_id": "source-unresolved",
                    "content": "legacy unresolved",
                    "occurred_at": "2026-08-23 01:01:00",
                    "received_at": "2026-08-23 01:01:01",
                },
            ],
        )
        connection.execute(
            text(
                """
                INSERT INTO hermes_dispatch_records (
                    id, idempotency_key, message_id, enterprise_identity_id,
                    workspace_id, ai_thread_id, status, attempt_count,
                    manual_retry_approved
                ) VALUES (
                    1, 'v1:hermes-chat:message:1', 1, 'identity-1',
                    'workspace-1', 'thread-1', 'queued', 0, false
                )
                """
            )
        )


def _foreign_keys(engine, table: str) -> set[tuple[tuple[str, ...], str, tuple[str, ...]]]:
    return {
        (
            tuple(foreign_key["constrained_columns"]),
            str(foreign_key["referred_table"]),
            tuple(foreign_key["referred_columns"]),
        )
        for foreign_key in inspect(engine).get_foreign_keys(table)
    }


def test_durable_admission_backfill_is_conservative_and_preserves_v2_rows() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        migration.upgrade_database(engine, PRE_ADMISSION_REVISION)
        _install_legacy_fixture(engine)
        preserved_tables = (
            "messages",
            "hermes_dispatch_records",
            "employee_workspaces",
            "ai_threads",
        )
        with engine.connect() as connection:
            before_counts = {
                table: connection.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in preserved_tables
            }
            before_dispatch = connection.execute(
                text(
                    "SELECT status, attempt_count, manual_retry_approved "
                    "FROM hermes_dispatch_records WHERE id = 1"
                )
            ).one()
        before_dispatch_foreign_keys = _foreign_keys(engine, "hermes_dispatch_records")

        migration.upgrade_database(engine, DURABLE_ADMISSION_REVISION)

        assert migration.get_schema_version(engine) == DURABLE_ADMISSION_REVISION
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT message_id, state, decision, evidence_origin,
                           admission_reason, should_create_task,
                           requested_scope, requested_skill_ids, risk_level,
                           enterprise_identity_id, workspace_id, ai_thread_id
                    FROM message_admission_outcomes
                    ORDER BY message_id
                    """
                )
            ).all()
            after_counts = {
                table: connection.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in preserved_tables
            }
            after_dispatch = connection.execute(
                text(
                    "SELECT status, attempt_count, manual_retry_approved "
                    "FROM hermes_dispatch_records WHERE id = 1"
                )
            ).one()
            assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []

        assert rows == [
            (
                1,
                "completed",
                "allowed",
                "legacy_dispatch",
                "allowed",
                1,
                None,
                None,
                None,
                "identity-1",
                "workspace-1",
                "thread-1",
            ),
            (
                2,
                "completed",
                "unresolved",
                "legacy_unresolved",
                "legacy_unresolved",
                0,
                None,
                None,
                None,
                None,
                None,
                None,
            ),
        ]
        assert after_counts == before_counts
        assert after_dispatch == before_dispatch == ("queued", 0, 0)
        assert _foreign_keys(engine, "hermes_dispatch_records") == before_dispatch_foreign_keys

        config = migration.create_migration_config()
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, PRE_ADMISSION_REVISION)

        assert migration.get_schema_version(engine) == PRE_ADMISSION_REVISION
        assert "message_admission_outcomes" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert {
                table: connection.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in preserved_tables
            } == before_counts
            assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    finally:
        engine.dispose()


def test_durable_admission_upgrade_fails_closed_on_partial_schema() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        migration.upgrade_database(engine, PRE_ADMISSION_REVISION)
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE message_admission_outcomes (id INTEGER PRIMARY KEY)")
            )

        with pytest.raises(RuntimeError, match="partial durable admission schema"):
            migration.upgrade_database(engine, DURABLE_ADMISSION_REVISION)

        assert migration.get_schema_version(engine) == PRE_ADMISSION_REVISION
    finally:
        engine.dispose()


def test_durable_admission_downgrade_refuses_runtime_evidence() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        migration.upgrade_database(engine, DURABLE_ADMISSION_REVISION)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, source, source_account_id, conversation_id, conversation_type) "
                    "VALUES (1, 'wechat', 'bot-1', 'conversation-1', 'private')"
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO messages (
                        id, event_id, source, source_account_id, source_message_id,
                        conversation_id, conversation_type, is_mentioned, is_self,
                        sender_type, sender_id, message_type, content, timestamp,
                        source_message_id_is_fallback, direction, occurred_at, received_at
                    ) VALUES (
                        1, 'event-1', 'wechat', 'bot-1', 'source-1',
                        'conversation-1', 'private', NULL, false,
                        'human', 'sender-1', 'text', 'runtime denial', CURRENT_TIMESTAMP,
                        false, 'inbound', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO message_admission_outcomes (
                        message_id, state, decision, evidence_origin,
                        admission_reason, should_create_task,
                        requested_scope, requested_skill_ids, risk_level,
                        routing_mode, attempt_count, completed_at
                    ) VALUES (
                        1, 'completed', 'denied', 'runtime',
                        'access_denied', false,
                        '[]', '[]', 'normal', 'none', 1, CURRENT_TIMESTAMP
                    )
                    """
                )
            )
        config = migration.create_migration_config()

        with engine.connect() as connection:
            config.attributes["connection"] = connection
            with pytest.raises(RuntimeError, match="runtime admission evidence exists"):
                command.downgrade(config, PRE_ADMISSION_REVISION)

        assert migration.get_schema_version(engine) == DURABLE_ADMISSION_REVISION
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM message_admission_outcomes")) == 1
    finally:
        engine.dispose()


def test_durable_admission_migration_renders_for_postgresql() -> None:
    upgrade_output = StringIO()
    upgrade_config = migration.create_migration_config()
    upgrade_config.output_buffer = upgrade_output
    upgrade_config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://gateway:gateway@localhost/gateway",
    )

    command.upgrade(
        upgrade_config,
        f"{PRE_ADMISSION_REVISION}:{DURABLE_ADMISSION_REVISION}",
        sql=True,
    )

    upgrade_sql = " ".join(upgrade_output.getvalue().lower().split())
    assert "create table message_admission_outcomes" in upgrade_sql
    assert "uq_message_admission_outcome_message" in upgrade_sql
    assert "insert into message_admission_outcomes" in upgrade_sql
    assert "legacy_unresolved" in upgrade_sql

    downgrade_output = StringIO()
    downgrade_config = migration.create_migration_config()
    downgrade_config.output_buffer = downgrade_output
    downgrade_config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://gateway:gateway@localhost/gateway",
    )

    with pytest.raises(RuntimeError, match="offline downgrade is disabled"):
        command.downgrade(
            downgrade_config,
            f"{DURABLE_ADMISSION_REVISION}:{PRE_ADMISSION_REVISION}",
            sql=True,
        )

    assert "drop table message_admission_outcomes" not in downgrade_output.getvalue().lower()
