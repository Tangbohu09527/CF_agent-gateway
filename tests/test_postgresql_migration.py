from __future__ import annotations

import os

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

from cf_agent_gateway import migration
from cf_agent_gateway.database import create_database_engine

POSTGRES_URL_ENV = "CF_GATEWAY_TEST_POSTGRESQL_URL"
POSTGRES_ENABLE_ENV = "CF_GATEWAY_RUN_POSTGRESQL_MIGRATION_TEST"
PRE_CHECKPOINT_REVISION = "20260810_01"
HEAD_REVISION = "20260823_02"

PRESERVED_TABLES = (
    "wechat_sync_checkpoints",
    "messages",
    "hermes_dispatch_records",
    "hermes_dispatch_responses",
    "hermes_responses",
    "hermes_response_parts",
    "delivery_outbox",
    "delivery_attempts",
)


def _test_database_url() -> str:
    if os.getenv(POSTGRES_ENABLE_ENV) != "1":
        pytest.skip("ephemeral PostgreSQL migration fixture is not enabled")
    value = os.getenv(POSTGRES_URL_ENV)
    if not value:
        pytest.fail(f"{POSTGRES_URL_ENV} is required when PostgreSQL migration tests run")
    url = make_url(value)
    database = url.database or ""
    if url.get_backend_name() != "postgresql":
        pytest.fail("PostgreSQL migration fixture requires a PostgreSQL URL")
    if url.host not in {"127.0.0.1", "localhost"}:
        pytest.fail("PostgreSQL migration fixture only permits a local ephemeral service")
    if not database.endswith(("_ci", "_test")):
        pytest.fail("PostgreSQL migration fixture database must end in _ci or _test")
    return value


def _insert_runtime_fixture(engine) -> None:
    statements = (
        """
        INSERT INTO conversations (
            id, source, source_account_id, conversation_id,
            conversation_type, conversation_name
        ) VALUES (1, 'wechat', 'wxid_bot', 'team@chatroom', 'group', 'Team')
        """,
        """
        INSERT INTO enterprise_identities (id, employee_id, display_name)
        VALUES ('identity-1', 'employee-1', 'Operator')
        """,
        """
        INSERT INTO employee_workspaces (id, enterprise_identity_id)
        VALUES ('workspace-1', 'identity-1')
        """,
        """
        INSERT INTO ai_threads (
            id, workspace_id, thread_type, thread_key, hermes_thread_id
        ) VALUES (
            'thread-1', 'workspace-1', 'group', 'thread-key-1', 'hermes-thread-1'
        )
        """,
        """
        INSERT INTO messages (
            id, event_id, source, source_account_id, source_message_id,
            conversation_id, conversation_type, is_mentioned, is_self,
            sender_type, sender_id, sender_name, message_type, content,
            timestamp, source_local_id, source_server_id,
            source_message_id_is_fallback, direction, occurred_at, received_at
        ) VALUES (
            1, 'event-1', 'wechat', 'wxid_bot', 'server-message-1',
            'team@chatroom', 'group', true, false,
            'human', 'wxid_sender', 'Sender', 'text', 'fixture message',
            '2026-08-22 01:00:00+00', '15', 'server-message-1',
            false, 'inbound', '2026-08-22 01:00:00+00', '2026-08-22 01:00:01+00'
        )
        """,
        """
        INSERT INTO hermes_dispatch_records (
            id, idempotency_key, message_id, enterprise_identity_id,
            workspace_id, ai_thread_id, status, attempt_count,
            claim_token, claimed_at, lease_expires_at, completed_at,
            last_error_code
        ) VALUES (
            1, 'dispatch-key-1', 1, 'identity-1',
            'workspace-1', 'thread-1', 'uncertain', 2,
            NULL, '2026-08-22 01:00:02+00', NULL, '2026-08-22 01:00:03+00',
            'hermes_timeout'
        )
        """,
        """
        INSERT INTO hermes_dispatch_responses (
            id, dispatch_record_id, hermes_response_id, assistant_content
        ) VALUES (1, 1, 'hermes-response-upstream-1', 'persisted response')
        """,
        f"""
        INSERT INTO hermes_responses (
            response_id, idempotency_key, message_id, workspace_id,
            ai_thread_id, content_sha256, part_count, status, generated_at
        ) VALUES (
            'response-1', 'response-key-1', 1, 'workspace-1',
            'thread-1', '{"a" * 64}', 1, 'generated', '2026-08-22 01:00:04+00'
        )
        """,
        """
        INSERT INTO hermes_response_parts (
            response_id, ordinal, part_type, text, artifact_id
        ) VALUES ('response-1', 0, 'text', 'persisted response', NULL)
        """,
        f"""
        INSERT INTO delivery_outbox (
            id, idempotency_key, response_id, channel, account_id,
            conversation_id, target_key, status, next_part_ordinal,
            attempt_count, claim_token, claimed_at, completed_at,
            last_error_code
        ) VALUES (
            1, 'delivery-key-1', 'response-1', 'wechat', 'wxid_bot',
            'team@chatroom', '{"b" * 64}', 'uncertain', 0,
            1, NULL, '2026-08-22 01:00:05+00', '2026-08-22 01:00:06+00',
            'wechat_timeout'
        )
        """,
        """
        INSERT INTO delivery_attempts (
            id, delivery_id, part_ordinal, attempt_number,
            provider_idempotency_key, status, error_code, completed_at
        ) VALUES (
            1, 1, 0, 1, 'provider-key-1', 'uncertain',
            'wechat_timeout', '2026-08-22 01:00:06+00'
        )
        """,
        """
        INSERT INTO wechat_sync_checkpoints (
            id, source_account_id, conversation_id, last_local_id
        ) VALUES (1, 'wxid_bot', 'team@chatroom', 15)
        """,
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def _counts(engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            table: int(connection.scalar(text(f"SELECT count(*) FROM {table}")) or 0)
            for table in PRESERVED_TABLES
        }


def _foreign_keys(engine) -> dict[str, set[tuple[tuple[str, ...], str, tuple[str, ...]]]]:
    inspector = inspect(engine)
    return {
        table: {
            (
                tuple(foreign_key["constrained_columns"]),
                str(foreign_key["referred_table"]),
                tuple(foreign_key["referred_columns"]),
            )
            for foreign_key in inspector.get_foreign_keys(table)
        }
        for table in PRESERVED_TABLES
    }


def _downgrade(engine, revision: str) -> None:
    config = migration.create_migration_config()
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, revision)


