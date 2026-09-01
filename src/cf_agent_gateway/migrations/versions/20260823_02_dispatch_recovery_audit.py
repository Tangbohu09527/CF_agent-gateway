"""Add dispatch recovery authorization and audit history.

Revision ID: 20260823_02
Revises: 20260823_01
Create Date: 2026-08-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "20260823_02"
down_revision: str | Sequence[str] | None = "20260823_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _require_schema_state(upgrading=True)
    with op.batch_alter_table("hermes_dispatch_records", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "manual_retry_approved",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )

    op.create_table(
        "hermes_dispatch_recovery_audits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dispatch_record_id", sa.Integer(), nullable=False),
        sa.Column(
            "action",
            sa.Enum(
                "retry_approved",
                "mark_dead",
                "confirm_success",
                name="hermes_dispatch_recovery_action",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("operator", sa.String(length=128), nullable=False),
        sa.Column("reference", sa.String(length=255), nullable=False),
        sa.Column("reason", sa.String(length=1024), nullable=False),
        sa.Column(
            "before_status",
            sa.Enum(
                "queued",
                "running",
                "success",
                "failed",
                "uncertain",
                "dead",
                name="hermes_dispatch_recovery_before_status",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "after_status",
            sa.Enum(
                "queued",
                "running",
                "success",
                "failed",
                "uncertain",
                "dead",
                name="hermes_dispatch_recovery_after_status",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("before_error_code", sa.String(length=128), nullable=True),
        sa.Column("evidence_dispatch_response_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('retry_approved', 'mark_dead', 'confirm_success')",
            name="ck_dispatch_recovery_action",
        ),
        sa.CheckConstraint(
            "before_status IN ('queued', 'running', 'success', 'failed', 'uncertain', 'dead')",
            name="ck_dispatch_recovery_before_status",
        ),
        sa.CheckConstraint(
            "after_status IN ('queued', 'running', 'success', 'failed', 'uncertain', 'dead')",
            name="ck_dispatch_recovery_after_status",
        ),
        sa.CheckConstraint(
            "length(trim(operator)) > 0",
            name="ck_dispatch_recovery_operator",
        ),
        sa.CheckConstraint(
            "length(trim(reference)) > 0",
            name="ck_dispatch_recovery_reference",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_dispatch_recovery_reason",
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_record_id"],
            ["hermes_dispatch_records.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_dispatch_response_id"],
            ["hermes_dispatch_responses.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dispatch_record_id",
            "action",
            "reference",
            name="uq_dispatch_recovery_reference",
        ),
    )
    op.create_index(
        "ix_dispatch_recovery_record_created",
        "hermes_dispatch_recovery_audits",
        ["dispatch_record_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    _require_schema_state(upgrading=False)
    _require_downgrade_data_safe()
    op.drop_index(
        "ix_dispatch_recovery_record_created",
        table_name="hermes_dispatch_recovery_audits",
    )
    op.drop_table("hermes_dispatch_recovery_audits")
    with op.batch_alter_table("hermes_dispatch_records", schema=None) as batch_op:
        batch_op.drop_column("manual_retry_approved")


def _require_schema_state(*, upgrading: bool) -> None:
    if context.is_offline_mode():
        return
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "hermes_dispatch_records" not in tables:
        raise RuntimeError("Hermes dispatch table is missing")
    dispatch_columns = {
        column["name"] for column in inspector.get_columns("hermes_dispatch_records")
    }
    has_authorization = "manual_retry_approved" in dispatch_columns
    has_audit = "hermes_dispatch_recovery_audits" in tables
    if upgrading and (has_authorization or has_audit):
        raise RuntimeError("partial dispatch recovery schema detected")
    if not upgrading and (not has_authorization or not has_audit):
        raise RuntimeError("partial dispatch recovery schema detected")


def _require_downgrade_data_safe() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    audit_count = bind.scalar(sa.text("SELECT count(*) FROM hermes_dispatch_recovery_audits"))
    approved_retry_count = bind.scalar(
        sa.text("SELECT count(*) FROM hermes_dispatch_records WHERE manual_retry_approved = true")
    )
    if audit_count:
        raise RuntimeError(
            "dispatch recovery audit history exists; restore the pre-upgrade backup "
            "to roll back; never delete audit rows"
        )
    if approved_retry_count:
        raise RuntimeError(
            "pending approved retry exists; resolve it and restore the pre-upgrade "
            "backup to roll back"
        )
