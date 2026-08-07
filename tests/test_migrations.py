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
from sqlalchemy import create_engine, inspect, text

from cf_agent_gateway.database import (
    Base,
    create_database_engine,
    initialize_database,
    load_model_metadata,
)

BASELINE_REVISION = "20260806_0001"
FOUNDATION_REVISION = "20260806_01"
ARCHIVE_REVISION = "20260806_0002"
HEAD_REVISION = "20260807_01"
ROOT = Path(__file__).resolve().parents[1]


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
    assert "CREATE TABLE artifacts" in ddl
    assert "ck_message_direction" in ddl
    assert "uq_hermes_dispatch_idempotency_key" in ddl
    assert "uq_hermes_dispatch_message" in ddl
    assert "ck_hermes_dispatch_state_fields" in ddl
    assert "ix_hermes_dispatch_queue" in ddl
    assert "ix_hermes_dispatch_thread_queue" in ddl
    assert "uq_artifact_storage_key" in ddl
    assert "artifact_kind" in ddl
    assert "artifact_status" in ddl
    assert "ck_artifact_ready_metadata" in ddl
    assert "ck_artifact_size_nonnegative" in ddl
    assert "ix_artifact_response_id" in ddl
