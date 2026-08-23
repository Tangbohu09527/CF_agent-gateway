"""Harden reconciliation state and dispatch recovery audit invariants.

Revision ID: 20260823_04
Revises: 20260823_03
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "20260823_04"
down_revision: str | Sequence[str] | None = "20260823_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DISPATCH_TABLE = "hermes_dispatch_records"
_AUDIT_TABLE = "hermes_dispatch_recovery_audits"
_SQLITE_UPDATE_TRIGGER = "trg_dispatch_recovery_audit_no_update"
_SQLITE_DELETE_TRIGGER = "trg_dispatch_recovery_audit_no_delete"
_POSTGRES_TRIGGER = "trg_dispatch_recovery_audit_immutable"
_POSTGRES_FUNCTION = "cf_gateway_reject_dispatch_recovery_audit_mutation"


def upgrade() -> None:
    _require_schema_state(upgrading=True)
    _require_upgrade_data_safe()
    with op.batch_alter_table(_DISPATCH_TABLE, schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "reconciliation_failure_count",
                sa.Integer(),
                server_default="0",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "reconciliation_next_attempt_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "reconciliation_quarantined_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "reconciliation_last_error_code",
                sa.String(length=128),
                nullable=True,
            )
        )
        batch_op.create_check_constraint(
            "ck_hermes_dispatch_manual_retry_status",
            "manual_retry_approved = false OR status = 'failed'",
        )
        batch_op.create_check_constraint(
            "ck_hermes_dispatch_reconciliation_failure_count",
            "reconciliation_failure_count >= 0",
        )
        batch_op.create_check_constraint(
            "ck_hermes_dispatch_reconciliation_state",
            "(reconciliation_failure_count = 0 "
            "AND reconciliation_next_attempt_at IS NULL "
            "AND reconciliation_quarantined_at IS NULL "
            "AND reconciliation_last_error_code IS NULL) OR "
            "(reconciliation_failure_count BETWEEN 1 AND 4 "
            "AND reconciliation_next_attempt_at IS NOT NULL "
            "AND reconciliation_quarantined_at IS NULL "
            "AND reconciliation_last_error_code IS NOT NULL) OR "
            "(reconciliation_failure_count >= 5 "
            "AND reconciliation_next_attempt_at IS NULL "
            "AND reconciliation_quarantined_at IS NOT NULL "
            "AND reconciliation_last_error_code IS NOT NULL)",
        )

    with op.batch_alter_table(_AUDIT_TABLE, schema=None) as batch_op:
        batch_op.create_check_constraint(
            "ck_dispatch_recovery_action_transition",
            "(action = 'retry_approved' AND before_status = 'uncertain' "
            "AND after_status = 'failed' AND evidence_dispatch_response_id IS NULL) OR "
            "(action = 'mark_dead' AND before_status = 'uncertain' "
            "AND after_status = 'dead' AND evidence_dispatch_response_id IS NULL) OR "
            "(action = 'confirm_success' AND before_status = 'uncertain' "
            "AND after_status = 'success' "
            "AND evidence_dispatch_response_id IS NOT NULL)",
        )

    op.create_index(
        "ix_hermes_dispatch_reconciliation",
        _DISPATCH_TABLE,
        [
            "status",
            "reconciliation_quarantined_at",
            "reconciliation_next_attempt_at",
            "id",
        ],
        unique=False,
    )
    _create_immutability_triggers()


def downgrade() -> None:
    _require_schema_state(upgrading=False)
    _require_downgrade_data_safe()
    _drop_immutability_triggers()
    op.drop_index(
        "ix_hermes_dispatch_reconciliation",
        table_name=_DISPATCH_TABLE,
    )
    with op.batch_alter_table(_AUDIT_TABLE, schema=None) as batch_op:
        batch_op.drop_constraint(
            "ck_dispatch_recovery_action_transition",
            type_="check",
        )
    with op.batch_alter_table(_DISPATCH_TABLE, schema=None) as batch_op:
        batch_op.drop_constraint(
            "ck_hermes_dispatch_reconciliation_state",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_hermes_dispatch_reconciliation_failure_count",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_hermes_dispatch_manual_retry_status",
            type_="check",
        )
        batch_op.drop_column("reconciliation_last_error_code")
        batch_op.drop_column("reconciliation_quarantined_at")
        batch_op.drop_column("reconciliation_next_attempt_at")
        batch_op.drop_column("reconciliation_failure_count")


def _create_immutability_triggers() -> None:
    dialect = op.get_context().dialect.name
    if dialect == "sqlite":
        op.execute(
            sa.text(
                f"CREATE TRIGGER {_SQLITE_UPDATE_TRIGGER} "
                f"BEFORE UPDATE ON {_AUDIT_TABLE} "
                "BEGIN "
                "SELECT RAISE(ABORT, 'dispatch recovery audit rows are immutable'); "
                "END"
            )
        )
        op.execute(
            sa.text(
                f"CREATE TRIGGER {_SQLITE_DELETE_TRIGGER} "
                f"BEFORE DELETE ON {_AUDIT_TABLE} "
                "BEGIN "
                "SELECT RAISE(ABORT, 'dispatch recovery audit rows are immutable'); "
                "END"
            )
        )
        return
    if dialect == "postgresql":
        op.execute(
            sa.text(
                f"CREATE FUNCTION {_POSTGRES_FUNCTION}() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ "
                "BEGIN "
                "RAISE EXCEPTION 'dispatch recovery audit rows are immutable' "
                "USING ERRCODE = '55000'; "
                "END; "
                "$$"
            )
        )
        op.execute(
            sa.text(
                f"CREATE TRIGGER {_POSTGRES_TRIGGER} "
                f"BEFORE UPDATE OR DELETE ON {_AUDIT_TABLE} "
                f"FOR EACH ROW EXECUTE FUNCTION {_POSTGRES_FUNCTION}()"
            )
        )
        return
    raise RuntimeError("dispatch recovery audit immutability requires PostgreSQL or SQLite")


def _drop_immutability_triggers() -> None:
    dialect = op.get_context().dialect.name
    if dialect == "sqlite":
        op.execute(sa.text(f"DROP TRIGGER {_SQLITE_UPDATE_TRIGGER}"))
        op.execute(sa.text(f"DROP TRIGGER {_SQLITE_DELETE_TRIGGER}"))
        return
    if dialect == "postgresql":
        op.execute(sa.text(f"DROP TRIGGER {_POSTGRES_TRIGGER} ON {_AUDIT_TABLE}"))
        op.execute(sa.text(f"DROP FUNCTION {_POSTGRES_FUNCTION}()"))
        return
    raise RuntimeError("dispatch recovery audit immutability requires PostgreSQL or SQLite")


def _require_schema_state(*, upgrading: bool) -> None:
    if context.is_offline_mode():
        return
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if {_DISPATCH_TABLE, _AUDIT_TABLE} - tables:
        raise RuntimeError("dispatch recovery schema is missing")
    columns = {column["name"] for column in inspector.get_columns(_DISPATCH_TABLE)}
    reconciliation_columns = {
        "reconciliation_failure_count",
        "reconciliation_next_attempt_at",
        "reconciliation_quarantined_at",
        "reconciliation_last_error_code",
    }
    existing = columns & reconciliation_columns
    if upgrading and existing:
        raise RuntimeError("partial runtime recovery invariant schema detected")
    if not upgrading and existing != reconciliation_columns:
        raise RuntimeError("partial runtime recovery invariant schema detected")


def _require_upgrade_data_safe() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    invalid_retry_count = bind.scalar(
        sa.text(
            f"SELECT count(*) FROM {_DISPATCH_TABLE} "
            "WHERE manual_retry_approved = true AND status != 'failed'"
        )
    )
    if invalid_retry_count:
        raise RuntimeError(
            "manual retry authorization exists outside failed status; "
            "resolve the inconsistent dispatch rows before upgrading"
        )
    invalid_audit_count = bind.scalar(
        sa.text(
            f"SELECT count(*) FROM {_AUDIT_TABLE} WHERE NOT ("
            "(action = 'retry_approved' AND before_status = 'uncertain' "
            "AND after_status = 'failed' AND evidence_dispatch_response_id IS NULL) OR "
            "(action = 'mark_dead' AND before_status = 'uncertain' "
            "AND after_status = 'dead' AND evidence_dispatch_response_id IS NULL) OR "
            "(action = 'confirm_success' AND before_status = 'uncertain' "
            "AND after_status = 'success' "
            "AND evidence_dispatch_response_id IS NOT NULL))"
        )
    )
    if invalid_audit_count:
        raise RuntimeError(
            "dispatch recovery audit transition is inconsistent; "
            "restore verified audit data before upgrading"
        )


def _require_downgrade_data_safe() -> None:
    if context.is_offline_mode():
        raise RuntimeError(
            "offline runtime recovery invariant downgrade is disabled because "
            "audit and reconciliation evidence cannot be inspected safely"
        )
    bind = op.get_bind()
    audit_count = bind.scalar(sa.text(f"SELECT count(*) FROM {_AUDIT_TABLE}"))
    if audit_count:
        raise RuntimeError(
            "dispatch recovery audit history exists; restore the pre-upgrade backup "
            "to roll back; never weaken audit immutability around existing evidence"
        )
    reconciliation_count = bind.scalar(
        sa.text(f"SELECT count(*) FROM {_DISPATCH_TABLE} WHERE reconciliation_failure_count > 0")
    )
    if reconciliation_count:
        raise RuntimeError(
            "dispatch reconciliation recovery evidence exists; restore the pre-upgrade "
            "backup to roll back"
        )
