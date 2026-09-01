"""Add the Hermes dispatch outbox foundation.

Revision ID: 20260806_03
Revises: 20260806_02
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260806_03"
down_revision: str | Sequence[str] | None = "20260806_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hermes_dispatch_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("enterprise_identity_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("ai_thread_id", sa.String(length=36), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "running",
                "success",
                "failed",
                "uncertain",
                name="hermes_dispatch_status",
                native_enum=False,
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claim_token", sa.String(length=255), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'success', 'failed', 'uncertain')",
            name="ck_hermes_dispatch_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_hermes_dispatch_nonnegative_attempt_count",
        ),
        sa.CheckConstraint(
            "(status = 'queued' AND attempt_count = 0) OR "
            "(status != 'queued' AND attempt_count > 0)",
            name="ck_hermes_dispatch_status_attempt_count",
        ),
        sa.CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_hermes_dispatch_nonempty_idempotency_key",
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR length(trim(last_error_code)) > 0",
            name="ck_hermes_dispatch_nonempty_error_code",
        ),
        sa.CheckConstraint(
            "claim_token IS NULL OR length(trim(claim_token)) > 0",
            name="ck_hermes_dispatch_nonempty_claim_token",
        ),
        sa.CheckConstraint(
            "(status = 'queued' AND claim_token IS NULL "
            "AND claimed_at IS NULL AND completed_at IS NULL "
            "AND last_error_code IS NULL) OR "
            "(status = 'running' AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND completed_at IS NULL "
            "AND last_error_code IS NULL) OR "
            "(status = 'success' AND claim_token IS NULL AND claimed_at IS NOT NULL "
            "AND completed_at IS NOT NULL AND last_error_code IS NULL) OR "
            "(status IN ('failed', 'uncertain') AND claim_token IS NULL "
            "AND claimed_at IS NOT NULL "
            "AND completed_at IS NOT NULL AND last_error_code IS NOT NULL)",
            name="ck_hermes_dispatch_state_fields",
        ),
        sa.ForeignKeyConstraint(
            ["ai_thread_id"],
            ["ai_threads.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["enterprise_identity_id"],
            ["enterprise_identities.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["employee_workspaces.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_hermes_dispatch_idempotency_key",
        ),
        sa.UniqueConstraint(
            "message_id",
            name="uq_hermes_dispatch_message",
        ),
    )
    op.create_index(
        "ix_hermes_dispatch_queue",
        "hermes_dispatch_records",
        ["status", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_hermes_dispatch_records_message_id",
        "hermes_dispatch_records",
        ["message_id"],
        unique=False,
    )
    op.create_index(
        "ix_hermes_dispatch_thread_queue",
        "hermes_dispatch_records",
        ["ai_thread_id", "status", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_hermes_dispatch_thread_queue",
        table_name="hermes_dispatch_records",
    )
    op.drop_index(
        "ix_hermes_dispatch_records_message_id",
        table_name="hermes_dispatch_records",
    )
    op.drop_index(
        "ix_hermes_dispatch_queue",
        table_name="hermes_dispatch_records",
    )
    op.drop_table("hermes_dispatch_records")
