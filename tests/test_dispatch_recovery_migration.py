from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO

import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from cf_agent_gateway import migration
from cf_agent_gateway.database import (
    create_database_engine,
    create_database_session_factory,
)
from cf_agent_gateway.hermes.models import HermesDispatchOutcome
from cf_agent_gateway.identity.service import IdentityService
from cf_agent_gateway.message.schemas import MessageEvent
from cf_agent_gateway.message.store import MessageStore
from cf_agent_gateway.response.store import DeliveryTarget, ResponseStore
from cf_agent_gateway.task.model import (
    HermesDispatchRecoveryAction,
    HermesDispatchRecoveryAudit,
    HermesDispatchStatus,
)
from cf_agent_gateway.workspace.models import EmployeeWorkspace
from cf_agent_gateway.workspace.service import WorkspaceService

CHECKPOINT_REVISION = "20260823_01"
RECOVERY_REVISION = "20260823_02"
DURABLE_ADMISSION_REVISION = "20260823_03"
RUNTIME_INVARIANTS_REVISION = "20260823_04"


def _foreign_keys(engine) -> set[tuple[tuple[str, ...], str, tuple[str, ...]]]:
    return {
        (
            tuple(foreign_key["constrained_columns"]),
            foreign_key["referred_table"],
            tuple(foreign_key["referred_columns"]),
        )
        for foreign_key in inspect(engine).get_foreign_keys("hermes_dispatch_records")
    }


def _install_pre_recovery_fixture(engine) -> dict[str, object]:
    factory = create_database_session_factory(engine)
    with factory() as session:
        identity = IdentityService(session).create_identity(employee_id="migration-employee")
        thread = WorkspaceService(session).ensure_thread_for_authorized_request(
            enterprise_identity_id=identity.id,
            platform="wechat",
            account_id="migration-account",
            physical_conversation_id="migration-conversation",
            conversation_type="private",
            sender_id="migration-sender",
        )
        workspace = session.get(EmployeeWorkspace, thread.workspace_id)
        assert workspace is not None
        message, created = MessageStore(session).create(
            MessageEvent(
                event_id="migration-event",
                source="wechat",
                source_account_id="migration-account",
                source_message_id="migration-source-message",
                conversation_id="migration-conversation",
                conversation_type="private",
                is_mentioned=None,
                is_self=False,
                sender_type="human",
                sender_id="migration-sender",
                sender_name="Migration fixture",
                message_type="text",
                content="migration fixture content",
                timestamp=datetime(2026, 8, 23, 3, 0, tzinfo=UTC),
            )
        )
        assert created is True
        outcome = HermesDispatchOutcome(
            message_id=message.id,
            workspace_id=workspace.id,
            ai_thread_id=thread.id,
            assistant_content="persisted migration response",
        )
        response, delivery, response_created = ResponseStore(session).save_generated(
            outcome,
            target=DeliveryTarget(
                channel="wechat",
                account_id="migration-account",
                conversation_id="migration-conversation",
            ),
        )
        assert response_created is True
        fixture = {
            "identity_id": identity.id,
            "workspace_id": workspace.id,
            "thread_id": thread.id,
            "message_id": message.id,
            "response_id": response.response_id,
            "delivery_id": delivery.id,
        }

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO hermes_dispatch_records "
                "(idempotency_key, message_id, enterprise_identity_id, workspace_id, "
                "ai_thread_id, status, attempt_count, claimed_at, completed_at, "
                "last_error_code) "
                "VALUES (:key, :message_id, :identity_id, :workspace_id, :thread_id, "
                "'uncertain', 4, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'hermes_timeout')"
            ),
            {
                "key": f"v1:hermes-chat:message:{fixture['message_id']}",
                "message_id": fixture["message_id"],
                "identity_id": fixture["identity_id"],
                "workspace_id": fixture["workspace_id"],
                "thread_id": fixture["thread_id"],
            },
        )
        dispatch_id = connection.scalar(
            text("SELECT id FROM hermes_dispatch_records WHERE message_id = :message_id"),
            {"message_id": fixture["message_id"]},
        )
        connection.execute(
            text(
                "INSERT INTO hermes_dispatch_responses "
                "(dispatch_record_id, assistant_content) "
                "VALUES (:dispatch_id, 'claim-fenced raw result')"
            ),
            {"dispatch_id": dispatch_id},
        )
        connection.execute(
            text(
                "INSERT INTO wechat_sync_checkpoints "
                "(source_account_id, conversation_id, last_local_id, regression_generation) "
                "VALUES ('migration-account', 'migration-conversation', 15, 0)"
            )
        )
    fixture["dispatch_id"] = dispatch_id
    return fixture


