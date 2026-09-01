"""Add the standalone Hermes dispatch worker runtime schema.

Revision ID: 20260807_02
Revises: 20260807_01
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260807_02"
down_revision: str | Sequence[str] | None = "20260807_01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("hermes_dispatch_records", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
        )

    # A pre-worker running row has no renewable lease or upstream idempotency contract.
    op.execute(
        sa.text(
            "UPDATE hermes_dispatch_records "
            "SET status = 'uncertain', claim_token = NULL, "
            "completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP), "
            "last_error_code = 'migration_running_state_uncertain', "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE status = 'running'"
        )
    )

    with op.batch_alter_table("hermes_dispatch_records", schema=None) as batch_op:
        batch_op.drop_constraint("ck_hermes_dispatch_status", type_="check")
        batch_op.drop_constraint("ck_hermes_dispatch_state_fields", type_="check")
        batch_op.create_check_constraint(
            "ck_hermes_dispatch_status",
            "status IN ('queued', 'running', 'success', 'failed', 'uncertain', 'dead')",
        )
        batch_op.create_check_constraint(
            "ck_hermes_dispatch_state_fields",
            "(status = 'queued' AND claim_token IS NULL "
            "AND claimed_at IS NULL AND lease_expires_at IS NULL "
            "AND completed_at IS NULL "
            "AND last_error_code IS NULL) OR "
            "(status = 'running' AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND completed_at IS NULL AND last_error_code IS NULL) OR "
            "(status = 'success' AND claim_token IS NULL AND claimed_at IS NOT NULL "
            "AND lease_expires_at IS NULL AND completed_at IS NOT NULL "
            "AND last_error_code IS NULL) OR "
            "(status IN ('failed', 'uncertain', 'dead') AND claim_token IS NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NULL "
            "AND completed_at IS NOT NULL AND last_error_code IS NOT NULL)",
        )

    op.create_index(
        "uq_hermes_dispatch_running_thread",
        "hermes_dispatch_records",
        ["ai_thread_id"],
        unique=True,
        sqlite_where=sa.text("status = 'running'"),
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "ix_hermes_dispatch_claim",
        "hermes_dispatch_records",
        ["status", "lease_expires_at", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_hermes_dispatch_fifo",
        "hermes_dispatch_records",
        ["ai_thread_id", "created_at", "id", "status"],
        unique=False,
    )

    op.create_table(
        "hermes_dispatch_responses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dispatch_record_id", sa.Integer(), nullable=False),
        sa.Column("hermes_response_id", sa.String(length=255), nullable=True),
        sa.Column("assistant_content", sa.Text(), nullable=False),
        sa.Column("response_payload", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_record_id"],
            ["hermes_dispatch_records.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dispatch_record_id",
            name="uq_hermes_dispatch_response_dispatch_record",
        ),
        sa.UniqueConstraint(
            "hermes_response_id",
            name="uq_hermes_dispatch_response_hermes_id",
        ),
    )
    op.create_index(
        "ix_hermes_dispatch_responses_dispatch_record_id",
        "hermes_dispatch_responses",
        ["dispatch_record_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_hermes_dispatch_responses_dispatch_record_id",
        table_name="hermes_dispatch_responses",
    )
    op.drop_table("hermes_dispatch_responses")

    op.drop_index("ix_hermes_dispatch_fifo", table_name="hermes_dispatch_records")
    op.drop_index("ix_hermes_dispatch_claim", table_name="hermes_dispatch_records")
    op.drop_index(
        "uq_hermes_dispatch_running_thread",
        table_name="hermes_dispatch_records",
    )

    op.execute(
        sa.text("UPDATE hermes_dispatch_records SET status = 'failed' WHERE status = 'dead'")
    )
    with op.batch_alter_table("hermes_dispatch_records", schema=None) as batch_op:
        batch_op.drop_constraint("ck_hermes_dispatch_state_fields", type_="check")
        batch_op.drop_constraint("ck_hermes_dispatch_status", type_="check")
        batch_op.create_check_constraint(
            "ck_hermes_dispatch_status",
            "status IN ('queued', 'running', 'success', 'failed', 'uncertain')",
        )
        batch_op.create_check_constraint(
            "ck_hermes_dispatch_state_fields",
            "(status = 'queued' AND claim_token IS NULL "
            "AND claimed_at IS NULL AND completed_at IS NULL "
            "AND last_error_code IS NULL) OR "
            "(status = 'running' AND claim_token IS NOT NULL "
            "AND claimed_at IS NOT NULL AND completed_at IS NULL "
            "AND last_error_code IS NULL) OR "
            "(status = 'success' AND claim_token IS NULL AND claimed_at IS NOT NULL "
            "AND completed_at IS NOT NULL AND last_error_code IS NULL) OR "
            "(status IN ('failed', 'uncertain') AND claim_token IS NULL "
            "AND claimed_at IS NOT NULL AND completed_at IS NOT NULL "
            "AND last_error_code IS NOT NULL)",
        )
        batch_op.drop_column("lease_expires_at")
