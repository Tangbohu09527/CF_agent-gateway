from __future__ import annotations

from collections.abc import Iterator
from io import StringIO
from multiprocessing import get_context
from pathlib import Path
from queue import Empty
from tempfile import TemporaryDirectory
from traceback import format_exc

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Integer, create_engine, inspect, text

from cf_agent_gateway.database import (
    Base,
    create_database_engine,
    initialize_database,
    load_model_metadata,
)

BASELINE_REVISION = "20260806_0001"
FOUNDATION_REVISION = "20260806_01"
ARCHIVE_REVISION = "20260806_0002"
HEAD_REVISION = "20260823_02"
ROOT = Path(__file__).resolve().parents[1]
CHECKPOINT_REVISION = "20260823_01"
PRE_CHECKPOINT_REVISION = "20260810_01"


def migration_config(database_url: str, *, output_buffer: StringIO | None = None) -> Config:
    config = Config(ROOT / "alembic.ini", output_buffer=output_buffer)
    config.attributes["configure_logger"] = False
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def sqlite_url(path: Path) -> str:
    return f"sqlite+pysqlite:///{path.as_posix()}"


@pytest.fixture
def isolated_migration_path() -> Iterator[Path]:
    with TemporaryDirectory(prefix=".migration-tests-", dir=ROOT) as directory:
        yield Path(directory)


def initialize_database_process(database_url: str, start_event: object, results: object) -> None:
    start_event.wait()  # type: ignore[attr-defined]
    engine = create_database_engine(database_url)
    try:
        initialize_database(engine)
    except BaseException:
        results.put(format_exc())  # type: ignore[attr-defined]
    else:
        results.put(None)  # type: ignore[attr-defined]
    finally:
        engine.dispose()


def test_upgrade_empty_database_to_head(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "fresh.db")
    config = migration_config(database_url)

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        load_model_metadata()
        assert set(Base.metadata.tables).issubset(inspector.get_table_names())
        assert {
            "direction",
            "occurred_at",
            "received_at",
            "timestamp",
        }.issubset({column["name"] for column in inspector.get_columns("messages")})
        assert {
            constraint["name"] for constraint in inspector.get_check_constraints("messages")
        } >= {"ck_message_direction"}
        snapshot_column_types = {
            column["name"]: column["type"] for column in inspector.get_columns("context_snapshots")
        }
        assert set(snapshot_column_types) == {
            "thread_id",
            "snapshot_version",
            "summary",
            "covered_until",
            "created_at",
        }
        assert isinstance(snapshot_column_types["covered_until"], Integer)
        assert inspector.get_pk_constraint("context_snapshots")["constrained_columns"] == [
            "thread_id",
            "snapshot_version",
        ]
        assert {
            constraint["name"]
            for constraint in inspector.get_check_constraints("context_snapshots")
        } >= {
            "ck_context_snapshot_positive_version",
            "ck_context_snapshot_nonempty_summary",
            "ck_context_snapshot_positive_covered_until",
        }
        with engine.connect() as connection:
            current_revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert current_revision == HEAD_REVISION
        assert ScriptDirectory.from_config(config).get_current_head() == HEAD_REVISION
    finally:
        engine.dispose()