def _business_counts(engine) -> dict[str, int]:
    tables = (
        "messages",
        "hermes_dispatch_records",
        "hermes_dispatch_responses",
        "hermes_responses",
        "delivery_outbox",
        "wechat_sync_checkpoints",
    )
    with engine.connect() as connection:
        return {
            table: int(connection.scalar(text(f"SELECT count(*) FROM {table}")) or 0)
            for table in tables
        }


def test_dispatch_recovery_upgrade_and_downgrade_preserve_v2_rows_and_foreign_keys() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        migration.upgrade_database(engine, CHECKPOINT_REVISION)
        fixture = _install_pre_recovery_fixture(engine)
        before_counts = _business_counts(engine)
        before_foreign_keys = _foreign_keys(engine)
        with engine.connect() as connection:
            assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []

        migration.upgrade_database(engine, RECOVERY_REVISION)

        assert migration.get_schema_version(engine) == RECOVERY_REVISION
        assert _business_counts(engine) == before_counts
        assert _foreign_keys(engine) == before_foreign_keys
        with engine.connect() as connection:
            dispatch = connection.execute(
                text(
                    "SELECT status, attempt_count, manual_retry_approved "
                    "FROM hermes_dispatch_records WHERE id = :dispatch_id"
                ),
                {"dispatch_id": fixture["dispatch_id"]},
            ).one()
            assert tuple(dispatch) == ("uncertain", 4, 0)
            assert (
                connection.scalar(text("SELECT last_local_id FROM wechat_sync_checkpoints")) == 15
            )
            assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []

        config = migration.create_migration_config()
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, CHECKPOINT_REVISION)

        assert migration.get_schema_version(engine) == CHECKPOINT_REVISION
        assert _business_counts(engine) == before_counts
        assert _foreign_keys(engine) == before_foreign_keys
        assert "manual_retry_approved" not in {
            column["name"] for column in inspect(engine).get_columns("hermes_dispatch_records")
        }
        assert "hermes_dispatch_recovery_audits" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT status, attempt_count FROM hermes_dispatch_records "
                    "WHERE id = :dispatch_id"
                ),
                {"dispatch_id": fixture["dispatch_id"]},
            ).one() == ("uncertain", 4)
            assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    finally:
        engine.dispose()


def test_dispatch_recovery_upgrade_fails_closed_on_partial_schema() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        migration.upgrade_database(engine, CHECKPOINT_REVISION)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE hermes_dispatch_records "
                    "ADD COLUMN manual_retry_approved BOOLEAN NOT NULL DEFAULT 0"
                )
            )

        with pytest.raises(RuntimeError, match="partial dispatch recovery schema"):
            migration.upgrade_database(engine, RECOVERY_REVISION)

        assert migration.get_schema_version(engine) == CHECKPOINT_REVISION
        assert "hermes_dispatch_recovery_audits" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_dispatch_recovery_downgrade_refuses_to_delete_audit_history() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        migration.upgrade_database(engine, CHECKPOINT_REVISION)
        fixture = _install_pre_recovery_fixture(engine)
        migration.upgrade_database(engine, RECOVERY_REVISION)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO hermes_dispatch_recovery_audits "
                    "(dispatch_record_id, action, operator, reference, reason, "
                    "before_status, after_status, before_error_code) "
                    "VALUES (:dispatch_id, 'mark_dead', 'operator', 'INC-AUDIT', "
                    "'unsafe to retry', 'uncertain', 'dead', 'hermes_timeout')"
                ),
                {"dispatch_id": fixture["dispatch_id"]},
            )
        config = migration.create_migration_config()

        with engine.connect() as connection:
            config.attributes["connection"] = connection
            with pytest.raises(
                RuntimeError,
                match="audit history exists; restore the pre-upgrade backup",
            ):
                command.downgrade(config, CHECKPOINT_REVISION)

        assert migration.get_schema_version(engine) == RECOVERY_REVISION
        with engine.connect() as connection:
            assert (
                connection.scalar(text("SELECT count(*) FROM hermes_dispatch_recovery_audits")) == 1
            )
    finally:
        engine.dispose()


def test_dispatch_recovery_downgrade_refuses_pending_manual_retry() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        migration.upgrade_database(engine, CHECKPOINT_REVISION)
        fixture = _install_pre_recovery_fixture(engine)
        migration.upgrade_database(engine, RECOVERY_REVISION)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE hermes_dispatch_records "
                    "SET status = 'failed', manual_retry_approved = true "
                    "WHERE id = :dispatch_id"
                ),
                {"dispatch_id": fixture["dispatch_id"]},
            )
        config = migration.create_migration_config()

        with engine.connect() as connection:
            config.attributes["connection"] = connection
            with pytest.raises(
                RuntimeError,
                match="pending approved retry exists; resolve it and restore",
            ):
                command.downgrade(config, CHECKPOINT_REVISION)

        assert migration.get_schema_version(engine) == RECOVERY_REVISION
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT manual_retry_approved FROM hermes_dispatch_records "
                        "WHERE id = :dispatch_id"
                    ),
                    {"dispatch_id": fixture["dispatch_id"]},
                )
                == 1
            )
    finally:
        engine.dispose()


