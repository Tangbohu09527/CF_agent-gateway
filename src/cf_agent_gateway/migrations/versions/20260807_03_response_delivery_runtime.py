"""Add Hermes response persistence and delivery outbox runtime.

Revision ID: 20260807_03
Revises: 20260807_02
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260807_03"
down_revision: str | Sequence[str] | None = "20260807_02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hermes_responses",
        sa.Column("response_id", sa.String(length=255), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("ai_thread_id", sa.String(length=36), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("part_count", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "generated",
                "delivering",
                "delivered",
                "failed",
                "uncertain",
                name="hermes_response_status",
                native_enum=False,
                create_constraint=False,
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivering_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uncertain_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('queued', 'generated', 'delivering', 'delivered', 'failed', 'uncertain')",
            name="ck_hermes_response_status",
        ),
        sa.CheckConstraint(
            "part_count > 0",
            name="ck_hermes_response_positive_part_count",
        ),
        sa.CheckConstraint(
            "length(content_sha256) = 64",
            name="ck_hermes_response_content_sha256",
        ),
        sa.CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_hermes_response_nonempty_idempotency_key",
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR length(trim(last_error_code)) > 0",
            name="ck_hermes_response_nonempty_error_code",
        ),
        sa.ForeignKeyConstraint(["ai_thread_id"], ["ai_threads.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["employee_workspaces.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("response_id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_hermes_response_idempotency_key",
        ),
        sa.UniqueConstraint("message_id", name="uq_hermes_response_message"),
    )
    op.create_index(
        "ix_hermes_responses_message_id",
        "hermes_responses",
        ["message_id"],
        unique=False,
    )
    op.create_index(
        "ix_hermes_response_status_created",
        "hermes_responses",
        ["status", "created_at", "response_id"],
        unique=False,
    )
    op.create_index(
        "ix_hermes_response_thread",
        "hermes_responses",
        ["ai_thread_id", "created_at", "response_id"],
        unique=False,
    )

    op.create_table(
        "hermes_response_parts",
        sa.Column("response_id", sa.String(length=255), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column(
            "part_type",
            sa.Enum(
                "text",
                "artifact_ref",
                name="hermes_response_part_kind",
                native_enum=False,
                create_constraint=False,
            ),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("artifact_id", sa.String(length=36), nullable=True),
        sa.CheckConstraint(
            "ordinal >= 0",
            name="ck_hermes_response_part_nonnegative_ordinal",
        ),
        sa.CheckConstraint(
            "part_type IN ('text', 'artifact_ref')",
            name="ck_hermes_response_part_type",
        ),
        sa.CheckConstraint(
            "(part_type = 'text' AND text IS NOT NULL AND length(text) > 0 "
            "AND artifact_id IS NULL) OR "
            "(part_type = 'artifact_ref' AND text IS NULL AND artifact_id IS NOT NULL "
            "AND length(trim(artifact_id)) > 0)",
            name="ck_hermes_response_part_shape",
        ),
        sa.ForeignKeyConstraint(
            ["response_id"],
            ["hermes_responses.response_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("response_id", "ordinal"),
    )
    op.create_index(
        "ix_hermes_response_part_artifact",
        "hermes_response_parts",
        ["artifact_id"],
        unique=False,
    )

    op.create_table(
        "delivery_outbox",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("response_id", sa.String(length=255), nullable=False),
        sa.Column("channel", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.String(length=255), nullable=False),
        sa.Column("conversation_id", sa.String(length=255), nullable=False),
        sa.Column("target_key", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "delivering",
                "delivered",
                "failed",
                "uncertain",
                name="delivery_outbox_status",
                native_enum=False,
                create_constraint=False,
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("next_part_ordinal", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
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
            "status IN ('queued', 'delivering', 'delivered', 'failed', 'uncertain')",
            name="ck_delivery_outbox_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_delivery_outbox_attempt_count",
        ),
        sa.CheckConstraint(
            "next_part_ordinal >= 0",
            name="ck_delivery_outbox_next_part",
        ),
        sa.CheckConstraint("length(target_key) = 64", name="ck_delivery_outbox_target_key"),
        sa.CheckConstraint(
            "length(trim(idempotency_key)) > 0",
            name="ck_delivery_outbox_nonempty_idempotency_key",
        ),
        sa.CheckConstraint(
            "claim_token IS NULL OR length(trim(claim_token)) > 0",
            name="ck_delivery_outbox_nonempty_claim_token",
        ),
        sa.CheckConstraint(
            "last_error_code IS NULL OR length(trim(last_error_code)) > 0",
            name="ck_delivery_outbox_nonempty_error_code",
        ),
        sa.ForeignKeyConstraint(
            ["response_id"],
            ["hermes_responses.response_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_delivery_outbox_idempotency_key",
        ),
        sa.UniqueConstraint(
            "id",
            "response_id",
            name="uq_delivery_outbox_id_response",
        ),
        sa.UniqueConstraint(
            "response_id",
            "channel",
            "target_key",
            name="uq_delivery_outbox_response_target",
        ),
    )
    op.create_index(
        "ix_delivery_outbox_queue",
        "delivery_outbox",
        ["status", "available_at", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_delivery_outbox_response",
        "delivery_outbox",
        ["response_id", "created_at", "id"],
        unique=False,
    )

    op.create_table(
        "delivery_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("delivery_id", sa.Integer(), nullable=False),
        sa.Column("part_ordinal", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("provider_idempotency_key", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "delivering",
                "delivered",
                "failed",
                "uncertain",
                name="delivery_attempt_status",
                native_enum=False,
                create_constraint=False,
            ),
            server_default="delivering",
            nullable=False,
        ),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("receipt_payload", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "part_ordinal >= 0",
            name="ck_delivery_attempt_part_ordinal",
        ),
        sa.CheckConstraint(
            "attempt_number > 0",
            name="ck_delivery_attempt_positive_number",
        ),
        sa.CheckConstraint(
            "status IN ('delivering', 'delivered', 'failed', 'uncertain')",
            name="ck_delivery_attempt_status",
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["delivery_outbox.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "delivery_id",
            "part_ordinal",
            "attempt_number",
            name="uq_delivery_attempt_part_number",
        ),
        sa.UniqueConstraint(
            "id",
            "delivery_id",
            "part_ordinal",
            name="uq_delivery_attempt_identity",
        ),
    )
    op.create_index(
        "ix_delivery_attempt_delivery",
        "delivery_attempts",
        ["delivery_id", "part_ordinal", "id"],
        unique=False,
    )

    op.create_table(
        "delivery_receipts",
        sa.Column("delivery_id", sa.Integer(), nullable=False),
        sa.Column("part_ordinal", sa.Integer(), nullable=False),
        sa.Column("response_id", sa.String(length=255), nullable=False),
        sa.Column("attempt_id", sa.Integer(), nullable=False),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("receipt_payload", sa.JSON(none_as_null=True), nullable=False),
        sa.Column(
            "delivered_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "part_ordinal >= 0",
            name="ck_delivery_receipt_part_ordinal",
        ),
        sa.ForeignKeyConstraint(
            ["delivery_id", "response_id"],
            ["delivery_outbox.id", "delivery_outbox.response_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id", "delivery_id", "part_ordinal"],
            [
                "delivery_attempts.id",
                "delivery_attempts.delivery_id",
                "delivery_attempts.part_ordinal",
            ],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["response_id", "part_ordinal"],
            ["hermes_response_parts.response_id", "hermes_response_parts.ordinal"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("delivery_id", "part_ordinal"),
        sa.UniqueConstraint("attempt_id", name="uq_delivery_receipt_attempt"),
    )


def downgrade() -> None:
    op.drop_table("delivery_receipts")
    op.drop_index("ix_delivery_attempt_delivery", table_name="delivery_attempts")
    op.drop_table("delivery_attempts")
    op.drop_index("ix_delivery_outbox_response", table_name="delivery_outbox")
    op.drop_index("ix_delivery_outbox_queue", table_name="delivery_outbox")
    op.drop_table("delivery_outbox")
    op.drop_index("ix_hermes_response_part_artifact", table_name="hermes_response_parts")
    op.drop_table("hermes_response_parts")
    op.drop_index("ix_hermes_response_thread", table_name="hermes_responses")
    op.drop_index("ix_hermes_response_status_created", table_name="hermes_responses")
    op.drop_index("ix_hermes_responses_message_id", table_name="hermes_responses")
    op.drop_table("hermes_responses")