def test_upgrade_main_schema_preserves_and_backfills_messages(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "upgrade.db")
    config = migration_config(database_url)
    command.upgrade(config, BASELINE_REVISION)

    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO conversations (
                        source, source_account_id, conversation_id, conversation_type
                    ) VALUES (
                        'wechat', 'wxid_bot', 'team@chatroom', 'group'
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO messages (
                        id, event_id, source, source_account_id, source_message_id,
                        conversation_id, conversation_type, is_mentioned, is_self,
                        sender_type, sender_id, message_type, content, timestamp,
                        source_message_id_is_fallback, created_at
                    ) VALUES (
                        :id, :event_id, 'wechat', 'wxid_bot', :source_message_id,
                        'team@chatroom', 'group', false, :is_self,
                        :sender_type, :sender_id, :message_type, :content, :timestamp,
                        false, :created_at
                    )
                    """
                ),
                [
                    {
                        "id": 11,
                        "event_id": "event-inbound",
                        "source_message_id": "message-inbound",
                        "is_self": False,
                        "sender_type": "human",
                        "sender_id": "wxid_alice",
                        "message_type": "text",
                        "content": "inbound fact",
                        "timestamp": "2026-08-01 10:00:00",
                        "created_at": "2026-08-01 10:00:01",
                    },
                    {
                        "id": 12,
                        "event_id": "event-outbound",
                        "source_message_id": "message-outbound",
                        "is_self": True,
                        "sender_type": "human",
                        "sender_id": "wxid_bot",
                        "message_type": "text",
                        "content": "outbound fact",
                        "timestamp": "2026-08-01 10:01:00",
                        "created_at": "2026-08-01 10:01:01",
                    },
                    {
                        "id": 13,
                        "event_id": "event-system",
                        "source_message_id": "message-system",
                        "is_self": False,
                        "sender_type": "system",
                        "sender_id": None,
                        "message_type": "system",
                        "content": "system fact",
                        "timestamp": "2026-08-01 10:02:00",
                        "created_at": "2026-08-01 10:02:01",
                    },
                ],
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT id, content, direction,
                           occurred_at = timestamp AS occurred_preserved,
                           received_at = created_at AS received_preserved
                    FROM messages
                    ORDER BY id
                    """
                )
            ).mappings()
            assert [dict(row) for row in rows] == [
                {
                    "id": 11,
                    "content": "inbound fact",
                    "direction": "inbound",
                    "occurred_preserved": 1,
                    "received_preserved": 1,
                },
                {
                    "id": 12,
                    "content": "outbound fact",
                    "direction": "outbound",
                    "occurred_preserved": 1,
                    "received_preserved": 1,
                },
                {
                    "id": 13,
                    "content": "system fact",
                    "direction": "system",
                    "occurred_preserved": 1,
                    "received_preserved": 1,
                },
            ]
        columns = {column["name"]: column for column in inspect(engine).get_columns("messages")}
        assert columns["direction"]["nullable"] is False
        assert columns["occurred_at"]["nullable"] is False
        assert columns["received_at"]["nullable"] is False
    finally:
        engine.dispose()