def test_runtime_invariants_upgrade_and_downgrade_preserve_v2_rows_and_foreign_keys() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        migration.upgrade_database(engine, CHECKPOINT_REVISION)
        fixture = _install_pre_recovery_fixture(engine)
        migration.upgrade_database(engine, DURABLE_ADMISSION_REVISION)
        before_counts = _business_counts(engine)
        before_foreign_keys = _foreign_keys(engine)

        migration.upgrade_database(engine, RUNTIME_INVARIANTS_REVISION)

        assert migration.get_schema_version(engine) == RUNTIME_INVARIANTS_REVISION
        assert _business_counts(engine) == before_counts
        assert _foreign_keys(engine) == before_foreign_keys
        with engine.connect() as connection:
            reconciliation = connection.execute(
                text(
                    "SELECT reconciliation_failure_count, "
                    "reconciliation_next_attempt_at, reconciliation_quarantined_at, "
                    "reconciliation_last_error_code "
                    "FROM hermes_dispatch_records WHERE id = :dispatch_id"
                ),
                {"dispatch_id": fixture["dispatch_id"]},
            ).one()
            assert tuple(reconciliation) == (0, None, None, None)
            triggers = set(
                connection.scalars(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'trigger' "
                        "AND tbl_name = 'hermes_dispatch_recovery_audits'"
                    )
                )
            )
            assert triggers == {
                "trg_dispatch_recovery_audit_no_update",
                "trg_dispatch_recovery_audit_no_delete",
            }
            assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []

        config = migration.create_migration_config()
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, DURABLE_ADMISSION_REVISION)

        assert migration.get_schema_version(engine) == DURABLE_ADMISSION_REVISION
        assert _business_counts(engine) == before_counts
        assert _foreign_keys(engine) == before_foreign_keys
        columns = {
            column["name"] for column in inspect(engine).get_columns("hermes_dispatch_records")
        }
        assert "reconciliation_failure_count" not in columns
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM sqlite_master "
                        "WHERE type = 'trigger' "
                        "AND tbl_name = 'hermes_dispatch_recovery_audits'"
                    )
                )
                == 0
            )
            assert list(connection.execute(text("PRAGMA foreign_key_check"))) == []
    finally:
        engine.dispose()