def test_postgresql_populated_v2_runtime_upgrade_and_downgrade() -> None:
    engine = create_database_engine(_test_database_url())
    try:
        existing_tables = set(inspect(engine).get_table_names())
        if existing_tables:
            pytest.fail("PostgreSQL migration fixture requires an empty ephemeral database")

        migration.upgrade_database(engine, PRE_CHECKPOINT_REVISION)
        _insert_runtime_fixture(engine)
        before_counts = _counts(engine)
        before_foreign_keys = _foreign_keys(engine)

        migration.upgrade_database(engine, HEAD_REVISION)

        assert migration.get_schema_version(engine) == HEAD_REVISION
        assert _counts(engine) == before_counts
        assert _foreign_keys(engine) == before_foreign_keys
        with engine.connect() as connection:
            checkpoint = connection.execute(
                text(
                    "SELECT last_local_id, regression_generation, "
                    "last_message_fingerprint FROM wechat_sync_checkpoints"
                )
            ).one()
            dispatch = connection.execute(
                text(
                    "SELECT status, attempt_count, manual_retry_approved, "
                    "last_error_code FROM hermes_dispatch_records"
                )
            ).one()
            delivery = connection.execute(
                text("SELECT status, attempt_count, last_error_code FROM delivery_outbox")
            ).one()
        assert checkpoint == (15, 0, None)
        assert dispatch == ("uncertain", 2, False, "hermes_timeout")
        assert delivery == ("uncertain", 1, "wechat_timeout")

        _downgrade(engine, PRE_CHECKPOINT_REVISION)

        assert migration.get_schema_version(engine) == PRE_CHECKPOINT_REVISION
        assert _counts(engine) == before_counts
        assert _foreign_keys(engine) == before_foreign_keys
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT source_account_id, conversation_id, last_local_id "
                    "FROM wechat_sync_checkpoints"
                )
            ).one() == ("wxid_bot", "team@chatroom", 15)
            assert connection.execute(
                text("SELECT status, attempt_count, last_error_code FROM hermes_dispatch_records")
            ).one() == ("uncertain", 2, "hermes_timeout")

        migration.upgrade_database(engine, HEAD_REVISION)
        assert migration.get_schema_version(engine) == HEAD_REVISION
        assert _counts(engine) == before_counts
    finally:
        engine.dispose()