def test_upgrade_adopts_foundation_versioned_main_schema(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "foundation.db")
    config = migration_config(database_url)
    command.upgrade(config, BASELINE_REVISION)

    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO conversations (
                        source, source_account_id, conversation_id, conversation_type
                    ) VALUES (
                        'wechat', 'wxid_bot', 'team@chatroom', 'group'
                    )
                    """
                )
            )
    finally:
        engine.dispose()

    command.stamp(config, FOUNDATION_REVISION)
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD_REVISION
            )
            assert connection.scalar(text("SELECT count(*) FROM conversations")) == 1
        assert "message_raw_payloads" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_unversioned_main_schema_can_be_stamped_then_upgraded(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "adopt.db")
    config = migration_config(database_url)
    command.upgrade(config, BASELINE_REVISION)

    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE alembic_version"))
    finally:
        engine.dispose()

    command.stamp(config, BASELINE_REVISION)
    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            current_revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert current_revision == HEAD_REVISION
    finally:
        engine.dispose()


def test_archive_migration_refuses_destructive_downgrade(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "irreversible.db")
    config = migration_config(database_url)
    command.upgrade(config, ARCHIVE_REVISION)

    with pytest.raises(RuntimeError, match="archive migration is irreversible"):
        command.downgrade(config, BASELINE_REVISION)

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            current_revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert current_revision == ARCHIVE_REVISION
        assert "message_raw_payloads" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_initialize_database_serializes_concurrent_sqlite_migrations(
    isolated_migration_path: Path,
) -> None:
    database_url = sqlite_url(isolated_migration_path / "concurrent.db")
    context = get_context("spawn")
    start_event = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=initialize_database_process,
            args=(database_url, start_event, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=30)
        if process.is_alive():
            process.terminate()
            process.join()
            pytest.fail("concurrent migration process did not finish")
        assert process.exitcode == 0

    try:
        assert [results.get(timeout=1) for _ in processes] == [None, None]
    except Empty:
        raise AssertionError("concurrent migration process returned no result") from None

    engine = create_database_engine(database_url)
    try:
        with engine.connect() as connection:
            current_revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert current_revision == HEAD_REVISION
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
    finally:
        engine.dispose()


def test_migrations_render_for_postgresql() -> None:
    output = StringIO()
    config = migration_config(
        "postgresql+psycopg://gateway:gateway@localhost/gateway",
        output_buffer=output,
    )

    command.upgrade(config, "head", sql=True)

    ddl = output.getvalue()
    assert "CREATE TABLE message_raw_payloads" in ddl
    assert "CREATE TABLE message_delivery_attempts" in ddl
    assert "CREATE TABLE hermes_dispatch_records" in ddl
    assert "CREATE TABLE hermes_dispatch_responses" in ddl
    assert "CREATE TABLE artifacts" in ddl
    assert "CREATE TABLE context_snapshots" in ddl
    assert "ck_context_snapshot_positive_version" in ddl
    assert "ck_context_snapshot_nonempty_summary" in ddl
    assert "ck_context_snapshot_positive_covered_until" in ddl
    assert "ck_message_direction" in ddl
    assert "uq_hermes_dispatch_idempotency_key" in ddl
    assert "uq_hermes_dispatch_message" in ddl
    assert "ck_hermes_dispatch_state_fields" in ddl
    assert "ix_hermes_dispatch_queue" in ddl
    assert "ix_hermes_dispatch_thread_queue" in ddl
    assert "ix_hermes_dispatch_context_timeline" in ddl
    assert "ix_hermes_dispatch_claim" in ddl
    assert "ix_hermes_dispatch_fifo" in ddl
    assert "uq_hermes_dispatch_running_thread" in ddl
    assert "uq_artifact_storage_key" in ddl
    assert "artifact_kind" in ddl
    assert "artifact_status" in ddl
    assert "ck_artifact_ready_metadata" in ddl
    assert "ck_artifact_size_nonnegative" in ddl
    assert "ix_artifact_response_id" in ddl


def test_checkpoint_migration_preserves_nonzero_v2_runtime_fixtures(tmp_path: Path) -> None:
    database_url = sqlite_url(tmp_path / "checkpoint-upgrade.db")
    config = migration_config(database_url)
    command.upgrade(config, PRE_CHECKPOINT_REVISION)
    engine = create_database_engine(database_url)
    preserved_tables = (
        "wechat_sync_checkpoints",
        "messages",
        "hermes_dispatch_records",
        "hermes_dispatch_responses",
        "hermes_responses",
        "hermes_response_parts",
        "delivery_outbox",
        "delivery_attempts",
    )
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO conversations (
                        id, source, source_account_id, conversation_id,
                        conversation_type, conversation_name
                    ) VALUES (
                        1, 'wechat', 'wxid_bot', 'team@chatroom', 'group', 'Team'
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO enterprise_identities (id, employee_id, display_name)
                    VALUES ('identity-1', 'employee-1', 'Operator')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO employee_workspaces (id, enterprise_identity_id)
                    VALUES ('workspace-1', 'identity-1')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO ai_threads (
                        id, workspace_id, thread_type, thread_key, hermes_thread_id
                    ) VALUES (
                        'thread-1', 'workspace-1', 'group', 'thread-key-1', 'hermes-thread-1'
                    )
                    """
                )
            )
            connection.execute(
                text(
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
                        '2026-08-22 01:00:00', '15', 'server-message-1',
                        false, 'inbound', '2026-08-22 01:00:00', '2026-08-22 01:00:01'
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO hermes_dispatch_records (
                        id, idempotency_key, message_id, enterprise_identity_id,
                        workspace_id, ai_thread_id, status, attempt_count,
                        claim_token, claimed_at, lease_expires_at, completed_at,
                        last_error_code
                    ) VALUES (
                        1, 'dispatch-key-1', 1, 'identity-1',
                        'workspace-1', 'thread-1', 'uncertain', 2,
                        NULL, '2026-08-22 01:00:02', NULL, '2026-08-22 01:00:03',
                        'hermes_timeout'
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO hermes_dispatch_responses (
                        id, dispatch_record_id, hermes_response_id, assistant_content
                    ) VALUES (
                        1, 1, 'hermes-response-upstream-1', 'persisted response'
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO hermes_responses (
                        response_id, idempotency_key, message_id, workspace_id,
                        ai_thread_id, content_sha256, part_count, status, generated_at
                    ) VALUES (
                        'response-1', 'response-key-1', 1, 'workspace-1',
                        'thread-1', :content_sha256, 1, 'generated',
                        '2026-08-22 01:00:04'
                    )
                    """
                ),
                {"content_sha256": "a" * 64},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO hermes_response_parts (
                        response_id, ordinal, part_type, text, artifact_id
                    ) VALUES ('response-1', 0, 'text', 'persisted response', NULL)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO delivery_outbox (
                        id, idempotency_key, response_id, channel, account_id,
                        conversation_id, target_key, status, next_part_ordinal,
                        attempt_count, claim_token, claimed_at, completed_at,
                        last_error_code
                    ) VALUES (
                        1, 'delivery-key-1', 'response-1', 'wechat', 'wxid_bot',
                        'team@chatroom', :target_key, 'uncertain', 0,
                        1, NULL, '2026-08-22 01:00:05', '2026-08-22 01:00:06',
                        'wechat_timeout'
                    )
                    """
                ),
                {"target_key": "b" * 64},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO delivery_attempts (
                        id, delivery_id, part_ordinal, attempt_number,
                        provider_idempotency_key, status, error_code, completed_at
                    ) VALUES (
                        1, 1, 0, 1, 'provider-key-1', 'uncertain',
                        'wechat_timeout', '2026-08-22 01:00:06'
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO wechat_sync_checkpoints (
                        id, source_account_id, conversation_id, last_local_id
                    ) VALUES (1, 'wxid_bot', 'team@chatroom', 15)
                    """
                )
            )
            before_counts = {
                table: connection.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in preserved_tables
            }
            assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []

        command.upgrade(config, CHECKPOINT_REVISION)

        inspector = inspect(engine)
        checkpoint_columns = {
            column["name"]: column for column in inspector.get_columns("wechat_sync_checkpoints")
        }
        assert {
            "regression_generation",
            "last_message_fingerprint",
        }.issubset(checkpoint_columns)
        assert checkpoint_columns["regression_generation"]["nullable"] is False
        assert checkpoint_columns["last_message_fingerprint"]["nullable"] is True
        with engine.connect() as connection:
            upgraded_counts = {
                table: connection.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in preserved_tables
            }
            upgraded_checkpoint = connection.execute(
                text(
                    """
                    SELECT last_local_id, regression_generation, last_message_fingerprint
                    FROM wechat_sync_checkpoints
                    """
                )
            ).one()
            dispatch_state = connection.execute(
                text(
                    """
                    SELECT status, attempt_count, last_error_code
                    FROM hermes_dispatch_records
                    """
                )
            ).one()
            delivery_state = connection.execute(
                text(
                    """
                    SELECT status, attempt_count, last_error_code
                    FROM delivery_outbox
                    """
                )
            ).one()
            assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
        assert upgraded_counts == before_counts
        assert upgraded_checkpoint == (15, 0, None)
        assert dispatch_state == ("uncertain", 2, "hermes_timeout")
        assert delivery_state == ("uncertain", 1, "wechat_timeout")

        command.downgrade(config, PRE_CHECKPOINT_REVISION)

        assert {
            "regression_generation",
            "last_message_fingerprint",
        }.isdisjoint(
            {column["name"] for column in inspect(engine).get_columns("wechat_sync_checkpoints")}
        )
        with engine.connect() as connection:
            downgraded_counts = {
                table: connection.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in preserved_tables
            }
            assert connection.execute(
                text(
                    """
                    SELECT source_account_id, conversation_id, last_local_id
                    FROM wechat_sync_checkpoints
                    """
                )
            ).one() == ("wxid_bot", "team@chatroom", 15)
            assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
        assert downgraded_counts == before_counts
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("generation", "fingerprint"),
    [
        (1, None),
        (0, "a" * 64),
        (2, "b" * 64),
    ],
)
def test_checkpoint_migration_refuses_downgrade_with_recovery_evidence(
    tmp_path: Path,
    generation: int,
    fingerprint: str | None,
) -> None:
    database_url = sqlite_url(tmp_path / f"checkpoint-evidence-{generation}.db")
    config = migration_config(database_url)
    command.upgrade(config, CHECKPOINT_REVISION)
    engine = create_database_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO wechat_sync_checkpoints (
                        source_account_id, conversation_id, last_local_id,
                        regression_generation, last_message_fingerprint
                    ) VALUES ('wxid_bot', 'team@chatroom', 12, :generation, :fingerprint)
                    """
                ),
                {"generation": generation, "fingerprint": fingerprint},
            )

        with pytest.raises(RuntimeError, match="checkpoint regression evidence exists"):
            command.downgrade(config, PRE_CHECKPOINT_REVISION)

        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == CHECKPOINT_REVISION
            )
            assert connection.execute(
                text(
                    """
                    SELECT last_local_id, regression_generation, last_message_fingerprint
                    FROM wechat_sync_checkpoints
                    """
                )
            ).one() == (12, generation, fingerprint)
    finally:
        engine.dispose()


def test_checkpoint_migration_rejects_partial_schema_without_advancing_revision(
    tmp_path: Path,
) -> None:
    database_url = sqlite_url(tmp_path / "checkpoint-partial.db")
    config = migration_config(database_url)
    command.upgrade(config, PRE_CHECKPOINT_REVISION)
    engine = create_database_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO wechat_sync_checkpoints (
                        source_account_id, conversation_id, last_local_id
                    ) VALUES ('wxid_bot', 'team@chatroom', 15)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    ALTER TABLE wechat_sync_checkpoints
                    ADD COLUMN regression_generation BIGINT DEFAULT 0 NOT NULL
                    """
                )
            )

        with pytest.raises(RuntimeError, match="partial WeChat checkpoint generation schema"):
            command.upgrade(config, CHECKPOINT_REVISION)

        columns = {
            column["name"] for column in inspect(engine).get_columns("wechat_sync_checkpoints")
        }
        assert "regression_generation" in columns
        assert "last_message_fingerprint" not in columns
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                PRE_CHECKPOINT_REVISION
            )
            assert connection.execute(
                text(
                    """
                    SELECT source_account_id, conversation_id, last_local_id,
                           regression_generation
                    FROM wechat_sync_checkpoints
                    """
                )
            ).one() == ("wxid_bot", "team@chatroom", 15, 0)
    finally:
        engine.dispose()


def test_checkpoint_migration_renders_postgresql_upgrade_and_downgrade() -> None:
    upgrade_output = StringIO()
    upgrade_config = migration_config(
        "postgresql+psycopg://gateway:gateway@localhost/gateway",
        output_buffer=upgrade_output,
    )
    command.upgrade(
        upgrade_config,
        f"{PRE_CHECKPOINT_REVISION}:{CHECKPOINT_REVISION}",
        sql=True,
    )
    upgrade_sql = " ".join(upgrade_output.getvalue().lower().split())

    assert "add column regression_generation bigint" in upgrade_sql
    assert "add column last_message_fingerprint varchar(64)" in upgrade_sql
    assert "ck_wechat_sync_checkpoint_nonnegative_generation" in upgrade_sql
    assert "ck_wechat_sync_checkpoint_fingerprint_length" in upgrade_sql

    downgrade_output = StringIO()
    downgrade_config = migration_config(
        "postgresql+psycopg://gateway:gateway@localhost/gateway",
        output_buffer=downgrade_output,
    )
    command.downgrade(
        downgrade_config,
        f"{CHECKPOINT_REVISION}:{PRE_CHECKPOINT_REVISION}",
        sql=True,
    )
    downgrade_sql = " ".join(downgrade_output.getvalue().lower().split())

    assert "drop column last_message_fingerprint" in downgrade_sql
    assert "drop column regression_generation" in downgrade_sql