def test_runtime_invariants_reject_illegal_orm_and_sql_and_make_audit_immutable() -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        migration.upgrade_database(engine, CHECKPOINT_REVISION)
        fixture = _install_pre_recovery_fixture(engine)
        migration.upgrade_database(engine, RUNTIME_INVARIANTS_REVISION)
        dispatch_id = int(fixture["dispatch_id"])
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO hermes_dispatch_recovery_audits "
                    "(dispatch_record_id, action, operator, reference, reason, "
                    "before_status, after_status, before_error_code) "
                    "VALUES (:dispatch_id, 'mark_dead', 'operator', 'INC-VALID', "
                    "'unsafe to retry', 'uncertain', 'dead', 'hermes_timeout')"
                ),
                {"dispatch_id": dispatch_id},
            )

        with pytest.raises(IntegrityError, match="immutable"), engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE hermes_dispatch_recovery_audits "
                    "SET reason = 'changed' WHERE dispatch_record_id = :dispatch_id"
                ),
                {"dispatch_id": dispatch_id},
            )
        with pytest.raises(IntegrityError, match="immutable"), engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM hermes_dispatch_recovery_audits "
                    "WHERE dispatch_record_id = :dispatch_id"
                ),
                {"dispatch_id": dispatch_id},
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO hermes_dispatch_recovery_audits "
                    "(dispatch_record_id, action, operator, reference, reason, "
                    "before_status, after_status) "
                    "VALUES (:dispatch_id, 'retry_approved', 'operator', "
                    "'INC-INVALID-SQL', 'invalid transition', "
                    "'queued', 'failed')"
                ),
                {"dispatch_id": dispatch_id},
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE hermes_dispatch_records "
                    "SET manual_retry_approved = true "
                    "WHERE id = :dispatch_id"
                ),
                {"dispatch_id": dispatch_id},
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE hermes_dispatch_records "
                    "SET reconciliation_failure_count = 1, "
                    "reconciliation_last_error_code = 'invalid_without_backoff' "
                    "WHERE id = :dispatch_id"
                ),
                {"dispatch_id": dispatch_id},
            )

        factory = create_database_session_factory(engine)
        with factory() as session:
            session.add(
                HermesDispatchRecoveryAudit(
                    dispatch_record_id=dispatch_id,
                    action=HermesDispatchRecoveryAction.CONFIRM_SUCCESS,
                    operator="operator",
                    reference="INC-INVALID-ORM",
                    reason="missing evidence",
                    before_status=HermesDispatchStatus.UNCERTAIN,
                    after_status=HermesDispatchStatus.SUCCESS,
                    before_error_code="hermes_timeout",
                    evidence_dispatch_response_id=None,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

        with engine.connect() as connection:
            audit = connection.execute(
                text(
                    "SELECT action, before_status, after_status, reason "
                    "FROM hermes_dispatch_recovery_audits"
                )
            ).one()
            assert tuple(audit) == (
                "mark_dead",
                "uncertain",
                "dead",
                "unsafe to retry",
            )
            assert (
                connection.scalar(text("SELECT count(*) FROM hermes_dispatch_recovery_audits")) == 1
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("evidence_sql", "message"),
    [
        (
            "INSERT INTO hermes_dispatch_recovery_audits "
            "(dispatch_record_id, action, operator, reference, reason, "
            "before_status, after_status) "
            "VALUES (:dispatch_id, 'mark_dead', 'operator', 'INC-DOWNGRADE', "
            "'unsafe to retry', 'uncertain', 'dead')",
            "audit history exists",
        ),
        (
            "UPDATE hermes_dispatch_records "
            "SET reconciliation_failure_count = 1, "
            "reconciliation_next_attempt_at = CURRENT_TIMESTAMP, "
            "reconciliation_last_error_code = "
            "'dispatch_reconciliation_candidate_invalid' "
            "WHERE id = :dispatch_id",
            "reconciliation recovery evidence exists",
        ),
    ],
)
def test_runtime_invariants_downgrade_fails_closed_on_recovery_evidence(
    evidence_sql: str,
    message: str,
) -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        migration.upgrade_database(engine, CHECKPOINT_REVISION)
        fixture = _install_pre_recovery_fixture(engine)
        migration.upgrade_database(engine, RUNTIME_INVARIANTS_REVISION)
        with engine.begin() as connection:
            connection.execute(
                text(evidence_sql),
                {"dispatch_id": fixture["dispatch_id"]},
            )
        config = migration.create_migration_config()

        with engine.connect() as connection:
            config.attributes["connection"] = connection
            with pytest.raises(RuntimeError, match=message):
                command.downgrade(config, DURABLE_ADMISSION_REVISION)

        assert migration.get_schema_version(engine) == RUNTIME_INVARIANTS_REVISION
    finally:
        engine.dispose()


def test_runtime_invariants_offline_downgrade_fails_closed() -> None:
    config = migration.create_migration_config()
    config.output_buffer = StringIO()
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://gateway:gateway@localhost/gateway",
    )

    with pytest.raises(
        RuntimeError,
        match="offline runtime recovery invariant downgrade is disabled",
    ):
        command.downgrade(
            config,
            f"{RUNTIME_INVARIANTS_REVISION}:{DURABLE_ADMISSION_REVISION}",
            sql=True,
        )


@pytest.mark.parametrize(
    ("invalid_sql", "message"),
    [
        (
            "UPDATE hermes_dispatch_records "
            "SET manual_retry_approved = true WHERE id = :dispatch_id",
            "manual retry authorization exists outside failed status",
        ),
        (
            "INSERT INTO hermes_dispatch_recovery_audits "
            "(dispatch_record_id, action, operator, reference, reason, "
            "before_status, after_status) "
            "VALUES (:dispatch_id, 'mark_dead', 'operator', 'INC-BAD-UPGRADE', "
            "'bad fixture', 'queued', 'dead')",
            "audit transition is inconsistent",
        ),
    ],
)
def test_runtime_invariants_upgrade_fails_closed_on_invalid_existing_data(
    invalid_sql: str,
    message: str,
) -> None:
    engine = create_database_engine("sqlite+pysqlite:///:memory:")
    try:
        migration.upgrade_database(engine, CHECKPOINT_REVISION)
        fixture = _install_pre_recovery_fixture(engine)
        migration.upgrade_database(engine, DURABLE_ADMISSION_REVISION)
        with engine.begin() as connection:
            connection.execute(
                text(invalid_sql),
                {"dispatch_id": fixture["dispatch_id"]},
            )

        with pytest.raises(RuntimeError, match=message):
            migration.upgrade_database(engine, RUNTIME_INVARIANTS_REVISION)

        assert migration.get_schema_version(engine) == DURABLE_ADMISSION_REVISION
    finally:
        engine.dispose()
