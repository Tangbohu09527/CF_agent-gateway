"""add message archive foundation

Revision ID: 20260806_0002
Revises: 20260806_0001
Create Date: 2026-08-06 16:59:13.574190
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260806_0002"
down_revision: str | Sequence[str] | None = "20260806_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "message_delivery_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column(
            "attempted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=255), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("response_payload", sa.JSON(none_as_null=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed')",
            name="ck_message_delivery_attempt_status",
        ),
        sa.CheckConstraint(
            "attempt_number > 0", name="ck_message_delivery_attempt_positive_number"
        ),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "message_id", "attempt_number", name="uq_message_delivery_attempt_message_attempt"
        ),
    )
    with op.batch_alter_table("message_delivery_attempts", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_message_delivery_attempts_message_id"), ["message_id"], unique=False
        )

    op.create_table(
        "message_raw_payloads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(none_as_null=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", name="uq_message_raw_payload_message_id"),
    )
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("direction", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("received_at", sa.DateTime(timezone=True), nullable=True))

    op.execute(
        sa.text(
            """
            UPDATE messages
            SET direction = CASE
                    WHEN sender_type = 'system' THEN 'system'
                    WHEN is_self THEN 'outbound'
                    ELSE 'inbound'
                END,
                occurred_at = timestamp,
                received_at = created_at
            """
        )
    )

    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.alter_column(
            "direction",
            existing_type=sa.String(length=16),
            nullable=False,
            server_default=sa.text("'inbound'"),
        )
        batch_op.alter_column(
            "occurred_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
        batch_op.alter_column(
            "received_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        )
        batch_op.create_check_constraint(
            "ck_message_direction", "direction IN ('inbound', 'outbound', 'assistant', 'system')"
        )


def downgrade() -> None:
    raise RuntimeError(
        "message archive migration is irreversible; export retained payload and delivery "
        "facts before designing a replacement migration"
    )
